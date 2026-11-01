"""The user's day: one definition of "today" for every money figure (Play's day limit and P&L, Record, the agents'
global limits, the Telegram daily summary).

A day runs from local midnight to local midnight in ONE named zone (an IANA name such as "America/Chicago"), resolved
once at startup (bootstrap: the process's `TZ`, which compose sets; unset = "UTC", said so on screen). There is no
"this machine's zone" path: a container's own zone is UTC whatever the user's is, and a fixed UTC offset carried
across a daylight-saving change puts midnight an hour off (review B5#5). Pure: no clock, no I/O."""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_ZONE = "UTC"


def zone(name: str | None) -> ZoneInfo:
    """The zone named `name` (a leading ":" as in `TZ=:America/Chicago` is dropped). None or blank = UTC, the stated
    default. An unknown name is an error naming it, never another zone."""
    raw = (name or "").strip().lstrip(":").strip()
    if not raw:
        return ZoneInfo(DEFAULT_ZONE)
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown time zone {raw!r}: use an IANA name such as 'America/Chicago' or 'UTC'") from None


def zone_name(tz: tzinfo) -> str:
    return getattr(tz, "key", None) or str(tz)


def day_start(now_ms: int, tz: tzinfo, days_back: int = 0) -> int:
    """Local midnight (in `tz`) of the day holding `now_ms`, or of `days_back` local days before it. By calendar
    date, so a day is 23 or 25 hours across a daylight-saving change. A midnight that does not exist (a zone that
    springs forward at 00:00) is the first instant of that date. `days_back` steps calendar days, never 24 h."""
    if tz is None:
        raise ValueError("day_start needs a zone (domain/days.py): there is no implicit local zone")
    d = datetime.fromtimestamp(now_ms / 1000, tz).date() - timedelta(days=days_back)
    midnight = datetime(d.year, d.month, d.day, tzinfo=tz)
    # A skipped midnight (fold=0) is read with the offset before the change: the instant the date begins.
    return int(midnight.timestamp() * 1000)
