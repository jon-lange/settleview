import statistics
import time

from btc_signal_engine.domain.features.state import FeatureState
from btc_signal_engine.domain.models.events import BookSecond, Trade

T0 = 1_699_999_200_000
AS_OF = T0 + 14 * 3_600_000 + 900_000
BUDGET_MS = 10.0  # U5 verification target


def full_state() -> FeatureState:
    """14h of 1m candles (a trade every 15s) plus 900s of dense trades (20/s) and per-second book."""
    s = FeatureState()
    i = 0
    for ts in range(T0, AS_OF - 900_000, 15_000):
        s.on_trade(Trade(trade_id=i, ts=ts, ingest_ts=ts, price=30_000 + (i % 97), size=0.1, buy=i % 2 == 0))
        i += 1
    for ts in range(AS_OF - 900_000, AS_OF, 50):
        s.on_trade(Trade(trade_id=i, ts=ts, ingest_ts=ts, price=30_000 + (i % 89), size=0.01, buy=i % 3 == 0))
        i += 1
    for sec in range(AS_OF - 900_000, AS_OF, 1_000):
        s.on_book(BookSecond(ts=sec, ingest_ts=sec + 1_000, bid=30_000, ask=30_001, bid_depth_10=1,
                             ask_depth_10=2, bid_depth_25=3, ask_depth_25=4, bid_size=0.5 + sec % 3, ask_size=0.7))
    return s


def test_snapshot_is_under_10ms_on_a_full_state():
    s = full_state()
    features, avail = s.snapshot(AS_OF)
    assert not avail, avail  # every feature computed, so the timing covers the full path
    timings = []
    for _ in range(30):
        start = time.perf_counter()
        s.snapshot(AS_OF)
        timings.append((time.perf_counter() - start) * 1_000)
    median = statistics.median(timings)
    print(f"snapshot median {median:.2f} ms, max {max(timings):.2f} ms")
    assert median < BUDGET_MS
