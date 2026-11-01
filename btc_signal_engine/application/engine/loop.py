"""Boundary timing (U7, KTD14): when to decide, and which boundaries were missed while asleep."""

from __future__ import annotations

from btc_signal_engine.domain.horizons.boundaries import MINUTE_MS, floor_minute

LATENESS_MS = 2_000  # wait this long after a boundary before freezing its snapshot (KTD5)
MISS_GRACE_MS = 10_000  # a boundary noticed later than this is MISSED, never computed late


def due_boundaries(last_done: int | None, now: int) -> tuple[list[int], int | None]:
    """(boundaries to record as MISSED, boundary to decide now or None) given the clock.

    `last_done` is the last boundary handled (decided or missed); None on first start.
    """
    ready = floor_minute(now - LATENESS_MS)  # newest boundary whose lateness budget has passed
    if last_done is None:
        last_done = ready - MINUTE_MS
    pending = list(range(last_done + MINUTE_MS, ready + 1, MINUTE_MS))
    if not pending:
        return [], None
    newest = pending[-1]
    if now - newest <= LATENESS_MS + MISS_GRACE_MS:
        return pending[:-1], newest
    return pending, None
