"""Multi-agent (docs/plans/2026-10-31-0010-feat-multi-agent-plan.md U1-U5, U8): the session registry (names, one
market per agent, per-session queries), the gate per session with ownership and KD3 global limits, KD2 overlap
warnings, the wake scheduler (cap, urgent first, coalescing, fairness, metrics), the shared signal detection (KD6) and,
end to end with the fake harness, two agents at once (a Kalshi 15m agent and a Polymarket 15m agent, as live): both
tick, both take a rule entry in the same window independently, their wakes queue fairly, stopping one leaves the
other running and the kill switch pauses both. Real AgentSessions / TradingService on the shadow exchange; no
network, no real model."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from btc_signal_engine.adapters.http.agent_feed import AgUiFeed, FleetFeeds
from btc_signal_engine.adapters.http.agent_routes import register_agent_routes
from btc_signal_engine.adapters.http.push import FeedHub
from btc_signal_engine.application.actor import as_agent
from btc_signal_engine.application.agent_events import AgentEvents, EventsConfig, MarketState, SignalBoard, Snapshot
from btc_signal_engine.application.agent_fleet import AgentFleet, WakeScheduler
from btc_signal_engine.application.agent_fleet_view import FleetView
from btc_signal_engine.application.agent_prompts import AgentPrompts
from btc_signal_engine.application.agent_runner import AgentRunner
from btc_signal_engine.application.agent_sessions import AgentSessions
from btc_signal_engine.application.ports import WakeResult
from tests.agent.fake_harness import FakeHarness
from tests.agent.test_rules import RuleRig
from tests.agent.test_runner import _yield
from tests.agent.test_tools import HOUR, SID, entry
from tests.trading.helpers import NOW
from tests.pc import as_pc

K15, P15 = "kalshi_15m", "polymarket_15m"
SID2 = "sess0002abcd"


class Svc:
    def __init__(self, mode="shadow"):
        self.mode, self.kill = mode, False

    def killed(self):
        return self.kill

    def present(self, now):
        return False


def spec(**kw):
    s = {"kind": "in_app", "market": K15, "budget": 5.0, "per_trade": 1.0, "ends_ts": NOW + HOUR, "spend_cap": 5.0,
         "model": "claude-haiku-5-5", "level": "auto"}
    s.update(kw)
    return s


def registry(store, svc=None, **kw):
    ids = iter(f"sess000{i}abcd" for i in range(1, 20))
    services = {"you": svc or Svc(), "you_poly": Svc(), "you_poly5": Svc()}
    return AgentSessions(store, services, id_fn=lambda: next(ids), **kw)


def bracket(sid, bid, **kw):
    b = {"id": bid, "env": "shadow", "session_id": sid, "kind": "entry", "ticker": "T", "side": "UP",
         "window_end": NOW + 600_000, "stake": 1.0, "contracts": 1.0, "limit_price": 0.6, "take_profit": 0.99,
         "stop": 0.0, "max_cost": 1.0, "ticket_hash": "h", "state": "open", "filled": 1.0, "entry_cost": 0.61,
         "held": 1.0, "proceeds": 0.0, "created_ts": NOW, "updated_ts": NOW, "strategy": "you"}
    b.update(kw)
    return b


# -- U1: the registry ----------------------------------------------------------------------------------------------
def test_any_number_of_sessions_run_each_named_and_one_market_each(store):
    ag = registry(store)
    a = ag.launch(spec(), NOW)
    b = ag.launch(spec(), NOW)
    c = ag.launch(spec(market=P15), NOW)
    assert [x["session"]["name"] for x in (a, b, c)] == ["Kalshi 15m agent", "Kalshi 15m agent 2",
                                                         "Polymarket 15m agent"]
    assert [s["id"] for s in ag.live()] == [a["id"], b["id"], c["id"]] and ag.latest()["id"] == c["id"]
    assert b["also_traded_by"] == [{"id": a["id"], "name": "Kalshi 15m agent"}] and c["also_traded_by"] == []
    lim = a["session"]["limits"]
    assert (lim["market"], lim["venues"], a["session"]["market"]) == (K15, [K15], K15)
    assert ag.launch(spec(market=[K15, P15]), NOW)["code"] == "one_market"
    assert ag.launch(spec(market=None, venues=[K15, P15]), NOW)["code"] == "one_market"
    assert ag.launch(spec(venues=[K15]), NOW)["code"] == "invalid"  # both fields: refused, never guessed
    assert ag.launch(spec(market="kalshi"), NOW)["code"] == "unknown_venue"
    assert ag.launch(spec(market=None, venues=[P15]), NOW)["session"]["market"] == P15  # the old field, one market
    # names: an agent's, unique among unarchived agents, editable
    assert ag.launch(spec(name="kalshi 15M agent"), NOW)["code"] == "name_taken"
    assert ag.launch(spec(name="  "), NOW)["code"] == "invalid_name"
    assert ag.rename(b["id"], "Cushion   bot", NOW)["session"]["name"] == "Cushion bot"
    assert ag.agent(b["agent_id"])["name"] == "Cushion bot"  # the agent is renamed, not just this run
    assert ag.rename(a["id"], "cushion bot", NOW)["code"] == "name_taken"
    # L0: an agent is lasting; a launch without a name runs the market's idle default agent again
    ag.stop(a["id"], NOW)
    again = ag.launch(spec(), NOW)
    assert again["agent_id"] == a["agent_id"] and again["session"]["name"] == "Kalshi 15m agent"
    assert ag.launch(spec(agent_id=a["agent_id"]), NOW)["code"] == "agent_busy"  # one run at a time
    assert ag.launch(spec(agent_id="nope"), NOW)["code"] == "unknown_agent"
    assert ag.launch(spec(agent_id=c["agent_id"]), NOW)["code"] == "market_mismatch"
    assert ag.launch(spec(agent_id=c["agent_id"], market=None, name="x"), NOW)["code"] == "invalid"
    assert ag.preset_target("Cushion bot", K15) == {"name": "Cushion bot 2"}  # that agent is running
    assert ag.preset_target(None, P15) == {}


def test_every_query_names_its_session(store):
    ag = registry(store)
    a, b = ag.launch(spec(), NOW)["id"], ag.launch(spec(market=P15), NOW)["id"]
    store.insert("trading_brackets", [bracket(a, "a1", state="closed", entry_cost=1.0, proceeds=0.4, held=0),
                                      bracket(b, "b1", strategy="you_poly", max_cost=0.8)])
    assert ag.view(a, NOW)["budget_used"] == 0.6 and ag.view(b, NOW)["budget_used"] == 0.8
    ag.pause(a, NOW, "why", "other")
    assert (ag.get(a)["state"], ag.get(b)["state"]) == ("paused", "running")
    ag.note_usage(b, NOW, api_usd=0.25)
    assert ag.get(b)["usage"] == {"api_usd": 0.25} and ag.get(a)["usage"] == {}
    ag.stop(b, NOW)
    assert ag.get(b) is None and ag.get(a) is not None and ag.view(b, NOW)["state"] == "ended"
    assert [j["data"].get("state") for j in ag.journal_rows(b)] == ["running", "ended"]
    with pytest.raises(TypeError):
        ag.journal("note", {}, NOW)  # a journal row always names its session
    fresh = registry(store)
    fresh.restore(NOW)  # start-up ends every live session, as before
    assert fresh.live() == [] and store.select("agent_sessions", {"id": a})[0]["end_reason"] == "restart"


# -- U2: the gate per session, ownership, global limits, kill switch -------------------------------------------------
def test_the_gate_reads_the_acting_session_and_its_own_limits_and_positions(store):
    ag = registry(store)
    a, b = ag.launch(spec(budget=2.0), NOW)["id"], ag.launch(spec(budget=5.0), NOW)["id"]
    store.insert("trading_brackets", [bracket(a, "a1", max_cost=1.5)])
    with as_agent(a):
        assert ag.check("you", {"kind": "entry", "max_cost": 1.0}, NOW, False).code == "over_budget"
        assert ag.check("you", {"bracket_id": "a1"}, NOW, True).ok
    with as_agent(b):
        assert ag.check("you", {"kind": "entry", "max_cost": 1.0}, NOW, False).ok  # its own budget, untouched
        assert ag.check("you", {"bracket_id": "a1"}, NOW, True).code == "not_yours"  # KD2: only its own
        assert ag.check("you_poly", {"kind": "entry", "max_cost": 1.0}, NOW, False).code == "venue_not_allowed"
    ag.stop(a, NOW)
    with as_agent(a):
        assert ag.check("you", {"kind": "entry", "max_cost": 0.1}, NOW, False).code == "no_agent_session"


def test_global_limits_are_off_by_default_then_refuse_and_pause_every_agent(store):
    ag = registry(store)
    a, b = ag.launch(spec(), NOW)["id"], ag.launch(spec(market=P15), NOW)["id"]
    assert ag.global_limits() == {"max_loss_day": None, "api_day": None}
    assert ag.set_global_limits({"max_loss": 3}, NOW)["code"] == "invalid"  # unknown key: refused, never dropped
    assert ag.set_global_limits({"max_loss_day": 0}, NOW)["code"] == "invalid"
    assert ag.set_global_limits({"max_loss_day": 2.0}, NOW)["ok"]
    store.insert("trading_brackets", [bracket(a, "a1", max_cost=1.0), bracket(b, "b1", strategy="you_poly",
                                                                              max_cost=0.5)])
    with as_agent(b):  # 1.5 of 2.0 used across agents; this 1.0 would pass it
        r = ag.check("you_poly", {"kind": "entry", "max_cost": 1.0}, NOW, False)
    assert r.code == "over_global_limit" and ag.get(b)["state"] == "running"
    store.insert("trading_brackets", [bracket(a, "a2", max_cost=0.5)])  # 2.0 used: reached
    ag.review(NOW)
    assert {s["state"] for s in ag.live()} == {"paused"} and {s["pause_code"] for s in ag.live()} == {"global_limit"}
    assert ag.resume(a, NOW)["code"] == "cannot_resume"
    assert ag.set_global_limits({"max_loss_day": None}, NOW)["ok"]
    assert ag.resume(a, NOW)["ok"]
    assert ag.set_global_limits({"api_day": 0.5}, NOW)["ok"]
    ag.note_usage(a, NOW, api_usd=0.3)
    ag.note_usage(b, NOW, api_usd=0.2)
    ag.review(NOW)
    assert ag.get(a)["pause_code"] == "global_limit" and "API" in ag.get(a)["pause_reason"]
    assert ag.global_api_left(NOW) == 0.0


def test_the_kill_switch_pauses_every_agent(store):
    svc = Svc()
    ag = registry(store, svc)
    ids = [ag.launch(spec(), NOW)["id"], ag.launch(spec(market=P15), NOW)["id"]]
    svc.kill = True  # one switch reaches every phone service; any killed service pauses the whole fleet
    ag.review(NOW)
    assert [(ag.get(i)["state"], ag.get(i)["pause_code"]) for i in ids] == [("paused", "kill_switch")] * 2


def test_opposite_sides_of_one_window_are_warned_once_never_blocked(store):
    seen = []
    ag = registry(store, overlap_fn=seen.append)
    a, b = ag.launch(spec(), NOW)["id"], ag.launch(spec(), NOW)["id"]
    store.insert("trading_brackets", [bracket(a, "a1", side="UP"), bracket(b, "b1", side="DOWN")])
    (w,) = ag.overlaps()
    assert {x["session_id"] for x in w["sides"]["UP"]} == {a} and {x["session_id"] for x in w["sides"]["DOWN"]} == {b}
    assert len(ag.check_overlaps(NOW)) == 1 and ag.check_overlaps(NOW) == []  # once
    assert "Kalshi 15m agent Up vs Kalshi 15m agent 2 Down" in seen[0]["text"] and len(seen) == 1
    assert [j["kind"] for j in ag.journal_rows(a)][-1] == "overlap" and [j["kind"] for j in ag.journal_rows(b)][-1] \
        == "overlap"
    with as_agent(b):
        assert ag.check("you", {"kind": "entry", "max_cost": 1.0}, NOW, False).ok  # a warning, not a block


# -- U3: the wake scheduler -----------------------------------------------------------------------------------------
def test_the_wake_queue_caps_runs_urgent_first_and_coalesces_per_session():
    q = WakeScheduler(cap=2)
    assert [q.offer("a", "normal", 0), q.offer("b", "normal", 10), q.offer("c", "normal", 20),
            q.offer("d", "urgent", 30)] == ["queued"] * 4
    assert q.offer("a", "normal", 40) == "coalesced"  # the unstarted one stays (its place too); newest events win
    assert q.offer("c", "urgent", 50) == "upgraded"
    assert q.metrics("d", 60)["position"] == 2 and q.metrics("c", 60)["position"] == 1  # urgent: first come first
    assert q.take(100) == ["c", "d"]  # the cap: two at once
    assert q.take(100) == [] and q.offer("c", "normal", 100) == "running"
    q.done("c")
    assert q.take(200) == ["a"]  # then normal ones, first come first served
    m = q.metrics("a", 200)
    assert (m["state"], m["wakes"], m["coalesced"], m["last_wait_ms"]) == ("running", 1, 1, 200)
    assert q.metrics("b", 200) == {**q.metrics("b", 200), "state": "queued", "position": 1, "waiting_ms": 190}
    assert q.summary(200) == {"cap": 2, "running": 2, "queued": 1, "urgent_queued": 0, "oldest_wait_ms": 190}
    with pytest.raises(ValueError):
        WakeScheduler(cap=0)
    with pytest.raises(ValueError):
        q.offer("x", "soon", 0)


# -- U4: one detection per market, delivered to every session arming it -----------------------------------------------
def view(now, action, leans, start=0):
    return {"venue": "kalshi", "span": "15m", "positions": [],
            "window": {"start": start, "end": start + 900_000, "entry_cutoff_ms": 120_000, "entry_open": True},
            "market": {"mid_up": 0.5, "strike": 80_000.0, "fresh": True}, "btc": {"btc": 80_010.0, "gap": 10.0},
            "call": {"action": action, "agree": {"n": 4}}, "signals": [{"key": k, "lean": v} for k, v in leans.items()]}


def test_a_call_flip_is_detected_once_per_market_and_reaches_every_engine():
    cfg = EventsConfig()
    board = SignalBoard(cfg.signal_hold_ms)
    engines = [AgentEvents(cfg, board=board) for _ in range(3)]
    calls = []
    ms = board.market("kalshi", "15m")
    real = ms._diff_call
    ms._diff_call = lambda now, m: calls.append(now) or real(now, m)
    t = 1_000
    for action in ("ENTER UP", "ENTER DOWN", "ENTER DOWN"):
        for e in engines:
            e.observe(t, Snapshot.from_views([view(t, action, {"1m": "UP"})]))
        t += cfg.signal_hold_ms
    assert len(calls) == 3  # one detection a tick for the market, not one per session
    for e in engines:
        assert [x["type"] for x in e.pending()] == ["call_changed"]
    with pytest.raises(ValueError):
        AgentEvents(EventsConfig(signal_hold_ms=5), board=board)
    assert MarketState.from_view(view(0, "WAIT", {})).ready


# -- end to end: two agents at once (Kalshi 15m + Polymarket 15m) ---------------------------------------------------
class GateHarness(FakeHarness):
    """A model wake that holds until the test releases it (one release per wake, in order)."""

    def __init__(self) -> None:
        super().__init__()
        self.started: list[str] = []
        self.release: asyncio.Queue | None = None

    async def run_wake(self, system_prompt, wake_prompt, tool_specs, execute, *, session_id, **kw) -> WakeResult:
        self.started.append(session_id)
        await self.release.get()
        return WakeResult(status="done", stop_reason="end_turn", text=None, tool_calls=(), turns=0, cost_usd=0.0,
                          detail={"fake": True})


class Fleet:
    """RuleRig's two shadow books (Kalshi 15m and Polymarket 15m, same window and strike) with H4 armed on a Kalshi
    15m agent (the rig's own session) and a Polymarket 15m agent, one fleet over both."""

    def __init__(self, store, cap=4):
        self.r = r = RuleRig(store, venues=(K15, P15))
        r.ag._id = lambda: SID2
        out = r.ag.launch({"kind": "in_app", "market": P15, "budget": 5.0, "per_trade": 1.0, "ends_ts": NOW + HOUR,
                           "spend_cap": 5.0, "model": "claude-haiku-5-5", "rules": ["H4"], "level": "auto"}, NOW)
        assert out["ok"], out
        self.harness = GateHarness()
        self.board = SignalBoard(EventsConfig().signal_hold_ms)
        made = [r.runner]
        self.reads: list[str] = []
        real = r.play.now_view

        def counted(now, venue, **kw):
            self.reads.append(venue)
            return real(now, venue, **kw)
        r.play.now_view = counted
        self.fleet = AgentFleet(r.ag, r.play, lambda: made.pop() if made else self.runner(), clock=r.runner.clock,
                                cap=cap, tools=r.tools, sleep=_yield)
        self.fleet.attach(SID)
        self.fleet.attach(SID2)

    def runner(self) -> AgentRunner:
        return AgentRunner(self.r.ag, self.r.tools, self.r.play, self.harness,
                           prompts_fn=lambda: ("You supervise.", "", {"system": 1}), clock=self.r.runner.clock,
                           sleep=_yield, signal_board=self.board)

    async def ticks(self, n=1, step=1_000):
        for _ in range(n):
            await self.fleet.tick()
            await asyncio.sleep(0)
            self.r.runner.clock.t += step


def test_two_agents_tick_and_take_their_rule_entries_in_the_same_window_independently(store):
    f = Fleet(store)
    f.r.runner.harness = f.harness  # the rig's own runner shares the gated model too

    async def main():
        f.harness.release = asyncio.Queue()
        await f.ticks(3)
        for _ in range(4):
            f.harness.release.put_nowait(True)
        await f.ticks(2)
    asyncio.run(main())
    assert len(f.r.buys()) == 1 and len(f.r.buys(f.r.pm_ex)) == 1  # one H4 entry each, same window
    rows = {sid: [j["data"] for j in f.r.ag.journal_rows(sid) if j["kind"] == "rule"] for sid in (SID, SID2)}
    assert [(x["market"], x["ok"]) for x in rows[SID]] == [(K15, True)]
    assert [(x["market"], x["ok"]) for x in rows[SID2]] == [(P15, True)]
    owners = {b["strategy"]: b["session_id"] for b in store.select("trading_brackets")}
    assert owners == {"you": SID, "you_poly": SID2}
    # KD6: each tick read each market once for the whole fleet (the steps share those reads)
    assert f.reads.count("kalshi") <= 5 + 2 and f.reads.count("polymarket") <= 5 + 2
    assert set(f.harness.started) == {SID, SID2}  # both supervised their entry (urgent rule_entry wakes)


def test_wakes_queue_fairly_under_the_cap_and_rules_never_wait_for_the_model(store):
    f = Fleet(store, cap=1)
    f.r.runner.harness = f.harness

    async def main():
        f.harness.release = asyncio.Queue()
        await f.ticks(3)  # both enter (rules run while the model is busy), both want a wake; one slot
        assert len(f.harness.started) == 1 and len(f.r.buys()) == 1 and len(f.r.buys(f.r.pm_ex)) == 1
        first = f.harness.started[0]
        other = SID2 if first == SID else SID
        m = f.fleet.metrics(other, f.fleet.now())
        assert (m["state"], m["priority"], m["position"]) == ("queued", "urgent", 1)
        f.harness.release.put_nowait(True)
        await f.ticks(2)
        assert f.harness.started == [first, other]  # the waiting one goes next, not the first one again
        assert f.fleet.metrics(first, f.fleet.now())["wakes"] == 1
        f.harness.release.put_nowait(True)
        await f.ticks(1)
        assert f.fleet.summary(f.fleet.now())["running"] == 0
    asyncio.run(main())


def test_stopping_one_agent_leaves_the_other_running_and_the_kill_switch_pauses_both(store):
    f = Fleet(store)
    f.r.runner.harness = f.harness

    async def main():
        f.harness.release = asyncio.Queue()
        for _ in range(8):
            f.harness.release.put_nowait(True)
        await f.ticks(2)
        out = await f.fleet.stop(SID, f.fleet.now())
        assert out["ok"] and out["session"]["end_reason"] == "stopped"
        assert f.r.ag.get(SID) is None and f.r.ag.get(SID2)["state"] == "running"
        assert f.fleet.runner(SID) is None and f.fleet.runner(SID2) is not None
        await f.ticks(1)
        assert f.r.ag.get(SID2)["state"] == "running"
        await f.r.svc.kill(f.fleet.now())
        _launch_second_kalshi(f)
        await f.ticks(1)
        assert {(s["state"], s["pause_code"]) for s in f.r.ag.live()} == {("paused", "kill_switch")}
        assert len(f.r.ag.live()) == 2
        outs = await f.fleet.stop_all(f.fleet.now())
        assert [o["ok"] for o in outs] == [True, True] and f.r.ag.live() == []
    asyncio.run(main())


def _launch_second_kalshi(f: Fleet) -> None:
    """A third agent launched while the kill switch is on: launch allows it, the next review pauses it too."""
    f.r.ag._id = lambda: "sess0003abcd"
    assert f.r.ag.launch({"kind": "in_app", "market": K15, "budget": 5.0, "per_trade": 1.0, "ends_ts": NOW + HOUR,
                          "spend_cap": 5.0, "model": "claude-haiku-5-5", "level": "auto"}, NOW)["ok"]
    f.fleet.attach("sess0003abcd")


# -- U5: the fleet routes --------------------------------------------------------------------------------------------
def routes(f: Fleet):
    prompts = AgentPrompts(f.r.store, "SYSTEM")
    prompts.seed(NOW)
    main = AgUiFeed(FeedHub(), lambda: {"session": f.r.ag.latest_view(NOW)}, now_fn=lambda: NOW)
    feed = FleetFeeds(main, lambda sid: AgUiFeed(FeedHub(), lambda: {"session": f.r.ag.view(sid, NOW)},
                                                 now_fn=lambda: NOW), live_fn=lambda sid: f.r.ag.get(sid) is not None)
    app = FastAPI()
    register_agent_routes(app, sessions=f.r.ag, tools=f.r.tools, prompts=prompts, feed=feed,
                          now_fn=f.fleet.now, llm=FakeHarness(), fleet=f.fleet, start_task=lambda *a: None)
    return TestClient(as_pc(app)), feed


def test_the_fleet_routes_list_act_per_session_and_stop_all(store):
    f = Fleet(store)
    c, feed = routes(f)
    out = c.get("/api/agent/sessions").json()
    assert [r["id"] for r in out["sessions"]] == [SID, SID2]
    row = out["sessions"][0]
    assert (row["name"], row["market"], row["level"], row["chip"], row["net"], row["trades"]) == (
        "Kalshi 15m agent", K15, "auto", "Watching", 0.0, 0)
    assert row["budget"] == {"total": 5.0, "used": 0.0, "left": 5.0, "pct": 0.0} and row["needs_you"] == []
    assert row["wakes"]["state"] == "idle" and row["line"].startswith("Watching · H4 · ")
    s = out["summary"]
    assert (s["count"], s["running"], s["net"]) == (2, 2, 0.0)
    assert [m["market"] for m in s["by_market"]] == [K15, P15]
    assert c.get("/api/agent/sessions/nope").status_code == 404
    st = c.get(f"/api/agent/sessions/{SID2}").json()
    assert st["session"]["name"] == "Polymarket 15m agent" and st["row"]["market"] == P15
    assert c.post(f"/api/agent/sessions/{SID2}/pause").json()["session"]["state"] == "paused"
    assert c.get("/api/agent/fleet").json()["summary"]["needs_you"][0]["reasons"][0]["code"] == "paused"
    assert c.post(f"/api/agent/sessions/{SID2}/resume", json={}).json()["ok"]
    assert c.post(f"/api/agent/sessions/{SID2}/rename", json={"name": "Poly H4"}).json()["session"]["name"] == \
        "Poly H4"
    note = c.post(f"/api/agent/sessions/{SID2}/note", json={"text": "careful"}).json()
    assert note["ok"] and note["note"]["session_id"] == SID2
    assert f.fleet.events(SID2).pending()[0]["type"] == "human_note" and f.fleet.events(SID).pending() == []
    j = c.get(f"/api/agent/sessions/{SID2}/journal").json()
    assert j["session_id"] == SID2 and any(r["kind"] == "note" for r in j["rows"])
    assert c.get(f"/api/agent/sessions/{SID}/journal").json()["rows"][-1]["kind"] != "note"
    out = c.post(f"/api/agent/sessions/{SID}/stop").json()
    assert out["ok"] and f.r.ag.get(SID2)["state"] == "running"
    assert c.post(f"/api/agent/sessions/{SID2}/stop").json()["session"]["id"] == SID2
    assert c.post("/api/agent/stop_all").json()["code"] == "no_session"
    assert c.get("/api/agent/sessions", params={"ended": 5}).json()["sessions"][0]["chip"] == "Ended"
    assert c.get("/api/agent/scorecard").json()["agents"][0]["id"] in (SID, SID2)
    lim = c.post("/api/agent/global_limits", json={"max_loss_day": 3}).json()
    assert lim["ok"] and c.get("/api/agent/global_limits").json()["limits"]["max_loss_day"] == 3.0


def test_stop_all_and_the_launch_answer(store):
    f = Fleet(store)
    c, feed = routes(f)
    out = c.post("/api/agent/stop_all").json()
    assert out["ok"] and [x["name"] for x in out["stopped"]] == ["Kalshi 15m agent", "Polymarket 15m agent"]
    assert f.r.ag.live() == []
    f.r.ag._id = lambda: "sess0009abcd"
    body = {"kind": "in_app", "market": K15, "budget": 5.0, "per_trade": 1.0, "minutes": 30, "spend_cap": 5.0,
            "model": "claude-haiku-5-5", "level": "auto", "confirm": "START", "name": "Night H4"}
    out = c.post("/api/agent/launch", json=body).json()
    assert out["ok"] and out["id"] == "sess0009abcd" and out["session"]["name"] == "Night H4"
    assert f.fleet.runner("sess0009abcd") is not None
    two = c.post("/api/agent/launch", json={**body, "market": [K15, P15], "name": None}).json()
    assert two["code"] == "one_market"


def test_the_fleet_view_puts_agents_that_need_you_first(store):
    ag = registry(store)
    a, b = ag.launch(spec(), NOW)["id"], ag.launch(spec(budget=1.0), NOW)["id"]
    store.insert("trading_brackets", [bracket(b, "b1", max_cost=0.95)])  # 95% of b's budget at risk
    rows = FleetView(ag).rows(NOW)
    assert [r["id"] for r in rows] == [b, a] and rows[0]["needs_you"][0]["code"] == "budget_near"
    assert rows[0]["chip"] == "Holding" and rows[0]["open"] == {"positions": 1, "at_risk": 0.95}


def test_tool_reads_hide_another_agents_positions(store):
    f = Fleet(store)

    async def main():
        out = await f.r.tools.call("place_entry", entry(market=P15, order_type="market", max_worst_case=1.0,
                                                        stake=1.0),
                                   NOW, session_id=SID2)
        return out
    asyncio.run(main())  # refused (rules own the entries) or sent: either way nothing of SID2 shows to SID
    store.insert("trading_brackets", [bracket(SID2, "x2", strategy="you", ticker=f.r.runner.play.now_view(
        NOW, "kalshi", span="15m")["window"].get("ticker") or "T")])
    obs = asyncio.run(f.r.tools.call("observe", {}, NOW, session_id=SID))
    assert all(p["id"] != "x2" for p in obs["markets"][K15].get("positions") or [])


def test_a_preset_is_one_market_with_a_name_template_made_unique_at_launch(store):
    from btc_signal_engine.application.agent_presets import AgentPresets
    ag = registry(store)
    presets = AgentPresets(store, ag.validate, lambda: {K15: "shadow", P15: "shadow"}, target_fn=ag.preset_target)
    body = {k: v for k, v in spec(name="H4 agent", rules=["H4"]).items() if k != "ends_ts"}
    body["minutes"] = 60
    saved = presets.save("H4", body, NOW)
    assert saved["ok"] and saved["preset"]["modes"] == {K15: "shadow"}
    assert presets.save("two", {**body, "market": [K15, P15]}, NOW)["code"] == "one_market"
    old = {**{k: v for k, v in body.items() if k not in ("market", "name")}, "venues": [K15]}
    assert presets.save("old", old, NOW)["ok"]  # saved before 2026-10-31: venues of one market
    first = presets.launchable("H4")["launch"]
    out = ag.launch(first, NOW)
    assert first["name"] == "H4 agent" and out["ok"]
    assert presets.launchable("H4")["launch"]["name"] == "H4 agent 2"  # that agent runs: the template, numbered
    ag.stop(out["id"], NOW)
    rerun = presets.launchable("H4")["launch"]
    assert rerun["agent_id"] == out["agent_id"] and "name" not in rerun  # idle again: the same agent runs again
    old_launch = presets.launchable("old")["launch"]
    assert "name" not in old_launch and "agent_id" not in old_launch  # the market's default agent


# -- L0: lasting agents and their journal across runs ----------------------------------------------------------------
def test_an_agents_journal_spans_its_runs_newest_first_with_run_summaries(store):
    ag = registry(store)
    first = ag.launch(spec(), NOW)
    aid = first["agent_id"]
    store.insert("trading_brackets", [bracket(first["id"], "a1", state="closed", entry_cost=0.6, proceeds=1.0,
                                              held=0)])
    ag.stop(first["id"], NOW + 1_000, "time", "The end time.")
    other = ag.launch(spec(market=P15), NOW + 2_000)
    second = ag.launch(spec(agent_id=aid), NOW + 3_000)
    assert second["agent_id"] == aid and second["session"]["name"] == "Kalshi 15m agent"
    ag.journal("note", {"text": "hi"}, NOW + 4_000, session_id=second["id"])
    page = ag.journal_page(agent_id=aid, limit=2)
    assert [r["kind"] for r in page["rows"]] == ["note", "state"] and page["next_before_id"] is not None
    assert {r["session_id"] for r in page["rows"]} == {second["id"]} and all(r["agent_id"] == aid
                                                                             for r in page["rows"])
    rest = ag.journal_page(agent_id=aid, before_id=page["next_before_id"], limit=50)
    assert [r["session_id"] for r in rest["rows"]] == [first["id"]] * 2 and rest["next_before_id"] is None
    (run1,) = rest["runs"]
    assert (run1["run"], run1["end_reason"], run1["net"], run1["trades"]) == (1, "time", 0.4, 1)
    runs = ag.runs(agent_id=aid)
    assert [r["run"] for r in runs] == [2, 1] and runs[0]["state"] == "running"
    notes = ag.journal_page(agent_id=aid, kinds=["note"])
    assert [r["kind"] for r in notes["rows"]] == ["note"]
    with pytest.raises(ValueError):
        ag.journal_page(kinds=["notez"])  # an unknown kind is refused, never ignored
    every = ag.journal_page(limit=100)  # the fleet-wide audit
    assert {r["agent_id"] for r in every["rows"]} == {aid, other["agent_id"]}
    listed = {a["id"]: a for a in ag.agent_list(NOW)}
    assert listed[aid]["runs"] == 2 and listed[aid]["running_session_id"] == second["id"]
    assert listed[aid]["net"] == 0.4 and listed[aid]["last_run"]["session_id"] == second["id"]


def test_the_agent_routes_list_agents_and_page_their_journal(store):
    f = Fleet(store)
    c, feed = routes(f)
    agents = c.get("/api/agent/agents").json()["agents"]
    assert [a["name"] for a in agents] == ["Kalshi 15m agent", "Polymarket 15m agent"]
    aid = agents[0]["id"]
    out = c.get(f"/api/agent/agents/{aid}/journal", params={"limit": 1}).json()
    assert out["ok"] and len(out["rows"]) == 1 and out["rows"][0]["session_id"] == SID
    assert out["runs"][0]["session_id"] == SID and out["runs"][0]["run"] == 1
    assert c.get(f"/api/agent/agents/{aid}/journal", params={"kinds": "state,bogus"}).json()["code"] == "invalid"
    assert c.get("/api/agent/agents/nope/journal").status_code == 404
    every = c.get("/api/agent/journal/all", params={"kinds": "state"}).json()
    assert {r["session_id"] for r in every["rows"]} == {SID, SID2} and every["ok"]
    assert c.post(f"/api/agent/agents/{aid}/rename", json={"name": "K agent"}).json()["agent"]["name"] == "K agent"
