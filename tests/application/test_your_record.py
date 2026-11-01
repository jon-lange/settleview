"""Your record (application/your_record.py): the user's own closed Play trades in the warnings' buckets."""

from __future__ import annotations

import pytest

from btc_signal_engine.application.trading import MODES
from btc_signal_engine.application.your_record import YourRecord, against, stats

W_END = 1_793_207_700_000  # a 15-minute close
STRIKE = 80_000.0
PM_MODES = {"shadow": "pm_shadow", "prod": "pm_prod"}
STRATS = {"you": MODES, "you_poly": PM_MODES, "you_poly5": PM_MODES}


def bracket(i: int, *, side="UP", entry_avg=0.60, filled=2.0, proceeds=2.0, left_s=400, strategy="you", env="prod",
            exit_reason="settled", state="closed", session_id=None, window_end=W_END) -> dict:
    created = window_end - left_s * 1000
    cost = round(filled * entry_avg + 0.02, 4)
    return {"id": f"b{i}", "env": env, "session_id": session_id, "kind": "entry", "ticker": "KXBTC15M-T", "side": side,
            "window_end": window_end, "stake": 5.0, "contracts": filled, "limit_price": entry_avg, "take_profit": 0.99,
            "stop": 0.0, "max_cost": cost, "ticket_hash": "h", "state": state, "filled": filled, "entry_avg": entry_avg,
            "entry_cost": cost, "held": 0.0, "proceeds": proceeds, "exit_reason": exit_reason, "created_ts": created,
            "updated_ts": created, "closed_ts": window_end, "strategy": strategy}


def index(store, ts: int, price: float) -> None:
    store.insert("index_1s", [{"ts": ts, "price": price, "last_trade_ts": ts}])


def kalshi(store, result="yes", close=W_END) -> None:
    store.insert("kalshi_markets", [{"ticker": "KXBTC15M-T", "open_ts": close - 900_000, "close_ts": close,
                                     "status": "finalized", "floor_strike": STRIKE, "result": result}])


def test_against_is_btc_on_the_other_side_of_the_price_to_beat():
    assert against("UP", -5) and against("DOWN", 5)
    assert not against("UP", 5) and not against("DOWN", -5)
    assert against("UP", 0) is False and against("DOWN", 0) is False
    assert against("UP", None) is None


def test_the_buckets_from_the_database(store):
    kalshi(store, result="no")  # DOWN won
    rows = [
        bracket(1, side="UP", entry_avg=0.60, proceeds=0.0, left_s=400),  # BTC below: against, band, lost
        bracket(2, side="DOWN", entry_avg=0.40, proceeds=2.0, left_s=120),  # BTC below: with BTC, late, won
        bracket(3, side="UP", entry_avg=0.80, proceeds=1.0, left_s=500, exit_reason="manual"),  # sold early
        bracket(4, side="UP", entry_avg=0.70, proceeds=0.0, left_s=400, session_id="agent1"),  # an agent's: out
        bracket(5, side="UP", entry_avg=0.70, proceeds=0.0, left_s=400, state="open"),  # not closed: out
        bracket(6, side="UP", entry_avg=0.70, proceeds=0.0, left_s=400, env="shadow"),  # another mode: out
        bracket(7, side="UP", entry_avg=0.70, proceeds=0.0, left_s=400, filled=0.0),  # no fill: out
    ]
    store.insert("trading_brackets", rows)
    for r in rows:
        index(store, r["created_ts"] - 2000, STRIKE - 30)  # BTC $30 below the strike at every entry
    s = YourRecord(store, STRATS).stats("prod")
    assert s["all"]["n"] == 3
    ag = s["against_btc"]
    assert (ag["n"], ag["wins"]) == (2, 0) and ag["net_usd"] == pytest.approx(-1.22 + (1.0 - 1.62))
    late = s["late"]
    assert (late["n"], late["wins"]) == (1, 1) and late["net_c"] == pytest.approx((2.0 - 0.82) / 2 * 100)
    band = s["band_50_75"]
    assert band["n"] == 1 and band["net_usd"] == pytest.approx(-1.22)
    ee = s["early_exits"]
    assert ee["n"] == 1 and ee["vs_hold_usd"] == pytest.approx((0.0 - 1.62) - (1.0 - 1.62))  # UP lost: selling saved $1
    assert s["unknown_gap"] == 0
    with pytest.raises(ValueError, match="no phone strategy has the mode"):
        YourRecord(store, STRATS).stats("sandboxx")


def test_unknown_btc_or_strike_is_left_out_of_against(store):
    # a 5-minute Polymarket trade: no recorded strike; a 15-minute one with no index row near the entry
    store.insert("trading_brackets", [bracket(1, strategy="you_poly5", env="pm_prod", window_end=W_END - 300_000),
                                      bracket(2, strategy="you")])
    kalshi(store)
    s = YourRecord(store, STRATS).stats("prod")
    assert s["all"]["n"] == 2 and s["against_btc"]["n"] == 0 and s["unknown_gap"] == 2


def test_the_shown_record_gives_btc_vs_the_price_to_beat(store):
    store.insert("trading_brackets", [bracket(1, strategy="you_poly5", env="pm_prod", side="DOWN", proceeds=0.0)])
    store.insert("trading_audit", [{"ts": 1, "event": "copilot_shown", "env": "pm_prod", "bracket_id": "b1",
                                    "strategy": "you_poly5", "detail": {"btc_gap": 12.5}}])
    s = YourRecord(store, STRATS).stats("prod")
    assert s["against_btc"]["n"] == 1 and s["unknown_gap"] == 0


def test_an_unknown_strategy_is_an_error(store):
    with pytest.raises(ValueError):
        YourRecord(store, {"taker": MODES})


def test_stats_is_pure_and_counts_honestly():
    assert stats([])["against_btc"] == {"n": 0, "wins": 0, "net_usd": 0, "contracts": 0, "net_c": None}


def test_a_result_that_lands_after_the_first_read_is_picked_up(store):
    """Review B5#3: the first read of a trade before its official result was recorded was kept for good (an "unknown"
    until a restart). Now an unfinished 15-minute trade is read again (at most every RETRY_MS) until it is final."""
    from btc_signal_engine.application.your_record import RETRY_MS

    clock = {"t": W_END + 60_000}
    store.insert("trading_brackets", [bracket(1, side="UP", exit_reason="manual", proceeds=1.0)])
    index(store, W_END - 400_000 - 2000, STRIKE - 30)
    rec = YourRecord(store, STRATS, now_fn=lambda: clock["t"])
    assert rec.stats("prod")["early_exits"]["n"] == 0  # no result recorded yet: not counted as an early exit
    kalshi(store, result="no")
    assert rec.stats("prod")["early_exits"]["n"] == 0  # read again only after RETRY_MS
    clock["t"] += RETRY_MS
    s = rec.stats("prod")
    assert s["early_exits"]["n"] == 1 and s["against_btc"]["n"] == 1  # the result and the strike now count
    assert "b1" in rec._seen  # final now: kept


def test_an_unknown_becomes_final_after_the_cutoff_and_other_spans_are_final_at_once(store):
    from btc_signal_engine.application.your_record import FINAL_AFTER_MS

    clock = {"t": W_END + FINAL_AFTER_MS}
    store.insert("trading_brackets", [bracket(1), bracket(2, strategy="you_poly5", env="pm_prod")])
    rec = YourRecord(store, STRATS, now_fn=lambda: clock["t"])
    assert rec.stats("prod")["unknown_gap"] == 2
    assert set(rec._seen) == {"b1", "b2"} and not rec._pending
