"""The shared formatters (domain/fmt.py), characterized against the copies they replace (main at a17bab7).

Unchanged: every non-negative amount, every price in cents, every side word. Named fixes: negative dollars read
"−$" (U+2212) where three copies wrote a hyphen ("-$0.40") or put the sign after the $ ("$-0.40"); a signed amount
under half a cent reads "+$0.00", never "-$0.00"; rule_scores' dollars gain thousands separators like the rest.
"""

from __future__ import annotations

import random

import pytest

from btc_signal_engine.domain import fmt

M = "−"


# -- the old copies, verbatim -----------------------------------------------------------------------------------------
def old_notifier_usd(x):
    return (M + "$" if x < 0 else "$") + f"{abs(x):,.2f}"


def old_notifier_signed(x):
    r = round(float(x), 2)
    return ("+" if r >= 0 else M) + f"${abs(r):,.2f}"


def old_notifier_money(x):
    r = round(abs(float(x)), 2)
    s = f"${r:,.0f}" if abs(r - round(r)) < 1e-9 else f"${r:,.2f}"
    return (M if x < -0.004 else "") + s


def old_cents_round(p):  # notifier / setups / agent_proposals / domain.coach.text
    return f"{round(p * 100):.0f}¢"


def old_cents_format(x):  # agent_tools
    return f"{x * 100:.0f}¢"


def old_hyphen_usd(x):  # agent_proposals._usd, rule_scores.usd (no separators there)
    return ("-$" if x < 0 else "$") + f"{abs(x):,.2f}"


def old_plain_money(x):  # trading._money, agent_fleet_view._money, play_limits._money
    return f"${x:,.2f}"


def old_trading_signed(x):
    return ("+" if x >= 0 else "-") + old_plain_money(abs(x))


def old_perp_signed4(x):
    return ("+" if x >= 0 else "-") + f"${abs(x):,.4f}"


def amounts() -> list[float]:
    rng = random.Random(7)
    fixed = [0.0, 0.004, 0.005, 0.0049, 0.01, 0.1, 0.125, 0.135, 0.5, 0.86, 1.0, 1.005, 12.345, 999.995, 1234.5,
             1_000_000.0]
    return fixed + [rng.randrange(0, 2_000_000) / 10_000 for _ in range(3000)]


AMOUNTS = amounts()


def test_non_negative_dollars_are_unchanged():
    for x in AMOUNTS:
        assert fmt.usd(x) == old_notifier_usd(x) == old_plain_money(x) == old_hyphen_usd(x)
        assert fmt.signed_usd(x) == old_notifier_signed(x) == old_trading_signed(x)
        assert fmt.usd_short(x) == old_notifier_money(x)
        assert fmt.usd(x, dp=4) == f"${x:,.4f}"
        assert fmt.signed_usd(x, dp=4) == old_perp_signed4(x)


def test_negative_dollars_use_the_minus_sign():
    for x in (-y for y in AMOUNTS if y > 0):
        assert fmt.usd(x) == old_notifier_usd(x) == M + old_plain_money(-x)
        assert fmt.usd(x) == old_hyphen_usd(x).replace("-", M)  # fix: hyphen -> minus sign
        assert fmt.signed_usd(x) == old_notifier_signed(x)
        assert fmt.usd_short(x) == old_notifier_money(x)
        if round(x, 2) < 0:
            assert fmt.signed_usd(x) == old_trading_signed(x).replace("-", M, 1)  # fix: hyphen -> minus sign
        else:
            assert fmt.signed_usd(x) == "+$0.00"  # fix: was "-$0.00"
    assert fmt.usd(-0.4) == M + "$0.40"  # was "$-0.40" in trading._money / the fleet's holding line
    assert fmt.signed_usd(-0.00004, dp=4) == "+$0.0000"
    assert fmt.signed_usd(-0.0123, dp=4) == M + "$0.0123"


def test_cents_are_unchanged():
    rng = random.Random(3)
    prices = [0.0, 0.005, 0.015, 0.125, 0.43, 0.435, 0.5, 0.995, 0.999, 1.0] + [rng.random() for _ in range(5000)]
    prices += [i / 10_000 for i in range(10_001)]
    for p in prices:
        assert fmt.cents(p) == old_cents_round(p) == old_cents_format(p)
    assert fmt.cents(None) == "—"
    assert fmt.cents(None, missing="?") == "?"


@pytest.mark.parametrize(("side", "word"), [("UP", "Up"), ("DOWN", "Down"), ("long", "long"), (None, "None")])
def test_side_word(side, word):
    assert fmt.side_word(side) == word
