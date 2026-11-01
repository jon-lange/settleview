"""Ask Settleview (application/copilot.py): the line's priority, the folded warnings, the coin-flip context, the hold
note, the service over a real PlayService, and the record of what was shown with an order."""

from __future__ import annotations

import pytest

from btc_signal_engine.application.copilot import (Copilot, check_warnings, context, fold, hold_note, pick_line,
                                                   warning)
from btc_signal_engine.application.trading import MODES
from btc_signal_engine.application.your_record import YourRecord
from tests.agent.test_tools import K, Rig
from tests.application.test_setups import END, view
from tests.application.test_your_record import bracket
from tests.trading.helpers import CLOSE, NOW

GREEN_L = {"rule": "CUSHION100", "state": "green", "line": "CUSHION100 fits this window.", "details": ["d"],
           "progress": 1.0, "side": "UP"}
AMBER_L = {"rule": "H4", "state": "amber", "line": "H4 is setting up: BTC needs $10 more.", "details": [],
           "progress": 0.9, "side": "UP"}
AMBER_LOW = {**AMBER_L, "rule": "LATERESID", "line": "LATERESID is setting up: x.", "progress": 0.6}
GREY_L = {**AMBER_L, "state": "grey", "line": None, "progress": 0.0}
WARN = {"kind": "warning", "tone": "warn", "text": "This is late.", "details": [], "keys": ["late"]}
CTX = {"kind": "context", "tone": "agent", "text": "Coin flip. Passing fits your record.", "details": []}


def bucket(n, wins, net, contracts=None):
    contracts = n if contracts is None else contracts
    return {"n": n, "wins": wins, "net_usd": net, "contracts": contracts,
            "net_c": round(net / contracts * 100, 2) if contracts else None}


def record(**kw):
    out = {k: bucket(0, 0, 0.0) for k in ("all", "against_btc", "late", "band_50_75")}
    out["early_exits"] = {**bucket(0, 0, 0.0), "vs_hold_usd": 0.0}
    out.update(kw)
    return out


# -- the line's priority --------------------------------------------------------------------------------------------------
def test_line_priority_ready_then_setting_up_then_warning_then_context_then_nothing():
    assert pick_line("full", [AMBER_L, GREEN_L], WARN, CTX)["kind"] == "ready"
    assert pick_line("full", [AMBER_LOW, AMBER_L, GREY_L], WARN, CTX)["rule"] == "H4"  # the one furthest along
    assert pick_line("full", [GREY_L], WARN, CTX) is WARN
    assert pick_line("full", [GREY_L], None, CTX) is CTX
    assert pick_line("full", [GREY_L], None, None) is None
    assert pick_line("full", [], None, None) is None


def test_quiet_shows_warnings_only_and_off_nothing():
    assert pick_line("quiet", [GREEN_L], WARN, CTX) is WARN
    assert pick_line("quiet", [GREEN_L], None, CTX) is None
    assert pick_line("off", [GREEN_L], WARN, CTX) is None
    with pytest.raises(ValueError, match="unknown Ask Settleview mode"):
        pick_line("loud", [], None, None)
    with pytest.raises(ValueError, match="unknown warning"):
        check_warnings(["late", "lucky"])


# -- warnings -------------------------------------------------------------------------------------------------------------
def test_warnings_fold_into_one_sentence_and_need_a_losing_record():
    on = check_warnings(["against_btc", "late", "band_50_75"])
    v = view(150, 30, yes_ask=0.71, no_ask=0.31)  # BTC $30 above, 2:30 left
    rec = record(against_btc=bucket(5, 1, -1.05), late=bucket(4, 1, -1.0), band_50_75=bucket(9, 4, -0.81))
    w = warning("DOWN", v, v["now"], rec, on)
    assert w["text"] == "This is against BTC, and late." and w["keys"] == ["against_btc", "late"]
    assert w["details"] == ["Against BTC: you've won 1 of 5, −$1.05.",
                            "Under 3 minutes left: your average is −25¢ a contract over only 4 trades."]
    up = warning("UP", v, v["now"], rec, on)  # with BTC, but late and a 71c favourite
    assert up["text"] == "This is late, and at 50–75¢." and up["details"][1] == \
        "Your trades at 50–75¢: −9¢ per contract over 9 trades."
    assert warning("DOWN", v, v["now"], rec, check_warnings(["late"]))["text"] == "This is late."  # toggles
    assert warning("DOWN", v, v["now"], rec, frozenset()) is None
    # a winning or empty record has nothing to warn about
    ok = record(against_btc=bucket(6, 5, 1.2), late=bucket(0, 0, 0.0), band_50_75=bucket(3, 3, 0.5))
    assert warning("DOWN", v, v["now"], ok, on) is None
    assert warning("UP", view(400, 30, yes_ask=0.71), END - 400_000, rec, on)["text"] == "This is at 50–75¢."
    assert fold(["a", "b", "c"]) == "This is a, b, and c."


def test_context_is_a_coin_flip_with_no_rule_in_play_and_a_losing_band():
    v = view(400, 5, yes_ask=0.52, no_ask=0.50)
    rec = record(band_50_75=bucket(9, 4, -0.81))
    c = context(v, [GREY_L], rec)
    assert c["text"] == "Coin flip. Passing fits your record."
    assert c["details"] == ["No tested rule is setting up in this window.",
                            "Your trades at 50–75¢: −9¢ per contract over 9 trades."]
    assert context(v, [AMBER_L], rec) is None
    assert context(v, [], record(band_50_75=bucket(3, 0, -0.9))) is None  # only 3 trades: no strong claim
    assert context({**v, "call": {"action": "ENTER UP", "mid_up": 0.7}}, [], rec) is None


