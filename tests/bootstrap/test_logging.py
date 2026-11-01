"""Logs (R17): main() writes a rotating log file under the data dir next to the stderr handler; a repeated Kalshi
live-poll failure logs one WARNING a minute (not one per attempt); a Polymarket balance failure names its error."""

from __future__ import annotations

import asyncio
import logging
from logging.handlers import RotatingFileHandler

import pytest

from btc_signal_engine.bootstrap import app as app_mod
from btc_signal_engine.bootstrap.app import Runtime
from btc_signal_engine.bootstrap.config import Settings


@pytest.fixture(autouse=True)
def logs_reach_caplog(monkeypatch):
    for k in ("BTCFLOW_TRADING_STREAM", "BTCFLOW_API_HOST", "BTCFLOW_ACCESS_PIN", "BTCFLOW_POLYMARKET_KEY_ID",
              "BTCFLOW_POLYMARKET_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    lg = logging.getLogger("btc_signal_engine")  # other tests may detach it
    monkeypatch.setattr(lg, "disabled", False)
    monkeypatch.setattr(lg, "propagate", True)


@pytest.fixture
def rt(tmp_path):
    r = Runtime(Settings(_env_file=None, db_path=tmp_path / "logs.db", trading_stream=False))
    yield r
    r.writer.close()


def _file_handlers(root: logging.Logger) -> list[RotatingFileHandler]:
    return [h for h in root.handlers if isinstance(h, RotatingFileHandler)]


def test_main_writes_app_log_under_the_data_dir_10mb_x10(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, db_path=tmp_path / "data" / "btcflow.db", trading_stream=False)
    monkeypatch.setattr(app_mod, "Settings", lambda: settings)  # never the real .env

    class Quick:
        def __init__(self, s):
            assert s is settings

        async def run(self):
            logging.getLogger("btc_signal_engine.test").warning("hello from the run")
    monkeypatch.setattr(app_mod, "Runtime", Quick)
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        app_mod.main([])
        added = [h for h in _file_handlers(root) if h not in before]
        assert len(added) == 1
        h = added[0]
        assert h.maxBytes == 10 * 2**20 and h.backupCount == 10
        h.flush()
        text = (tmp_path / "data" / "logs" / "app.log").read_text(encoding="utf-8")
        assert "WARNING btc_signal_engine.test: hello from the run" in text
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()


def test_the_log_file_rotates_at_its_size_limit(tmp_path):
    root = logging.getLogger()
    h = app_mod.install_file_log(tmp_path / "logs", max_bytes=500, backups=3)
    try:
        lg = logging.getLogger("btc_signal_engine.rotate")
        for i in range(50):
            lg.warning("line %d %s", i, "x" * 40)
    finally:
        root.removeHandler(h)
        h.close()
    names = sorted(p.name for p in (tmp_path / "logs").iterdir())
    assert names == ["app.log", "app.log.1", "app.log.2", "app.log.3"]
    assert all(p.stat().st_size <= 500 for p in (tmp_path / "logs").iterdir())


def test_a_repeated_kalshi_poll_failure_warns_once_a_minute(rt, monkeypatch, caplog):
    t = [1_700_000_000_000]
    calls = []

    def now_ms():
        return t[0]

    async def current_market(now):
        calls.append(now)
        if len(calls) > 7:
            raise asyncio.CancelledError  # end the loop after 7 attempts
        t[0] += 20_000  # each attempt 20 s after the previous one: attempts at 0, 20, ..., 120 s
        raise ConnectionError("kalshi down")
    monkeypatch.setattr(rt.clock, "now_ms", now_ms)
    monkeypatch.setattr(rt.kalshi_sync, "current_market", current_market)
    monkeypatch.setattr(app_mod, "KALSHI_LIVE_POLL_S", 0.001)
    with caplog.at_level(logging.DEBUG, logger="btc_signal_engine"):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(rt.kalshi_live_loop())
    warns = [r for r in caplog.records if r.levelno == logging.WARNING and "kalshi live poll" in r.getMessage()]
    assert len(calls) == 8
    assert len(warns) == 3  # the 1st failure, then once each full minute: 60 s and 120 s later
    assert "ConnectionError: kalshi down" in warns[0].getMessage()


def test_a_polymarket_balance_failure_names_the_error(rt, monkeypatch, caplog):
    class Gw:
        async def read_balance(self):
            raise TimeoutError("balances took too long")
    rt.pm_gw = Gw()
    rt.portfolio_gw = None

    async def go():
        task = asyncio.create_task(rt.balance_loop())
        await asyncio.sleep(0.05)
        task.cancel()
    with caplog.at_level(logging.WARNING, logger="btc_signal_engine"):
        asyncio.run(go())
    msgs = [r.getMessage() for r in caplog.records if "Polymarket balance" in r.getMessage()]
    assert msgs == ["Polymarket balance read failed: TimeoutError: balances took too long"]


def test_log_lines_start_with_utc_iso_8601_whatever_the_time_zone(monkeypatch):
    from btc_signal_engine.bootstrap.logs import UtcFormatter
    rec = logging.LogRecord("btc_signal_engine.x", logging.WARNING, __file__, 1, "hi %s", ("there",), None)
    import calendar
    rec.created, rec.msecs = calendar.timegm((2026, 10, 31, 18, 50, 0)) + 0.123, 123.0
    assert UtcFormatter().format(rec) == "2026-10-31T18:50:00.123Z WARNING btc_signal_engine.x: hi there"


def test_the_http_client_loggers_default_to_warning_and_the_setting_changes_them(monkeypatch):
    from btc_signal_engine.bootstrap.logs import log_to_stderr, set_http_client_log_level
    for name in ("httpx", "httpcore"):
        monkeypatch.setattr(logging.getLogger(name), "level", logging.NOTSET)
    log_to_stderr()
    assert logging.getLogger("httpx").level == logging.WARNING == logging.getLogger("httpcore").level
    set_http_client_log_level(Settings(_env_file=None, http_client_log_level="INFO").http_client_log_level)
    assert logging.getLogger("httpx").level == logging.INFO
    with pytest.raises(ValueError):
        Settings(_env_file=None, http_client_log_level="LOUD")
