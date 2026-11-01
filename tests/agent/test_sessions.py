"""Agent sessions (U3): launch validation, the order gate's session step, the budget, ownership and the lifecycle.

The gate tests drive a real TradingService on the shadow exchange (no network); the lifecycle tests use a stand-in
service with just a mode and a kill switch."""

from __future__ import annotations

import asyncio

import pytest

from btc_signal_engine.application.actor import as_agent
from btc_signal_engine.application.agent_sessions import AgentSessions
from tests.trading.helpers import NOW, Exchange, Market, proposal, ready, service

HOUR = 3_600_000


class Svc:
    def __init__(self, mode="shadow"):
        self.mode, self.kill = mode, False

    def killed(self):
        return self.kill


def spec(**kw):
    s = {"kind": "in_app", "venues": ["kalshi_15m"], "budget": 20.0, "per_trade": 10.0, "ends_ts": NOW + HOUR,
         "spend_cap": 5.0, "model": "claude-haiku-5-5", "level": "auto"}
    s.update(kw)
    return s


def bracket(sid, **kw):
    b = {"id": "b1", "env": "shadow", "session_id": sid, "kind": "entry", "ticker": "T", "side": "UP",
         "window_end": NOW + 600_000, "stake": 5.0, "contracts": 8.0, "limit_price": 0.6, "take_profit": 0.99,
         "stop": 0.0, "max_cost": 5.0, "ticket_hash": "h", "state": "open", "filled": 8.0, "entry_cost": 4.9,
         "held": 8.0, "proceeds": 0.0, "created_ts": NOW, "updated_ts": NOW, "strategy": "you"}
    b.update(kw)
    return b


def sessions(store, svc=None, **kw):
    return AgentSessions(store, {"you": svc or Svc()}, id_fn=lambda: "sess0001abcd", **kw)


# -- launch ---------------------------------------------------------------------------------------------------------
def test_launch_refuses_unknown_venues_bad_amounts_and_production_without_review(store):
    ag = sessions(store)
    for bad, code in ((spec(venues=["kalshi "]), "unknown_venue"), (spec(venues=["poly"]), "unknown_venue"),
                      (spec(budget=0), "invalid"), (spec(per_trade=30.0), "invalid"),
                      (spec(ends_ts=NOW - 1), "invalid"), (spec(ends_ts=NOW + 9 * HOUR), "invalid"),
                      (spec(kind="robot"), "invalid"), (spec(model=None), "invalid")):
        r = ag.launch(bad, NOW)
        assert (r["ok"], r["code"]) == (False, code), bad
    prod = sessions(store, Svc(mode="prod"))
    r = prod.launch(spec(), NOW)
    assert r["code"] == "production_locked" and "paper" in r["reason"]
    assert store.select("agent_sessions") == []


def test_launch_pins_modes_and_an_external_session_gets_a_token_shown_once(store):
    ag = sessions(store)
    r = ag.launch(spec(kind="external", spend_cap=None, model=None), NOW)
    assert r["ok"] and r["session"]["pinned"] == {"you": "shadow"} and r["session"]["state"] == "running"
    token = r["token"]
    row = store.select("agent_sessions")[0]
    assert token not in str(row) and row["token_hash"]
    assert ag.session_for_token(token) == "sess0001abcd" and ag.session_for_token("guess") is None
    ag._id = lambda: "sess0002abcd"
    second = ag.launch(spec(), NOW)  # multi-agent: another live session never refuses a launch
    assert second["ok"] and second["id"] == "sess0002abcd" and len(ag.live()) == 2
    assert second["also_traded_by"] == [{"id": "sess0001abcd", "name": "Kalshi 15m agent"}]
    assert second["session"]["name"] == "Kalshi 15m agent 2"


# -- the gate step ----------------------------------------------------------------------------------------------------
def test_budget_counts_realized_loss_and_open_worst_case(store):  # Covers AE1
    ag = sessions(store)
    ag.launch(spec(), NOW)
    sid = ag.latest()["id"]
    store.insert("trading_brackets", [bracket(sid, id="lost", state="closed", entry_cost=10.0, proceeds=4.0, held=0),
                                      bracket(sid, id="open", max_cost=9.0)])
    with as_agent(sid):
        r = ag.check("you", {"kind": "entry", "max_cost": 6.0}, NOW, False)
        assert (r.ok, r.code) == (False, "over_budget") and "$5.00 of the $20.00" in r.reason
        assert ag.check("you", {"kind": "entry", "max_cost": 5.0}, NOW, False).ok
        assert ag.check("you", {"kind": "entry", "max_cost": 11.0}, NOW, False).code == "over_per_trade"
    v = ag.latest_view(NOW)
    assert v["budget_parts"] == {"realized": 6.0, "at_risk": 9.0, "pending_settlement": 0.0} and v["budget_used"] == 15.0
    assert v["budget_left"] == 5.0


