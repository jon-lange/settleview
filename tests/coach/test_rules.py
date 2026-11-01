"""Coach 15m entry, value-based exit and replay scoring (the rules the research scripts replay).

All quotes, prices and fair values here are SYNTHETIC test inputs, not market data.
"""

import pytest

from btc_signal_engine.domain.coach.rules import (
    NoTrade,
    Setup,
    entry_decision,
    exit_advice,
    pnl,
    replay,
    settlement_exit,
)
from btc_signal_engine.domain.settlement.fees import kalshi_fee

T = 1_800_000_000_000 - 1_800_000_000_000 % 900_000
END = T + 900_000


def quote(yes_ask, no_ask, yes_bid=None, no_bid=None):
    return {"yes_ask": yes_ask, "no_ask": no_ask,
            "yes_bid": yes_bid if yes_bid is not None else round(yes_ask - 0.01, 2),
            "no_bid": no_bid if no_bid is not None else round(no_ask - 0.01, 2)}


# -- entry ---------------------------------------------------------------------------------
def test_enter_needs_five_cents_of_edge_after_the_buy_fee():
    # fee to buy at 54c is 1.74c (exact Kalshi fee), so 62% odds leave 8c - 1.74c = 6.26c of edge.
    # Odds are now blended halfway with the 53.5% market mid before the edge is measured: raw 70.5% blends to 62%.
    s = entry_decision(0.705, quote(0.54, 0.47), T + 300_000, END)
    assert isinstance(s, Setup) and s.side == "UP" and s.entry == 0.54
    assert s.stop == 0.39 and s.time_exit == END - 60_000 and s.edge == pytest.approx(0.0626, abs=1e-4)
    # a 6c gross gap is only 4.26c after the fee: no trade (raw 66.5% blends to 60%)
    n = entry_decision(0.665, quote(0.54, 0.47), T + 300_000, END)
    assert isinstance(n, NoTrade) and n.code == "no_edge"
    assert "already prices it" in n.sentence and "5¢" in n.sentence


def test_down_side_buys_no():
    # raw 10% UP (was 20%): the blend with the 29.5% market mid (19.75%) still leaves DOWN cheap enough
    s = entry_decision(0.10, quote(0.30, 0.71), T + 60_000, END)
    assert s.side == "DOWN" and s.entry == 0.71 and s.stop == pytest.approx(0.56)


def test_stop_is_floored():
    # The 2c stop floor only applies to entries under 17c, which the 25c longshot floor now forbids:
    # a 10c DOWN is refused, and the cheapest allowed entry (25c) gets the plain 15c-below stop.
    assert entry_decision(0.01, quote(0.95, 0.10), T + 60_000, END).code == "longshot"
    s = entry_decision(0.01, quote(0.76, 0.25), T + 60_000, END)
    assert s.side == "DOWN" and s.stop == 0.10


def test_needs_more_than_two_minutes():
    assert entry_decision(0.80, quote(0.54, 0.47), END - 120_000, END).code == "too_late"
    assert isinstance(entry_decision(0.80, quote(0.54, 0.47), END - 121_000, END), Setup)


def test_missing_ask_is_no_market():
    n = entry_decision(0.80, {"yes_ask": None, "no_ask": 0.4}, T, END)
    assert n.code == "no_market"


def test_engine_pointing_the_other_way_blocks_but_sitting_out_does_not():
    n = entry_decision(0.70, quote(0.54, 0.47), T + 60_000, END, engine_against="DOWN")
    assert isinstance(n, NoTrade) and n.code == "mixed" and "engine points DOWN" in n.sentence
    assert isinstance(entry_decision(0.70, quote(0.54, 0.47), T + 60_000, END, engine_against=None), Setup)
    assert isinstance(entry_decision(0.70, quote(0.54, 0.47), T + 60_000, END, engine_against="UP"), Setup)


# -- value-based exits ----------------------------------------------------------------------
def up_setup():
    return entry_decision(0.70, quote(0.54, 0.47), T + 300_000, END)  # stop 0.39