def test_hold_note():
    rec = record(early_exits={**bucket(6, 2, -3.0), "vs_hold_usd": 32.0})
    n = hold_note({"stop_level": 76_560.0}, "CUSHION100", rec)
    assert n["text"] == "The rule says hold. Your stop is set."
    assert n["details"] == ["CUSHION100 holds to settlement. Your BTC stop is at $76,560.",
                            "Your early sells have cost about $32.00 against holding (6 trades)."]
    assert hold_note({"stop_level": None}, "H4", record())["text"] == "The rule says hold. No stop is set."
    assert hold_note({"stop_level": None}, None, rec)["text"] == "Holding has beaten your early sells."
    assert hold_note({"stop_level": None}, None, record()) is None


# -- the service ----------------------------------------------------------------------------------------------------------
def world(store):
    r = Rig(store)
    yours = YourRecord(store, {"you": MODES})
    return r, Copilot(r.play, r.ag.rule_book, yours)


def green_book(r):
    """CUSHION100 green on the rig: BTC $120 over, Up at 86c (10:00 left, the top of its window)."""
    r.play.index_fn = lambda: (NOW, K + 120)
    r.ex.market.set(0.84, 0.14)  # YES 84c bid / 86c ask


def test_the_view_full_and_quiet(store):
    r, cp = world(store)
    green_book(r)
    full = cp.view(NOW, "kalshi", "15m", mode="full", warnings=["late"])
    by = {x["rule"]: x["state"] for x in full["lights"]}
    assert by == {"H4": "grey", "CUSHION100": "green", "LATERESID": "grey"}  # HPANIC retired: no light
    assert full["line"]["none"]["text"] == "CUSHION100 fits this window." and full["line"]["UP"]["kind"] == "ready"
    f = full["fill"]
    assert (f["rule"], f["side"], f["max_price"], f["stop_index_below"]) == ("CUSHION100", "UP", 0.88, K + 40)
    assert full["btc"]["gap"] == 120 and full["btc"]["left_ms"] == CLOSE - NOW
    quiet = cp.view(NOW, "kalshi", "15m", mode="quiet", warnings=[])
    assert quiet["lights"] == [] and quiet["fill"] is None and quiet["line"]["none"] is None
    with pytest.raises(ValueError, match="off"):
        cp.view(NOW, "kalshi", "15m", mode="off", warnings=[])
    for span in ("5m", "1h"):  # Kalshi has no 5-minute or hourly market: unavailable, as Play answers it
        none = cp.view(NOW, "kalshi", span, mode="full", warnings=[])
        assert none["available"] is False and none["market"] is None and none["reason"]
        assert none["lights"] == [] and none["fill"] is None and none["line"]["none"] is None
    for venue, span in (("kalshi", "2h"), ("binance", "15m")):  # an unknown span or venue is still an error
        with pytest.raises(ValueError):
            cp.view(NOW, venue, span, mode="full", warnings=[])
    assert full["available"] is True and full["reason"] is None


def test_a_retired_rule_vanishes_from_the_view(store):
    r, cp = world(store)
    green_book(r)
    r.ag.rule_book.set("CUSHION100", "retired", NOW, by="user", reason="test")
    v = cp.view(NOW, "kalshi", "15m", mode="full", warnings=[])
    assert [x["rule"] for x in v["lights"]] == ["H4", "LATERESID"] and v["fill"] is None


def test_record_shown_once_per_bracket(store):
    r, cp = world(store)
    store.insert("trading_brackets", [{**bracket(1, state="open"), "strategy": "you", "env": "shadow"}])
    shown = {"mode": "full", "side": "UP", "ask": 0.86, "lights": [{"rule": "CUSHION100", "state": "green"}],
             "line": {"kind": "ready", "text": "CUSHION100 fits this window.", "rule": "CUSHION100"},
             "warning": None, "filled": {"rule": "CUSHION100", "side": "UP", "max_price": 0.88, "stop": K + 40},
             "btc": K + 120, "strike": K, "btc_gap": 120.0, "left_ms": 600_000}
    assert cp.record_shown("you", "b1", shown, NOW) == {"ok": True, "repeat": False}
    assert cp.record_shown("you", "b1", shown, NOW) == {"ok": True, "repeat": True}
    rows = store.select("trading_audit", {"event": "copilot_shown"})
    assert len(rows) == 1 and rows[0]["detail"] == shown and rows[0]["env"] == "shadow"
    assert rows[0]["code"] == "ready" and rows[0]["strategy"] == "you"
    assert cp.record_shown("you", "nope", shown, NOW)["code"] == "unknown_bracket"
    assert cp.record_shown("you_poly", "b1", shown, NOW)["code"] == "unknown_bracket"
    for bad in ({**shown, "mode": "off"}, {**shown, "lights": [{"rule": "X", "state": "green"}]},
                {**shown, "warning": {"keys": ["lucky"], "text": "t"}}, {**shown, "filled": {"rule": "X"}}):
        with pytest.raises(ValueError):
            cp.record_shown("you", "b1", bad, NOW)
