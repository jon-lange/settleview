"""The agent fleet at scale (review fix plan 2026-10-31 P1, "Event loop and database"): with 30 live sessions every
fleet tick makes a fixed number of database reads (one brackets read for every session, the shared market view, the
overlap check), not three or more per session (~200-300 queries/s at 30 agents before), and the agent list asks a
fixed number of queries however many agents it shows. Real AgentSessions / TradingService on the shadow exchange; the
store's select / insert / update calls are counted. Budgets are on the median tick (docs/solutions/
2026-11-01-ci-timing-budgets.md)."""

from __future__ import annotations

import asyncio
import statistics
import time

from btc_signal_engine.application.agent_events import EventsConfig, SignalBoard
from btc_signal_engine.application.agent_fleet import AgentFleet
from btc_signal_engine.application.agent_runner import AgentRunner
from tests.agent.test_fleet import GateHarness
from tests.agent.test_rules import K, RuleRig
from tests.agent.test_runner import _yield
from tests.agent.test_tools import HOUR
from tests.trading.helpers import NOW

TICKS = 12
FLEET_QUERIES_PER_TICK = 8  # measured 6 at 5 and at 30 sessions (2026-11-01); before: 21 at 5, 96 at 30
AGENT_LIST_QUERIES = 3  # agents, their sessions, those sessions' brackets; before: 91 for 30 agents


class Counted:
    """The store with its read / write calls counted (by method)."""

    def __init__(self, store) -> None:
        self.store, self.calls = store, []
        for name in ("select", "insert", "update"):
            real = getattr(store, name)

            def counted(*a, _real=real, _name=name, **kw):
                self.calls.append((_name, a[0] if a else kw.get("table")))
                return _real(*a, **kw)
            setattr(store, name, counted)

    def reads(self) -> int:
        return sum(1 for name, _ in self.calls if name == "select")


def fleet_of(store, n: int) -> tuple[RuleRig, AgentFleet, Counted]:
    """n live in-app sessions on Kalshi 15m with H4 armed, BTC at the strike (no rule matches: the steady state)."""
    r = RuleRig(store, btc=K)  # the rig's own session is the first
    ids = iter(f"load{i:04d}abcdef" for i in range(1, n))
    r.ag._id = lambda: next(ids)
    for _ in range(n - 1):
        out = r.ag.launch({"kind": "in_app", "market": "kalshi_15m", "budget": 5.0, "per_trade": 1.0,
                           "ends_ts": NOW + HOUR, "spend_cap": 5.0, "model": "claude-haiku-5-5", "rules": ["H4"],
                           "level": "auto"}, NOW)
        assert out["ok"], out
    harness, board = GateHarness(), SignalBoard(EventsConfig().signal_hold_ms)
    made = [r.runner]

    def make() -> AgentRunner:
        if made:
            return made.pop()
        return AgentRunner(r.ag, r.tools, r.play, harness, prompts_fn=lambda: ("You supervise.", "", {"system": 1}),
                           clock=r.runner.clock, sleep=_yield, signal_board=board)
    fleet = AgentFleet(r.ag, r.play, make, clock=r.runner.clock, tools=r.tools, sleep=_yield)
    for s in r.ag.live():
        fleet.attach(s["id"])
    assert len(fleet._runners) == n
    return r, fleet, Counted(store)


def measure(store, n: int) -> tuple[float, float]:
    """(median database reads per fleet tick, median tick ms) over TICKS warm ticks."""
    r, fleet, counted = fleet_of(store, n)
    reads, ms = [], []

    async def main():
        for i in range(TICKS + 2):
            before = counted.reads()
            t0 = time.perf_counter()
            await fleet.tick()
            await asyncio.sleep(0)
            if i >= 2:  # warm: attach-time reads and the first enforce are behind
                ms.append((time.perf_counter() - t0) * 1000)
                reads.append(counted.reads() - before)
            r.runner.clock.t += 1_000
    asyncio.run(main())
    return statistics.median(reads), statistics.median(ms)


def test_a_fleet_tick_reads_the_database_a_fixed_number_of_times_however_many_sessions_run(store):
    few, few_ms = measure(store, 5)
    print(f"fleet tick, 5 sessions: {few:.0f} reads, {few_ms:.1f} ms")
    assert few <= FLEET_QUERIES_PER_TICK, few


def test_thirty_sessions_cost_no_more_reads_per_tick_than_five(store):
    many, many_ms = measure(store, 30)
    print(f"fleet tick, 30 sessions: {many:.0f} reads, {many_ms:.1f} ms")
    assert many <= FLEET_QUERIES_PER_TICK, many


def test_the_agent_list_asks_a_fixed_number_of_queries(store):
    r, fleet, counted = fleet_of(store, 30)
    before = counted.reads()
    rows = r.ag.agent_list(NOW)
    n = counted.reads() - before
    print(f"agent list, 30 agents: {n} reads")
    assert len(rows) == 30 and all(x["runs"] == 1 for x in rows)
    assert n <= AGENT_LIST_QUERIES, n


def test_the_fleet_view_reads_every_holding_sessions_rule_links_at_once(store):
    """FleetView.rows: one journal read for every session holding a position (was one per holding session)."""
    from btc_signal_engine.application.agent_fleet_view import FleetView
    from tests.agent.test_fleet import bracket
    r, fleet, counted = fleet_of(store, 30)
    sids = [s["id"] for s in r.ag.live()]
    store.insert("trading_brackets", [bracket(sid, f"h{i}") for i, sid in enumerate(sids)])
    for i, sid in enumerate(sids):
        r.ag.journal("rule", {"rule": "H4", "ok": True, "bracket_id": f"h{i}", "market": "kalshi_15m"}, NOW,
                     session_id=sid)
    before = len([c for c in counted.calls if c[1] == "agent_journal"])
    rows = FleetView(r.ag).rows(NOW)
    assert len([c for c in counted.calls if c[1] == "agent_journal"]) - before == 1
    assert all(x["holding"] and x["holding"][0]["rule"] == "H4" for x in rows)