def test_exit_rules():
    s = up_setup()
    mid = T + 400_000
    a = exit_advice(s, 0.84, 0.91, mid)
    assert a.action == "hold_to_settlement"
    a = exit_advice(s, 0.80, 0.79, mid)
    assert a.action == "exit" and a.reason == "take_value"
    a = exit_advice(s, 0.77, 0.79, mid)  # bid exactly at fair - 2c
    assert a.action == "exit" and a.reason == "take_value"
    a = exit_advice(s, 0.74, 0.78, mid)  # between: plain hold
    assert a.action == "hold" and a.target_bid == pytest.approx(0.76)
    a = exit_advice(s, 0.30, 0.35, mid)
    assert a.action == "exit" and a.reason == "stop"
    late = END - 30_000
    assert exit_advice(s, 0.70, 0.74, late).reason == "time"
    assert exit_advice(s, 0.70, 0.80, late).action == "hold_to_settlement"
    assert exit_advice(s, None, 0.10, mid).action == "hold"


def test_down_setup_uses_down_odds():
    # raw 10% (was 20%) so DOWN still clears 5c on the blended odds; exits below use the raw odds passed in
    s = entry_decision(0.10, quote(0.30, 0.71), T + 60_000, END)
    assert exit_advice(s, 0.86, 0.12, T + 100_000).reason == "take_value"  # fair DOWN 0.88
    assert exit_advice(s, 0.60, 0.50, T + 100_000).reason == "stop"  # fair DOWN 0.50 < 0.56


# -- replay on a recorded path -------------------------------------------------------------
def path(*bids, start=T + 302_000, step=2_000, side="yes"):
    return [{"ts": start + i * step, f"{side}_bid": b} for i, b in enumerate(bids)]


def test_replay_take_value_is_profit_after_fees():
    s = up_setup()
    # quotes after the 30s minimum hold (entry at T+300s)
    fair = {T + 332_000: 0.72, T + 334_000: 0.75, T + 336_000: 0.79}
    ex = replay(s, path(0.60, 0.66, 0.78, start=T + 332_000), lambda ts: fair.get(ts))
    assert ex.outcome == "take_value" and ex.price == 0.78 and ex.ts == T + 336_000
    net, fees = pnl(s.entry, ex.price, settled=False)
    assert fees == pytest.approx(kalshi_fee(1, 0.54) + kalshi_fee(1, 0.78))
    assert net == pytest.approx(0.78 - 0.54 - fees) and net > 0


def test_replay_stop_is_loss():
    s = up_setup()
    ex = replay(s, path(0.50, 0.30), lambda ts: 0.60 if ts == T + 302_000 else 0.33)
    assert ex.outcome == "stop" and ex.price == 0.30
    assert pnl(s.entry, ex.price, settled=False)[0] < 0


def test_replay_time_exit_and_skips_missing_fair():
    s = up_setup()
    quotes = path(0.60, 0.61, start=s.time_exit - 2_000, step=2_000)
    ex = replay(s, quotes, lambda ts: 0.64)  # in the last minute, 0.64 < 0.61 + 0.05
    assert ex.outcome == "time" and ex.ts == s.time_exit and ex.price == 0.61
    assert replay(s, quotes, lambda ts: None) is None


def test_replay_holds_to_settlement():
    s = up_setup()
    quotes = path(0.85, 0.90, start=END - 10_000, step=4_000) + path(0.99, start=END)
    assert replay(s, quotes, lambda ts: 0.97) is None  # clearly better to hold; post-close quote ignored
    ex = settlement_exit(s, "UP")
    assert ex.outcome == "settled" and ex.price == 1.0
    net, fees = pnl(s.entry, ex.price, settled=True)
    assert fees == kalshi_fee(1, 0.54) and net == pytest.approx(1 - 0.54 - fees)
    assert settlement_exit(s, "DOWN").price == 0.0


