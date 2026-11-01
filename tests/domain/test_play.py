"""Play screens' pure numbers: the call, ideal entry / exit, BTC vs strike, the in-trade advice, the scoreboard."""

from __future__ import annotations

from btc_signal_engine.domain.strategy import play as P

DAY = 86_400_000
NOW = 1793206800000 + 12 * 3_600_000


def test_lean_thresholds_and_mid():
    assert P.mid(0.60, 0.62) == 0.61 and P.mid(None, 0.5) is None and P.mid(0.0, 0.5) is None
    assert P.lean(0.55) == "UP" and P.lean(0.549) == "COIN FLIP" and P.lean(0.45) == "DOWN"
    assert P.lean(None) is None
    assert P.call_lean("SKIP") is None and P.call_lean("UP") == "UP"
    assert P.crowd_lean(0.25) == "UP" and P.crowd_lean(-0.3) == "DOWN" and P.crowd_lean(0.1) == "BALANCED"
    assert P.crowd_lean(None) is None


def test_ideal_entry_is_min_of_ask_and_bid_plus_a_cent():
    assert P.ideal_entry(0.60, 0.64) == 0.61
    assert P.ideal_entry(0.63, 0.64) == 0.64
    assert P.ideal_entry(None, 0.64) == 0.64
    assert P.ideal_entry(0.60, None) is None


def test_ride_plan_matches_pnw_ride_exit():
    up = P.ride_plan("UP", 80_000.0, 0.62)
    assert up["max_price"] == 0.63 and up["take_profit"] == 0.99
    assert up["stop_index_below"] == 79_990.0 and up["stop_index_above"] is None and up["buyable"]
    down = P.ride_plan("DOWN", 80_000.0, 0.40)
    assert down["stop_index_above"] == 80_010.0 and down["stop_index_below"] is None
    assert P.ride_plan("UP", 80_000.0, 0.98)["buyable"] is False  # limit 99c leaves no room under the 99c sell
    assert P.ride_plan("UP", None, 0.6)["buyable"] is False and P.ride_plan("UP", 1.0, None)["buyable"] is False


def test_btc_gap_and_flip_move():
    g = P.btc_gap(80_035.0, 80_000.0)
    assert g["gap"] == 35.0 and g["leader"] == "UP" and g["flip_move"] == -35.0 and "drop" in g["sentence"]
    g = P.btc_gap(79_990.0, 80_000.0)
    assert g["leader"] == "DOWN" and g["flip_move"] == 10.0 and "rise" in g["sentence"]
    assert P.btc_gap(None, 1.0)["gap"] is None


def test_stake_default_capped_by_server_limit():
    assert P.stake_default(5.0) == 5.0 and P.stake_default(25.0) == 5.0 and P.stake_default(1.0) == 1.0


def test_advice_holds_by_default_with_numbers():
    a = P.trade_advice("UP", bid=0.70, btc=80_060.0, stop_level=79_990.0, held=5.0, cost=3.2)
    assert a["action"] == "HOLD" and a["stop_distance"] == 70.0
    assert a["if_wins"] == 5.0 and a["pnl_if_wins"] == 1.8 and a["pnl_if_loses"] == -3.2
    assert a["sell_now"] == round(5 * 0.70 - 0.0735, 2) and a["pnl_sell_now"] == round(a["sell_now"] - 3.2, 2)


def test_advice_consider_exit_near_stop_or_under_half():
    near = P.trade_advice("UP", bid=0.62, btc=80_005.0, stop_level=79_990.0, held=5.0, cost=3.0)
    assert near["action"] == "CONSIDER EXIT" and "$15 from your stop" in near["sentence"]
    down = P.trade_advice("DOWN", bid=0.62, btc=79_990.0, stop_level=80_010.0, held=5.0, cost=3.0)
    assert down["stop_distance"] == 20.0 and down["action"] == "CONSIDER EXIT"  # exactly $20 counts
    cheap = P.trade_advice("UP", bid=0.45, btc=80_100.0, stop_level=79_990.0, held=5.0, cost=3.0)
    assert cheap["action"] == "CONSIDER EXIT" and "under 50%" in cheap["sentence"]
    past = P.trade_advice("UP", bid=0.30, btc=79_980.0, stop_level=79_990.0, held=5.0, cost=3.0)
    assert "past your stop" in past["sentence"]
    pending = P.trade_advice("UP", bid=0.6, btc=80_100.0, stop_level=79_990.0, held=0.0, cost=None)
    assert pending["action"] == "WAITING"