def test_a_settling_position_with_a_partial_take_profit_counts_its_net_worst_case(store):
    """Code review 2026-10-30 #4: a settling bracket whose take profit sold part of it before the close counts what it
    cost (entry + take-profit fees) less what the take profit already brought back: the held rest paying $0."""
    ag = sessions(store)
    ag.launch(spec(), NOW)
    sid = ag.latest()["id"]
    store.insert("trading_brackets", [bracket(sid, id="settling", state="settling", entry_cost=4.9, tp_fees=0.02,
                                              tp_filled=3.0, held=5.0, proceeds=2.1, max_cost=5.0)])
    parts = ag.latest_view(NOW)["budget_parts"]
    assert parts == {"realized": 0.0, "at_risk": 2.82, "pending_settlement": 2.82}


def test_a_winning_trade_frees_budget(store):
    ag = sessions(store)
    ag.launch(spec(budget=5.0, per_trade=5.0), NOW)
    sid = ag.latest()["id"]
    store.insert("trading_brackets", [bracket(sid, id="won", state="closed", entry_cost=4.0, proceeds=7.0, held=0)])
    with as_agent(sid):
        assert ag.budget_used(sid) == -3.0
        assert ag.check("you", {"kind": "entry", "max_cost": 5.0}, NOW, False).ok


def test_only_the_running_session_may_order_and_only_on_its_venues(store):
    ag = sessions(store)
    ag.launch(spec(), NOW)
    sid = ag.latest()["id"]
    with as_agent("someone-else"):
        assert ag.check("you", {"max_cost": 1.0}, NOW, False).code == "no_agent_session"
    with as_agent(sid):
        assert ag.check("you_poly", {"max_cost": 1.0}, NOW, False).code == "venue_not_allowed"
        assert ag.check("you", {"max_cost": 1.0}, NOW + HOUR, False).code == "session_ended"
    assert ag.latest() is None and store.select("agent_sessions")[0]["end_reason"] == "time"


def test_a_mode_change_pauses_the_session_and_refuses(store):  # Covers AE2
    svc = Svc()
    ag = sessions(store, svc)
    ag.launch(spec(), NOW)
    sid = ag.latest()["id"]
    svc.mode = "prod"
    with as_agent(sid):
        r = ag.check("you", {"max_cost": 1.0}, NOW, False)
    assert r.code == "mode_changed" and "switched from shadow to prod" in r.reason
    assert ag.latest()["state"] == "paused" and ag.latest_view(NOW)["pause_code"] == "mode_changed"
    assert ag.resume(ag.latest()["id"], NOW)["code"] == "cannot_resume"


def test_an_agent_order_needs_no_one_at_the_screen_and_is_stamped_with_its_session(store):
    ex = Exchange(Market())
    svc = asyncio.run(ready(service(store, ex, mode="shadow")))
    svc._last_seen = None  # nobody at the screen
    ag = AgentSessions(store, {"you": svc}, id_fn=lambda: "sess0001abcd")
    svc._agent_gate_fn, svc._agent_takeover_fn = ag.check, ag.takeover
    ag.launch(spec(), NOW)

    async def go(as_sid):
        async def run():
            prep = await svc.prepare(proposal(stake=5.0), NOW)
            if not prep["ok"]:
                return prep
            return await svc.approve(prep["token"], "yes", prep["ticket"], NOW)
        if as_sid is None:
            return await run()
        with as_agent(as_sid):
            return await run()

    assert asyncio.run(go(None))["code"] == "not_present"  # a person still needs the screen
    out = asyncio.run(go("sess0001abcd"))
    assert out["ok"] and out["bracket"]["state"] == "open", out
    b = store.select("trading_brackets")[0]
    assert b["session_id"] == "sess0001abcd"
    agent_rows = [r for r in store.select("trading_audit") if r["actor"] == "agent:sess0001"]
    assert agent_rows and {r["session_id"] for r in agent_rows} == {"sess0001abcd"}
    assert ag.budget_used("sess0001abcd") == b["max_cost"]


def test_a_person_taking_over_an_agent_position_keeps_it_counted_and_off_limits_to_the_agent(store):
    ex = Exchange(Market())
    svc = asyncio.run(ready(service(store, ex, mode="shadow")))
    ag = AgentSessions(store, {"you": svc}, id_fn=lambda: "sess0001abcd")
    svc._agent_gate_fn, svc._agent_takeover_fn = ag.check, ag.takeover
    ag.launch(spec(), NOW)

    async def enter():
        with as_agent("sess0001abcd"):
            prep = await svc.prepare(proposal(stake=5.0, take_profit=0.9), NOW)
            return await svc.approve(prep["token"], "yes", prep["ticket"], NOW)

    bid = asyncio.run(enter())["bracket"]["id"]
    used = ag.budget_used("sess0001abcd")
    r = asyncio.run(svc.set_take_profit(bid, 0.8, NOW))  # the person moves the take profit
    assert r["ok"], r
    assert ag.latest()["taken_over"] == [bid] and ag.budget_used("sess0001abcd") == used

    async def agent_exit():
        with as_agent("sess0001abcd"):
            prep = await svc.prepare_exit(bid, NOW)
            return await svc.approve(prep["token"], "yes", prep["ticket"], NOW)

    assert asyncio.run(agent_exit())["code"] == "not_yours"


