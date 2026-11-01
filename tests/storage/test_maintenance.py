"""Daily maintenance (U14, R18, R19): the online backup, backup pruning (7 daily + 4 weekly), the Coinbase trade archive
(copy, verify, then delete; rows older than the retention window move to `<db>-archive.db`), the quiet-minute schedule,
and the write engine's synchronous=FULL. Every test builds its own databases; none touches data/btcflow.db."""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
import time

import pytest

from btc_signal_engine.adapters.sqlite.db import archive_trades, online_backup, read_engine, write_engine
from btc_signal_engine.adapters.sqlite.store import SqliteStore
from btc_signal_engine.bootstrap.maintenance import DailyMaintenance, prune_backups, seconds_to_quiet_minute
from btc_signal_engine.domain.models.events import IndexSecond, Trade

@pytest.fixture
def store(db_path):
    """These tests are about SQLite's own files (WAL, the online backup, the archive file): always SQLite, also on a
    Postgres run (Postgres's archive: tests/storage/test_postgres.py)."""
    return SqliteStore(write_engine(db_path), read_engine(db_path))


DAY = 86_400_000
NOW = int(dt.datetime(2026, 10, 30, 12, 2, tzinfo=dt.UTC).timestamp() * 1000)


def _trade(i: int, ts: int) -> Trade:
    return Trade(trade_id=i, ts=ts, ingest_ts=ts + 50, price=70_000.0 + i, size=0.01, buy=i % 2 == 0)


def _ids(path, where: str = "1") -> list[int]:
    with sqlite3.connect(path) as c:
        return [r[0] for r in c.execute(f"SELECT trade_id FROM trades WHERE {where} ORDER BY trade_id")]


