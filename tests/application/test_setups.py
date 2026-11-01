"""Setup lights (application/setups.py): grey / amber / green per rule; green is exactly the agent runner's entry
decision on the same view; a retired rule has no light; the mirrored thresholds match the rule functions."""

from __future__ import annotations

import itertools

import pytest

from btc_signal_engine.application import setups
from btc_signal_engine.application.agent_rules import RULES, evaluate
from btc_signal_engine.application.setups import AMBERS, GREEN, GREY, THRESHOLDS, light, lights, ready

K15 = "kalshi_15m"
END = 1_793_207_700_000
STRIKE = 80_000.0
ACTIVE = {r: {"status": "active"} for r in RULES}


def view(left_s: float, gap: float, *, yes_ask: float | None = 0.80, no_ask: float | None = 0.22,
         fair_up: float | None = None, fresh: bool = True, cutoff_ms: int = 120_000) -> dict:
    now = int(END - left_s * 1000)
    btc = STRIKE + gap
    return {"available": True, "now": now,
            "window": {"start": END - 900_000, "end": END, "entry_open": left_s * 1000 > cutoff_ms,
                       "entry_cutoff_ms": cutoff_ms},
            "market": {"fresh": fresh, "ticker": "KXBTC15M-T", "yes_ask": yes_ask, "no_ask": no_ask},
            "btc": {"btc": btc, "strike": STRIKE, "gap": round(gap, 2)},
            "call": {"fair_up": fair_up, "action": "WAIT", "mid_up": 0.5},
            "stake": {"fee_rate": 0.07},
            "entry": {"UP": {"ask": yes_ask}, "DOWN": {"ask": no_ask}}}


# -- each rule: grey, amber, green ---------------------------------------------------------------------------------------
def test_cushion100_grey_amber_green():
    assert light("CUSHION100", K15, view(480, 30, yes_ask=0.78))["state"] == GREY  # under $50 past the strike
    assert light("CUSHION100", K15, view(200, 150, yes_ask=0.80))["state"] == GREY  # past its window
    a = light("CUSHION100", K15, view(490, 72, yes_ask=0.78))
    assert a["state"] == "amber" and a["line"] == "CUSHION100 is setting up: BTC needs $28 more."
    assert a["progress"] == pytest.approx(0.72) and a["side"] == "UP"
    assert a["details"][0] == "Needs BTC $100 past the strike, Up at 70¢–90¢, 5:00–10:00 left."
    assert a["details"][1] == "Have: $72, 78¢, 8:10 left."
    early = light("CUSHION100", K15, view(690, 120, yes_ask=0.80))
    assert early["state"] == "amber" and early["line"] == "CUSHION100 is setting up: its window opens in 1:30."
    pricey = light("CUSHION100", K15, view(400, 130, yes_ask=0.93))
    assert pricey["state"] == "amber" and "Up is 93¢, over its 90¢ cap" in pricey["line"]
    g = light("CUSHION100", K15, view(400, 118, yes_ask=0.86))
    assert g["state"] == GREEN and g["line"] == "CUSHION100 fits this window."
    assert g["match"]["side"] == "UP" and g["match"]["max_price"] == 0.88  # the 86c ask + 2c, under the 90c cap
    assert g["match"]["stop_index_below"] == STRIKE + 40 and g["match"]["stop_index_above"] is None
    down = light("CUSHION100", K15, view(400, -118, yes_ask=0.16, no_ask=0.86))
    assert down["state"] == GREEN and down["match"]["side"] == "DOWN" and down["match"]["stop_index_above"] == STRIKE - 40


def test_h4_grey_amber_green():
    need = 0.004 * STRIKE  # $320
    assert light("H4", K15, view(650, 100, yes_ask=0.80))["state"] == GREY  # under half the gap
    assert light("H4", K15, view(900, 400, yes_ask=0.90))["state"] == GREY  # too early
    a = light("H4", K15, view(650, 200, yes_ask=0.80))
    assert a["state"] == "amber" and a["line"] == "H4 is setting up: BTC needs $120 more."
    assert a["progress"] == pytest.approx(200 / need)
    g = light("H4", K15, view(650, 330, yes_ask=0.90))
    assert g["state"] == GREEN and g["match"]["stop_index_below"] == round(STRIKE + 165, 2)
    cap = light("H4", K15, view(650, 400, yes_ask=0.97))
    assert cap["state"] == "amber" and "over its 95¢ cap" in cap["line"]


