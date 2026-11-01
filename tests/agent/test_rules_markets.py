"""Entry rules per (rule, market): H4 / CUSHION100 on Polymarket 15m, the two backtest-killed rules (HPANIC from
h_panic, LATERESID from late_residual) as live forward checks, each market's entry cutoff, the cross-venue "one bet
per window" session option, launch validation against the session's venues, and the catalog shape."""

from __future__ import annotations

import asyncio

import pytest

from btc_signal_engine.application import agent_rules as R
from btc_signal_engine.domain.settlement.fees import kalshi_fee, polymarket_fee
from tests.agent.test_rules import K, RuleRig, view
from tests.agent.test_tools import HOUR
from tests.trading.helpers import CLOSE, NOW

K15, P15, P5 = "kalshi_15m", "polymarket_15m", "polymarket_5m"
KILLED = "killed in backtest — live forward check"


# -- the table: which rule runs on which market -------------------------------------------------------------------------
def test_the_rule_by_market_table():
    assert {r["id"]: r["markets"] for r in R.catalog()} == {
        "H4": [K15, P15], "CUSHION100": [K15, P15], "HPANIC": [K15, P15, P5], "LATERESID": [K15, P15]}


def test_catalog_shape_and_labels():
    for r in R.catalog():
        assert set(r) == {"id", "markets", "market_labels", "label", "description"}
        assert r["description"] and len(r["market_labels"]) == len(r["markets"])
    lab = {r["id"]: r["label"] for r in R.catalog()}
    assert lab["HPANIC"] == lab["LATERESID"] == KILLED
    assert lab["H4"] != KILLED and lab["CUSHION100"] != KILLED
    for rid in ("HPANIC", "LATERESID"):
        d = next(r["description"] for r in R.catalog() if r["id"] == rid)
        assert KILLED in d and "no stop" in d


def test_a_rule_on_a_market_it_does_not_trade_is_an_error():
    for rid, mk in (("H4", P5), ("CUSHION100", P5), ("LATERESID", P5), ("HPANIC", "polymarket_1h"),
                    ("H4", "nope")):
        with pytest.raises(ValueError):
            R.evaluate(rid, mk, view())


# -- H4 / CUSHION100 on Polymarket 15m: same thresholds, the view's price to beat as the strike -------------------------
def test_h4_and_cushion100_on_polymarket_15m_use_the_price_to_beat():
    ptb = 80_012.5  # Polymarket's priceToBeat for the window (the view's btc.strike on that market)
    m = R.evaluate("H4", P15, view(strike=ptb, btc=ptb + 400))
    assert m["strike"] == ptb and m["market"] == P15 and m["stop_index_below"] == ptb + 200
    assert R.evaluate("H4", P15, view(strike=ptb, btc=ptb + 0.004 * ptb - 0.01)) is None
    m = R.evaluate("CUSHION100", P15, view(left_s=450, strike=ptb, btc=ptb - 100, no_ask=0.80, yes_ask=0.22))
    assert m["side"] == "DOWN" and m["strike"] == ptb and m["stop_index_above"] == ptb - 40
    assert R.evaluate("CUSHION100", P15, view(left_s=450, strike=ptb, btc=ptb - 99.99, no_ask=0.80)) is None
    for left, hit in ((721, False), (720, True), (600, True), (599, False)):
        assert (R.evaluate("H4", P15, view(left_s=left)) is not None) == hit, left


# -- HPANIC (h_panic: <= 4 min left, BTC within 0.1%, cheapest side <= 35c, residual > 3c, hold, no stop) --------------
def hp(market=K15, **kw):
    kw = {"left_s": 200, "btc": K + 50, "yes_ask": 0.30, "no_ask": 0.72, "fair_up": 0.40, **kw}
    if market == P5:
        kw.setdefault("window_ms", 300_000)
    return R.evaluate("HPANIC", market, view(**kw))


def test_hpanic_time_band_respects_each_markets_entry_cutoff():
    for mk, cases in ((K15, ((241, False), (240, True), (121, True), (120, False))),
                      (P15, ((241, False), (240, True), (121, True), (120, False))),
                      (P5, ((241, False), (240, True), (61, True), (60, False)))):
        for left, hit in cases:
            assert (hp(mk, left_s=left) is not None) == hit, (mk, left)


def test_hpanic_needs_btc_within_01_percent_of_the_strike():
    assert hp(btc=K + 80) is not None and hp(btc=K - 80) is not None  # 0.1% of 80,000 = $80
    assert hp(btc=K + 80.01) is None and hp(btc=K - 80.01) is None
    assert hp(btc=K)["side"] == "UP"  # at the strike itself: still decided (the cheaper side)


