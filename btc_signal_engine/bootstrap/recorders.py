"""Entry point of the research recorders: `python -m btc_signal_engine.recorders pm-quotes | pm-settle`.

  pm-quotes   Polymarket US quotes and trades over the authenticated markets WebSocket (needs
              BTCFLOW_POLYMARKET_KEY_ID and BTCFLOW_POLYMARKET_SECRET_KEY, or their _FILE forms; read-only market data)
  pm-settle   Polymarket US settlement terms of every closed window (public client, no keys)

Both write `<BTCFLOW_RESEARCH_DIR>/polymarket_us.db` (default `<db dir>/research`), or, when BTCFLOW_DATABASE_URL is
set, the `research` schema of that Postgres database (same tables and columns). Options:
  --stop-file PATH   exit cleanly when this file appears (the supervisor's stop on every OS); deleted on exit
  --check            check the configuration and say where the recorder would write, then exit (no network, no
                     database write): exit 0 when it would start, 78 when it would refuse
  --every S          pm-settle only: seconds between passes (default 5)

Liveness: each recorder touches `<temp folder>/settleview-<name>.alive` from its main loop (bootstrap/heartbeat.py);
the container healthcheck is `python -m btc_signal_engine.ops alive <name>`, and a main loop that stops beating for
heartbeat.STALE_EXIT_S ends the process with 75 by itself (Compose never restarts an unhealthy container); pm-quotes
also exits 75 when one lane's own loop stops turning (adapters/polymarket_us/recorders.py, LANE_STUCK_S). Data
freshness (`ops fresh`) is an alert and readiness signal, not a restart trigger.

SIGTERM / SIGINT (and SIGBREAK on Windows) stop the recorder cleanly: it flushes what it holds, closes its sockets and
exits 0. A configuration error exits 78 (the supervisor does not restart it); a bad OTEL_* setting is one too.
pm-quotes exits 75 when it has recorded no quote for 10 minutes despite reconnects and no lane shows a venue-side book
outage (adapters/polymarket_us/recorders.py): Docker's restart policy and the supervisor start it again. Logs go
to stderr, and with an OTLP endpoint set also to OpenTelemetry with its traces and metrics (bootstrap/telemetry.py;
service.name settleview-rec-<name> unless OTEL_SERVICE_NAME says otherwise).
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import logging
import signal
import sys
import threading
import time
from pathlib import Path

from btc_signal_engine.adapters.polymarket_us import recorders as rec
from btc_signal_engine.adapters.sqlite.urls import describe, is_postgres, use_pg_timeouts
from btc_signal_engine.bootstrap import heartbeat, telemetry
from btc_signal_engine.bootstrap.config import Settings
from btc_signal_engine.bootstrap.logs import (
    EX_CONFIG,
    install_scrubbing,
    log_to_stderr,
    scrub_secrets,
    set_http_client_log_level,
)

log = logging.getLogger("btc_signal_engine.recorders")
NAMES = ("pm-quotes", "pm-settle")
KEYS_MISSING = ("Polymarket US keys are not configured (BTCFLOW_POLYMARKET_KEY_ID and BTCFLOW_POLYMARKET_SECRET_KEY, "
                "or their _FILE forms)")


class StopFlag:
    """True once a stop signal arrived or the stop file exists."""

    def __init__(self, stop_file: Path | None = None) -> None:
        self.stop_file = stop_file
        self.event = threading.Event()

    def set(self, *_args) -> None:
        self.event.set()

    def __call__(self) -> bool:
        if not self.event.is_set() and self.stop_file is not None and self.stop_file.exists():
            log.info("stop file %s found: stopping", self.stop_file)
            self.event.set()
        return self.event.is_set()

    def wait(self, seconds: float, slice_s: float = 0.5) -> bool:
        """Sleep up to `seconds`; True as soon as a stop is requested."""
        end = time.monotonic() + seconds
        while not self():
            left = end - time.monotonic()
            if left <= 0:
                return False
            self.event.wait(min(slice_s, left))
        return True

    def install(self) -> None:
        for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is not None:
                signal.signal(sig, self.set)


def parse(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m btc_signal_engine.recorders",
                                 description="Settleview research recorders (read-only market data, no orders).")
    ap.add_argument("name", choices=NAMES)
    ap.add_argument("--stop-file", type=Path, default=None)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--every", type=float, default=5.0)
    return ap.parse_args(argv)


def _keys(s: Settings) -> tuple[str, str] | None:
    if not (s.polymarket_key_id and s.polymarket_secret_key and s.polymarket_secret_key.get_secret_value()):
        return None
    return s.polymarket_key_id, s.polymarket_secret_key.get_secret_value()


def main(argv: list[str] | None = None, *, settings: Settings | None = None) -> int:
    a = parse(argv)
    log_to_stderr()
    try:
        s = settings if settings is not None else Settings()
    except ValueError as exc:  # a bad setting (also a bad _FILE): say which, never a value
        log.error("configuration error: %s", exc)
        return EX_CONFIG
    scrub_secrets(telemetry.secret_values(s))
    set_http_client_log_level(s.http_client_log_level)
    use_pg_timeouts(s.pg_timeouts)
    db = s.database if is_postgres(s.database) else s.research_path / rec.DB_NAME
    keys = _keys(s) if a.name == "pm-quotes" else None
    if a.name == "pm-quotes" and keys is None:
        log.error(KEYS_MISSING)
        return EX_CONFIG
    if a.check:
        print(f"{a.name}: ok, would write {describe(db)}")
        return 0
    try:
        tel = telemetry.setup(f"settleview-rec-{a.name}", secrets=telemetry.secret_values(s))
    except ValueError as exc:  # a bad OTEL_* setting, or the SDK missing with an endpoint set
        log.error("configuration error: %s", exc)
        return EX_CONFIG
    install_scrubbing()
    stop = StopFlag(a.stop_file)
    stop.install()
    alive = heartbeat.path(a.name)  # the healthcheck's liveness (`ops alive`), touched from the main loop
    beat = functools.partial(heartbeat.beat, alive)
    beat()
    watch = heartbeat.exit_when_stale(alive, heartbeat.STALE_EXIT_S[a.name])  # a wedged main loop ends the process
    log.info("%s recorder writing %s", a.name, describe(db))
    try:
        from polymarket_us import PolymarketUS
        if a.name == "pm-quotes":
            key_id, secret = keys
            try:
                asyncio.run(rec.run_quotes(db, lambda: PolymarketUS(key_id=key_id, secret_key=secret), stop=stop,
                                           beat=beat))
            except rec.RecorderStalled as exc:  # already logged; the restart policy starts a fresh process
                log.error("pm-quotes exiting with %d: %s", rec.EX_STALLED, exc)
                return rec.EX_STALLED
        else:
            rec.run_settle(db, PolymarketUS(timeout=10.0), every_s=a.every, wait=stop.wait,  # public: no key
                           beat=beat)
    finally:
        watch.set()
        if a.stop_file is not None:
            a.stop_file.unlink(missing_ok=True)
        alive.unlink(missing_ok=True)
        tel.shutdown()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
