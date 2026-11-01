"""Startup's database reads: a transient error (a statement timeout on a cold cache) is retried a bounded number of
times with loud warnings; anything else, or the last failure, stops the start with an error line naming the read.

Every trade row written here is SYNTHETIC test data, built only for these tests.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from sqlalchemy.exc import OperationalError, ProgrammingError

from btc_signal_engine.adapters import retry
from btc_signal_engine.bootstrap import app as app_mod
from btc_signal_engine.bootstrap.app import Runtime, startup_read
from btc_signal_engine.bootstrap.config import Settings
from btc_signal_engine.domain.models.events import Trade


def timeout() -> OperationalError:
    return OperationalError("SELECT ...", {}, Exception("canceling statement due to statement timeout"))


@pytest.fixture
def logs(monkeypatch, caplog):
    lg = logging.getLogger("btc_signal_engine")  # other tests may have detached it (dictConfig / propagate)
    monkeypatch.setattr(lg, "disabled", False)
    monkeypatch.setattr(lg, "propagate", True)
    caplog.set_level(logging.WARNING)
    return caplog


@pytest.fixture
def waits(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(app_mod.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(retry, "_uniform", lambda: 1.0)  # the top of each jittered draw: the doubling ceilings
    return slept


def flaky(failures: list[BaseException], value=7):
    calls = []

    def read():
        calls.append(1)
        if failures:
            raise failures.pop(0)
        return value
    return read, calls


def test_a_timeout_is_retried_with_doubling_waits_and_a_warning_each_time(logs, waits):
    read, calls = flaky([timeout(), timeout()])
    assert asyncio.run(startup_read("the probe", read, attempts=3, backoff_s=2.0)) == 7
    assert len(calls) == 3 and waits == [2.0, 4.0]
    warned = [r.getMessage() for r in logs.records if r.levelno == logging.WARNING]
    assert len(warned) == 2 and all("the probe" in m and "statement timeout" in m for m in warned)
    assert "attempt 1 of 3" in warned[0]


def test_the_last_failure_is_raised_after_an_error_naming_the_read(logs, waits):
    read, calls = flaky([timeout(), timeout(), timeout()])
    with pytest.raises(OperationalError):
        asyncio.run(startup_read("the probe", read, attempts=3, backoff_s=2.0))
    assert len(calls) == 3 and waits == [2.0, 4.0]
    (err,) = [r for r in logs.records if r.levelno == logging.ERROR]
    assert "the probe" in err.getMessage() and "after 3 attempt(s)" in err.getMessage()


def test_a_non_transient_error_is_not_retried(logs, waits):
    read, calls = flaky([ProgrammingError("SELECT ...", {}, Exception("relation does not exist"))])
    with pytest.raises(ProgrammingError):
        asyncio.run(startup_read("the probe", read))
    assert len(calls) == 1 and waits == []
    assert any(r.levelno == logging.ERROR and "the probe" in r.getMessage() for r in logs.records)


def test_attempts_below_one_are_an_error(waits):
    with pytest.raises(ValueError, match="attempts"):
        asyncio.run(startup_read("the probe", lambda: 1, attempts=0))


@pytest.fixture
def rt(tmp_path, monkeypatch):
    for k in ("BTCFLOW_TRADING_STREAM", "BTCFLOW_API_HOST", "BTCFLOW_ACCESS_PIN", "BTCFLOW_POLYMARKET_KEY_ID",
              "BTCFLOW_POLYMARKET_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    r = Runtime(Settings(_env_file=None, db_path=tmp_path / "startup.db", trading_stream=False))
    yield r
    r.writer.close()


def offline(monkeypatch):
    calls = []

    async def backfill(*a, **k):
        calls.append("backfill")
        return 0

    async def nothing(*a, **k):
        return 0

    monkeypatch.setattr(app_mod, "backfill", backfill)
    monkeypatch.setattr(app_mod, "repair_gaps", nothing)
    monkeypatch.setattr(app_mod, "backfill_kalshi", nothing)
    return calls


def test_startup_survives_one_probe_timeout_and_still_sees_the_stored_history(rt, monkeypatch, logs, waits):
    backfills = offline(monkeypatch)
    now = rt.clock.now_ms()
    rt.store.add_trades([Trade(41, now - 60_000, now - 60_000, 100_000.0, 0.01, True)])
    real = rt.store.has_trades_since
    failures = [timeout()]

    def probe(ts):
        if failures:
            raise failures.pop(0)
        return real(ts)

    monkeypatch.setattr(rt.store, "has_trades_since", probe)
    asyncio.run(rt.startup())
    assert backfills == []  # the retried probe saw the recent trade: no warm-up backfill
    assert rt._hydrated_max_id == 41
    assert any("recent-trades probe" in r.getMessage() for r in logs.records if r.levelno == logging.WARNING)


def test_has_trades_since_on_either_dialect(store):
    store.add_trades([Trade(1, 1_000, 1_000, 100.0, 0.1, True), Trade(2, 5_000, 5_000, 100.0, 0.1, False)])
    assert store.has_trades_since(5_000) is True
    assert store.has_trades_since(5_001) is False
    assert store.has_trades_since(0) is True