def test_hpanic_ask_cap_is_35c():
    assert hp(yes_ask=0.35, fair_up=0.50)["ask"] == 0.35
    assert hp(yes_ask=0.36, fair_up=0.50) is None


def test_hpanic_residual_must_exceed_3c_after_the_venues_fee():
    f = kalshi_fee(1, 0.30)
    assert hp(fair_up=round(0.30 + f + 0.0301, 4)) is not None
    assert hp(fair_up=round(0.30 + f + 0.03, 4)) is None  # strictly more than 3c
    assert hp(fair_up=None) is None  # no model fair value: no trade
    # Polymarket's fee (nearest cent per order: 1c on one 30c contract) is not Kalshi's (1.47c)
    assert polymarket_fee(1, 0.30) == 0.01
    assert hp(P15, fair_up=0.3402) is not None and hp(K15, fair_up=0.3402) is None
    m = hp(fair_up=0.40)
    assert m["fee"] == f and m["residual"] == round(0.40 - 0.30 - f, 4) and m["fair_up"] == 0.40


def test_hpanic_side_is_the_cheaper_qualifying_side_and_has_no_stop():
    dn = hp(btc=K - 50, yes_ask=0.72, no_ask=0.30, fair_up=0.60)
    assert dn["side"] == "DOWN" and dn["ask"] == 0.30
    both = hp(yes_ask=0.30, no_ask=0.25, fair_up=0.50)
    assert both["side"] == "DOWN"
    tie = hp(yes_ask=0.30, no_ask=0.30, fair_up=0.50)
    assert tie["side"] == "UP"
    # the cheaper side fails its residual: the other qualifying side is bought
    assert hp(yes_ask=0.30, no_ask=0.25, fair_up=0.80)["side"] == "UP"
    assert (dn["stop_index_below"], dn["stop_index_above"]) == (None, None) and dn["ask_cap"] == 0.35
    assert R.note(dn).startswith("[rule:HPANIC] DOWN") and "no stop" in R.note(dn)


# -- LATERESID (late_residual best-holdout variant: 4:00-3:00 left, larger edge, edge >= 5c, ask <= 65c) ---------------
def lr(market=K15, **kw):
    kw = {"left_s": 200, "btc": K + 30, "yes_ask": 0.50, "no_ask": 0.52, "fair_up": 0.60, **kw}
    return R.evaluate("LATERESID", market, view(**kw))


def test_lateresid_time_band_is_4_to_3_minutes_left_inclusive():
    for mk in (K15, P15):
        for left, hit in ((241, False), (240, True), (180, True), (179, False)):
            assert (lr(mk, left_s=left) is not None) == hit, (mk, left)


def test_lateresid_edge_at_least_5c_after_fee():
    f = kalshi_fee(1, 0.50)
    assert lr(fair_up=round(0.50 + f + 0.05, 4)) is not None
    assert lr(fair_up=round(0.50 + f + 0.0499, 4)) is None
    m = lr()
    assert m["side"] == "UP" and m["edge"] == round(0.60 - 0.50 - f, 4) and m["fee"] == f
    assert lr(fair_up=None) is None
    assert polymarket_fee(1, 0.50) == 0.02  # PM: nearest cent of 1.74c; Kalshi 1.75c
    assert lr(K15, fair_up=0.5676) is not None and lr(P15, fair_up=0.5676) is None


def test_lateresid_ask_cap_is_65c():
    assert lr(yes_ask=0.65, no_ask=0.40, fair_up=0.80)["ask"] == 0.65
    assert lr(yes_ask=0.66, no_ask=0.40, fair_up=0.80) is None  # the larger edge is UP's, and its ask is too high


def test_lateresid_takes_the_side_with_the_larger_edge_any_btc_side_no_stop():
    dn = lr(yes_ask=0.47, no_ask=0.55, fair_up=0.30)
    assert dn["side"] == "DOWN" and dn["ask"] == 0.55
    assert lr(yes_ask=0.40, no_ask=0.40, fair_up=0.50)["side"] == "UP"  # tie -> UP
    assert lr(btc=K - 30)["side"] == "UP"  # not tied to BTC's side
    assert (dn["stop_index_below"], dn["stop_index_above"]) == (None, None) and dn["ask_cap"] == 0.65
    assert "no stop" in R.note(dn)