# -- lifecycle --------------------------------------------------------------------------------------------------------
def test_kill_switch_pauses_and_resume_waits_for_it_to_be_off(store):
    svc = Svc()
    ag = sessions(store, svc)
    ag.launch(spec(), NOW)
    svc.kill = True
    ag.review(NOW)
    assert ag.latest()["state"] == "paused" and "kill switch" in ag.latest()["pause_reason"]
    assert ag.latest_view(NOW)["pause_code"] == "kill_switch"
    assert store.select("agent_sessions")[0]["pause_code"] == "kill_switch"
    assert ag.resume(ag.latest()["id"], NOW)["code"] == "cannot_resume"
    svc.kill = False
    assert ag.resume(ag.latest()["id"], NOW)["ok"] and ag.latest()["state"] == "running"
    assert ag.latest_view(NOW)["pause_code"] is None and store.select("agent_sessions")[0]["pause_code"] is None


def test_pause_codes_are_a_closed_set_and_an_end_clears_the_code(store):
    ag = sessions(store)
    ag.launch(spec(), NOW)
    with pytest.raises(ValueError):
        ag.pause(ag.latest()["id"], NOW, "why", "sleepy")
    assert ag.latest()["state"] == "running"
    ag.pause(ag.latest()["id"], NOW, "The model is unavailable: overloaded", "model_unavailable")
    assert ag.latest_view(NOW)["pause_code"] == "model_unavailable"
    paused = [j for j in ag.journal_rows("sess0001abcd") if j["data"].get("state") == "paused"]
    assert paused[-1]["data"]["code"] == "model_unavailable"
    ag.stop(ag.latest()["id"], NOW)
    assert ag.latest_view(NOW)["pause_code"] is None and ag.latest_view(NOW)["pause_reason"] is None


def test_launch_takes_minutes_on_the_server_clock_or_ends_ts_but_exactly_one(store):
    ag = sessions(store)
    no_end = {k: v for k, v in spec().items() if k != "ends_ts"}
    for bad in (no_end, spec(minutes=30), {**no_end, "minutes": 0}, {**no_end, "minutes": 481},
                {**no_end, "minutes": True}, {**no_end, "minutes": "30"}, {**no_end, "minutes": -5}):
        r = ag.launch(bad, NOW)
        assert (r["ok"], r["code"]) == (False, "invalid"), bad
    assert store.select("agent_sessions") == []
    r = ag.launch({**no_end, "minutes": 30}, NOW)
    assert r["ok"] and r["session"]["ends_ts"] == NOW + 30 * 60_000
    ag.stop(ag.latest()["id"], NOW)
    ag._id = lambda: "sess0002abcd"
    assert ag.launch({**no_end, "minutes": 480}, NOW)["session"]["ends_ts"] == NOW + 8 * HOUR


def test_a_session_left_running_at_start_up_ends_as_restart(store):
    ag = sessions(store)
    ag.launch(spec(), NOW)
    fresh = sessions(store)
    fresh.restore(NOW + 1000)
    row = store.select("agent_sessions")[0]
    assert (row["state"], row["end_reason"], fresh.latest()) == ("ended", "restart", None)
    kinds = [j["data"].get("state") for j in fresh.journal_rows(row["id"])]
    assert kinds == ["running", "ended"]


def test_a_mode_change_never_traps_the_sessions_own_position_on_its_pinned_environment(store):
    """Review fix plan 2026-10-31 P0 #3: after the venue's mode changed, the agent may still EXIT its own position on
    the environment it was pinned to (entries stay refused and the session pauses, as before)."""
    svc = Svc()
    svc.modes = {"sandbox": "demo", "shadow": "shadow", "prod": "prod"}
    ag = sessions(store, svc)
    ag.launch(spec(), NOW)
    sid = ag.latest()["id"]
    store.insert("trading_brackets", [bracket(sid)], ignore_conflicts=False)
    svc.mode = "sandbox"  # e.g. a credential problem dropped the service to its safe mode
    exit_ticket = {"kind": "exit", "env": "shadow", "bracket_id": "b1", "max_cost": 0.0}
    with as_agent(sid):
        assert ag.check("you", {"max_cost": 1.0, "env": "demo"}, NOW, False).code == "mode_changed"  # entries: no
        assert ag.latest()["state"] == "paused"
        assert ag.check("you", exit_ticket, NOW, True).ok  # its own position on its pinned env: yes
        assert ag.check("you", {**exit_ticket, "env": "demo"}, NOW, True).code == "session_paused"
