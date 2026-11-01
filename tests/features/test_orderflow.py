"""Order-flow imbalance, microprice, trade intensity and kc_z on SYNTHETIC sequences (test data only)."""

import math

import pytest

from btc_signal_engine.domain.features import FEATURES
from btc_signal_engine.domain.features.state import FeatureState
from btc_signal_engine.domain.models.events import BookSecond, Trade

T0 = 1_699_999_200_000
T = T0 + 120_000  # snapshot boundary


def tr(i, ts, price=100.0, size=0.1, buy=True):
    return Trade(trade_id=i, ts=ts, ingest_ts=ts, price=price, size=size, buy=buy)


def book(sec_before_t, bid, bid_size, ask, ask_size):
    ts = T - sec_before_t * 1_000
    return BookSecond(ts=ts, ingest_ts=ts + 1_000, bid=bid, ask=ask, bid_depth_10=1.0, ask_depth_10=1.0,
                      bid_depth_25=1.0, ask_depth_25=1.0, bid_size=bid_size, ask_size=ask_size)


def state_with(books, trades=()):
    s = FeatureState()
    s.on_trade(tr(0, T0))  # a price long before T
    for t in trades:
        s.on_trade(t)
    for b in books:
        s.on_book(b)
    return s


def test_registry_documents_new_features_with_book_ones_not_backfillable():
    for w in (5, 15, 30, 60):
        assert not FEATURES[f"ofi_{w}"].backfillable
    assert not FEATURES["microprice_bps"].backfillable
    assert FEATURES["intensity_ratio"].backfillable
    assert FEATURES["kc_z"].backfillable


def test_ofi_positive_when_bid_size_grows_at_same_price():
    # seconds T-6..T-1; best bid 100 grows from 1 to 3 BTC at T-5, ask unchanged (101 x 2)
    books = [book(6, 100.0, 1.0, 101.0, 2.0)] + [book(k, 100.0, 3.0, 101.0, 2.0) for k in range(5, 0, -1)]
    f, avail = state_with(books).snapshot(T)
    # e = +2 BTC once; average best-level depth over the 5 pairs = (3 + 2) / 2 = 2.5
    assert f["ofi_5"] == pytest.approx(2.0 / 2.5)
    assert "ofi_5" not in avail


def test_ofi_negative_when_ask_price_drops():
    books = [book(6, 100.0, 1.0, 101.0, 2.0)] + [book(k, 100.0, 1.0, 100.5, 1.0) for k in range(5, 0, -1)]
    f, _ = state_with(books).snapshot(T)
    # ask improves to 100.5 x 1: e = -1 (new ask queue) ; depth (1 + 1) / 2 = 1
    assert f["ofi_5"] == pytest.approx(-1.0)


def test_ofi_bid_drop_counts_the_lost_queue_and_is_scale_free():
    base = [book(6, 100.0, 4.0, 101.0, 4.0)] + [book(k, 99.5, 4.0, 101.0, 4.0) for k in range(5, 0, -1)]
    f1, _ = state_with(base).snapshot(T)
    big = [BookSecond(b.ts, b.ingest_ts, b.bid, b.ask, 1, 1, 1, 1, b.bid_size * 10, b.ask_size * 10) for b in base]
    f2, _ = state_with(big).snapshot(T)
    # bid falls: e = -prev bid queue (4); depth 4 -> -1.0, unchanged when every size is 10x
    assert f1["ofi_5"] == pytest.approx(-1.0)
    assert f2["ofi_5"] == pytest.approx(f1["ofi_5"])


def test_ofi_and_microprice_unavailable_without_sizes_or_enough_book_seconds():
    no_sizes = [BookSecond(T - k * 1_000, T - k * 1_000 + 1_000, 100.0, 101.0, 1, 1, 1, 1) for k in range(1, 7)]
    f, avail = state_with(no_sizes).snapshot(T)
    for name in ("ofi_5", "ofi_60", "microprice_bps"):
        assert f[name] is None and avail[name] == "book_unavailable"
    # only 2 book seconds: too few changes to cover even half of a 5s window
    f, avail = state_with([book(2, 100.0, 1.0, 101.0, 1.0), book(1, 100.0, 2.0, 101.0, 1.0)]).snapshot(T)
    assert f["ofi_5"] is None and avail["ofi_5"] == "book_unavailable"
    assert f["microprice_bps"] is not None


def test_microprice_lies_between_bid_and_ask_and_leans_to_the_thinner_side():
    f, _ = state_with([book(1, 100.0, 3.0, 101.0, 1.0)]).snapshot(T)
    # I = 3 / 4; micro = 0.75 * 101 + 0.25 * 100 = 100.75; mid 100.5
    assert f["microprice_bps"] == pytest.approx((100.75 - 100.5) / 100.5 * 1e4)
    half_spread_bps = 0.5 / 100.5 * 1e4
    assert 0 < f["microprice_bps"] < half_spread_bps  # above mid (ask is thinner), below the ask
    f, _ = state_with([book(1, 100.0, 1.0, 101.0, 3.0)]).snapshot(T)
    assert f["microprice_bps"] == pytest.approx(-(100.75 - 100.5) / 100.5 * 1e4)


def test_intensity_ratio_decays_with_a_ten_second_half_life():
    t_sec = T // 1_000
    # 2 buys 10s before 1 sell: each buy has half the sell's weight, so the intensities balance.
    trades = [tr(1, (t_sec - 11) * 1_000 + 100), tr(2, (t_sec - 11) * 1_000 + 200),
              tr(3, (t_sec - 1) * 1_000 + 100, buy=False)]
    f, avail = state_with([], trades).snapshot(T)
    assert f["intensity_ratio"] == pytest.approx(0.5, abs=1e-3)  # the T0 trade is ~12 half-lives old
    # 20s apart instead: the buys weigh a quarter each -> 0.5 / 1.5
    trades = [tr(1, (t_sec - 21) * 1_000), tr(2, (t_sec - 21) * 1_000 + 1), tr(3, (t_sec - 1) * 1_000, buy=False)]
    f, _ = state_with([], trades).snapshot(T)
    assert f["intensity_ratio"] == pytest.approx(1 / 3, abs=1e-3)
    assert "intensity_ratio" not in avail


def test_intensity_ratio_is_warmup_at_start():
    s = FeatureState()
    s.on_trade(tr(1, T - 5_000))
    f, avail = s.snapshot(T)
    assert f["intensity_ratio"] is None and avail["intensity_ratio"] == "warmup"


def _jump_state(after):
    s = FeatureState()
    for k in range(120):  # one trade per second; price jumps at T-30s
        s.on_trade(tr(k, T0 + k * 1_000, price=100.0 if k < 90 else after))
    return s


def test_kc_z_is_known_component_in_units_of_one_minutes_move():
    f, _ = _jump_state(100.6).snapshot(T)
    # 30s at 100 and 30s at 100.6 in the last minute: open_avg 100.3, kc 0.30; one move = 100.6 * ln(1.006)
    assert f["known_component"] == pytest.approx(0.3)
    assert f["kc_z"] == pytest.approx(0.3 / (100.6 * math.log(1.006)))
    assert f["kc_z"] == pytest.approx(f["known_component"] / (f["last_price"] * f["rv_60"]))
    f, _ = _jump_state(99.4).snapshot(T)
    assert f["kc_z"] < 0 and f["kc_z"] == pytest.approx(-0.3 / (99.4 * abs(math.log(0.994))))


def test_kc_z_is_zero_on_a_flat_minute():
    f, _ = _jump_state(100.0).snapshot(T)
    assert f["rv_60"] == 0 and f["kc_z"] == 0.0