# -- the runner: HPANIC under 3 minutes on Kalshi (its own cutoff), rows carry rule and market ---------------------------
def test_hpanic_enters_kalshi_with_2_30_left_with_no_btc_stop(store):
    r = RuleRig(store, rules=("HPANIC",), btc=K + 40, book=(0.28, 0.70), reactivate=("HPANIC",))  # YES ask 30c, NO ask 72c; fair 0.58
    r.runner.clock.t = CLOSE - 150_000
    rows = asyncio.run(r.runner.rules_tick(r.runner.now()))
    assert [x["ok"] for x in rows] == [True] and rows[0]["market"] == K15 and len(r.buys()) == 1
    (b,) = store.select("trading_brackets")
    assert b["side"] == "UP" and b["stop_index_below"] is None and b["stop_index_above"] is None
    d = [x for x in r.journal("decision") if x.get("source") == "rule"]
    assert d[0]["rule"] == "HPANIC" and d[0]["market"] == K15 and "no stop" in d[0]["invalidation"]


def test_h4_still_waits_out_the_3_minute_floor(store):
    r = RuleRig(store, rules=("H4",))
    r.runner.clock.t = CLOSE - 179_000
    assert asyncio.run(r.runner.rules_tick(r.runner.now())) == []


# -- one bet per window (session option, default on). One agent = one market (multi-agent, 2026-10-31): the
# "across venues" case is now two agents, each with its own slot (tests/agent/test_fleet.py) --------------------------
def test_one_bet_per_window_must_be_a_boolean(store):
    r = RuleRig(store, rules=())
    r.ag.stop(r.ag.latest()["id"], NOW)
    base = {"kind": "in_app", "venues": [K15], "budget": 5.0, "per_trade": 1.0, "ends_ts": NOW + HOUR,
            "spend_cap": 5.0, "model": "claude-haiku-5-5", "level": "auto"}
    for bad in ("yes", 1, None):
        out = r.ag.launch({**base, "one_bet_per_window": bad}, NOW)
        assert (out["ok"], out["code"]) == (False, "invalid") and "one_bet_per_window" in out["reason"]


# -- launch: each rule against the session's venues ---------------------------------------------------------------------
def test_launch_refuses_a_rule_whose_markets_are_not_in_the_session(store):
    r = RuleRig(store, rules=())
    r.ag.stop(r.ag.latest()["id"], NOW)
    base = {"kind": "in_app", "budget": 5.0, "per_trade": 1.0, "ends_ts": NOW + HOUR, "spend_cap": 5.0,
            "model": "claude-haiku-5-5", "level": "auto"}
    for venues, rules in (([P5], ["H4"]), ([P5], ["LATERESID"]), ([K15], ["HPANIC", "CUSHION100", "H4", "x"])):
        out = r.ag.launch({**base, "venues": venues, "rules": rules}, NOW)
        assert out["ok"] is False and out["code"] in ("invalid", "unavailable"), (venues, rules)
    out = r.ag.launch({**base, "venues": [P5], "rules": ["H4"]}, NOW)
    assert "Kalshi 15 min" in out["reason"] and "Polymarket 15 min" in out["reason"]
    out = r.ag.launch({**base, "venues": [K15], "rules": ["HPANIC", "LATERESID", "H4", "CUSHION100"],
                       "one_bet_per_window": False}, NOW)
    assert out["code"] == "rule_retired" and "HPANIC" in out["reason"]  # retired by the user 2026-10-30
    r.ag.rule_book.set("HPANIC", "active", NOW, by="user", reason="Reactivated for a test.")
    out = r.ag.launch({**base, "venues": [K15], "rules": ["HPANIC", "LATERESID", "H4", "CUSHION100"],
                       "one_bet_per_window": False}, NOW)
    assert out["ok"], out
    lim = out["session"]["limits"]
    assert lim["rules"] == ["HPANIC", "LATERESID", "H4", "CUSHION100"] and lim["one_bet_per_window"] is False


def test_status_lists_rules_with_markets_and_the_session_shows_the_option(store):
    from tests.agent.test_routes import World, launch_body
    w = World(store)
    st = w.pc.get("/api/agent/status").json()
    assert {x["id"]: x["markets"] for x in st["rules"]}["HPANIC"] == [K15, P15, P5]
    out = w.pc.post("/api/agent/launch", json=launch_body(rules=["H4"], one_bet_per_window=False)).json()
    assert out["ok"], out
    assert out["session"]["limits"]["one_bet_per_window"] is False
    st = w.pc.get(f"/api/agent/sessions/{out['id']}").json()
    assert st["session"]["limits"]["one_bet_per_window"] is False and st["session"]["id"] == out["session"]["id"]
