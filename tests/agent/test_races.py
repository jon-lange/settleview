"""Agent stop / pause / reservation races (docs/plans/2026-10-31-1800-review-fix-plan.md P0 #4 and the agent P1s;
docs/solutions/agent-stop-and-wake-races.md). Each test failed on main before its fix. Real AgentSessions /
AgentTools / TradingService over the shadow exchange, the scripted fake harness, injected clocks; no network, no
real model."""

from __future__ import annotations

import asyncio
import json

from btc_signal_engine.application.agent_runner import RunnerConfig, worst_case_usd
from tests.agent.fake_harness import text, tool_use, turn
from tests.agent.test_fleet import Fleet, bracket
from tests.agent.test_rules import RuleRig
from tests.agent.test_runner import note, setup, wakes
from tests.agent.test_tools import SID, entry
from tests.trading.helpers import NOW


def _hold_posts(ex) -> dict:
    """Every order POST to this shadow exchange waits for box["release"]; box["posting"] is set when one arrives."""
    sim = ex.sim
    real = sim.handle_async_request
    box: dict = {}

    async def slow(request):
        if request.method == "POST":
            box["posting"].set()
            await box["release"].wait()
        return await real(request)
    sim.handle_async_request = slow
    return box


def _watch_end(r) -> list[dict]:
    """What the store held when each session ended: its "rule" rows and its brackets' filled contracts."""
    seen: list[dict] = []
    real = r.ag._end

    def end(sid, reason, now, text_):
        seen.append({"sid": sid, "rules": len([j for j in r.ag.journal_rows(sid) if j["kind"] == "rule"]),
                     "filled": [float(b.get("filled") or 0) for b in r.store.select("trading_brackets",
                                                                                    {"session_id": sid})]})
        real(sid, reason, now, text_)
    r.ag._end = end
    return seen


# -- P0 #4: Stop drains every order in flight before the session ends ------------------------------------------------
def test_stop_of_a_session_run_alone_waits_for_its_rule_entry_and_journals_it_before_the_end(store):
    r = RuleRig(store)
    box = _hold_posts(r.ex)
    ended = _watch_end(r)

    async def main():
        box["posting"], box["release"] = asyncio.Event(), asyncio.Event()
        loop = asyncio.create_task(r.runner.run(SID))
        await box["posting"].wait()  # the rule's IoC is on its way
        stopping = asyncio.create_task(r.runner.stop(r.runner.now()))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not stopping.done() and ended == []  # the session stays until the order is answered
        box["release"].set()
        assert (await stopping)["ok"]
        await asyncio.sleep(0)
        assert loop.done()
    asyncio.run(main())
    (rule,) = r.journal("rule")  # main: the cancelled loop dropped the row (the fill had no rule row)
    assert rule["ok"] and rule["bracket_id"]
    assert ended == [{"sid": SID, "rules": 1, "filled": [ended[0]["filled"][0]]}] and ended[0]["filled"][0] > 0
    # the scorekeeper sees the trade (auto-retire counts it)
    assert [t["bracket_id"] for t in r.ag.rule_book.trades.all()] == [rule["bracket_id"]]


def test_fleet_stop_ends_the_session_only_after_the_rule_entry_in_flight_is_filled_and_journaled(store):
    f = Fleet(store)
    f.r.runner.harness = f.harness
    box = _hold_posts(f.r.ex)  # the Kalshi agent's order hangs; the Polymarket agent's goes through
    ended = _watch_end(f.r)

    async def main():
        box["posting"], box["release"] = asyncio.Event(), asyncio.Event()
        f.harness.release = asyncio.Queue()
        for _ in range(8):
            f.harness.release.put_nowait(True)
        ticking = asyncio.create_task(f.fleet.tick())
        await box["posting"].wait()
        stopping = asyncio.create_task(f.fleet.stop(SID, f.fleet.now()))
        for _ in range(5):
            await asyncio.sleep(0)
        assert ended == []  # main: ended at once, the rule row and the fill landing on an ended session
        box["release"].set()
        out = await stopping
        await ticking
        assert out["ok"] and f.r.ag.get(SID) is None
    asyncio.run(main())
    (mine,) = [e for e in ended if e["sid"] == SID]
    assert mine["rules"] == 1 and mine["filled"] and mine["filled"][0] > 0


