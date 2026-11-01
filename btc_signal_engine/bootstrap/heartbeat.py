"""Process-local liveness for the research recorders (docs/plans/2026-10-31-1800-review-fix-plan.md P2, B4#16).

A recorder touches its heartbeat file from its own main loop (the quotes recorder's watchdog every second, the
settlement recorder once per pass). Its container healthcheck (`python -m btc_signal_engine.ops alive <name>`) reads
only that file's age: the process is alive and its loop turns. Whether data is flowing (a venue or database outage)
is a different question, answered by the newest rows (`ops fresh`, the app's `data` alert, /api/health `recorders`),
never by a restart: a restarted recorder cannot bring a venue's book or the database back.

The file lives in the temp folder (`/tmp` in the image, a tmpfs with the read-only root filesystem), so it never
outlives the container, and on a host it is per user; standard library only (the healthcheck runs every 30 s).
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path


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
