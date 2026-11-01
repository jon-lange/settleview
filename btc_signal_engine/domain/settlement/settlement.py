"""KTD6 settlement: 60-second index averages at window open and close, Kalshi tie rule."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from btc_signal_engine.domain.models.decisions import Call, Settlement
from btc_signal_engine.domain.models.events import IndexSecond

AVG_WINDOW_MS = 60_000
STALENESS_MS = 5_000
MIN_FRESH_SECONDS = 54

TiePolicy = Literal["kalshi", "push"]


def to_cents(x: float) -> float:
    return float(Decimal(repr(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def window_average(rows: Sequence[IndexSecond], end: int) -> float | None:
    """Mean index price over [end − 60s, end), rounded to cents.

    Valid only when at least 54 of the 60 seconds carry a price no older than 5s;
    otherwise None (the coverage rule makes the outcome UNRESOLVED).
    """
    start = end - AVG_WINDOW_MS
    in_window = [r for r in rows if start <= r.ts < end]
    fresh = sum(1 for r in in_window if r.ts - r.last_trade_ts <= STALENESS_MS)
    if fresh < MIN_FRESH_SECONDS:
        return None
    return to_cents(sum(r.price for r in in_window) / len(in_window))


def settle_window(horizon_seconds: int, boundary: int, rows: Sequence[IndexSecond]) -> Settlement:
    """Open average ends at the boundary; close average ends at boundary + horizon."""
    return Settlement(
        horizon=horizon_seconds,
        boundary=boundary,
        open_avg=window_average(rows, boundary),
        close_avg=window_average(rows, boundary + horizon_seconds * 1000),
    )


def actual_direction(open_avg: float, close_avg: float, tie_policy: TiePolicy) -> tuple[Call | None, bool]:
    """(direction that happened, was it an exact tie). Under `push` a tie has no direction."""
    tie = close_avg == open_avg
    if tie and tie_policy == "push":
        return None, True
    return (Call.UP if close_avg >= open_avg else Call.DOWN), tie