# -- a queued wake re-checks the session when it starts --------------------------------------------------------------
def test_a_queued_wake_that_starts_after_a_pause_or_an_end_does_nothing_and_keeps_its_events(store):
    r, runner, llm, _ = setup(store, turn(text("never")))
    note(runner)
    assert runner.wake_due(runner.now()) is not None  # queued while running...
    r.ag.pause(SID, NOW, "Paused by you.", "other")  # ...paused before it started
    out = asyncio.run(runner.wake(runner.now()))
    assert out["status"] == "not_running" and llm.requests == [] and wakes(r) == []
    assert [e["type"] for e in runner.events.pending()] == ["human_note"]  # kept for the next wake
    assert r.ag.resume(SID, NOW)["ok"]
    r.ag.stop(SID, NOW)  # ended while queued: main raised TypeError outside the wake's try
    assert asyncio.run(runner.wake(runner.now()))["status"] == "not_running" and llm.requests == []


def test_a_write_dispatched_after_a_pause_mid_wake_is_refused_by_the_runner(store):
    def pause_then_buy(req):
        r.ag.pause(SID, NOW, "Paused by you.", "other")
        return turn(tool_use("t1", "place_entry", entry()))
    r, runner, llm, _ = setup(store, pause_then_buy, turn(text("done")))
    note(runner)
    asyncio.run(runner.wake(runner.now()))
    (w,) = wakes(r)
    assert [(x["name"], x["code"]) for x in w["tools"]] == [("place_entry", "session_paused")]
    assert r.buys() == [] and w["status"] == "session_stopped"  # the gate between turns stops the wake too


# -- the account diff after a tool call re-baselines only the touched bracket -----------------------------------------
def test_another_positions_stop_out_during_a_tool_call_is_still_told_to_the_agent(store):
    r, runner, llm, _ = setup(store, turn(tool_use("t1", "place_entry", entry())), turn(text("done")))
    store.insert("trading_brackets", [bracket(SID, "other", strategy="you")])
    runner._scan(runner.now(), push=False)  # baseline: the other position is open
    real = runner.tools.call

    async def call(name, args, now, **kw):
        if name == "place_entry":  # while the agent's order is out, the other position is stopped out
            store.update("trading_brackets", {"id": "other"}, {"state": "closed", "exit_reason": "stop",
                                                              "held": 0.0, "proceeds": 0.4})
        return await real(name, args, now, **kw)
    runner.tools.call = call
    note(runner)
    asyncio.run(runner.wake(runner.now()))
    (w,) = wakes(r)
    told = [e for e in [*(w.get("new_events") or []), *runner.events.pending()]
            if e["type"] == "position_closed"]
    assert [b["id"] for e in told for b in e["latest"]["brackets"]] == ["other"]  # main: swallowed
    mine = [b for b in store.select("trading_brackets") if b["id"] != "other"]
    assert len(mine) == 1 and not any(e["type"] == "fill" and e["value"]["brackets"][0]["id"] == mine[0]["id"]
                                      for e in told)


def test_a_rule_entry_never_swallows_another_positions_settlement(store):
    r = RuleRig(store)
    store.insert("trading_brackets", [bracket(SID, "old", strategy="you", window_end=NOW - 1, state="settling")])
    r.runner._scan(r.runner.now(), push=False)
    real = r.tools.call

    async def call(name, args, now, **kw):
        out = await real(name, args, now, **kw)
        if name == "place_entry":  # the earlier window's result is booked while the rule's order is answered
            store.update("trading_brackets", {"id": "old"}, {"state": "closed", "exit_reason": "settled",
                                                            "held": 1.0, "proceeds": 1.0})
        return out
    r.tools.call = call
    asyncio.run(r.runner.rules_tick(r.runner.now()))
    kinds = [(e["type"], [b["id"] for b in (e.get("latest") or {}).get("brackets") or []])
             for e in r.runner.events.pending()]
    assert ("settled", ["old"]) in kinds and r.runner._closed_seen  # told, and the scorekeeper checks it


