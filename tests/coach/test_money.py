"""Dollar sizing (pure domain; numbers here are synthetic examples)."""

import pytest

from btc_signal_engine.domain.coach.money import position_for_stake
from btc_signal_engine.domain.settlement.fees import kalshi_fee


@pytest.mark.parametrize("stake,price", [(5, 0.58), (5, 0.76), (100, 0.31), (25, 0.97), (1, 0.03), (10_000, 0.5)])
def test_position_for_stake_spends_at_most_the_stake_including_fee(stake, price):
    c = position_for_stake(stake, price)
    assert c == round(c, 2) and c > 0
    assert c * price + kalshi_fee(c, price) <= stake + 1e-9
    nxt = round(c + 0.01, 2)  # one more hundredth of a contract would not fit
    assert nxt * price + kalshi_fee(nxt, price) > stake


def test_position_for_stake_known_value_and_degenerate_inputs():
    # $5 at 58c: 5 / (0.58 + 0.07*0.58*0.42) = 8.374... -> 8.37 contracts
    assert position_for_stake(5, 0.58) == 8.37
    assert position_for_stake(0, 0.58) == 0.0
    assert position_for_stake(5, 0.0) == 0.0
    assert position_for_stake(5, 1.0) == 0.0


def test_exit_advice_min_hold_is_a_parameter_with_live_default():
    from btc_signal_engine.domain.coach.rules import MIN_HOLD_MS, Setup, exit_advice
    s = Setup("UP", 0.60, 0.9, 0.45, 10_000_000, 10_060_000, 0.1, 1_000_000)
    assert exit_advice(s, 0.96, 0.97, 1_005_000).action == "hold"  # default: inside the 30s hold
    assert exit_advice(s, 0.96, 0.97, 1_005_000, min_hold_ms=0).reason == "take_value"
    assert MIN_HOLD_MS == 30_000