def _t(pnl, ts, entry=0.6, state="closed", exit_="settled", cost=6.17, filled=10.0):
    return {"id": str(ts), "side": "UP", "state": state, "pnl": pnl, "exit_ts": ts, "entry_avg": entry, "exit": exit_,
            "env": "prod", "entry_cost": cost, "filled": filled}


DAY0 = NOW - NOW % DAY  # the tests' "local" midnight (a UTC day here; the zone is the caller's: domain/days.py)


def _sb(trades, **kw):
    return P.scoreboard(trades, NOW, day_start=DAY0, week_start=DAY0 - 6 * DAY, **kw)


def test_scoreboard_record_streak_and_periods():
    trades = [_t(1.0, NOW - 10 * DAY), _t(-2.0, NOW - 3 * DAY), _t(0.5, NOW - 2 * 3_600_000, exit_="manual"),
              _t(0.7, NOW - 1000), _t(None, NOW, state="open"), _t(0.0, NOW - 5 * DAY)]
    sb = _sb(trades)
    assert sb["record"] == {"wins": 3, "losses": 1, "pushes": 1, "trades": 5, "pnl": 0.2}
    assert sb["streak"]["text"] == "W2"
    assert sb["pnl"] == {"today": 1.2, "week": -0.8, "all": 0.2}
    assert sb["open"] == 1
    c = sb["calls"]
    # break-even is the cost WITH the entry fee: $6.17 for 10 contracts = 61.7c, not the 60c price (review B5#1)
    assert c["n"] == 4 and c["win_rate"] == 0.75 and c["avg_price"] == 0.6 and c["break_even"] == 0.617
    assert c["edge"] == 0.133 and c["break_even_n"] == 4
    assert c["mostly_luck"] and "mostly luck" in c["verdict"] and "61.7¢ with fees (break-even 61.7%)" in c["verdict"]
    assert [r["result"] for r in sb["recent"]][:2] == ["W", "W"] and "vs_bot" not in sb
    assert [p["break_even"] for p in sb["attempts"]["points"]][:1] == [0.617]


def test_scoreboard_days_are_the_callers_local_days_not_utc():
    """Today and the week are from the caller's local midnights: a trade after a local midnight that is not the UTC
    one is in today; one before it is not (review B5#2)."""
    local0 = DAY0 + 5 * 3_600_000  # a zone five hours behind UTC: its midnight is 05:00 UTC
    now = local0 + 3_600_000
    trades = [_t(1.0, local0 + 60_000), _t(-0.25, local0 - 60_000)]
    sb = P.scoreboard(trades, now, day_start=local0, week_start=local0 - 6 * DAY)
    assert sb["pnl"]["today"] == 1.0 and sb["pnl"]["week"] == 0.75


def test_scoreboard_break_even_skips_unknown_costs_and_says_so():
    sb = _sb([_t(1.0, NOW - 2000), _t(-1.0, NOW - 1000, cost=None)])
    c = sb["calls"]
    assert c["break_even"] == 0.617 and c["break_even_n"] == 1 and c["edge"] == round(0.5 - 0.617, 4)
    assert "from the 1 of 2 with a known cost" in c["verdict"]
    none = _sb([_t(1.0, NOW - 2000, cost=None)])["calls"]
    assert none["break_even"] is None and none["edge"] is None and "break-even" not in none["verdict"]


def test_scoreboard_empty_and_loss_streak():
    assert _sb([])["calls"]["verdict"] == "No finished calls yet."
    assert _sb([])["streak"]["text"] == "—"
    mine = [_t(1.0, NOW - DAY), _t(-1.0, NOW - 2000), _t(-0.5, NOW - 1000)]
    assert _sb(mine)["streak"] == {"kind": "L", "n": 2, "text": "L2"}