def test_lateresid_grey_amber_green():
    assert light("LATERESID", K15, view(500, 0, yes_ask=0.60, no_ask=0.42, fair_up=0.70))["state"] == GREY
    assert light("LATERESID", K15, view(200, 0, yes_ask=0.60, no_ask=0.42, fair_up=None))["state"] == GREY
    g = light("LATERESID", K15, view(200, 10, yes_ask=0.60, no_ask=0.42, fair_up=0.70))
    assert g["state"] == GREEN and g["match"]["side"] == "UP" and g["match"]["stop"] is None
    a = light("LATERESID", K15, view(200, 10, yes_ask=0.60, no_ask=0.42, fair_up=0.66))
    assert a["state"] == "amber" and a["line"].startswith("LATERESID is setting up: its edge is 4.")
    assert 0.5 <= a["progress"] < 1


def test_hpanic_has_an_amber_definition_too():
    v = view(230, 120, yes_ask=0.60, no_ask=0.42, fair_up=0.55)  # $120 from the strike (0.15%)
    assert light("HPANIC", K15, v)["state"] == "amber"


# -- green is the runner's decision ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("rid", list(RULES))
def test_green_is_exactly_the_agent_rules_entry_decision(rid):
    seen = set()
    for market in RULES[rid].markets:
        cutoff = 60_000 if market == "polymarket_5m" else 120_000
        for left, gap, ask, fair, fresh in itertools.product(
                (100, 150, 179, 180, 200, 230, 240, 290, 300, 400, 600, 650, 720, 800),
                (-400, -330, -118, -60, -10, 0, 10, 60, 99.99, 100, 118, 320, 330),
                (0.30, 0.35, 0.60, 0.65, 0.70, 0.86, 0.90, 0.95, 0.97, None), (None, 0.40, 0.66, 0.75), (True, False)):
            v = view(left, gap, yes_ask=ask, no_ask=None if ask is None else round(1.02 - ask, 2), fair_up=fair,
                     fresh=fresh, cutoff_ms=cutoff)
            want = evaluate(rid, market, v)
            floor = RULES[rid].min_left_ms
            if want is not None and floor is not None and END - v["now"] < floor:
                want = None
            got = light(rid, market, v)
            assert (got["state"] == GREEN) == (want is not None), (rid, market, left, gap, ask, fair, fresh)
            if want is not None:
                assert got["match"]["side"] == want["side"] and got["match"]["ask"] == want["ask"]
                assert got["match"]["stop_index_below"] == want["stop_index_below"]
                assert got["match"]["stop_index_above"] == want["stop_index_above"]
            seen.add(got["state"])
    assert GREEN in seen and GREY in seen


def test_ready_applies_the_runners_time_floor():
    v = view(200, 10, yes_ask=0.60, no_ask=0.42, fair_up=0.70)
    assert ready("LATERESID", K15, v) is not None
    assert ready("LATERESID", K15, {**v, "now": END - 179_000}) is None  # under MIN_LEFT_MS


# -- which lights --------------------------------------------------------------------------------------------------------
def test_retired_rules_have_no_light_and_a_rule_needs_a_status():
    st = {**ACTIVE, "HPANIC": {"status": "retired"}}
    v = view(400, 118, yes_ask=0.86)
    assert [x["rule"] for x in lights(K15, v, st, {}, {})] == ["H4", "CUSHION100", "LATERESID"]
    assert [x["rule"] for x in lights(K15, v, ACTIVE, {}, {})] == ["H4", "CUSHION100", "HPANIC", "LATERESID"]
    assert [x["rule"] for x in lights("polymarket_5m", v, ACTIVE, {}, {})] == ["HPANIC"]
    with pytest.raises(ValueError, match="no status"):
        lights(K15, v, {"H4": {"status": "active"}}, {}, {})
    with pytest.raises(ValueError, match="unknown rule"):
        light("NOPE", K15, v)