def test_take_value_never_sells_at_a_loss_and_waits_the_minimum_hold():
    from btc_signal_engine.domain.coach.rules import MIN_HOLD_MS, Setup, exit_advice
    s = Setup("UP", 0.76, 0.84, 0.61, END - 60_000, END, 0.06, T + 300_000)
    # 2s after entry the odds jitter to 77% and Up bids 75c: before, this was an instant losing "take value"
    assert exit_advice(s, 0.75, 0.77, T + 302_000).action == "hold"
    # after the minimum hold, a bid at our odds is still not taken if it loses money after fees
    assert exit_advice(s, 0.75, 0.77, T + 300_000 + MIN_HOLD_MS).action == "hold"
    # a profitable bid at our odds is taken
    a = exit_advice(s, 0.85, 0.86, T + 300_000 + MIN_HOLD_MS)
    assert (a.action, a.reason) == ("exit", "take_value")
    # the stop still works immediately
    assert exit_advice(s, 0.40, 0.55, T + 301_000).reason == "stop"


# -- entry limit slack: live tests lost 3 of 4 setups to a 1c move between proposal and send -----------------------
from btc_signal_engine.domain.coach.rules import ENTRY_SLACK, entry_limit  # noqa: E402


def test_entry_limit_allows_up_to_two_cents_while_the_edge_still_clears():
    # fair 0.45 vs ask 0.33: the edge stays well above 5c at 34c and 35c
    assert ENTRY_SLACK == 0.02 and entry_limit(0.33, 0.45) == 0.35


def test_entry_limit_stops_where_the_edge_would_fall_below_the_minimum():
    fair = 0.40
    lim = entry_limit(0.33, fair)
    assert fair - lim - kalshi_fee(1, lim) >= 0.05 - 1e-9
    nxt = round(lim + 0.01, 2)
    assert lim == 0.33 or fair - nxt - kalshi_fee(1, nxt) < 0.05 or nxt > 0.35


def test_entry_limit_never_below_the_seen_ask_or_at_a_dollar():
    assert entry_limit(0.30, 0.30) == 0.30  # no edge at all: limit is just the ask (the gate still decides)
    assert entry_limit(0.98, 1.0) < 1.0


# -- longshot floor and zone (MERID: 0/16 wins under 20c; under 25c net negative, 25-34c needs extra edge) ---------
from btc_signal_engine.domain.coach.rules import (  # noqa: E402
    LONGSHOT_EXTRA_EDGE,
    LONGSHOT_FLOOR,
    LONGSHOT_ZONE,
    MARKET_WEIGHT,
    blend_fair,
)


def edge_at(fair_side, ask):
    return fair_side - ask - kalshi_fee(1, ask)


def test_longshot_constants():
    assert (LONGSHOT_FLOOR, LONGSHOT_ZONE, LONGSHOT_EXTRA_EDGE, MARKET_WEIGHT) == (0.25, 0.35, 0.03, 0.5)


def test_ask_under_25c_is_never_bought_even_with_a_big_raw_edge():
    # UP asks 20c; even our raw 60% (and the 40% blend) is a huge edge, but under the floor: sit out
    n = entry_decision(0.60, quote(0.20, 0.81), T + 300_000, END)
    assert isinstance(n, NoTrade) and n.code == "longshot"
    assert "long shot" in n.sentence and "25¢" in n.sentence


def test_ask_in_the_longshot_zone_needs_eight_cents():
    q = quote(0.30, 0.71, yes_bid=0.29)  # market mid UP 29.5%
    # blended edge just under 8c: no trade, and the reason is the zone, not "no edge"
    raw = 2 * (0.30 + kalshi_fee(1, 0.30) + 0.079) - 0.295
    n = entry_decision(raw, q, T + 300_000, END)
    assert isinstance(n, NoTrade) and n.code == "longshot" and "8¢" in n.sentence
    raw = 2 * (0.30 + kalshi_fee(1, 0.30) + 0.081) - 0.295
    s = entry_decision(raw, q, T + 300_000, END)
    assert isinstance(s, Setup) and s.side == "UP" and s.edge == pytest.approx(0.081, abs=1e-4)


