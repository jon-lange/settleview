import numpy as np
import pytest

from btc_signal_engine.domain.features.candles import Candles, aggregate
from btc_signal_engine.domain.features.state import FeatureState
from btc_signal_engine.domain.models.events import Trade

T0 = 1_699_999_200_000  # aligned to a 15-minute boundary
MIN = 60_000


class Feeder:
    def __init__(self):
        self.state = FeatureState()
        self.next_id = 0

    def trade(self, ts, price, size=0.1, buy=True):
        self.next_id += 1
        self.state.on_trade(Trade(trade_id=self.next_id, ts=ts, ingest_ts=ts, price=price, size=size, buy=buy))

    def candle(self, minute, o, h, l, c, vol=1.0):
        start = T0 + minute * MIN
        for k, (off, p) in enumerate(((1_000, o), (10_000, h), (20_000, l), (50_000, c))):
            self.trade(start + off, p, size=vol / 4, buy=k % 2 == 0)


def test_bullish_engulfing_only_after_second_candle_closes():
    f = Feeder()
    for m in range(3):
        f.candle(m, 100, 100, 100, 100)
    f.candle(3, 100, 100.5, 94.5, 95)  # red
    f.candle(4, 94, 101.5, 93.5, 101)  # green, engulfs the red body
    before, _ = f.state.snapshot(T0 + 4 * MIN + 59_000)
    after, avail = f.state.snapshot(T0 + 5 * MIN)
    assert before["bull_engulf"] == 0
    assert after["bull_engulf"] == 1
    assert after["bear_engulf"] == 0
    assert after["last_close_1m"] == 101
    assert "bull_engulf" not in avail


def test_bearish_engulfing():
    f = Feeder()
    for m in range(3):
        f.candle(m, 100, 100, 100, 100)
    f.candle(3, 95, 100.5, 94.5, 100)  # green
    f.candle(4, 101, 101.5, 93.5, 94)  # red, engulfs
    snap, _ = f.state.snapshot(T0 + 5 * MIN)
    assert (snap["bear_engulf"], snap["bull_engulf"]) == (1, 0)


def test_hammer_after_decline():
    f = Feeder()
    for m, c in enumerate((110, 105, 100)):
        f.candle(m, c, c, c, c)
    f.candle(3, 100, 100.6, 98, 100.5)  # body 0.5, lower wick 2.0, upper 0.1
    snap, _ = f.state.snapshot(T0 + 4 * MIN)
    assert (snap["hammer"], snap["shooting_star"]) == (1, 0)
    assert snap["body_ratio"] == pytest.approx(0.5 / 2.6)
    assert snap["range_pct"] == pytest.approx(2.6 / 100.5 * 100)


def test_shooting_star_after_rise():
    f = Feeder()
    for m, c in enumerate((90, 95, 100)):
        f.candle(m, c, c, c, c)
    f.candle(3, 100, 102, 99.4, 99.5)  # body 0.5, upper wick 2.0, lower 0.1
    snap, _ = f.state.snapshot(T0 + 4 * MIN)
    assert (snap["shooting_star"], snap["hammer"]) == (1, 0)


def test_patterns_warmup_with_too_few_candles():
    f = Feeder()
    f.candle(0, 100, 101, 99, 100)
    snap, avail = f.state.snapshot(T0 + MIN)
    assert snap["hammer"] is None and avail["hammer"] == "warmup"
    assert snap["body_ratio"] == 0.0  # one candle is enough for structure


def test_aggregate_builds_aligned_complete_candles():
    t = T0 - 2 * MIN + MIN * np.arange(12, dtype=np.int64)  # starts 2 minutes before a 5m boundary
    one = Candles(t=t, o=np.arange(12.0), h=np.arange(12.0) + 1, l=np.arange(12.0) - 1,
                  c=np.arange(12.0) + 0.5, v=np.ones(12))
    five = aggregate(one, 5)
    assert list(five.t) == [T0, T0 + 5 * MIN]
    assert list(five.o) == [2.0, 7.0]
    assert list(five.c) == [6.5, 11.5]
    assert list(five.h) == [7.0, 12.0]
    assert list(five.l) == [1.0, 6.0]
    assert list(five.v) == [5.0, 5.0]


@pytest.mark.parametrize("step,sign", [(0.05, 1), (-0.05, -1)])
def test_ema_alignment_and_trend_on_steady_drift(step, sign):
    f = Feeder()
    minutes = 14 * 60
    for m in range(minutes):
        f.trade(T0 + m * MIN + 30_000, 1000 + step * m)
    snap, avail = f.state.snapshot(T0 + minutes * MIN)
    for tf in ("1m", "5m", "15m"):
        assert snap[f"ema_align_{tf}"] == sign
        assert snap[f"trend_{tf}"] == sign
    assert snap["trend_agree"] == 3 * sign
    assert (snap["ema_votes_up"], snap["ema_votes_down"]) == ((3, 0) if sign > 0 else (0, 3))
    assert not any(k.startswith(("ema_", "trend_")) for k in avail)


def test_ema_15m_is_warmup_with_short_history():
    f = Feeder()
    for m in range(60):
        f.trade(T0 + m * MIN + 30_000, 1000 + m)
    snap, avail = f.state.snapshot(T0 + 60 * MIN)
    assert snap["ema_align_1m"] == 1
    assert snap["ema_align_15m"] is None and avail["ema_align_15m"] == "warmup"
    assert avail["trend_agree"] == "warmup"


def test_consolidation_range_excludes_the_last_candle():
    f = Feeder()
    for m in range(20):
        f.candle(m, 100.0, 100.05, 99.95, 100.0, vol=2.0)
    f.candle(20, 100.0, 100.3, 100.0, 100.2, vol=3.2)  # breakout candle
    snap, avail = f.state.snapshot(T0 + 21 * MIN)
    assert snap["range_high_20"] == 100.05
    assert snap["range_low_20"] == 99.95
    assert snap["range_width_pct_20"] == pytest.approx(0.1 / 99.95 * 100)
    assert snap["range_avg_vol_20"] == pytest.approx(2.0)
    assert snap["last_close_1m"] == 100.2
    assert snap["last_volume_1m"] == pytest.approx(3.2)
    assert snap["range_high_10"] == 100.05
    assert avail["range_high_45"] == "warmup"


def test_quiet_minutes_become_flat_candles():
    f = Feeder()
    f.candle(0, 100, 101, 99, 100.5)
    f.trade(T0 + 3 * MIN + 1_000, 102.0)
    snap, _ = f.state.snapshot(T0 + 3 * MIN)  # minutes 1 and 2 had no trades
    assert snap["last_close_1m"] == 100.5
    assert snap["last_volume_1m"] == 0.0
    assert snap["range_pct"] == 0.0