def test_sell_now_uses_the_bid_after_the_taker_fee_not_the_mid():
    """Sell now = held x bid - the venue's fee at the bid (never the mid); its P&L is that minus the cost."""
    a = P.trade_advice("UP", bid=0.55, btc=80_100.0, stop_level=79_990.0, held=10.0, cost=6.0)
    fee = 0.1733  # 0.07 x 10 x 0.55 x 0.45 = 0.17325, rounded up to $0.0001
    assert a["sell_now"] == round(10 * 0.55 - fee, 2) == 5.33 and a["pnl_sell_now"] == -0.67
    cent = P.trade_advice("UP", bid=0.55, btc=None, stop_level=None, held=10.0, cost=6.0,
                          fee_fn=lambda c, p: round(0.0695 * c * p * (1 - p), 2))  # Polymarket's fee
    assert cent["sell_now"] == round(5.5 - 0.17, 2) and cent["pnl_if_wins"] == 4.0


def test_entry_cutoff_and_clock_phase():
    assert P.entry_open(120_001, 120_000) and not P.entry_open(120_000, 120_000)
    assert P.clock_phase(600_000, 120_000) == "normal"
    assert P.clock_phase(119_000, 60_000) == "warn" and P.clock_phase(60_000, 60_000) == "late"
    assert P.clock_phase(120_000, 120_000) == "late" and P.clock_phase(5_000, 5_000) == "late"
    assert P.SETTLE_UP_OP == "≥"


def test_calibration_splits_followed_and_against_the_call_at_entry():
    def t(side, fair, pnl):
        return {"side": side, "fair_up": fair, "pnl": pnl, "exit_ts": NOW, "state": "closed"}
    trades = [t("UP", 0.60, 1.0), t("DOWN", 0.30, -0.5), t("UP", 0.40, 0.8), t("DOWN", 0.70, -1.0),
              t("UP", 0.50, 2.0), t("UP", None, 1.0), t("UP", 0.60, None)]
    cal = P.calibration(trades)
    assert (cal["followed"]["wins"], cal["followed"]["losses"], cal["followed"]["pnl"]) == (1, 1, 0.5)
    assert (cal["against"]["wins"], cal["against"]["losses"], cal["against"]["pnl"]) == (1, 1, -0.2)
    assert cal["unknown"] == 2  # a coin-flip call and no stored call
    assert P.attempts(trades)["calibration"] == cal


def test_window_levels_and_thin_series_cap():
    assert P.window_levels([]) == {"hi": None, "lo": None, "open": None}
    assert P.window_levels([(0, 100.0), (1, 99.5), (2, 101.25), (3, 100.0)]) == {"hi": 101.25, "lo": 99.5,
                                                                                  "open": 100.0}
    pts = [(t, float(t)) for t in range(901)]
    out = P.thin_series(pts, 300)
    assert len(out) <= 300 and out[0] == [0, 0.0] and out[-1] == [900, 900.0]


def test_ride_plan_keeps_the_stop_away_from_btc_at_entry():
    # strike rule alone: DOWN stops at strike + $10
    assert P.ride_plan("DOWN", 80_000.0, 0.58)["stop_index_above"] == 80_010.0
    # BTC already $8 above the strike (the 2026-10-29 15:16 case): stop moves to BTC + $25, not $2 away
    down = P.ride_plan("DOWN", 80_000.0, 0.58, btc=80_008.0)
    assert down["stop_index_above"] == 80_033.0 and down["stop_index_below"] is None
    # BTC far from the stop already: the strike rule stands
    assert P.ride_plan("DOWN", 80_000.0, 0.30, btc=79_900.0)["stop_index_above"] == 80_010.0
    up = P.ride_plan("UP", 80_000.0, 0.55, btc=79_995.0)
    assert up["stop_index_below"] == 79_970.0 and up["stop_index_above"] is None
    assert P.ride_plan("UP", 80_000.0, 0.80, btc=80_100.0)["stop_index_below"] == 79_990.0