def _seed(store, old: int, new: int) -> None:
    """`old` trades 61-70 days back (ids 0..old-1), then `new` trades in the last day (ids old..old+new-1)."""
    store.add_trades(_trade(i, NOW - 70 * DAY + i * (9 * DAY // max(old, 1))) for i in range(old))
    store.add_trades(_trade(old + i, NOW - DAY + i * 1000) for i in range(new))


def test_the_write_engine_commits_with_synchronous_full(db_path):
    with write_engine(db_path).connect() as conn:
        assert conn.exec_driver_sql("PRAGMA synchronous").scalar() == 2  # FULL (NORMAL is 1)


def test_a_backup_taken_while_the_database_is_written_is_a_consistent_copy(store, db_path, tmp_path):
    stop = threading.Event()
    written = [0]

    def keep_writing():
        i = 0
        while not stop.is_set():
            store.add_trades([_trade(i, NOW + i)])  # one commit per trade, ids in order
            i += 1
            written[0] = i

    t = threading.Thread(target=keep_writing)
    t.start()
    try:
        while written[0] < 200:
            pass
        dst = tmp_path / "backups" / "btcflow-2026-10-30.db"
        online_backup(db_path, dst)
        during = written[0]
    finally:
        stop.set()
        t.join()
    with sqlite3.connect(dst) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        n, top = c.execute("SELECT count(*), max(trade_id) FROM trades").fetchone()
    # The copy is one moment of the database: every committed trade up to some point, none missing in between.
    assert 200 <= n <= during + 1 and top == n - 1
    assert not list(dst.parent.glob("*.part"))


def test_pruning_keeps_seven_daily_and_four_weekly_backups(tmp_path):
    day = dt.date(2026, 8, 1)
    while day <= dt.date(2026, 10, 30):
        (tmp_path / f"btcflow-{day.isoformat()}.db").write_bytes(b"x")
        day += dt.timedelta(days=1)
    (tmp_path / "notes.txt").write_text("not a backup")
    (tmp_path / "other-2026-08-01.db").write_bytes(b"another database's file")
    prune_backups(tmp_path, "btcflow")
    kept = sorted(p.name for p in tmp_path.iterdir())
    # 2026-10-30 is a Friday. Daily: Oct 24-30. Weekly, the newest older backup of each of the 4 newest ISO weeks:
    # Fri Oct 23 (week of Oct 19), Sun Oct 18, Sun Oct 11, Sun Oct 4.
    assert kept == sorted([f"btcflow-2026-10-{d:02d}.db" for d in (4, 11, 18, 23, 24, 25, 26, 27, 28, 29, 30)]
                          + ["notes.txt", "other-2026-08-01.db"])


def test_archiving_moves_only_rows_older_than_the_window(store, db_path, tmp_path):
    _seed(store, old=30, new=20)
    store.add_index_seconds([IndexSecond(NOW - 70 * DAY, 70_000.0, NOW - 70 * DAY)])
    archive = tmp_path / "test-archive.db"
    moved = archive_trades(db_path, archive, NOW - 60 * DAY, batch=7)
    assert moved == 30
    assert _ids(archive) == list(range(30))
    assert _ids(db_path) == list(range(30, 50))
    with sqlite3.connect(archive) as a, sqlite3.connect(db_path) as live:
        cols = "trade_id, ts, ingest_ts, price, size, buy, backfilled"
        assert a.execute(f"SELECT {cols} FROM trades WHERE trade_id = 3").fetchone()[1:] == (
            NOW - 70 * DAY + 3 * (9 * DAY // 30), NOW - 70 * DAY + 3 * (9 * DAY // 30) + 50, 70_003.0, 0.01, 0, 0)
        assert live.execute("SELECT count(*) FROM index_1s").fetchone()[0] == 1  # index_1s is never archived
        idx = a.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL").fetchall()
        assert idx == [("ix_trades_ts",)]  # the live table's own schema, its ts index included


def test_an_interrupted_archive_run_only_duplicates_into_the_archive_and_a_rerun_finishes(store, db_path, tmp_path):
    _seed(store, old=10, new=5)
    archive = tmp_path / "test-archive.db"
    blocker = sqlite3.connect(db_path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")  # another writer holds the lock: the first delete cannot commit
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            archive_trades(db_path, archive, NOW - 60 * DAY, batch=4, busy_timeout_s=0.1)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert _ids(archive) == [0, 1, 2, 3]  # copied before the delete was tried ...
    assert _ids(db_path) == list(range(15))  # ... and nothing is gone from the live database
    assert archive_trades(db_path, archive, NOW - 60 * DAY, batch=4) == 10
    assert _ids(archive) == list(range(10))
    assert _ids(db_path) == list(range(10, 15))


def test_an_archive_copy_that_comes_up_short_deletes_nothing(store, db_path, tmp_path):
    _seed(store, old=10, new=5)
    archive = tmp_path / "test-archive.db"
    with sqlite3.connect(db_path) as live:
        ddl = live.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'trades'").fetchone()[0]
    with sqlite3.connect(archive) as a:  # the archive's insert silently drops trade 2 (no error, one row short)
        a.execute(ddl)
        a.execute("CREATE TRIGGER drop_one BEFORE INSERT ON trades WHEN NEW.trade_id = 2 "
                  "BEGIN SELECT RAISE(IGNORE); END")
    with pytest.raises(RuntimeError, match="archive check failed: 3 of 4 copied trades found; nothing deleted"):
        archive_trades(db_path, archive, NOW - 60 * DAY, batch=4)
    assert _ids(archive) == [0, 1, 3]
    assert _ids(db_path) == list(range(15))  # every live row is still there, trade 2 included


def test_a_database_with_nothing_older_than_the_window_is_left_alone(store, db_path, tmp_path):
    _seed(store, old=0, new=10)
    before = db_path.read_bytes()
    archive = tmp_path / "test-archive.db"
    assert archive_trades(db_path, archive, NOW - 60 * DAY) == 0
    assert not archive.exists()
    assert _ids(db_path) == list(range(10)) and db_path.read_bytes() == before


def test_a_synchronous_store_insert_during_an_archive_run_does_not_hit_a_lock_error(store, db_path, tmp_path):
    _seed(store, old=60_000, new=0)
    errors, done = [], threading.Event()

    def run():
        try:
            archive_trades(db_path, tmp_path / "test-archive.db", NOW - 60 * DAY, batch=500)
        except Exception as e:  # pragma: no cover - reported by the assertion below
            errors.append(e)
        finally:
            done.set()

    t = threading.Thread(target=run)
    t.start()
    inserted = 0
    while not done.is_set():
        row = {"horizon": 60, "boundary": NOW + inserted * 60_000, "ruleset_hash": "abc", "run_id": 0, "call": "UP",
               "explanation": "x", "conditions": [], "flags": [], "created_ts": inserted}
        store.insert("decisions", [row])
        inserted += 1
        time.sleep(0.005)  # trading writes come now and then, not back to back
    t.join()
    assert errors == [] and inserted > 5
    assert len(store.select("decisions")) == inserted
    assert _ids(db_path) == []


def test_an_archive_run_stops_between_batches_when_asked(store, db_path, tmp_path):
    _seed(store, old=10, new=0)
    archive = tmp_path / "test-archive.db"
    calls = []
    moved = archive_trades(db_path, archive, NOW - 60 * DAY, batch=3, stop=lambda: calls.append(1) or len(calls) > 2)
    assert moved == 6 and _ids(archive) == list(range(6)) and _ids(db_path) == list(range(6, 10))


def test_the_daily_job_backs_up_archives_and_prunes_once_a_day(store, db_path, tmp_path):
    _seed(store, old=10, new=5)
    backups = tmp_path / "backups"
    backups.mkdir()
    for d in range(1, 20):  # older backups: with today's, pruned to the 7 newest + the newest of 4 older ISO weeks
        (backups / f"test-2026-10-{d:02d}.db").write_bytes(b"x")
    job = DailyMaintenance(db_path, retention_days=60, now_ms=lambda: NOW)
    job.run_once()
    today = backups / "test-2026-10-30.db"
    with sqlite3.connect(today) as c:  # the backup is taken before the archive moves anything
        assert c.execute("SELECT count(*) FROM trades").fetchone()[0] == 15
    assert _ids(tmp_path / "test-archive.db") == list(range(10)) and _ids(db_path) == list(range(10, 15))
    assert len(list(backups.iterdir())) == 10  # daily: Oct 14-19 + today; weekly: Oct 13, 11, 4
    store.add_trades([_trade(99, NOW - 65 * DAY)])
    job.run_once()  # the same UTC day: already done
    assert _ids(db_path) == [10, 11, 12, 13, 14, 99]


@pytest.mark.parametrize("now_s, wait_s", [
    (0, 120), (120, 900), (119, 1), (121, 899), (1_200, 720), (86_400 - 1, 121)])
def test_the_job_waits_for_two_minutes_past_the_next_quarter_hour(now_s, wait_s):
    assert seconds_to_quiet_minute(now_s * 1000) == wait_s