def test_a_light_carries_the_rules_live_record():
    rec = {"trades": 2, "wins": 2, "break_even_pct": 87.0, "net_usd": 0.27}
    g = light("CUSHION100", K15, view(400, 118, yes_ask=0.86), rec, {"n": None})
    assert g["record"] == rec and g["record_text"] == "Live record 2 of 2, break-even 87%, net +$0.27: anecdotal, " \
                                                      "not proven."
    assert any("Break-even at this price: 87%" in d for d in g["details"])  # 86c + 0.85c fee
    assert light("H4", K15, view(400, 118), None, None)["record_text"] == "No live record yet."


def test_every_rule_has_amber_and_thresholds():
    assert set(AMBERS) == set(RULES) == set(THRESHOLDS)


# -- the mirrored thresholds match the rule functions (the words never drift from the rules) --------------------------
def test_mirrored_thresholds_match_the_rule_functions():
    t = THRESHOLDS["CUSHION100"]
    lo, hi = t.left_s
    ok = dict(yes_ask=0.80)
    assert evaluate("CUSHION100", K15, view(lo, 150, **ok)) and evaluate("CUSHION100", K15, view(hi, 150, **ok))
    assert not evaluate("CUSHION100", K15, view(lo - 1, 150, **ok))
    assert not evaluate("CUSHION100", K15, view(hi + 1, 150, **ok))
    assert evaluate("CUSHION100", K15, view(400, t.gap_usd, **ok))
    assert not evaluate("CUSHION100", K15, view(400, t.gap_usd - 0.01, **ok))
    for ask, want in ((t.ask_lo, True), (t.ask_lo - 0.01, False), (t.ask_hi, True), (t.ask_hi + 0.01, False)):
        assert bool(evaluate("CUSHION100", K15, view(400, 150, yes_ask=ask))) is want, ask

    t = THRESHOLDS["H4"]
    lo, hi = t.left_s
    need = t.gap_pct * STRIKE
    assert evaluate("H4", K15, view(lo, need, yes_ask=0.9)) and evaluate("H4", K15, view(hi, need, yes_ask=0.9))
    assert not evaluate("H4", K15, view(lo - 1, need, yes_ask=0.9))
    assert not evaluate("H4", K15, view(hi + 1, need, yes_ask=0.9))
    assert not evaluate("H4", K15, view(650, need - 0.01, yes_ask=0.9))
    assert evaluate("H4", K15, view(650, need, yes_ask=t.ask_hi))
    assert not evaluate("H4", K15, view(650, need, yes_ask=t.ask_hi + 0.01))

    t = THRESHOLDS["LATERESID"]
    lo, hi = t.left_s

    def lr(left, fair, ask):
        return evaluate("LATERESID", K15, view(left, 5, yes_ask=ask, no_ask=0.99, fair_up=fair))

    fee = setups._fee(view(200, 5), K15, 0.60)
    assert lr(lo, 0.60 + fee + t.edge, 0.60) and lr(hi, 0.60 + fee + t.edge, 0.60)
    assert not lr(lo - 1, 0.70, 0.60) and not lr(hi + 1, 0.70, 0.60)
    assert not lr(200, 0.60 + fee + t.edge - 0.001, 0.60)
    fee_hi = setups._fee(view(200, 5), K15, t.ask_hi)
    assert lr(200, t.ask_hi + fee_hi + 0.06, t.ask_hi)
    assert not lr(200, 0.99, t.ask_hi + 0.01)

    t = THRESHOLDS["HPANIC"]
    band = t.near_pct * STRIKE

    def hp(left, gap, fair, ask):
        return evaluate("HPANIC", K15, view(left, gap, yes_ask=ask, no_ask=0.99, fair_up=fair))

    fee = setups._fee(view(200, 5), K15, t.ask_hi)
    assert hp(t.left_s[1], band, t.ask_hi + fee + 0.031, t.ask_hi)
    assert not hp(t.left_s[1] + 1, band, 0.6, t.ask_hi)
    assert not hp(200, band + 0.01, 0.6, t.ask_hi)
    assert not hp(200, 0, 0.6, t.ask_hi + 0.01)
    assert not hp(200, 0, t.ask_hi + fee + t.edge - 0.001, t.ask_hi)
