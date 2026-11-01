"""Perp maths (pure): sizing, maximum leverage, protective price, worst case, liquidation estimate, add, funding, P&L,
distance-to-liquidation bands. Expected numbers are worked by hand in each test."""

from __future__ import annotations

from decimal import Decimal

import pytest

from btc_signal_engine.domain import perps as X

# Kalshi-shaped estimates (keys are notional dollars); values like the 2026-10-30 reads.
LONG_EST = {"1000": 6.4, "10000": 6.0, "100000": 5.0}
SHORT_EST = {"1000": 6.0, "10000": 5.5, "100000": 4.5}
IMM = 1.3  # initial_margin_multiplier for BTC


# --- maximum leverage --------------------------------------------------------------------------------------------

def test_max_leverage_is_the_estimate_at_smallest_key_at_or_above_notional():
    # Kalshi's estimate is 1 / initial margin rate (calibrated 2026-10-30, LEARNINGS): it is the opening maximum
    assert X.max_leverage(LONG_EST, 100.0) == 6.4
    assert X.max_leverage(LONG_EST, 1000.0) == 6.4  # on the key: that key
    assert X.max_leverage(LONG_EST, 1000.01) == 6.0
    assert X.max_leverage(SHORT_EST, 50_000.0) == 4.5


def test_max_leverage_refuses_above_largest_key_and_missing_estimates():
    with pytest.raises(X.PerpRefused, match="larger than"):
        X.max_leverage(LONG_EST, 100_001.0)
    with pytest.raises(X.PerpRefused, match="no leverage estimates"):
        X.max_leverage({}, 100.0)
    with pytest.raises(X.PerpRefused, match="no leverage estimates"):
        X.max_leverage(None, 100.0)


# --- sizing ------------------------------------------------------------------------------------------------------

def test_sizing_20_dollars_at_5x_mark_8_whole_contracts():
    # $20 x 5 = $100 notional / $8.0000 = 12.5 contracts -> 12 at count step 1.0; notional 12 x 8 = $96.
    s = X.size_position(20.0, 5.0, 8.0, 1.0, LONG_EST)
    assert s.contracts == 12.0
    assert s.notional == pytest.approx(96.0)
    assert s.max_leverage == 6.4


def test_sizing_at_fractional_count_step():
    s = X.size_position(20.0, 5.0, 8.0, 0.01, LONG_EST)
    assert s.contracts == 12.5
    assert s.notional == pytest.approx(100.0)


def test_sizing_refuses_leverage_above_max_with_reason():
    # 7x on $20 is $140 notional, under the $1,000 key whose estimate is 6.4x
    with pytest.raises(X.PerpRefused, match=r"7\.00x is above Kalshi's maximum of 6\.40x"):
        X.size_position(20.0, 7.0, 8.0, 1.0, LONG_EST)


def test_sizing_refuses_zero_contracts_and_non_positive_inputs():
    with pytest.raises(X.PerpRefused, match="zero contracts"):
        X.size_position(1.0, 2.0, 8.0, 1.0, LONG_EST)  # $2 notional < one $8 contract
    for bad in [(0.0, 2.0, 8.0, 1.0), (5.0, 0.0, 8.0, 1.0), (5.0, 2.0, -8.0, 1.0), (5.0, 2.0, 8.0, 0.0)]:
        with pytest.raises(X.PerpRefused, match="must be positive"):
            X.size_position(*bad, LONG_EST)


# --- protective price --------------------------------------------------------------------------------------------

def test_long_protective_price_rounds_down_short_rounds_up_onto_tick():
    # ask 7.80347 x 1.005 = 7.84248735 -> down to 7.8424; bid 7.80347 x 0.995 = 7.76445265 -> up to 7.7645
    long_cap = X.protective_price("long", 7.80347, 0.005, 0.0001)
    short_floor = X.protective_price("short", 7.80347, 0.005, 0.0001)
    assert long_cap == Decimal("7.8424")
    assert short_floor == Decimal("7.7645")
    assert X.price_str(long_cap, 0.0001) == "7.8424"
    assert X.price_str(Decimal("8"), 0.0001) == "8.0000"


def test_protective_price_on_tick_stays_put_and_unknown_side_raises():
    # 8.0000 x 1.005 = 8.04 exactly: already on the tick
    assert X.protective_price("long", 8.0, 0.005, 0.0001) == Decimal("8.04")
    with pytest.raises(X.PerpRefused, match="side"):
        X.protective_price("buy", 8.0, 0.005, 0.0001)


def test_price_str_refuses_off_tick_price():
    with pytest.raises(X.PerpRefused, match="not on the"):
        X.price_str(Decimal("7.84245"), 0.0001)


# --- fees and worst case -----------------------------------------------------------------------------------------

def test_fee_is_taker_rate_times_notional():
    assert X.fee_estimate(24.15, 0.0012) == pytest.approx(0.02898)


def test_worst_case_5_dollars_3_contracts_at_8_05():
    w = X.worst_case(5.0, 3.0, Decimal("8.0500"), 0.0012)
    assert w.notional == pytest.approx(24.15)
    assert w.entry_fee == pytest.approx(0.02898) and w.exit_fee == pytest.approx(0.02898)
    assert w.total == pytest.approx(5.0 + 2 * (24.15 * 0.0012))  # 5.05796


# --- liquidation estimate ----------------------------------------------------------------------------------------

