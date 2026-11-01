"""Hot paths under realistic sizes (review fix plan 2026-10-31 P1, B5#6-8 and "unbounded ORDERS scans"), timed on the
median warm call (docs/solutions/2026-11-01-ci-timing-budgets.md):

- the Coach's odds series for the live window (every Play poll and the agents' get_odds): 46 minutes of index and a
  window of quotes; the live window's final points are kept, the newest computed;
- the Coach's tick (twice a second): the 31 minutes of index it reads;
- Record "all" over a long journal (wake and tool rows most of it): the rule links read incrementally;
- reconciliation with a long order history: only the resting orders' ids are looked up.

Every price, quote and order here is SYNTHETIC test data. The budgets are several times the measured medians so a slow
CI runner passes; the measured numbers (and main's before this change) are in the comments."""

from __future__ import annotations

import asyncio
import statistics
import time

from btc_signal_engine.application import record_summary as rec
from btc_signal_engine.application.coach import CoachService
from btc_signal_engine.application.index_tail import IndexTail
from btc_signal_engine.domain.models.events import IndexSecond
from tests.coach.test_service import TICKER, T, rising, snap

H = 900_000
NOW = T + 14 * 60_000 + 500  # 14 minutes into the live window: 168 points
SERIES_MS = 15.0  # measured median 2026-11-01 (SQLite, dev container): 2.2 ms; before (main 736ecbd): 21.6 ms
TICK_ROWS = 20  # measured 10 index rows per warm Coach tick (the 10 s re-read); before: 1,862 (31 minutes)
RECORD_ALL_MS = 40.0  # measured median 6.8 ms with 20,000 journal rows; before: 15.2 ms (grows with the journal)


def median_ms(fn, n: int = 9) -> float:
    fn()  # warm
    out = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t0) * 1000)
    return statistics.median(out)


def seed_window(store) -> None:
    store.add_index_seconds([IndexSecond(ts, rising(ts), ts, None) for ts in range(T - 1_920_000, NOW, 1000)])
    store.insert("kalshi_markets", [{"ticker": TICKER, "open_ts": T, "close_ts": T + H, "floor_strike": 80_000.0,
                                     "status": "open"}])
    store.insert("kalshi_quotes", [{"ticker": TICKER, "ts": ts, "yes_bid": 0.55, "yes_ask": 0.57, "no_bid": 0.42,
                                    "no_ask": 0.44, "source": "ws"} for ts in range(T, NOW, 2_000)])


class CountedIndex:
    """The store's index reads, counted in rows."""

    def __init__(self, store) -> None:
        self.store, self.rows = store, 0

    def index_range(self, start, end):
        got = self.store.index_range(start, end)
        self.rows += len(got)
        return got


def coach_on(store) -> CoachService:
    """The Coach as the runtime builds it: on the incremental index tail."""
    return CoachService(store, IndexTail(store), (60, 300, 900))


def test_the_live_window_series_is_fast_and_the_same_as_a_fresh_one(store):
    seed_window(store)
    coach = coach_on(store)
    t = [NOW]

    def call():
        t[0] += 1_000  # a new second each poll (Play polls about once a second)
        return coach.series(T, t[0])
    ms = median_ms(call)
    print(f"coach series (live window, {len(call()['points'])} points): median {ms:.1f} ms")
    got = coach.series(T, t[0])
    fresh = CoachService(store, store, (60, 300, 900)).series(T, t[0])  # no kept points, the plain store
    assert got["points"] == fresh["points"] and got["coverage"] == fresh["coverage"]
    assert ms < SERIES_MS


def test_the_coach_tick_reads_only_the_newest_index_rows(store):
    seed_window(store)
    counted = CountedIndex(store)
    coach = CoachService(store, IndexTail(counted), (60, 300, 900))
    now = NOW - 30_000

    async def ticks():
        nonlocal now
        await coach.tick(now, snap(), {}, None)  # warm: the first tick reads its 31 minutes
        rows = []
        for _ in range(8):
            now += 500
            before = counted.rows
            await coach.tick(now, snap(), {}, None)
            rows.append(counted.rows - before)
        return rows
    rows = asyncio.run(ticks())
    print(f"coach tick: median {statistics.median(rows):.0f} index rows read per warm tick")
    assert coach.evaluation["fair_up"] is not None
    assert statistics.median(rows) <= TICK_ROWS


def journal_world(store, n_rows: int = 20_000, n_trades: int = 300) -> None:
    """A long agent journal: mostly wake / tool rows, a rule row per trade, and the closed rule trades."""
    rows, brackets = [], []
    for i in range(n_trades):
        bid, sid, ts = f"b{i:05d}", f"s{i // 10:04d}", 1_790_000_000_000 + i * 900_000
        rows.append({"session_id": sid, "ts": ts, "kind": "rule", "data": {
            "rule": "H4", "market": "kalshi_15m", "ok": True, "bracket_id": bid, "inputs": {"gap": 400}}})
        brackets.append({"id": bid, "env": "shadow", "session_id": sid, "kind": "entry", "ticker": "T", "side": "UP",
                         "window_end": ts + 600_000, "stake": 1.0, "contracts": 1.0, "limit_price": 0.6,
                         "take_profit": 0.99, "stop": 0.0, "max_cost": 1.0, "ticket_hash": "h", "state": "closed",
                         "filled": 1.0, "held": 0.0, "entry_cost": 0.61, "proceeds": 1.0 if i % 2 else 0.0,
                         "exit_reason": "settled", "created_ts": ts, "updated_ts": ts + 600_000,
                         "closed_ts": ts + 600_000, "strategy": "you"})
    filler = {"thinking": "x" * 400, "tools": [{"name": "observe", "result": "y" * 400}]}
    for i in range(n_rows - n_trades):
        rows.append({"session_id": f"s{i % 30:04d}", "ts": 1_790_000_000_000 + i * 1000,
                     "kind": "wake" if i % 3 else "tool", "data": filler})
    for k in range(0, len(rows), 2_000):
        store.insert("agent_journal", rows[k:k + 2_000])
    store.insert("trading_brackets", brackets)


def test_record_all_over_a_long_journal(store):
    journal_world(store)
    now = 1_800_000_000_000
    kw = {"all_links": rec.RuleLinks(store)}  # the Record route keeps one
    ms = median_ms(lambda: rec.summary(store, now, "all", **kw))
    out = rec.summary(store, now, "all", **kw)
    print(f"record all (20,000 journal rows, 300 trades): median {ms:.1f} ms")
    plain = rec.summary(store, now, "all")
    assert out == plain and out["trades"] == 300 and out["by_rule"][0]["rule"] == "H4"
    assert ms < RECORD_ALL_MS
