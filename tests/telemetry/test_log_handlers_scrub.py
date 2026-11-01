"""The log scrubber on every path out (P0 #6, B4#5 / B2): a child logger of an unrelated module logs an exception whose
message and traceback carry a planted bot token and API key; neither reaches the stderr handler, the rotating file, a
handler that formats the exception object itself, nor the in-memory OpenTelemetry log export."""

from __future__ import annotations

import io
import logging
import traceback
from logging.handlers import RotatingFileHandler

from btc_signal_engine.bootstrap import logs
from tests.telemetry.conftest import ANTHROPIC_KEY, TG_TOKEN


class FromExcInfo(logging.Handler):
    """Formats from record.exc_info directly, as the OpenTelemetry bridge does (no cached exc_text)."""

    def __init__(self) -> None:
        super().__init__()
        self.out: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.out.append(record.getMessage())
        if record.exc_info:
            self.out.append("".join(traceback.format_exception(*record.exc_info)))


def _fail(url: str, key: str) -> None:
    headers = {"x-api-key": key}  # a local in the frame: only a traceback object would still reach it
    raise ConnectionError(f"GET {url} failed (key {headers['x-api-key']})")


def test_an_exception_from_an_unrelated_child_logger_is_scrubbed_on_every_path(pipe, tmp_path, monkeypatch):
    root = logging.getLogger()
    factory = logging.getLogRecordFactory()
    stream = io.StringIO()
    sh = logging.StreamHandler(stream)
    sh.setFormatter(logs.UtcFormatter())
    fh = RotatingFileHandler(tmp_path / "app.log", maxBytes=1 << 20, backupCount=1, encoding="utf-8")
    fh.setFormatter(logs.UtcFormatter())
    raw = FromExcInfo()
    for h in (sh, fh, raw):
        root.addHandler(h)
    lg = logging.getLogger("btc_signal_engine.application.some_module.child")  # not telegram, not httpx
    monkeypatch.setattr(logging.getLogger("btc_signal_engine"), "disabled", False)
    monkeypatch.setattr(lg, "disabled", False)
    lg.setLevel(logging.INFO)
    logs.install_scrubbing()
    logs.scrub_secrets([])  # pattern rules only: nothing depends on knowing the exact values
    try:
        try:
            _fail(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates", ANTHROPIC_KEY)
        except ConnectionError:
            lg.exception("alert pump failed")
        sh.flush()
        fh.flush()
        outputs = {"stderr": stream.getvalue(), "file": (tmp_path / "app.log").read_text(encoding="utf-8"),
                   "exc_info handler": "\n".join(raw.out)}
        for where, text in outputs.items():
            assert "alert pump failed" in text, where
            assert "Traceback" in text and "_fail" in text, where  # the trace is still there, scrubbed
            for secret in (TG_TOKEN, TG_TOKEN.split(":")[1], ANTHROPIC_KEY):
                assert secret not in text, f"{where}: {secret}"
        mine = [r for r in pipe.logs() if "alert pump failed" in str(r.body)]
        assert mine, "the record reached the OpenTelemetry log export"
        exported = " ".join(str(r.body) + str(dict(r.attributes or {})) for r in mine)
        for secret in (TG_TOKEN, ANTHROPIC_KEY):
            assert secret not in exported
    finally:
        for h in (sh, fh, raw):
            root.removeHandler(h)
            h.removeFilter(logs.SCRUB)
        fh.close()
        logging.setLogRecordFactory(factory)
