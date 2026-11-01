"""The daily maintenance job (R18, R19): an online backup of the database, then the Coinbase trade archive.

Once per UTC day, at the first quiet minute (two minutes past a quarter-hour boundary) the app sees, a worker thread
(KTD1):
1. backs the database up with SQLite's online backup API to `<db dir>/backups/<stem>-YYYY-MM-DD.db` and prunes the
   backups to the 7 newest plus the newest of each of the 4 most recent older ISO weeks;
2. moves Coinbase `trades` rows older than `trades_retention_days` into `<db dir>/<stem>-archive.db` (copy, verify,
   then delete, in short batches; `adapters/sqlite/db.archive_trades`). `index_1s`, `book_1s` and every trading table
   stay where they are.

On Postgres (BTCFLOW_DATABASE_URL) there is no backup step here (the compose `backup` service, or the managed server's
automated backups, take it) and step 2 moves the rows into `trades_archive` in the same database, one transaction per
batch (`archive_trades_pg`).

Today's backup file is the "done" marker on SQLite: after a restart the job runs at the next quiet minute unless
today's backup already exists. On Postgres the marker is the day of the last run in memory: after a restart the archive
runs again at the next quiet minute (a no-op once nothing is older than the window). A failure is logged with its traceback by the supervisor, which restarts the loop: a failed backup is
retried at the next quiet minute, an archive run that failed (or stopped for a shutdown) continues the next day.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
from pathlib import Path
from collections.abc import Callable

from sqlalchemy import Engine

from btc_signal_engine.adapters.sqlite.db import archive_trades, archive_trades_pg, online_backup
from btc_signal_engine.adapters.sqlite.urls import DbTarget, is_postgres

log = logging.getLogger(__name__)

KEEP_DAILY = 7
KEEP_WEEKLY = 4
QUARTER_MS = 900_000
QUIET_OFFSET_MS = 120_000  # two minutes past the boundary: the window's opening burst of writes is over
DAY_MS = 86_400_000


def seconds_to_quiet_minute(now_ms: int) -> float:
    """Seconds until the next quiet minute strictly after `now_ms` (hh:02, hh:17, hh:32, hh:47)."""
    nxt = ((now_ms - QUIET_OFFSET_MS) // QUARTER_MS + 1) * QUARTER_MS + QUIET_OFFSET_MS
    return (nxt - now_ms) / 1000


def prune_backups(backup_dir: Path, stem: str) -> list[Path]:
    """Delete `<stem>-YYYY-MM-DD.db` backups beyond the 7 newest and, of the older ones, the newest of each of the 4
    most recent ISO weeks. Other files are left alone. Returns the deleted paths."""
    pat = re.compile(rf"{re.escape(stem)}-(\d{{4}}-\d{{2}}-\d{{2}})\.db")
    dated = []
    for p in backup_dir.iterdir():
        m = pat.fullmatch(p.name)
        if m:
            try:
                dated.append((dt.date.fromisoformat(m.group(1)), p))
            except ValueError:
                continue
    dated.sort(reverse=True)
    keep = {p for _, p in dated[:KEEP_DAILY]}
    weeks: dict[tuple[int, int], Path] = {}
    for day, p in dated[KEEP_DAILY:]:
        week = day.isocalendar()[:2]
        if week not in weeks and len(weeks) < KEEP_WEEKLY:
            weeks[week] = p
    keep.update(weeks.values())
    gone = [p for _, p in dated if p not in keep]
    for p in gone:
        p.unlink()
    return gone


class DailyMaintenance:
    def __init__(self, db: DbTarget, retention_days: int, now_ms: Callable[[], int],
                 beat: Callable[[], None] | None = None, stopping: Callable[[], bool] | None = None,
                 engine: Engine | None = None) -> None:
        self.postgres = is_postgres(db)
        if self.postgres and engine is None:
            raise ValueError("Postgres maintenance needs the app's write engine")
        self.engine = engine
        self._last_day: dt.date | None = None  # Postgres: the UTC day of the last run
        self.db_path = Path("btcflow.db") if self.postgres else Path(db)
        self.retention_days = retention_days
        self._now_ms = now_ms
        self._beat = beat
        self._stopping = stopping
        self.backup_dir = self.db_path.parent / "backups"
        self.archive_path = self.db_path.with_name(f"{self.db_path.stem}-archive.db")

    def _backup_path(self, now_ms: int) -> Path:
        day = dt.datetime.fromtimestamp(now_ms / 1000, dt.UTC).date()
        return self.backup_dir / f"{self.db_path.stem}-{day.isoformat()}.db"

    def run_once(self) -> None:
        """Today's backup, pruning and archive (blocking: run it in a worker thread). Does nothing if today's backup
        exists (Postgres: if it already ran today)."""
        now = self._now_ms()
        if self.postgres:
            day = dt.datetime.fromtimestamp(now / 1000, dt.UTC).date()
            if self._last_day == day:
                return
            t0 = time.monotonic()
            moved = archive_trades_pg(self.engine, now - self.retention_days * DAY_MS, stop=self._stopping)
            log.info("maintenance: archived %d Coinbase trades older than %d days to trades_archive in %.1f s", moved,
                     self.retention_days, time.monotonic() - t0)
            if self._stopping is None or not self._stopping():
                self._last_day = day
            return
        dst = self._backup_path(now)
        if dst.exists():
            return
        t0 = time.monotonic()
        online_backup(self.db_path, dst)
        log.info("maintenance: backed up the database to %s (%.0f MB) in %.1f s", dst, dst.stat().st_size / 2**20,
                 time.monotonic() - t0)
        gone = prune_backups(self.backup_dir, self.db_path.stem)
        if gone:
            log.info("maintenance: pruned %d old backups: %s", len(gone), ", ".join(p.name for p in gone))
        t0 = time.monotonic()
        moved = archive_trades(self.db_path, self.archive_path, now - self.retention_days * DAY_MS,
                               stop=self._stopping)
        log.info("maintenance: archived %d Coinbase trades older than %d days to %s in %.1f s", moved,
                 self.retention_days, self.archive_path, time.monotonic() - t0)

    async def run(self) -> None:
        while True:
            if self._beat is not None:
                self._beat()
            await asyncio.sleep(seconds_to_quiet_minute(self._now_ms()))
            await asyncio.to_thread(self.run_once)
