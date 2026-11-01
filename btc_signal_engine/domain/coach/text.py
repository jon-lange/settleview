"""Plain-English helpers for the Coach's decision rules (domain/coach/rules.py, also used by the research scripts)."""

from __future__ import annotations

from btc_signal_engine.domain import fmt


def cents(x: float) -> str:
    return fmt.cents(x)


def pct(p: float) -> str:
    return f"{p * 100:.0f}%"


def contract(side: str) -> str:
    """The contract that pays if the window goes `side`, named as Kalshi's app shows it (Up = YES, Down = NO)."""
    return "Up" if side == "UP" else "Down"
