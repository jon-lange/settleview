import pytest

from btc_signal_engine.domain.features import FEATURES, WINDOWS
from btc_signal_engine.domain.features.state import FeatureState
from btc_signal_engine.domain.models.events import BookSecond, Trade

T0 = 1_699_999_200_000  # aligned to a 15-minute boundary


def tr(i, ts, price=100.0, size=0.1, buy=True, backfilled=False):
    return Trade(trade_id=i, ts=ts, ingest_ts=ts, price=price, size=size, buy=buy, backfilled=backfilled)


def feed(state, trades):
    for t in trades:
        state.on_trade(t)
    return state


def test_imbalance_60_from_40_buy_10_sell():
    trades = [tr(i, T0 + i * 1_000, size=1.0, buy=True) for i in range(40)]
    trades += [tr(100 + i, T0 + (40 + i) * 1_000, size=1.0, buy=False) for i in range(10)]
    f, avail = feed(FeatureState(), trades).snapshot(T0 + 60_000)
    assert f["imbalance_60"] == pytest.approx(0.6)
    assert f["buy_vol_60"] == pytest.approx(40.0)
    assert f["sell_vol_60"] == pytest.approx(10.0)
    assert f["trades_60"] == 50
    assert "imbalance_60" not in avail


def test_imbalance_weights_by_volume_not_count():
    trades = [tr(i, T0 + i * 1_000, size=8.0, buy=True) for i in range(5)]
    trades += [tr(100 + i, T0 + (5 + i) * 1_000, size=0.2, buy=False) for i in range(50)]
    f, _ = feed(FeatureState(), trades).snapshot(T0 + 60_000)
    assert f["imbalance_60"] == pytest.approx(0.6)
    assert f["trades_60"] == 55


def test_short_window_sees_only_its_own_seconds():
    trades = [tr(1, T0, size=5.0, buy=False), tr(2, T0 + 55_000, size=1.0, buy=True)]
    f, _ = feed(FeatureState(), trades).snapshot(T0 + 60_000)
    assert f["imbalance_5"] == pytest.approx(1.0)
    assert f["imbalance_60"] == pytest.approx((1 - 5) / 6)


def test_vol_accel_is_last_window_minus_prior_window():
    trades = [tr(1, T0, size=2.0), tr(2, T0 + 31_000, size=5.0, buy=False)]
    f, _ = feed(FeatureState(), trades).snapshot(T0 + 60_000)
    assert f["vol_accel_30"] == pytest.approx(3.0)


def test_long_windows_are_warmup_after_120s():
    trades = [tr(i, T0 + i * 1_000, price=100.0 + i) for i in range(120)]
    f, avail = feed(FeatureState(), trades).snapshot(T0 + 120_000)
    for w in (180, 300, 900):
        for name in (f"imbalance_{w}", f"momentum_{w}", f"rv_{w}", f"trades_{w}"):
            assert f[name] is None
            assert avail[name] == "warmup"
    assert f["imbalance_60"] is not None and f["momentum_60"] is not None
    assert "momentum_60" not in avail


def test_book_features_unavailable_without_book_seconds():
    trades = [tr(i, T0 + i * 1_000, backfilled=True) for i in range(60)]
    f, avail = feed(FeatureState(), trades).snapshot(T0 + 60_000)
    book = [n for n, spec in FEATURES.items() if not spec.backfillable]
    assert set(book) == {"spread", "book_imbalance_10", "depth_bid_10", "depth_ask_10", "microprice_bps",
                         "ofi_5", "ofi_15", "ofi_30", "ofi_60"}
    for name in book:
        assert f[name] is None
        assert avail[name] == "book_unavailable"


def test_book_features_from_latest_book_second():
    s = feed(FeatureState(), [tr(i, T0 + i * 1_000) for i in range(60)])
    s.on_book(BookSecond(ts=T0 + 58_000, ingest_ts=T0 + 59_000, bid=99.0, ask=101.0,
                         bid_depth_10=3.0, ask_depth_10=1.0, bid_depth_25=6.0, ask_depth_25=2.0))
    s.on_book(BookSecond(ts=T0 + 59_000, ingest_ts=T0 + 60_000, bid=99.5, ask=100.5,
                         bid_depth_10=1.0, ask_depth_10=3.0, bid_depth_25=6.0, ask_depth_25=2.0))
    f, avail = s.snapshot(T0 + 60_000)
    assert f["spread"] == pytest.approx(1.0)
    assert f["book_imbalance_10"] == pytest.approx(-0.5)
    assert (f["depth_bid_10"], f["depth_ask_10"]) == (1.0, 3.0)
    assert "spread" not in avail


def test_every_registered_feature_is_in_the_snapshot():
    f, avail = feed(FeatureState(), [tr(1, T0)]).snapshot(T0 + 1_000)
    assert set(f) == set(FEATURES)
    assert set(avail) <= set(FEATURES)
    assert all(f[n] is None for n in avail)
    assert WINDOWS == (5, 15, 30, 60, 180, 300, 900)