def test_ask_at_40c_needs_only_five_cents():
    q = quote(0.40, 0.61, yes_bid=0.39)  # market mid UP 39.5%
    raw = 2 * (0.40 + kalshi_fee(1, 0.40) + 0.051) - 0.395
    s = entry_decision(raw, q, T + 300_000, END)
    assert isinstance(s, Setup) and s.side == "UP" and s.edge == pytest.approx(0.051, abs=1e-4)
    raw = 2 * (0.40 + kalshi_fee(1, 0.40) + 0.049) - 0.395
    assert entry_decision(raw, q, T + 300_000, END).code == "no_edge"


def test_best_eligible_side_wins_when_the_bigger_edge_is_a_longshot():
    # DOWN at 20c has the bigger edge but is under the floor; UP at 79c still clears 5c on the blend
    q = {"yes_ask": 0.79, "yes_bid": 0.78, "no_ask": 0.20, "no_bid": 0.19}
    s = entry_decision(0.99, q, T + 300_000, END)
    assert isinstance(s, Setup) and s.side == "UP"


# -- shrink toward the market (MERID weights the market 0.25-0.85; ML beat the mid by only 0.0014 log loss) ------
def test_blend_is_half_our_odds_half_the_market_mid():
    used, mid = blend_fair(0.70, {"yes_bid": 0.49, "yes_ask": 0.51, "no_ask": 0.50})
    assert mid == pytest.approx(0.50) and used == pytest.approx(0.60)
    # no yes bid: mid from the two asks
    used, mid = blend_fair(0.70, {"yes_bid": None, "yes_ask": 0.52, "no_ask": 0.50})
    assert mid == pytest.approx(0.51) and used == pytest.approx(0.605)
    # nothing computable: no shrink
    assert blend_fair(0.70, {}) == (0.70, None)


def test_raw_ten_cent_edge_against_the_market_becomes_about_five():
    q = {"yes_bid": 0.49, "yes_ask": 0.50, "no_ask": 0.51, "no_bid": 0.50}  # market says 49.5% UP
    used, mid = blend_fair(0.60, q)  # our odds 60% vs a 50c ask: a 10c raw gap
    assert used - 0.50 == pytest.approx(0.0475)  # about 5c once shrunk halfway to the market
    # after the 1.75c buy fee that is under 5c: no trade (on the raw odds it was 8.25c and would have entered)
    n = entry_decision(0.60, q, T + 300_000, END)
    assert isinstance(n, NoTrade) and n.code == "no_edge" and "blended 55%" in n.sentence
    # a bigger raw edge still enters; the Setup keeps the raw odds and records the blend and the market's odds
    s = entry_decision(0.70, q, T + 300_000, END)
    assert isinstance(s, Setup) and s.side == "UP" and s.fair_up == 0.70
    assert s.fair_used == pytest.approx(0.5975) and s.market_mid == pytest.approx(0.495)
    assert s.edge == pytest.approx(0.5975 - 0.50 - kalshi_fee(1, 0.50), abs=1e-4)


def test_entry_limit_uses_the_blended_fair():
    # raw 62% vs a 49.5% market: the blend is 55.75%. On the raw odds 2c of slack fits (8.25c left at 52c);
    # on the blend the edge at 50c is already under 5c, so there is no slack at all.
    used, _ = blend_fair(0.62, {"yes_bid": 0.49, "yes_ask": 0.50})
    assert used == pytest.approx(0.5575)
    assert entry_limit(0.50, 0.62) == 0.52
    assert entry_limit(0.50, used) == 0.50
    # blend 0.60: 51c leaves 7.25c and 52c 6.25c, so the 2c slack still applies while the BLENDED edge clears 5c
    assert entry_limit(0.50, 0.60) == 0.52 and edge_at(0.60, 0.52) >= 0.05


def test_entry_limit_slack_respects_the_longshot_zone_requirement():
    # 30c ask in the zone (needs 8c). With fair 0.40 the edge at 31c is ~7.5c: clears 5c but not 8c -> no slack.
    fair = 0.40
    assert fair - 0.31 - kalshi_fee(1, 0.31) < 0.08 and fair - 0.31 - kalshi_fee(1, 0.31) >= 0.05
    assert entry_limit(0.30, fair) == 0.30
