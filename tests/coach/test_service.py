"""CoachService (the evaluation behind Play) against a real (temporary) SQLite store.

Every price, quote and lean written here is SYNTHETIC test data, built only for these tests.
"""

import asyncio
import threading
import time

import pytest

from btc_signal_engine.application.coach import CoachService
from btc_signal_engine.domain.models.events import IndexSecond

T = 1_800_000_000_000 - 1_800_000_000_000 % 900_000
END = T + 900_000
AT = T + 300_000
TICKER = "SYNTHETIC-15M"


def wiggle(ts):
    return 1.0 if (ts // 1000) % 2 else -1.0


def seed_index(store, start, end, price_fn):
    store.add_index_seconds([IndexSecond(ts, price_fn(ts), ts, None) for ts in range(start, end, 1000)])


def rising(ts):
    """Flat 80,000 before the window, then up $0.20/s (synthetic)."""
    base = 80_000.0 if ts < T else 80_000.0 + 0.2 * (ts - T) / 1000
    return base + wiggle(ts) * 0.5


def live_quote(ts, yes_ask=0.60, no_ask=0.42, yes_bid=None, no_bid=None):
    return {"ticker": TICKER, "ts": ts, "yes_ask": yes_ask, "no_ask": no_ask,
            "yes_bid": yes_bid if yes_bid is not None else yes_ask - 0.01,
            "no_bid": no_bid if no_bid is not None else no_ask - 0.01, "last": None, "volume": None,
            "strike": 80_000.0, "open_ts": T, "close_ts": END}


def tick(coach, *args):
    asyncio.run(coach.tick(*args))


def snap(age=1.0):
    return lambda at: ({"last_trade_age": age}, {})


LEANS = {60: {"call": "UP", "side": "UP", "up_met": 3, "down_met": 1, "total": 4},
         300: {"call": "DOWN", "side": "DOWN", "up_met": 1, "down_met": 3, "total": 4},
         900: {"call": "UP", "side": "UP", "strength": 0.75, "up_met": 3, "down_met": 1, "total": 4}}


@pytest.fixture
def coach(store):
    return CoachService(store, store, (60, 300, 900))


def test_warming_before_any_data(coach):
    assert coach.evaluation is None
    tick(coach, AT, snap(), {}, None)
    ev = coach.evaluation
    assert ev["code"] == "warming" and "starting price" in ev["reason"]
    assert ev["fair_up"] is None and ev["model"] is None and ev["market"] is None
    assert (ev["boundary"], ev["window_end"]) == (T, END)


def test_evaluation_has_fair_odds_model_quote_and_leans(store, coach):
    seed_index(store, T - 1_920_000, AT, rising)
    tick(coach, AT + 200, snap(), LEANS, live_quote(AT + 100))
    ev = coach.evaluation
    assert ev["code"] is None and ev["fair_up"] > 0.8
    m = ev["model"]
    assert m["open_avg"] == pytest.approx(80_000.0, abs=1.0) and m["kalshi_strike"] == 80_000.0
    assert m["window_end"] == END and m["at"] == AT and m["sd_close"] > 0 and m["sigma_per_s"] > 0
    assert ev["market"]["ticker"] == TICKER and ev["market"]["yes_ask"] == 0.60
    assert ev["engine"]["lean"]["call"] == "UP" and ev["engine"]["lean"]["strength"] == 0.75
    assert [(p["label"], p["lean_call"]) for p in ev["pressure"]] == [("1m", "UP"), ("5m", "DOWN")]


def test_quote_must_be_this_window_and_fresh(store, coach):
    seed_index(store, T - 1_920_000, AT, rising)
    tick(coach, AT + 200, snap(), {}, live_quote(AT - 5_000))  # older than 2 s
    assert coach.evaluation["market"] is None
    tick(coach, AT + 200, snap(), {}, {**live_quote(AT + 100), "open_ts": T - 900_000})  # another window
    assert coach.evaluation["market"] is None and coach.evaluation["model"]["kalshi_strike"] is None


def test_stale_price_data_is_said(store, coach):
    seed_index(store, T - 1_920_000, AT, rising)
    tick(coach, AT + 200, snap(age=60), {}, live_quote(AT + 100))
    assert coach.evaluation["code"] == "stale" and "60 seconds" in coach.evaluation["reason"]


def test_a_slow_feature_snapshot_runs_off_the_loop(store, coach):
    """The feature snapshot (the indicator library) runs in a worker thread: another coroutine keeps running while
    it computes, and the evaluation still uses its result."""
    seed_index(store, T - 1_920_000, AT, rising)
    snap_threads: list[int] = []

    def slow_snap(at):
        snap_threads.append(threading.get_ident())
        time.sleep(0.4)
        return {"last_trade_age": 60}, {}

    async def main():
        beats = 0

        async def heartbeat():
            nonlocal beats
            while True:
                beats += 1
                await asyncio.sleep(0.01)

        hb = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)
        start = beats
        await coach.tick(AT + 200, slow_snap, {}, live_quote(AT + 100))
        hb.cancel()
        return beats - start

    assert asyncio.run(main()) >= 10  # ~40 beats in 0.4 s if the loop is free; 0 if the snapshot blocks it
    assert snap_threads and snap_threads[0] != threading.get_ident()
    assert coach.evaluation["code"] == "stale" and "60 seconds" in coach.evaluation["reason"]


