"""The one date-time parser (domain/timestamps.py), characterized against the copies it replaces (main at a17bab7).

Kalshi, Coinbase and the margin client: unchanged on every value they accepted. Polymarket: unchanged for its "Z"
times, except the named fix: the old copy went through a float (`timestamp() * 1000`, then int) and could land 1 ms
early; the new one is exact. It also no longer misreads a time that carries its own offset.
"""

from __future__ import annotations

import datetime as dt
import random

import pytest

from btc_signal_engine.domain.timestamps import iso_to_ms, iso_to_ms_or_none

EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)


def old_kalshi(iso):  # adapters/kalshi/client.py, adapters/coinbase/rest.py and stream.py
    return (dt.datetime.fromisoformat(iso.replace("Z", "+00:00")) - EPOCH) // dt.timedelta(milliseconds=1)


def old_polymarket(s):  # adapters/polymarket_us/market_data.py, trading.py, recorders.py
    if not s:
        return None
    try:
        return int(dt.datetime.fromisoformat(str(s)[:26].rstrip("Z") + "+00:00").timestamp() * 1000)
    except ValueError:
        return None


def samples(n=4000):
    rng = random.Random(11)
    out = []
    for _ in range(n):
        t = dt.datetime(2026, 1, 1, tzinfo=dt.UTC) + dt.timedelta(seconds=rng.randrange(0, 365 * 86400),
                                                                  microseconds=rng.randrange(0, 1_000_000))
        frac = rng.choice(["", f".{t.microsecond // 1000:03d}", f".{t.microsecond:06d}",
                           f".{t.microsecond:06d}{rng.randrange(0, 1000):03d}"])
        out.append(t.strftime("%Y-%m-%dT%H:%M:%S") + frac + "Z")
    return out


SAMPLES = samples()


def test_kalshi_and_coinbase_times_are_unchanged():
    for s in SAMPLES:
        assert iso_to_ms(s) == old_kalshi(s)
        assert iso_to_ms(s.replace("Z", "+00:00")) == old_kalshi(s)


def test_polymarket_times_are_unchanged_but_for_the_float_millisecond():
    early = 0
    for s in SAMPLES:
        new, old = iso_to_ms_or_none(s, naive="utc"), old_polymarket(s)
        assert new == iso_to_ms(s)
        if new != old:
            early += 1
            assert old == new - 1  # the float path's truncation, never anything else
    naive = SAMPLES[0][:-1]  # a Polymarket time without its "Z" is still UTC there
    assert iso_to_ms_or_none(naive, naive="utc") == old_polymarket(naive) == iso_to_ms(SAMPLES[0])
    assert early < len(SAMPLES)  # how often the old copy was 1 ms early is data-dependent; it is never late


def test_the_float_path_was_one_millisecond_early():
    s = "2026-10-29T12:05:30.001Z"
    assert iso_to_ms(s) % 1000 == 1
    found = [x for x in SAMPLES if old_polymarket(x) != iso_to_ms(x)]
    assert all(old_polymarket(x) == iso_to_ms(x) - 1 for x in found)


def test_an_offset_is_honoured_not_dropped():
    assert iso_to_ms("2026-10-30T14:00:00.5+02:00") == iso_to_ms("2026-10-30T12:00:00.500Z") == 1793361600500
    assert iso_to_ms_or_none("2026-10-30T14:00:00.123456+02:00", naive="utc") == 1793361600123
    assert old_polymarket("2026-10-30T14:00:00.123456+02:00") == 1793368800123  # fix: was read as UTC


@pytest.mark.parametrize("value", ["2026-10-30T12:00:00", "2026-10-30", "", None, 1793361600, "garbage", "T"])
def test_unreadable_or_offsetless_times_raise_and_the_optional_reader_says_none(value):
    with pytest.raises(ValueError):
        iso_to_ms(value)
    if value != "2026-10-30T12:00:00":
        assert iso_to_ms_or_none(value, naive="utc") is None
    assert iso_to_ms_or_none(value) is None


def test_unknown_naive_convention_is_an_error():
    with pytest.raises(ValueError):
        iso_to_ms("2026-10-30T12:00:00Z", naive="local")
