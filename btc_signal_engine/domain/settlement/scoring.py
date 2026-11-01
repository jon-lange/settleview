"""Turn a call plus its window's settled direction into WIN / LOSS / TIE / ... (R10)."""

from __future__ import annotations

from btc_signal_engine.domain.models.decisions import Call, Result

WINDOW_PENDING = "PENDING"
WINDOW_RESOLVED = "RESOLVED"
WINDOW_UNRESOLVED = "UNRESOLVED"


def score(call: Call | str, window_status: str | None, actual: Call | str | None, tie: bool) -> Result:
    call = Call(call)
    if call in (Call.SKIP, Call.MISSED):
        return Result.NOT_SCORED
    if window_status in (None, WINDOW_PENDING):
        return Result.PENDING
    if window_status == WINDOW_UNRESOLVED:
        return Result.UNRESOLVED
    if actual is None:
        return Result.TIE if tie else Result.UNRESOLVED
    return Result.WIN if call == Call(actual) else Result.LOSS