def test_pause_is_persisted(store, coach):
    assert coach.paused is False and coach.pause_state()["paused"] is False
    st = coach.set_paused(True, AT)
    assert st == {"paused": True, "since": AT, "sentence": st["sentence"]} and "refused" in st["sentence"]
    again = CoachService(store, store, (60, 300, 900))
    assert again.paused is True and again.pause_state()["since"] == AT
    assert again.set_paused(False, AT + 1)["paused"] is False
    assert CoachService(store, store, (60, 300, 900)).paused is False


def test_series_points_from_recorded_rows_only(store, coach):
    seed_index(store, T - 1_920_000, AT, rising)
    store.insert("kalshi_quotes", [{"ticker": TICKER, "ts": T + 10_000, "yes_bid": 0.55, "yes_ask": 0.57,
                                    "no_bid": 0.42, "no_ask": 0.44, "last": None, "volume": None,
                                    "source": "ws"}])
    s = coach.series(T, AT)
    assert s["boundary"] == T and s["ticker"] == TICKER and s["step_ms"] == 5_000
    assert len(s["points"]) == 60 and s["points"][0]["yes_bid"] is None
    assert s["points"][2] == {**s["points"][2], "ts": T + 10_000, "yes_bid": 0.55, "yes_ask": 0.57}
    assert s["points"][2 + 3]["yes_bid"] is None  # quotes older than 10 s are not carried forward
    assert s["coverage"]["quotes"] == 1 and s["coverage"]["with_odds"] > 0


def test_the_live_series_keeps_its_final_points_and_rebuilds_them_every_minute(store, coach):
    """B5#6: points SERIES_STABLE_MS old are kept and only the newest computed; a row that lands late changes a kept
    point at the next rebuild (SERIES_REBUILD_MS), never silently forever."""
    from btc_signal_engine.application import coach as coach_mod
    seed_index(store, T - 1_920_000, AT, rising)
    first = coach.series(T, AT)
    kept = coach._live_points[T][2]
    assert kept and all(p["ts"] <= AT - coach_mod.SERIES_STABLE_MS for p in kept)
    seed_index(store, AT, AT + 5_000, rising)
    later = coach.series(T, AT + 5_000)
    assert later["points"][:len(first["points"])] == first["points"]
    assert later["points"] == CoachService(store, store, (60, 300, 900)).series(T, AT + 5_000)["points"]
    # an old second rewritten (a late row): kept points stand until the rebuild, then match the store again
    store.add_index_seconds([IndexSecond(T + 100_000, 81_000.0, T + 100_000, None)])
    fresh = lambda now: CoachService(store, store, (60, 300, 900)).series(T, now)["points"]  # noqa: E731
    rebuilt = coach.series(T, AT + coach_mod.SERIES_REBUILD_MS)
    assert rebuilt["points"] == fresh(AT + coach_mod.SERIES_REBUILD_MS)