# -- the global daily API limit has one ledger for the fleet ----------------------------------------------------------
def test_concurrent_wakes_cannot_each_reserve_the_last_of_the_global_api_limit(store):
    f = Fleet(store)
    f.r.runner.harness = f.harness
    one = worst_case_usd("claude-haiku-5-5", 6_000, RunnerConfig().max_tokens)
    assert f.r.ag.set_global_limits({"api_day": round(one * 1.6, 4)}, NOW)["ok"]  # room for one wake, not two

    async def main():
        f.harness.release = asyncio.Queue()
        await f.ticks(2)  # both agents take their rule entry and want an urgent wake at once (cap 4)
        assert f.r.ag.api_held() > 0  # the running wake's turn is held for the whole fleet
        for _ in range(4):
            f.harness.release.put_nowait(True)
        await f.ticks(1)
    asyncio.run(main())
    assert len(f.harness.started) == 1  # main: both started, overshooting the limit
    other = next(s for s in ("sess0001abcd", "sess0002abcd") if s not in f.harness.started)
    rows = [j["data"] for j in f.r.ag.journal_rows(other) if j["kind"] == "wake"]
    assert [x["status"] for x in rows] == ["global_limit"]


# -- a write tool that raises still clears the write in flight --------------------------------------------------------
def test_a_write_tool_that_raises_is_recorded_and_clears_the_write_in_flight(store):
    r, runner, llm, _ = setup(store, turn(tool_use("t1", "place_entry", entry())), turn(text("done")))
    real = runner.tools.call

    async def call(name, args, now, **kw):
        if name == "place_entry":
            raise RuntimeError("venue client blew up")
        return await real(name, args, now, **kw)
    runner.tools.call = call
    note(runner)
    asyncio.run(runner.wake(runner.now()))
    (w,) = wakes(r)
    assert runner._inflight is None and runner._inflight_entry is None and runner._orders == set()
    assert w["status"] == "done" and r.ag.get(SID)["state"] == "running"  # main: the wake failed, session paused
    assert (w["tools"][0]["code"], w["tools"][0]["ok"]) == ("error", False)
    assert "venue client blew up" in json.loads(llm.requests[0].results[0][0][1])["reason"]


# -- the memory block stays bounded ------------------------------------------------------------------------------------
def test_the_memory_block_shows_open_positions_the_last_closed_ones_and_a_summary(store):
    r, runner, llm, _ = setup(store, turn(text("ok")), config=RunnerConfig(trades_kept=3))
    store.insert("trading_brackets", [bracket(SID, f"c{i:02d}", state="closed", created_ts=NOW - 100_000 + i,
                                              entry_cost=0.5, proceeds=1.0 if i % 2 else 0.0, held=0)
                                      for i in range(12)] + [bracket(SID, "open1", created_ts=NOW - 1)])
    memory = runner._memory(runner.now(), [])

    assert [t["id"] for t in memory["trades"]] == ["c09", "c10", "c11", "open1"]  # main: all 13
    assert memory["trades_summary"] == {"closed": 12, "closed_shown": 3, "filled": 12, "won": 6, "lost": 6,
                                        "net": 0.0}


# -- the scorekeeper's tally runs once per fleet tick, not once per session -------------------------------------------
def test_the_auto_retire_tally_runs_once_a_minute_for_the_whole_fleet(store):
    f = Fleet(store)
    f.r.runner.harness = f.harness
    calls = []
    book = f.r.ag.rule_book
    real = book.trades.all
    book.trades.all = lambda: calls.append(f.fleet.now()) or real()

    async def main():
        f.harness.release = asyncio.Queue()
        for _ in range(200):
            f.harness.release.put_nowait(True)
        await f.ticks(62)
    asyncio.run(main())
    assert len(calls) == 1  # main: one per session (2) at the minute
