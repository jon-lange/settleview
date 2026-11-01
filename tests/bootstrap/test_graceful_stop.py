"""Graceful stop for deploys (KTD9, R5): `data/stop.request` makes `Runtime.run` take its normal shutdown path (the
writer's pending rows are flushed), delete the file and return, so the process exits with code 0; without the file
nothing stops."""

from __future__ import annotations

import asyncio
import json
import os
import logging
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from btc_signal_engine.bootstrap import app as app_mod
from btc_signal_engine.bootstrap.app import Runtime
from btc_signal_engine.bootstrap.config import Settings
from btc_signal_engine.domain.models.events import Trade

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def logs_reach_caplog(monkeypatch):
    for name in ("btc_signal_engine", "btc_signal_engine.adapters.sqlite.writer"):  # other tests may detach them
        lg = logging.getLogger(name)
        monkeypatch.setattr(lg, "disabled", False)
        monkeypatch.setattr(lg, "propagate", True)


@pytest.fixture
def rt(tmp_path, monkeypatch):
    for k in ("BTCFLOW_TRADING_STREAM", "BTCFLOW_API_HOST", "BTCFLOW_ACCESS_PIN", "BTCFLOW_POLYMARKET_KEY_ID",
              "BTCFLOW_POLYMARKET_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    r = Runtime(Settings(_env_file=None, db_path=tmp_path / "stop.db", trading_stream=False))

    class FakeCollector:
        async def run(self):
            await asyncio.Event().wait()

        def close(self):
            pass

    class FakeServer:  # stays up until cancelled, like uvicorn without a signal
        def __init__(self, config):
            self.should_exit = False

        async def serve(self):
            await asyncio.Event().wait()

    class NoStallWatch:
        def __init__(self, loop):
            pass

        def start(self):
            pass

    async def no_startup():
        return None

    monkeypatch.setattr(app_mod.CoinbaseCollector, "live", classmethod(lambda cls, *a, **k: FakeCollector()))
    monkeypatch.setattr(app_mod.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(app_mod, "StallWatch", NoStallWatch)
    monkeypatch.setattr(r, "startup", no_startup)
    return r


def _trade(i: int) -> Trade:
    return Trade(trade_id=i, ts=1_700_000_000_000 + i, ingest_ts=1_700_000_000_100 + i, price=80_000.0, size=0.01,
                 buy=True)


def test_the_stop_file_flushes_pending_rows_deletes_the_file_and_run_returns(rt, tmp_path, monkeypatch, caplog):
    # Only the stop-file watcher runs: the rows below stay pending until the shutdown flush writes them.
    monkeypatch.setattr(rt, "loops", lambda: {"stop_file": rt.stop_file_loop})
    rt.writer.add_trades([_trade(1), _trade(2)])
    stop = tmp_path / "stop.request"
    stop.write_text("deploy")
    with caplog.at_level(logging.INFO):
        asyncio.run(asyncio.wait_for(rt.run(), timeout=10))
    assert {r["trade_id"] for r in rt.store.select("trades")} == {1, 2}
    assert not stop.exists()
    assert "clean shutdown (deploy)" in caplog.text


def test_a_stop_while_the_writer_keeps_failing_still_exits_and_logs_what_was_not_written(rt, tmp_path, monkeypatch,
                                                                                         caplog):
    from sqlalchemy.exc import OperationalError

    def locked(batch):
        raise OperationalError("COMMIT", {}, Exception("database is locked"))

    monkeypatch.setattr(rt.writer, "_apply", locked)
    monkeypatch.setattr(rt, "loops", lambda: {"writer": rt.writer.run, "stop_file": rt.stop_file_loop})
    rt.writer.add_trades([_trade(7)])
    (tmp_path / "stop.request").write_text("deploy")
    with caplog.at_level(logging.INFO):
        asyncio.run(asyncio.wait_for(rt.run(), timeout=10))
    assert rt.store.select("trades") == []
    dead = [json.loads(line) for line in (tmp_path / "logs" / "writer-dead-letter.jsonl").read_text().splitlines()]
    assert [w["method"] for d in dead for w in d["writes"]] == ["add_trades"]
    assert [d["reason"] for d in dead] == ["shutdown"]  # replayable: ops replay-dead-letter
    assert "dead-lettered" in caplog.text and "clean shutdown (deploy)" in caplog.text


def test_without_the_stop_file_the_watcher_never_asks_to_stop(rt):
    async def scenario():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(rt.stop_file_loop(), timeout=1.5)

    asyncio.run(scenario())
    assert not rt.supervisor.stopping
    rt.writer.close()


def test_main_exits_with_code_0_after_a_clean_stop_and_logs_the_exit(tmp_path):
    # A real interpreter runs main() with a Runtime whose run() returns, as after a stop-file shutdown.
    code = textwrap.dedent("""
        from btc_signal_engine.bootstrap import app
        class FakeRuntime:
            def __init__(self, settings):
                pass
            async def run(self):
                return None
        app.Runtime = FakeRuntime
        app.main([])
    """)
    p = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True, timeout=60,
                       env={"PYTHONPATH": str(ROOT), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")})
    assert p.returncode == 0, p.stderr
    assert "process exiting" in p.stderr


def test_run_records_its_pid_for_the_deploy_script_and_removes_it_on_exit(rt, tmp_path, monkeypatch):
    # scripts/deploy_stop.ps1 finds the app only through <data>/app.pid (the watchdog runs this checkout's .venv
    # python from another folder, so matching by executable path finds nothing).
    seen = {}

    async def watcher():
        seen["pid"] = (tmp_path / "app.pid").read_text(encoding="ascii")
        (tmp_path / "stop.request").write_text("deploy")
        await rt.stop_file_loop()

    monkeypatch.setattr(rt, "loops", lambda: {"stop_file": watcher})
    asyncio.run(asyncio.wait_for(rt.run(), timeout=10))
    assert seen["pid"] == str(os.getpid())
    assert not (tmp_path / "app.pid").exists()


def test_a_pid_file_another_process_wrote_meanwhile_is_left_alone(rt, tmp_path):
    rt.write_pid_file()
    (tmp_path / "app.pid").write_text("999999", encoding="ascii")  # a newer process replaced it
    rt.remove_pid_file()
    assert (tmp_path / "app.pid").read_text(encoding="ascii") == "999999"


def test_sigterm_is_a_graceful_stop_not_an_instant_kill(rt):
    """docker stop / tini send SIGTERM: Python's default action (and uvicorn re-raising it after its own exit) killed
    the process with 143 before the writer flushed. The app's handler turns it into a supervisor stop."""
    import signal

    async def scenario():
        restore = rt._handle_sigterm(asyncio.get_running_loop())
        try:
            signal.raise_signal(signal.SIGTERM)
            for _ in range(100):
                await asyncio.sleep(0.01)
                if rt.supervisor.stopping:
                    break
        finally:
            restore()

    before = signal.getsignal(signal.SIGTERM)
    asyncio.run(scenario())
    assert rt.supervisor.stopping
    assert signal.getsignal(signal.SIGTERM) == before
