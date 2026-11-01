import math

from btc_signal_engine.application.engine.engine import Engine
from btc_signal_engine.application.engine.hydrate import hydrate
from btc_signal_engine.application.engine.loop import due_boundaries
from btc_signal_engine.application.rulesets import RulesetService
from btc_signal_engine.application.settlement import SettlementService
from btc_signal_engine.domain.models.events import BookSecond, Trade

T12 = 1_793_188_800_000  # 2026-10-28 12:00:00 UTC (a 15m boundary)
SEEDS = [{"name": "momentum", "signals": ["momentum"], "horizons": [60, 300, 900], "champion": True},
         {"name": "breakout", "signals": ["breakout"], "horizons": [60, 300, 900]}]


def synthetic_events(start, end):
    """One trade per second on a gentle sine path, plus a book row per second (synthetic)."""
    for i, ts in enumerate(range(start, end, 1000)):
        price = 80_000 + 50 * math.sin(i / 300)
        yield Trade(trade_id=i + 1, ts=ts + 100, ingest_ts=ts + 150, price=price, size=0.01 + (i % 7) / 100, buy=i % 3 != 0)
        yield BookSecond(ts=ts, ingest_ts=ts + 1000, bid=price - 0.01, ask=price + 0.01,
                         bid_depth_10=5, ask_depth_10=4, bid_depth_25=9, ask_depth_25=8)


def make_engine(store):
    rs = RulesetService(store)
    rs.load_seeds(SEEDS, now=0)
    return Engine(store, rs, SettlementService(store, store), (60, 300, 900))


def test_horizons_decide_only_on_their_boundaries(store):  # AE1
    eng = make_engine(store)
    for ev in synthetic_events(T12 - 3_600_000, T12 + 300_000):
        if ev.ts < T12:
            eng.on_event(ev)
    rows = eng.on_boundary(T12, now=T12 + 2000)
    assert {r["horizon"] for r in rows} == {60, 300, 900}
    per_h = 2 + 3  # two rulesets + three baselines
    assert len(rows) == 3 * per_h
    for ev in synthetic_events(T12, T12 + 300_000):
        if ev.ts < T12 + 60_000:
            eng.on_event(ev)
    assert {r["horizon"] for r in eng.on_boundary(T12 + 60_000, now=T12 + 62_000)} == {60}
    for ev in synthetic_events(T12 + 60_000, T12 + 300_000):
        eng.on_event(ev)
    assert {r["horizon"] for r in eng.on_boundary(T12 + 300_000, now=T12 + 302_000)} == {60, 300}
    assert {w["horizon"] for w in store.select("windows")} == {60, 300, 900}
    snap_ts = {r["snapshot_ts"] for r in rows if r["horizon"] == 60}
    assert snap_ts == {T12}  # rulesets and baselines share one snapshot


def test_missed_boundaries_after_sleep_are_not_computed_late():  # AE4
    missed, decide = due_boundaries(last_done=T12 + 3 * 60_000, now=T12 + 20 * 60_000 + 30_000)
    assert decide is None
    assert missed[0] == T12 + 4 * 60_000 and missed[-1] == T12 + 20 * 60_000
    missed, decide = due_boundaries(last_done=T12 - 60_000, now=T12 + 2_500)
    assert missed == [] and decide == T12


def test_record_missed_writes_missed_rows(store):
    eng = make_engine(store)
    rows = eng.record_missed(T12, now=T12 + 600_000)
    assert {r["call"] for r in rows} == {"MISSED"} and {r["horizon"] for r in rows} == {60, 300, 900}


def test_restarted_engine_matches_uninterrupted_engine(store):
    start = T12 - 2 * 3_600_000
    events = list(synthetic_events(start, T12))
    store.add_trades([e for e in events if isinstance(e, Trade)])
    store.add_book_seconds([e for e in events if isinstance(e, BookSecond)])
    live = make_engine(store)
    for ev in sorted(events, key=lambda e: (e.ts, isinstance(e, BookSecond))):
        live.on_event(ev)
    restarted = make_engine(store)
    assert hydrate(restarted, store, now=T12) == len(events)
    assert live.state.snapshot(T12) == restarted.state.snapshot(T12)
