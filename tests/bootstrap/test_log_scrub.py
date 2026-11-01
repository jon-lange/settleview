"""Every log line is scrubbed before any handler writes it (docs/plans/2026-10-31-1800-review-fix-plan.md P0 #6, B4#5 /
B2): planted secrets logged through child loggers, arguments, exceptions and a non-propagating logger with its own
handler (as uvicorn's) never reach the stderr handler, the rotating file or that handler."""

from __future__ import annotations

import io
import logging
from logging.handlers import RotatingFileHandler

import pytest

from btc_signal_engine.bootstrap import logs

TOKEN = "7712345678:AAHk3xQ9vZ2mW8pL4tR6yU1oI5eN7bC0dFg"  # Telegram bot token shape (planted, not real)
ANTHROPIC = "sk-ant-api03-PlantedTestKeyNotRealAbCdEf0123456789"
PM_SECRET = "c2VjcmV0LXBsYW50ZWQtZm9yLXRoZS10ZXN0LW9ubHk="  # base64, the Polymarket secret's shape; gitleaks:allow - planted fake
KEY_ID = "4f1c2b9e-0d7a-4c55-9a51-planted0001"
PIN = "482193"
DB_PASSWORD = "hunter2-planted-db-password"


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
    own = io.StringIO()  # a library logger with its own handler that does not propagate (uvicorn's)
    lib = logging.getLogger("uvicorn.planted")
    lib_handler = logging.StreamHandler(own)
    lib_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    monkeypatch.setattr(lib, "handlers", [lib_handler])
    monkeypatch.setattr(lib, "propagate", False)
    root.handlers = [sh, fh]
    root.setLevel(logging.DEBUG)
    for name in ("btc_signal_engine", "telegram", "httpx"):
        monkeypatch.setattr(logging.getLogger(name), "disabled", False)
    logs.install_scrubbing()
    logs.scrub_secrets([TOKEN, ANTHROPIC, PM_SECRET, KEY_ID, DB_PASSWORD])
    yield lambda: (sh.flush(), fh.flush(), stream.getvalue() + (tmp_path / "app.log").read_text(encoding="utf-8")
                   + own.getvalue())[-1]
    fh.close()
    root.handlers, root.level = before_handlers, before_level
    logging.setLogRecordFactory(factory)
    logs.scrub_secrets([])
    for h in (sh, fh, lib_handler):
        if logs.SCRUB in h.filters:
            h.removeFilter(logs.SCRUB)


def _boom(token: str) -> None:
    raise RuntimeError(f"POST https://api.telegram.org/bot{token}/getUpdates failed")


def test_planted_secrets_never_reach_any_handler(handlers):
    child = logging.getLogger("telegram.ext._updater")  # a child logger: no logger-level redactor on it
    child.info("polling https://api.telegram.org/bot%s/getUpdates", TOKEN)
    logging.getLogger("httpx._client").warning("HTTP Request: POST https://api.telegram.org/bot%s/sendMessage", TOKEN)
    logging.getLogger("btc_signal_engine.adapters.agent_sdk").warning("agent env {'ANTHROPIC_API_KEY': '%s'}",
                                                                       ANTHROPIC)
    logging.getLogger("btc_signal_engine.adapters.polymarket_us").error("bad key id=%s secret=%s", KEY_ID, PM_SECRET)
    logging.getLogger("btc_signal_engine.adapters.http").warning("login body {'pin': '%s'}", PIN)
    logging.getLogger("btc_signal_engine.adapters.http").warning('login body {"pin": "%s"}', PIN)
    logging.getLogger("btc_signal_engine.adapters.sqlite").error(
        "connect failed: postgresql://settleview_app:%s@postgres:5432/settleview", DB_PASSWORD)
    try:
        _boom(TOKEN)
    except RuntimeError:
        logging.getLogger("telegram.ext").exception("updater crashed")  # the token in the exception text
    try:
        _boom(TOKEN)
    except RuntimeError:
        logging.getLogger("uvicorn.planted").exception("Exception in ASGI application")  # its own handler
    logging.getLogger("btc_signal_engine.x").warning("stack for %s", TOKEN, stack_info=True)
    out = handlers()
    for secret in (TOKEN, TOKEN.split(":")[1], ANTHROPIC, PM_SECRET, KEY_ID, PIN, DB_PASSWORD):
        assert secret not in out, secret
    assert out.count("<redacted>") >= 10
    assert "updater crashed" in out and "Traceback" in out and "Exception in ASGI application" in out


def test_ordinary_lines_are_left_alone(handlers):
    logging.getLogger("btc_signal_engine.app").info("boundary %d: %d decisions; KXBTC15M-25OCT311815 at %s", 1761, 4,
                                                    "0.52")
    out = handlers()
    assert "boundary 1761: 4 decisions; KXBTC15M-25OCT311815 at 0.52" in out


def test_main_installs_the_scrubber_with_the_settings_secrets(tmp_path, monkeypatch):
    from btc_signal_engine.bootstrap import app as app_mod
    from btc_signal_engine.bootstrap.config import Settings
    settings = Settings(_env_file=None, db_path=tmp_path / "data" / "btcflow.db", trading_stream=False,
                        file_log=True, telegram_bot_token=TOKEN, polymarket_secret_key=PM_SECRET)
    monkeypatch.setattr(app_mod, "Settings", lambda: settings)
    seen: dict = {}

    class Quick:
        def __init__(self, s):
            pass

        async def run(self):
            logging.getLogger("btc_signal_engine.test").warning("value %s and %s", PM_SECRET, "plain")
            seen["factory"] = logging.getLogRecordFactory()

    monkeypatch.setattr(app_mod, "Runtime", Quick)
    root = logging.getLogger()
    before, factory = list(root.handlers), logging.getLogRecordFactory()
    try:
        app_mod.main([])
        added = [h for h in root.handlers if h not in before]
        assert added and all(logs.SCRUB in h.filters for h in added)
        for h in added:
            h.flush()
        text = (tmp_path / "data" / "logs" / "app.log").read_text(encoding="utf-8")
        assert PM_SECRET not in text and "value <redacted> and plain" in text
    finally:
        for h in [h for h in root.handlers if h not in before]:
            root.removeHandler(h)
            h.close()
        logging.setLogRecordFactory(factory)
        logs.scrub_secrets([])