def test_liquidation_long_and_short_hand_computed():
    # C=$2, q=1, E=$8.00, estimate 6.4 at the $1,000 key, IMM 1.3 -> mmr 1 / (6.4 x 1.3) = 1 / 8.32, theta 1.0
    # long:  (1 x 8 - 2) / (1 - 1/8.32)    short: (2 + 1 x 8) / (1 + 1/8.32)
    est, mmr = {"1000": 6.4}, 1 / 8.32
    assert X.liquidation_estimate("long", 2.0, 1.0, 8.0, est, IMM, 1.0, fees_paid=0.0) == pytest.approx(6.0 / (1 - mmr))
    assert X.liquidation_estimate("short", 2.0, 1.0, 8.0, est, IMM, 1.0, fees_paid=0.0) == pytest.approx(
        10.0 / (1 + mmr))


def test_liquidation_subtracts_fees_paid_and_uses_theta():
    # C=2.5 less 0.5 fees = 2; theta 0.97
    est, mmr = {"1000": 6.4}, 1 / 8.32
    want = (8.0 - 2.0) / (1 - mmr / 0.97)
    assert X.liquidation_estimate("long", 2.5, 1.0, 8.0, est, IMM, 0.97, fees_paid=0.5) == pytest.approx(want)


def test_liquidation_none_when_no_positive_price():
    # long fully collateralised: C >= qE -> numerator <= 0, no liquidation price
    assert X.liquidation_estimate("long", 10.0, 1.0, 8.0, {"1000": 6.4}, IMM, 1.0, fees_paid=0.0) is None


def test_liquidation_estimate_on_the_calibrated_production_parameters():
    """Kalshi's production BTC parameters from the 2026-10-30 real-money check (LEARNINGS: long estimate 6.8473 at the
    $1,000 key, IMM 1.3, theta 1.0, fee $0.0094 on 1 contract at 7.7970), on a made-up $6.00 of equity. There the
    formula matched Kalshi's /margin/risk liquidation price within 0.01%. By hand: mmr = 1 / (6.8473 x 1.3)
    = 0.1123407; P = (7.7970 - (6.00 - 0.0094)) / (1 - 0.1123407) = 1.8064 / 0.8876593 = 2.03502. Plan bar: the
    entry-to-liquidation distance within 5% of the reference."""
    est = {"1000": 6.8473, "10000": 6.8413, "100000": 6.8049, "1000000": 6.6519}
    ours = X.liquidation_estimate("long", 6.00, 1.0, 7.7970, est, 1.3, 1.0, fees_paid=0.0094)
    reference = 2.03502
    assert abs((7.7970 - ours) - (7.7970 - reference)) / (7.7970 - reference) < 0.05
    assert ours == pytest.approx(reference, abs=0.0001)
    # the maintenance rate comes from the leverage estimate (1 / INITIAL margin rate), not from the estimate alone
    naive = (7.7970 - (6.00 - 0.0094)) / (1 - 1 / 6.8473)
    assert abs(ours - naive) > 0.05


# --- add ---------------------------------------------------------------------------------------------------------

def test_add_gives_new_average_size_and_liquidation():
    # 2 @ 8.00 + 1 @ 8.30 -> avg 24.30 / 3 = 8.10, size 3; C total $5; mmr 1/8.32
    # long liq = (3 x 8.10 - 5) / (3 x (1 - 1/8.32))
    a = X.add_to_position("long", 2.0, 8.0, 1.0, 8.3, 5.0, {"1000": 6.4}, IMM, 1.0, fees_paid=0.0)
    assert a.average_entry == pytest.approx(8.1)
    assert a.contracts == 3.0
    assert a.liquidation == pytest.approx(19.3 / (3 * (1 - 1 / 8.32)))


# --- funding -----------------------------------------------------------------------------------------------------

def test_funding_signs():
    # 0.0001 x 10 x 8.00 = $0.008 per interval
    assert X.funding_estimate("long", 0.0001, 10.0, 8.0) == pytest.approx(-0.008)  # a long pays
    assert X.funding_estimate("short", 0.0001, 10.0, 8.0) == pytest.approx(0.008)  # a short receives
    assert X.funding_estimate("long", 0.0, 10.0, 8.0) == 0.0
    assert X.funding_estimate("long", -0.0001, 10.0, 8.0) == pytest.approx(0.008)  # negative rate: long receives


def test_funding_cap_worst_case_percentage_and_unknown_unit_refused():
    # cap 0.02 (2%) x 10 x 8.00 = $1.60 at most per interval
    assert X.funding_cap_worst_case(0.02, "percentage", 10.0, 8.0) == pytest.approx(-1.6)
    with pytest.raises(X.PerpRefused, match="funding_limit_unit"):
        X.funding_cap_worst_case(0.02, "bps", 10.0, 8.0)


# --- P&L and distance --------------------------------------------------------------------------------------------

def test_pnl_total():
    # realized 1.00 + unrealized -0.40 + funding -0.05 - fees 0.06 = 0.49
    assert X.pnl_total(1.0, -0.4, -0.05, 0.06) == pytest.approx(0.49)


def test_liquidation_distance_and_band():
    assert X.liquidation_distance("long", 8.0, 7.6) == pytest.approx(0.05)
    assert X.liquidation_distance("short", 8.0, 8.12) == pytest.approx(0.015)
    assert X.liquidation_distance("long", 8.0, 8.1) == pytest.approx(-0.0125)  # past it
    assert X.liquidation_band(0.10, amber=0.05, red=0.02) == "ok"
    assert X.liquidation_band(0.04, amber=0.05, red=0.02) == "amber"
    assert X.liquidation_band(0.015, amber=0.05, red=0.02) == "red"
