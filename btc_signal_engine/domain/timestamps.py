"""The one ISO-8601 / RFC 3339 date-time parser (pure): venue timestamps -> integer epoch milliseconds.

Every venue adapter reads its date-times here (review fix plan 2026-10-31, P2 "DRY"; before this, Kalshi, Coinbase,
the margin client and Polymarket each had a copy, and Polymarket's went through a float and could land 1 ms early).

* Exact: the difference from the epoch in integer milliseconds; digits past the microsecond are cut (Python reads
  only six), never rounded, so "...:30.123456789Z" is ...:30.123.
* An offset is required ("Z", "z" or "+hh:mm"): a time without one raises, unless the caller states the venue's
  convention with `naive="utc"` (Polymarket's times are UTC; a missing "Z" is read as UTC there, as before).
* `iso_to_ms` raises ValueError on anything it cannot read; `iso_to_ms_or_none` returns None for an empty or
  unreadable value (an optional field: a fill's time that is not there yet).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MS = timedelta(milliseconds=1)


def iso_to_ms(value: Any, *, naive: str | None = None) -> int:
    """An RFC 3339 date-time ("2026-10-31T12:00:00.123Z", "...+00:00") -> epoch ms (module docstring)."""
    if naive not in (None, "utc"):
        raise ValueError(f"unknown naive-time convention {naive!r}")
    if not isinstance(value, str) or "T" not in value.upper():
        raise ValueError(f"not an RFC3339 date-time: {value!r}")
    text = value
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"not an RFC3339 date-time: {value!r}") from None
    if dt.tzinfo is None:
        if naive != "utc":
            raise ValueError(f"RFC3339 date-time without an offset: {value!r}")
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // _MS


def iso_to_ms_or_none(value: Any, *, naive: str | None = None) -> int | None:
    """`iso_to_ms`, or None when the value is empty or unreadable."""
    if not value:
        return None
    try:
        return iso_to_ms(value, naive=naive)
    except ValueError:
        return None
