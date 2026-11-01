"""Process-local liveness for the research recorders (docs/plans/2026-10-31-1800-review-fix-plan.md P2, B4#16).

A recorder touches its heartbeat file from its own main loop (the quotes recorder's watchdog every second, the
settlement recorder once per pass). Its container healthcheck (`python -m btc_signal_engine.ops alive <name>`) reads
only that file's age: the process is alive and its loop turns. Whether data is flowing (a venue or database outage)
is a different question, answered by the newest rows (`ops fresh`, the app's `data` alert, /api/health `recorders`),
never by a restart: a restarted recorder cannot bring a venue's book or the database back.

Plain Docker (Compose) never restarts an unhealthy container, so the process also watches its own file
(`exit_when_stale`, a daemon thread): a main loop that has not beaten for STALE_EXIT_S (a call that never returns, a
blocked event loop) ends the process with EX_STALLED (75), and every platform's restart policy starts a fresh one.

The file lives in the temp folder (`/tmp` in the image, a tmpfs with the read-only root filesystem), so it never
outlives the container, and on a host it is per user; standard library only (the healthcheck runs every 30 s).
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

EX_STALLED = 75  # sysexits.h EX_TEMPFAIL (the quotes recorder's own stall exit uses the same code)
# A recorder's main loop that has not beaten for this long ends the process: well past the healthcheck's limit
# (ops.ALIVE_DEFAULT_S: 60 s / 180 s), so the healthcheck reports it first and a slow pass is never cut short.
STALE_EXIT_S = {"pm-quotes": 300.0, "pm-settle": 900.0}


def path(name: str) -> Path:
    """The heartbeat file of recorder `name`, in the temp folder (TMPDIR moves it)."""
    return Path(tempfile.gettempdir()) / f"settleview-{name}.alive"


def beat(p: Path) -> None:
    """Mark `p` alive now (never raises: a full or read-only temp folder only makes the healthcheck fail)."""
    try:
        p.touch(exist_ok=True)
        os.utime(p, None)
    except OSError:
        pass


def age_s(p: Path, now: float | None = None) -> float | None:
    """Seconds since the last beat, None when there was none."""
    try:
        m = p.stat().st_mtime
    except OSError:
        return None
    return max(0.0, (time.time() if now is None else now) - m)


def exit_when_stale(p: Path, max_age_s: float, *, check_s: float = 10.0,
                    exit: Callable[[int], None] = os._exit) -> threading.Event:
    """Start a daemon thread that ends the process with EX_STALLED once `p` has not been beaten for `max_age_s`;
    setting the returned event stops the watch (the recorder's own end). `os._exit`: the stuck main thread cannot be
    interrupted or joined; a recorder keeps nothing unwritten that a clean exit would save (its rows are written per
    tick, and a stuck tick writes nothing)."""
    done = threading.Event()

    def watch() -> None:
        while not done.wait(check_s):
            age = age_s(p)
            if age is not None and age > max_age_s:
                log.error("the main loop has not beaten for %.0f s (limit %.0f s): exiting with %d so the restart "
                          "policy starts a fresh process", age, max_age_s, EX_STALLED)
                logging.shutdown()
                exit(EX_STALLED)
                return

    threading.Thread(target=watch, name="heartbeat-watch", daemon=True).start()
    return done
