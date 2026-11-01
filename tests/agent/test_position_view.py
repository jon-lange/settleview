"""The Agent panel's per-position figures (agent_sessions.position_view, used by bootstrap `_agent_state`): the exact
entry cost (contracts x price + the entry fee), the fees and the realized net, so trade rows need no caveat."""

from __future__ import annotations

from btc_signal_engine.application.agent_sessions import position_view
from btc_signal_engine.application.rule_scores import trade_of


def row(**kw) -> dict:
    return {"id": "b1", "strategy": "you", "ticker": "T", "side": "UP", "state": "open", "filled": 0.0,
            "entry_avg": None, "entry_cost": None, "entry_fees": None, "max_cost": 1.0, "take_profit": 0.99,
            "exit_reason": None, "proceeds": 0.0, "tp_fees": 0.0, "exit_fees": 0.0, "no_tp": True,
            "stop_index_below": 76_560.0, "stop_index_above": None, "window_end": 2, "created_ts": 1,
            "closed_ts": None, "ticket_hash": "never shown", **kw}


def test_an_unfilled_position_has_no_cost_fees_or_result():
    v = position_view(row())
    assert v["entry_cost"] is None and v["fees"] is None and v["realized"] is None
    assert v["take_profit"] is None  # no take-profit order: it rides to settlement
    assert "ticket_hash" not in v


def test_a_settled_position_carries_its_exact_cost_fees_and_net():
    b = row(state="closed", filled=1.16, entry_avg=0.86, entry_cost=1.0, entry_fees=0.0024, proceeds=1.16,
            exit_reason="settled", closed_ts=3)
    v = position_view(b)
    assert v["entry_cost"] == 1.0 and v["entry_fees"] == 0.0024 and v["fees"] == 0.0024
    assert v["realized"] == 0.16
    assert v["realized"] == trade_of({"rule": "CUSHION100", "market": "kalshi_15m"}, b)["net"]  # the scorekeeper's


def test_a_sold_position_counts_its_exit_fees_and_a_missing_entry_fee_is_derived():
    v = position_view(row(state="closed", filled=2.0, entry_avg=0.5, entry_cost=1.02, entry_fees=None, proceeds=0.8,
                          exit_fees=0.03, exit_reason="exit", no_tp=False))
    assert v["entry_fees"] == 0.02 and v["fees"] == 0.05 and v["realized"] == -0.25
    assert v["take_profit"] == 0.99
