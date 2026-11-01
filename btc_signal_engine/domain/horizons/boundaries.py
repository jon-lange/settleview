"""Clock-aligned boundary math. All times are UTC milliseconds."""

from __future__ import annotations

MINUTE_MS = 60_000


def floor_minute(ts: int) -> int:
    return ts - ts % MINUTE_MS


def is_boundary(ts: int, horizon_seconds: int) -> bool:
    return ts % (horizon_seconds * 1000) == 0


def next_boundary(now: int, horizon_seconds: int) -> int:
    """The first boundary strictly after `now` for this horizon."""
    step = horizon_seconds * 1000
    return (now // step + 1) * step


def minute_boundaries(start: int, end: int) -> list[int]:
    """Whole-minute boundaries in (start, end]."""
    first = floor_minute(start) + MINUTE_MS
    return list(range(first, end + 1, MINUTE_MS))
