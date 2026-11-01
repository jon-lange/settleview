"""The log mask (bootstrap/logs.py mask_secrets): the exact values of the app's own secrets never reach a log handler,
whichever logger made the record: arguments, child loggers, exceptions (their text and the exception object, as the
OpenTelemetry bridge formats it), stack dumps and a non-propagating logger with its own handler (as uvicorn's). It is
the one redaction left in the process: stderr and Docker's logs never pass through the OpenTelemetry Collector, which
redacts the exported telemetry by pattern (deploy/otel-collector/config.yaml, tests/telemetry/test_collector_e2e.py)."""

from __future__ import annotations

import io
import logging
import traceback
from logging.handlers import RotatingFileHandler

import pytest

from btc_signal_engine.bootstrap import logs

TOKEN = "7712345678:AAHk3xQ9vZ2mW8pL4tR6yU1oI5eN7bC0dFg"  # Telegram bot token shape (planted, not real)
ANTHROPIC = "sk-ant-api03-PlantedTestKeyNotRealAbCdEf0123456789"
PM_SECRET = "c2VjcmV0LXBsYW50ZWQtZm9yLXRoZS10ZXN0LW9ubHk="  # base64, the Polymarket secret's shape; gitleaks:allow - planted fake
KEY_ID = "4f1c2b9e-0d7a-4c55-9a51-planted0001"
DB_PASSWORD = "hunter2-planted-db-password"
PLANTED = (TOKEN, ANTHROPIC, PM_SECRET, KEY_ID, DB_PASSWORD)


class FromExcInfo(logging.Handler):
    """Formats from record.exc_info directly, as the OpenTelemetry bridge does (no cached exc_text)."""

    def __init__(self) -> None:
        super().__init__()
        self.out: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.out.append(record.getMessage())
        if record.exc_info:
            self.out.append("".join(traceback.format_exception(*record.exc_info)))


@pytest.fixture
def handlers(tmp_path, monkeypatch):
    root = logging.getLogger()
    before_handlers, before_level = list(root.handlers), root.level
    factory = logging.getLogRecordFactory()
    stream = io.StringIO()
    sh = logging.StreamHandler(stream)
    sh.setFormatter(logs.UtcFormatter())
    fh = RotatingFileHandler(tmp_path / "app.log", maxBytes=1 << 20, backupCount=1, encoding="utf-8")
    fh.setFormatter(logs.UtcFormatter())
    raw = FromExcInfo()
    own = io.StringIO()  # a library logger with its own handler that does not propagate (uvicorn's)
    lib = logging.getLogger("uvicorn.planted")
    lib_handler = logging.StreamHandler(own)
    lib_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    monkeypatch.setattr(lib, "handlers", [lib_handler])
    monkeypatch.setattr(lib, "propagate", False)
    root.handlers = [sh, fh, raw]
    root.setLevel(logging.DEBUG)
    for name in ("btc_signal_engine", "telegram", "httpx"):
        monkeypatch.setattr(logging.getLogger(name), "disabled", False)
    logs.mask_secrets([*PLANTED, "short", None, ""])  # values under 6 characters (and empty ones) are ignored
    yield lambda: (sh.flush(), fh.flush(), stream.getvalue() + (tmp_path / "app.log").read_text(encoding="utf-8")
                   + own.getvalue() + "\n".join(raw.out))[-1]
    fh.close()
    root.handlers, root.level = before_handlers, before_level
    logs.mask_secrets([])
    logging.setLogRecordFactory(factory)


def _boom(token: str, key: str) -> None:
    headers = {"x-api-key": key}  # a local in the frame: only a traceback object would still reach it
    raise RuntimeError(f"POST https://api.telegram.org/bot{token}/getUpdates failed (key {headers['x-api-key']})")


def test_the_settings_secrets_never_reach_any_handler(handlers):
    logging.getLogger("telegram.ext._updater").info("polling https://api.telegram.org/bot%s/getUpdates", TOKEN)
    logging.getLogger("btc_signal_engine.adapters.agent_sdk").warning("agent env {'ANTHROPIC_API_KEY': '%s'}",
                                                                       ANTHROPIC)
    logging.getLogger("btc_signal_engine.adapters.polymarket_us").error("bad key id=%s secret=%s", KEY_ID, PM_SECRET)
    logging.getLogger("btc_signal_engine.adapters.sqlite").error(
        "connect failed: postgresql://settleview_app:%s@postgres:5432/settleview", DB_PASSWORD)
    try:
        _boom(TOKEN, ANTHROPIC)
    except RuntimeError:
        logging.getLogger("btc_signal_engine.application.some_module.child").exception("alert pump failed")
    try:
        _boom(TOKEN, ANTHROPIC)
    except RuntimeError:
        logging.getLogger("uvicorn.planted").exception("Exception in ASGI application")  # its own handler
    logging.getLogger("btc_signal_engine.x").warning("stack for %s", TOKEN, stack_info=True)
    out = handlers()
    for secret in PLANTED:
        assert secret not in out, secret
    assert out.count(logs.MASK) >= 8
    assert "alert pump failed" in out and "Traceback" in out and "_boom" in out  # the trace is kept, masked
    assert "Exception in ASGI application" in out


def test_ordinary_lines_are_left_alone(handlers):
    logging.getLogger("btc_signal_engine.app").info("boundary %d: %d decisions; KXBTC15M-25OCT311815 at %s", 1761, 4,
                                                    "0.52")
    try:
        raise ValueError("no secret here")
    except ValueError:
        logging.getLogger("btc_signal_engine.app").exception("plain failure")
    out = handlers()
    assert "boundary 1761: 4 decisions; KXBTC15M-25OCT311815 at 0.52" in out
    assert "ValueError: no secret here" in out  # an exception without a secret keeps its own object
    assert logs.MASK not in out


def test_main_masks_the_settings_secrets(tmp_path, monkeypatch):
    from btc_signal_engine.bootstrap import app as app_mod
    from btc_signal_engine.bootstrap.config import Settings
    settings = Settings(_env_file=None, db_path=tmp_path / "data" / "btcflow.db", trading_stream=False,
                        file_log=True, telegram_bot_token=TOKEN, polymarket_secret_key=PM_SECRET)
    monkeypatch.setattr(app_mod, "Settings", lambda: settings)

    class Quick:
        def __init__(self, s):
            pass

        async def run(self):
            logging.getLogger("btc_signal_engine.test").warning("value %s and %s", PM_SECRET, "plain")

    monkeypatch.setattr(app_mod, "Runtime", Quick)
    root = logging.getLogger()
    before, factory = list(root.handlers), logging.getLogRecordFactory()
    try:
        app_mod.main([])
        for h in root.handlers:
            h.flush()
        text = (tmp_path / "data" / "logs" / "app.log").read_text(encoding="utf-8")
        assert PM_SECRET not in text and f"value {logs.MASK} and plain" in text
    finally:
        for h in [h for h in root.handlers if h not in before]:
            root.removeHandler(h)
            h.close()
        logging.setLogRecordFactory(factory)
        logs.mask_secrets([])
