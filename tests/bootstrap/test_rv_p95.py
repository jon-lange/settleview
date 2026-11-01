"""The engine's rv_p95 (the 95th percentile of the last 7 days' rv_60) is a cached value refreshed off the loop.

Every snapshot row written here is SYNTHETIC test data, built only for these tests.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from btc_signal_engine.bootstrap.app import Runtime
from btc_signal_engine.bootstrap.config import Settings


@pytest.fixture
def rt(tmp_path, monkeypatch):
    for k in ("BTCFLOW_TRADING_STREAM", "BTCFLOW_API_HOST", "BTCFLOW_ACCESS_PIN", "BTCFLOW_POLYMARKET_KEY_ID",
              "BTCFLOW_POLYMARKET_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    r = Runtime(Settings(_env_file=None, db_path=tmp_path / "rv.db", trading_stream=False))
    yield r
    r.writer.close()


def seed_rv(rt, values):
    now = rt.clock.now_ms()
    rt.store.insert("snapshots", [{"ts": now - 1_000 * (i + 1), "feature_version": "t", "features": {"rv_60": v},
                                   "availability": {}, "backfilled": False} for i, v in enumerate(values)])


def test_none_before_the_first_refresh_and_reading_never_scans(rt, monkeypatch):
    seed_rv(rt, [float(i) for i in range(600)])

    def no_scan(*a, **k):
        raise AssertionError("rv_p95() must not read the store")

    monkeypatch.setattr(rt.store, "select", no_scan)
    assert rt.rv_p95() is None


def test_refresh_sets_the_95th_percentile_with_600_or_more_values(rt):
    seed_rv(rt, [float(i) for i in range(600)])  # 0..599: index int(0.95 * 599) = 569
    asyncio.run(rt.refresh_rv_p95())
    assert rt.rv_p95() == 569.0


def test_fewer_than_600_values_give_none(rt):
    seed_rv(rt, [float(i) for i in range(599)])
    asyncio.run(rt.refresh_rv_p95())
    assert rt.rv_p95() is None


def test_a_failed_refresh_keeps_the_previous_value_and_warns(rt, monkeypatch, caplog):
    lg = logging.getLogger("btc_signal_engine")  # other tests may have detached it (dictConfig / propagate)
    monkeypatch.setattr(lg, "disabled", False)
    monkeypatch.setattr(lg, "propagate", True)
    seed_rv(rt, [float(i) for i in range(600)])
    asyncio.run(rt.refresh_rv_p95())

    def broken(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(rt.store, "select", broken)
    with caplog.at_level(logging.WARNING):
        asyncio.run(rt.refresh_rv_p95())
    assert rt.rv_p95() == 569.0
    assert any(r.levelno == logging.WARNING and "rv_p95" in r.getMessage() for r in caplog.records)


def test_the_refresh_runs_as_a_background_loop(rt):
    assert rt.loops()["rv_p95"] == rt.rv_p95_loop


def test_startup_computes_rv_p95_before_any_loop_runs(rt, monkeypatch):
    """Right after a restart the first boundaries must see the same rv_p95 as before it (skip_high_vol)."""
    from btc_signal_engine.bootstrap import app as app_mod

    async def offline(*a, **k):  # no network in tests: the startup backfills do nothing
        return 0

    for name in ("backfill", "repair_gaps", "backfill_kalshi"):
        monkeypatch.setattr(app_mod, name, offline)
    seed_rv(rt, [float(i) for i in range(600)])
    asyncio.run(rt.startup())
    assert rt.rv_p95() == 569.0


def test_the_loop_waits_a_full_period_before_its_first_refresh(rt, monkeypatch):
    from btc_signal_engine.bootstrap import app as app_mod

    monkeypatch.setattr(app_mod, "RV_P95_REFRESH_S", 0.05)
    calls = []

    async def counted():
        calls.append(asyncio.get_running_loop().time())

    monkeypatch.setattr(rt, "refresh_rv_p95", counted)

    async def scenario():
        t0 = asyncio.get_running_loop().time()
        task = asyncio.create_task(rt.rv_p95_loop())
        await asyncio.sleep(0.02)
        assert calls == []  # startup already refreshed it: no second scan at once
        await asyncio.sleep(0.1)
        task.cancel()
        return t0

    t0 = asyncio.run(scenario())
    assert calls and calls[0] - t0 >= 0.05