# -- "where it's headed": outlook, cone, plan payoff (synthetic inputs) ----------------------------------------
def test_outlook_down_side_chance_from_the_yes_book():
    no_bid, no_ask = P.short_side(0.30, 0.33)  # Polymarket: DOWN's book derived from YES (0.67 / 0.70)
    o = P.outlook("DOWN", bid=no_bid, ask=no_ask, fresh=True, fair_up=0.40, held=6.0, sell_now=3.97)
    assert o["chance_now"] == 0.685 and o["chance_bid"] == 0.67 and o["model_chance"] == 0.6
    assert o["disagree"] is False  # 8.5 points apart, under 10
    assert o["hold_value"] == round(0.685 * 6, 2) == 4.11 and o["give_up"] == 0.14


def test_outlook_up_side_and_stale_quote_is_null():
    o = P.outlook("UP", bid=0.60, ask=0.62, fresh=True, fair_up=0.58, held=2.0, sell_now=1.18)
    assert o["chance_now"] == 0.61 and o["model_chance"] == 0.58 and o["hold_value"] == 1.22 and o["give_up"] == 0.04
    s = P.outlook("UP", bid=0.60, ask=0.62, fresh=False, fair_up=0.58, held=2.0, sell_now=1.18)
    assert s["chance_now"] is None and s["chance_bid"] is None and s["disagree"] is None
    assert s["hold_value"] is None and s["give_up"] is None and s["model_chance"] == 0.58
    u = P.outlook("UP", bid=0.60, ask=0.62, fresh=True, fair_up=None, held=2.0, sell_now=None)
    assert u["model_chance"] is None and u["disagree"] is None and u["give_up"] is None and u["hold_value"] == 1.22


def test_outlook_disagree_threshold():
    at = P.outlook("UP", bid=0.60, ask=0.62, fresh=True, fair_up=0.51, held=1.0, sell_now=None)  # 10 points
    under = P.outlook("UP", bid=0.60, ask=0.62, fresh=True, fair_up=0.52, held=1.0, sell_now=None)  # 9 points
    over = P.outlook("UP", bid=0.60, ask=0.62, fresh=True, fair_up=0.75, held=1.0, sell_now=None)  # 14 the other way
    assert at["disagree"] is True and under["disagree"] is False and over["disagree"] is True


def test_cone_from_the_model_and_nulls():
    m = {"sigma_per_s": 5.0, "sd_close": 120.0, "mean_close": 80_040.0, "at": NOW - 1000, "window_end": NOW + 600_000}
    c = P.cone(now=NOW, end=NOW + 600_000, btc=80_045.0, strike=80_000.0, model=m)
    assert c["sigma_per_s"] == 5.0 and c["sd_close"] == 120.0 and c["btc"] == 80_045.0 and c["strike"] == 80_000.0
    assert c["bands"] == {"1": [79_920.0, 80_160.0], "2": [79_800.0, 80_280.0]} and c["end_ms"] == NOW + 600_000
    assert P.cone(now=NOW, end=NOW + 1, btc=None, strike=1.0, model=m) is None
    assert P.cone(now=NOW, end=NOW + 1, btc=1.0, strike=1.0, model=None) is None
    assert P.cone(now=NOW, end=NOW + 1, btc=1.0, strike=1.0, model={"price": 1.0}) is None  # no sigma: no band


def test_plan_payoff_with_fees():
    from btc_signal_engine.domain.settlement.fees import kalshi_fee, polymarket_fee
    k = P.plan_payoff(1.54, 0.63, lambda c, p: kalshi_fee(c, p))
    cost = 1.54 * 0.63 + kalshi_fee(1.54, 0.63)
    assert k == {"if_wins": round(1.54 - cost, 2), "if_loses": round(-cost, 2)}
    pm = P.plan_payoff(6.0, 0.71, polymarket_fee)
    assert pm == {"if_wins": round(6 - 4.26 - 0.09, 2), "if_loses": -4.35}  # fee 0.0695*6*.71*.29 = $0.0859 -> $0.09
    assert P.plan_payoff(0.0, 0.5, polymarket_fee) == {"if_wins": None, "if_loses": None}
    assert P.plan_payoff(None, 0.5, polymarket_fee) == {"if_wins": None, "if_loses": None}
