"""Shared log setup for every entry point (the app, the recorders, the supervisor, ops): one line format, stderr.

Every line starts with its UTC time in ISO 8601 with milliseconds and a `Z` (`2026-10-31T18:50:00.123Z`), whatever the
process's TZ is, so lines from the app, the recorders and Postgres (which logs UTC) line up in one collector. Then the
level and the logger. The HTTP client libraries (`httpx`, `httpcore`) log at WARNING by default: at INFO httpx writes
one line per request (Kalshi perps polling alone is ~240 a minute); BTCFLOW_HTTP_CLIENT_LOG_LEVEL changes it.

Secrets are kept out of the logs at the source (standard practice: nothing logs a credential, every database engine
has `hide_parameters`, httpx stays at WARNING so it never writes a request URL). One defence in depth stays here,
because stderr and Docker's logs never pass through the OpenTelemetry Collector that redacts the exported telemetry
(deploy/otel-collector/config.yaml): `mask_secrets` installs a log-record factory that replaces the EXACT values of the
app's own configured secrets (telemetry.secret_values: the Telegram token, the Anthropic key, the Polymarket secret,
the key ids, the database passwords) with `****` in every record as it is made: the message with its arguments, the
exception text and object, and the stack. It knows no patterns (those are the collector's job); it only makes sure a
value the process itself holds can never be printed, whichever logger or handler writes the line.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
EX_CONFIG = 78  # sysexits.h: a configuration error. The supervisor does not restart a child that exits with it.
HTTP_CLIENT_LOGGERS = ("httpx", "httpcore")
HTTP_CLIENT_LOG_LEVEL = "WARNING"
MASK = "****"
MIN_SECRET_LEN = 6  # shorter "secrets" (an empty value, a short test string) would blank ordinary text


class UtcFormatter(logging.Formatter):
    """LOG_FORMAT with `asctime` as UTC ISO 8601 (milliseconds, `Z`)."""

    converter = time.gmtime

    def __init__(self, fmt: str = LOG_FORMAT) -> None:
        super().__init__(fmt)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802 (logging API)
        return time.strftime("%Y-%m-%dT%H:%M:%S", self.converter(record.created)) + f".{int(record.msecs):03d}Z"


class MaskedError(Exception):
    """A logged exception whose text held a secret: the original type's name and its masked message, in place of the
    exception object in record.exc_info (the traceback is kept), so a handler that formats the object itself (the
    OpenTelemetry bridge's exception.message) shows the masked text too."""


_secrets: tuple[str, ...] = ()
_factory = logging.getLogRecordFactory()


def mask(text: str) -> str:
    """`text` with every exact secret value set by `mask_secrets` replaced by MASK (the longest first)."""
    for v in _secrets:
        if v in text:
            text = text.replace(v, MASK)
    return text


def _mask_record(record: logging.LogRecord) -> None:
    msg = record.getMessage()
    masked = mask(msg)
    if masked != msg:
        record.msg, record.args = masked, None
    if record.exc_info and record.exc_info[1] is not None:
        text = logging.Formatter().formatException(record.exc_info)
        if mask(text) != text:
            exc = record.exc_info[1]
            record.exc_text = mask(text)
            record.exc_info = (record.exc_info[0], MaskedError(f"{type(exc).__name__}: {mask(str(exc))}"),
                               record.exc_info[2])
    if record.stack_info:
        record.stack_info = mask(record.stack_info)


def _masking_factory(*args, **kwargs) -> logging.LogRecord:
    record = _factory(*args, **kwargs)
    if _secrets:
        try:
            _mask_record(record)
        except Exception:  # noqa: S110 - never lose a line to the mask (a bad format string: logging reports it itself)
            pass
    return record


def mask_secrets(values: Iterable[str | None]) -> None:
    """Mask these exact values (telemetry.secret_values(settings)) in every log record made from now on, by any logger
    (the record factory, installed once per process). Values shorter than MIN_SECRET_LEN are ignored."""
    global _secrets
    found = {v.strip() for v in values if isinstance(v, str) and len(v.strip()) >= MIN_SECRET_LEN}
    _secrets = tuple(sorted(found, key=len, reverse=True))
    if logging.getLogRecordFactory() is not _masking_factory:
        logging.setLogRecordFactory(_masking_factory)


def set_http_client_log_level(level: str | int = HTTP_CLIENT_LOG_LEVEL) -> None:
    """The `httpx` / `httpcore` loggers' level (a name such as "WARNING", or a number)."""
    for name in HTTP_CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(level)


def log_to_stderr(level: int = logging.INFO) -> None:
    """As logging.basicConfig: when the root logger has no handler yet, one that writes UTC ISO 8601 lines to stderr,
    and the root level. The HTTP client loggers at WARNING either way."""
    root = logging.getLogger()
    if not root.handlers:
        h = logging.StreamHandler()
        h.setFormatter(UtcFormatter())
        root.addHandler(h)
        root.setLevel(level)
    set_http_client_log_level()
