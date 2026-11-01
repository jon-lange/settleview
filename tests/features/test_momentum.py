import math

import pytest

from btc_signal_engine.domain.features.state import FeatureState
from btc_signal_engine.domain.models.events import Trade

T0 = 1_699_999_200_000


def tr(i, ts, price, size=0.1, buy=True):
    return Trade(trade_id=i, ts=ts, ingest_ts=ts, price=price, size=size, buy=buy)


def rising_path():
    """Price 100 before the window, +$10 linearly over 30s, then +$20 over the next 30s."""
    trades = [tr(0, T0 - 5_000, 100.0)]
    for s in range(60):
        price = 100.0 + 10.0 * (s + 1) / 30 if s < 30 else 110.0 + 20.0 * (s - 29) / 30
        trades.append(tr(s + 1, T0 + s * 1_000 + 500, price))
    return trades


def test_rising_path_has_positive_momentum_and_acceleration():
    s = FeatureState()
    for t in rising_path():
        s.on_trade(t)
    f, _ = s.snapshot(T0 + 60_000)
    assert f["momentum_60"] == pytest.approx(math.log(130.0 / 100.0))
    assert f["momentum_30"] == pytest.approx(math.log(130.0 / 110.0))
    assert f["accel_30"] == pytest.approx(math.log(130.0 / 110.0) - math.log(110.0 / 100.0))
    assert f["accel_30"] > 0
    assert f["rv_60"] > 0


def test_known_component_is_last_price_minus_mean_of_last_60_seconds():
    s = FeatureState()
    trades = rising_path()
    for t in trades:
        s.on_trade(t)
    f, avail = s.snapshot(T0 + 60_000)
    per_second = [t.price for t in trades[1:]]  # one trade per second in [T0, T0+60s)
    expected_open = round(sum(per_second) / 60, 2)
    assert f["last_price"] == 130.0
    assert f["open_avg"] == pytest.approx(expected_open)
    assert f["known_component"] == pytest.approx(130.0 - expected_open)
    assert "known_component" not in avail


def test_open_avg_carries_price_through_quiet_seconds():
    s = FeatureState()
    s.on_trade(tr(1, T0 - 1_000, 100.0))
    s.on_trade(tr(2, T0 + 30_000, 106.0))
    f, _ = s.snapshot(T0 + 60_000)
    assert f["open_avg"] == pytest.approx((30 * 100.0 + 30 * 106.0) / 60)
    assert f["last_trade_age"] == pytest.approx(30.0)
