"""Shared log setup for every entry point (the app, the recorders, the supervisor, ops): one line format, stderr.

Every line starts with its UTC time in ISO 8601 with milliseconds and a `Z` (`2026-10-31T18:50:00.123Z`), whatever the
process's TZ is, so lines from the app, the recorders and Postgres (which logs UTC) line up in one collector. Then the
level and the logger. The HTTP client libraries (`httpx`, `httpcore`) log at WARNING by default: at INFO httpx writes
one line per request (Kalshi perps polling alone is ~240 a minute); BTCFLOW_HTTP_CLIENT_LOG_LEVEL changes it.

Every log line is scrubbed before any handler writes it (stderr, the rotating files, uvicorn's own handlers, the
OpenTelemetry bridge): the message after its arguments are filled in, the exception text and the stack, through the
same `Scrubber` the telemetry exporters use (bootstrap/scrub.py: Telegram bot tokens, API keys, Authorization headers,
PINs and passwords as key=value, URL credentials, long secret-looking runs, and the exact values of the app's own
secrets once `scrub_secrets` has them). Two layers, so nothing depends on where a handler was attached: a log-record
factory scrubs each record as it is made (any logger, propagating or not), and `ScrubFilter` sits on every handler
(`install_scrubbing`) for records made elsewhere.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable

from btc_signal_engine.bootstrap.scrub import Scrubber

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
EX_CONFIG = 78  # sysexits.h: a configuration error. The supervisor does not restart a child that exits with it.
HTTP_CLIENT_LOGGERS = ("httpx", "httpcore")
HTTP_CLIENT_LOG_LEVEL = "WARNING"


class UtcFormatter(logging.Formatter):
    """LOG_FORMAT with `asctime` as UTC ISO 8601 (milliseconds, `Z`)."""

    converter = time.gmtime

    def __init__(self, fmt: str = LOG_FORMAT) -> None:
        super().__init__(fmt)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802 (logging API)
        return time.strftime("%Y-%m-%dT%H:%M:%S", self.converter(record.created)) + f".{int(record.msecs):03d}Z"


class ScrubbedError(Exception):
    """A logged exception after scrubbing: the original type's name and its scrubbed message. It replaces the
    exception object in record.exc_info (the traceback is kept: its frames print source lines, not values), so a
    handler that formats the object itself shows the same scrubbed text."""


class ScrubFilter(logging.Filter):
    """Scrubs a record in place (message with its arguments, exception text and object, stack); never drops one."""

    def __init__(self) -> None:
        super().__init__()
        self.scrubber = Scrubber()
        self._fmt = logging.Formatter()

    def scrub(self, record: logging.LogRecord) -> None:
        """Idempotent (no marker on the record: every attribute a record carries becomes an exported log attribute)."""
        sc = self.scrubber
        try:
            msg = record.getMessage()
        except Exception:  # a bad format string: logging reports it itself; scrub what is there
            msg = str(record.msg)
        record.msg, record.args = sc.text(msg), None
        if record.exc_info and not record.exc_text:
            record.exc_text = self._fmt.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = sc.text(record.exc_text)
        if record.exc_info and record.exc_info[1] is not None and not isinstance(record.exc_info[1], ScrubbedError):
            # Handlers that format the exception object themselves (the OpenTelemetry bridge's exception.message and
            # exception.stacktrace) never see the original message (nor its chained causes; exc_text keeps those,
            # scrubbed).
            exc = record.exc_info[1]
            record.exc_info = (record.exc_info[0], ScrubbedError(f"{type(exc).__name__}: {sc.text(str(exc))}"),
                               record.exc_info[2])
        if record.stack_info:
            record.stack_info = sc.text(record.stack_info)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            self.scrub(record)
        except Exception:  # noqa: S110 - never lose a line to the scrubber (the factory already scrubbed it); logging from inside the log filter would recurse
            pass
        return True


SCRUB = ScrubFilter()
_factory = logging.getLogRecordFactory()


def _scrubbing_factory(*args, **kwargs) -> logging.LogRecord:
    record = _factory(*args, **kwargs)
    SCRUB.filter(record)
    return record


def scrub_secrets(values: Iterable[str]) -> None:
    """The exact secret values (telemetry.secret_values(settings)) every later log line is scrubbed of too."""
    SCRUB.scrubber = Scrubber(values)


def install_scrubbing() -> None:
    """Scrub every log record: the record factory (once per process) and a ScrubFilter on every handler of every
    logger configured so far. Call it again after something adds handlers (uvicorn, a log file, OpenTelemetry)."""
    if logging.getLogRecordFactory() is not _scrubbing_factory:
        logging.setLogRecordFactory(_scrubbing_factory)
    loggers = [logging.getLogger()] + [lg for lg in list(logging.Logger.manager.loggerDict.values())
                                        if isinstance(lg, logging.Logger)]
    for lg in loggers:
        for h in list(lg.handlers):
            if SCRUB not in h.filters:
                h.addFilter(SCRUB)


def set_http_client_log_level(level: str | int = HTTP_CLIENT_LOG_LEVEL) -> None:
    """The `httpx` / `httpcore` loggers' level (a name such as "WARNING", or a number)."""
    for name in HTTP_CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(level)


def log_to_stderr(level: int = logging.INFO) -> None:
    """As logging.basicConfig: when the root logger has no handler yet, one that writes UTC ISO 8601 lines to stderr,
    and the root level. The HTTP client loggers at WARNING and every line scrubbed (install_scrubbing) either way."""
    root = logging.getLogger()
    if not root.handlers:
        h = logging.StreamHandler()
        h.setFormatter(UtcFormatter())
        root.addHandler(h)
        root.setLevel(level)
    set_http_client_log_level()
    install_scrubbing()
