"""The user's day (domain/days.py): one zone, local midnights by calendar date, across daylight-saving changes."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from btc_signal_engine.domain import days


def ms(y, mo, d, h=0, mi=0, tz=UTC) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=tz).timestamp() * 1000)


def test_zone_names_and_the_stated_default():
    assert days.zone_name(days.zone(None)) == "UTC" and days.zone_name(days.zone("  ")) == "UTC"
    assert days.zone_name(days.zone(":America/Chicago")) == "America/Chicago"  # TZ's leading colon form
    with pytest.raises(ValueError, match="unknown time zone 'Mars/Olympus'"):
        days.zone("Mars/Olympus")


def test_today_starts_at_local_midnight_not_utc():
    chi = days.zone("America/Chicago")
    now = ms(2026, 10, 30, 3, 0)  # 22:00 on Oct 29 in Chicago (UTC-5)
    assert days.day_start(now, chi) == ms(2026, 10, 29, 5, 0)  # Chicago's midnight = 05:00 UTC
    assert days.day_start(now, days.zone("UTC")) == ms(2026, 10, 30)


def test_the_day_daylight_saving_ends_is_25_hours_and_the_week_steps_by_date():
    """Review B5#5: a fixed UTC offset carried across the change put midnight an hour off."""
    ny = days.zone("America/New_York")
    now = ms(2026, 11, 1, 23, 0)  # 18:00 EST on Nov 1 (the clocks went back at 02:00 EDT that morning)
    today = days.day_start(now, ny)
    assert today == ms(2026, 11, 1, 4, 0)  # midnight EDT (UTC-4)
    assert days.day_start(today + 25 * 3_600_000 - 1, ny) == today  # Nov 1 has 25 hours
    assert days.day_start(now, ny, 6) == ms(2026, 10, 26, 4, 0)  # six days back: midnight EDT, not 23:00
    spring = ms(2026, 3, 9, 12, 0)  # the day after the clocks went forward (Mar 8)
    assert days.day_start(spring, ny, 1) == ms(2026, 3, 8, 5, 0)  # Mar 8's midnight was still EST (UTC-5)


def test_a_skipped_midnight_is_the_first_instant_of_the_date():
    """A zone that springs forward at 00:00 (America/Santiago, Sep 6 2026: 00:00 -> 01:00)."""
    scl = days.zone("America/Santiago")
    now = ms(2026, 9, 6, 15, 0)
    start = days.day_start(now, scl)
    assert datetime.fromtimestamp(start / 1000, scl).date().isoformat() == "2026-09-06"
    assert datetime.fromtimestamp((start - 1) / 1000, scl).date().isoformat() == "2026-09-05"


def test_no_zone_is_an_error_never_the_machines_zone():
    with pytest.raises(ValueError, match="needs a zone"):
        days.day_start(ms(2026, 11, 1), None)  # type: ignore[arg-type]
