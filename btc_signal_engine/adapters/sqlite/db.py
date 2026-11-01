"""Database engines (one writer, read-only readers), programmatic migrations, and the daily maintenance's database work
(the online backup and the Coinbase trade archive, R18 / R19), for both places the app's data can live:

- **SQLite** (a `Path`, BTCFLOW_DB_PATH): tests and hosts without Docker. WAL, synchronous=FULL, busy_timeout; the
  readers open the file with `mode=ro`.
- **Postgres** (a `URL`, BTCFLOW_DATABASE_URL; docs/plans/2026-10-31-0900-feat-postgres-plan.md): every deployment.
  The app's tables live in the schema `app`, the research recorders' in `research` (each engine sets its
  `search_path`, so the SQL is the same as on SQLite); readers run with `default_transaction_read_only=on`.

The SQL itself is dialect-neutral SQLAlchemy (store.py); only the connection set-up, the backup and the archive move
differ here. Every engine made here (and by research.py) is handed to each of `ENGINE_HOOKS` first: the composition
root's telemetry traces its statements (bootstrap/telemetry.py); empty unless OpenTelemetry is on.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import zlib
from pathlib import Path
from collections.abc import Callable

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.exc import OperationalError

from btc_signal_engine.adapters.sqlite import urls
from btc_signal_engine.adapters.sqlite.urls import DbTarget, describe, is_postgres

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
APP_SCHEMA = "app"
RESEARCH_SCHEMA = "research"
ENGINE_HOOKS: list[Callable[[Engine], None]] = []
log = logging.getLogger(__name__)


def made(engine: Engine) -> Engine:
    """`engine`, after every ENGINE_HOOKS hook has seen it."""
    for hook in list(ENGINE_HOOKS):
        hook(engine)
    return engine


def _apply_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    # FULL: a committed bracket survives a power cut. Measured on a copy of the real database (2026-10-30): a writer
    # flush commits in 0.6 ms median / 3.4 ms max at the mean trade rate and 2.0 / 6.7 ms at the busiest second seen
    # (NORMAL: 0.1 / 1.4 ms), far below the 250 ms flush interval, so the writer's backlog stays bounded.
    cur.execute("PRAGMA synchronous=FULL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


# TCP keepalives (libpq): a half-open connection (a server that vanished, a NAT that forgot the flow) is noticed in
# about 10 + 5 x 3 = 25 s instead of the OS default of hours. keepalives_count has no effect on Windows.
KEEPALIVES = {"keepalives": 1, "keepalives_idle": 10, "keepalives_interval": 5, "keepalives_count": 3}


def pg_connect_args(schema: str, *, read_only: bool = False, name: str = "settleview", long: bool = False,
                    timeouts: urls.PgTimeouts | None = None) -> dict:
    """psycopg connect arguments: the schema, the read-only flag and the server-side timeouts (urls.PgTimeouts; `long`
    for migrations and bulk moves), libpq keepalives, a 10 s connect timeout and the application name."""
    t = timeouts if timeouts is not None else urls.PG_TIMEOUTS
    stmt = t.migration_statement_ms if long else t.statement_ms
    options = (f"-c search_path={schema} -c statement_timeout={int(stmt)} -c lock_timeout={int(t.lock_ms)} "
               f"-c idle_in_transaction_session_timeout={int(t.idle_in_transaction_ms)}")
    if read_only:
        options += " -c default_transaction_read_only=on"
    return {"options": options, "application_name": name, "connect_timeout": 10, **KEEPALIVES}


def pg_engine(url, schema: str, *, read_only: bool = False, name: str = "settleview", pool_size: int = 5,
              long: bool = False) -> Engine:
    """A Postgres engine whose connections use `schema` (search_path) and, for readers, refuse every write. Postgres's
    default synchronous_commit=on gives the same guarantee as SQLite's synchronous=FULL: a commit is on disk. Every
    connection carries `pg_connect_args`'s timeouts and keepalives; a checkout waits at most pool_timeout_s for a free
    connection and is pinged first (a dead pooled connection is replaced, never handed out). Errors never show bound
    parameters (`hide_parameters`: row values, which can be account data, stay out of logs)."""
    return made(create_engine(url, pool_pre_ping=True, pool_size=pool_size, max_overflow=pool_size, pool_recycle=1800,
                              pool_timeout=urls.PG_TIMEOUTS.pool_timeout_s, hide_parameters=True,
                              connect_args=pg_connect_args(schema, read_only=read_only, name=name, long=long)))


def write_engine(target: DbTarget, *, name: str = "settleview-app") -> Engine:
    if is_postgres(target):
        return pg_engine(target, APP_SCHEMA, name=name)
    db_path = Path(target)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", hide_parameters=True)
    event.listen(engine, "connect", _apply_pragmas)
    return made(engine)


def read_engine(target: DbTarget, *, name: str = "settleview-app-read") -> Engine:
    """Read-only connections: SQLite's `mode=ro`, Postgres's read-only transactions; any write fails in the database."""
    if is_postgres(target):
        return pg_engine(target, APP_SCHEMA, read_only=True, name=name)
    db_path = Path(target)
    engine = create_engine(f"sqlite:///file:{db_path.resolve().as_posix()}?mode=ro&uri=true", hide_parameters=True)

    def _ro_pragmas(dbapi_conn, _record) -> None:
        dbapi_conn.execute("PRAGMA busy_timeout=5000")

    event.listen(engine, "connect", _ro_pragmas)
    return made(engine)


def lock_key(name: str) -> int:
    """A stable advisory-lock key for `name` (pg_advisory_xact_lock takes a bigint)."""
    return zlib.crc32(f"settleview:{name}".encode())


def ensure_schema(conn: Connection, schema: str) -> None:
    """Create a Postgres schema if it is missing, inside the caller's transaction and under an advisory lock (two
    services starting together do not race). Looked up first: CREATE needs a database privilege the app's role may
    not have where an administrator made the schemas (compose's init script, a managed server's setup)."""
    conn.execute(text("select pg_advisory_xact_lock(:k)"), {"k": lock_key(f"schema:{schema}")})
    if conn.execute(text("select 1 from pg_namespace where nspname = :s"), {"s": schema}).first() is None:
        conn.execute(text(f'create schema "{schema}"'))


def migrate(target: DbTarget) -> None:
    """Upgrade the database to the latest schema (idempotent). On Postgres the whole upgrade is one transaction
    (transactional DDL) under an advisory lock, so two processes starting together never migrate twice.

    A table lock held by someone else (the backup service's pg_dump holds ACCESS SHARE on every table for its whole
    run, ~30 s; docs/solutions/2026-11-01-migration-vs-backup-lock.md) is waited out, never a crash: each attempt
    takes its locks under the short lock_timeout (so the app's own queries never queue behind a waiting ALTER for
    longer than that), and a lock timeout rolls the attempt back and retries with backoff, logging who holds the lock,
    for at most `PgTimeouts.migration_lock_wait_s` (BTCFLOW_DB_MIGRATION_LOCK_WAIT_S) before it gives up."""
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    if not is_postgres(target):
        db_path = Path(target)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
        command.upgrade(cfg, "head")
        return
    t = urls.PG_TIMEOUTS
    engine = pg_engine(target, APP_SCHEMA, name="settleview-migrate", pool_size=1, long=True)
    deadline = time.monotonic() + t.migration_lock_wait_s
    attempt = 0
    try:
        while True:
            attempt += 1
            try:
                _upgrade_pg(engine, cfg, t)
                if attempt > 1:
                    log.info("migration: done after %d attempts (a lock was waited out)", attempt)
                return
            except OperationalError as e:
                left = deadline - time.monotonic()
                if not is_lock_timeout(e) or left <= 0:
                    raise
                pause = min(MIGRATION_RETRY_MAX_S, MIGRATION_RETRY_FIRST_S * 2 ** (attempt - 1), left)
                log.warning("migration: waiting for a lock held by %s; attempt %d, retrying in %.1f s (gives up "
                            "after %.0f s more)", lock_holders(engine) or "another session (gone now)", attempt,
                            pause, left)
                time.sleep(pause)
    finally:
        engine.dispose()


# Backoff between migration attempts that hit a lock: 1, 2, 4, 5, 5, ... seconds.
MIGRATION_RETRY_FIRST_S = 1.0
MIGRATION_RETRY_MAX_S = 5.0
LOCK_NOT_AVAILABLE = "55P03"  # SQLSTATE of a lock_timeout


def _upgrade_pg(engine: Engine, cfg: Config, t: urls.PgTimeouts) -> None:
    with engine.begin() as conn:
        # Wait for another process's migration as long as it takes (the long statement timeout bounds it), then
        # take every table lock under the short lock_timeout: DDL never queues behind a long-running query.
        conn.execute(text("set local lock_timeout = 0"))
        conn.execute(text("select pg_advisory_xact_lock(:k)"), {"k": lock_key("migrate")})
        conn.execute(text(f"set local lock_timeout = {int(t.lock_ms)}"))
        ensure_schema(conn, APP_SCHEMA)
        ensure_schema(conn, RESEARCH_SCHEMA)
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")


def is_lock_timeout(e: BaseException) -> bool:
    """A Postgres lock_timeout (SQLSTATE 55P03), as SQLAlchemy raises it."""
    return getattr(getattr(e, "orig", None), "sqlstate", None) == LOCK_NOT_AVAILABLE


def lock_holders(engine: Engine) -> str:
    """Who holds a granted lock on a table of the app's schema right now, for the log: application name, role and pid
    of each other session (`pg_dump as settleview_backup, pid 4242`). Names only (pg_stat_activity shows them to every
    role); never a query text. Empty if nobody does or the lookup fails."""
    try:
        with engine.connect() as c:
            rows = c.execute(text(
                "select distinct a.application_name, a.usename, a.pid from pg_locks l "
                "join pg_stat_activity a on a.pid = l.pid join pg_class r on r.oid = l.relation "
                "join pg_namespace n on n.oid = r.relnamespace "
                "where l.granted and n.nspname = :s and l.pid <> pg_backend_pid() order by a.pid"),
                {"s": APP_SCHEMA}).all()
    except Exception:  # only a log line's detail: never let it hide the migration's own error
        log.debug("migration: the lock-holder lookup failed", exc_info=True)
        return ""
    return ", ".join(f"{app or '?'} as {user or '?'}, pid {pid}" for app, user, pid in rows)


# Rows moved per archive batch. The delete holds the database's write lock, which trading's synchronous writes share;
# measured on a copy of the real database (2026-10-30), a 50,000-row delete commits in ~60 ms median / ~100 ms max
# (100,000 rows: ~160 / ~185 ms), well under the 500 ms bar.
ARCHIVE_BATCH = 50_000
_TRADE_COLS = "trade_id, ts, ingest_ts, price, size, buy, backfilled"


def online_backup(db_path: Path, dst: Path) -> None:
    """SQLite only (Postgres is backed up by the compose `backup` service or the managed server). Copy the database to
    `dst` with SQLite's online backup API while the app keeps writing. The copy is one step (one read transaction): a
    backup made in steps restarts whenever another connection writes, and against the live writer it never finishes.
    The copy is switched out of WAL mode. Written to `<dst>.part` first and renamed, so a failed backup leaves no file
    named `dst`."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".part")
    src = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True, timeout=5.0)
    try:
        out = sqlite3.connect(part)
        try:
            src.backup(out)
            out.execute("PRAGMA journal_mode=DELETE")  # a single self-contained file: opening it adds no -wal / -shm
        finally:
            out.close()
    finally:
        src.close()
    os.replace(part, dst)


def archive_trades(db_path: Path, archive_path: Path, cutoff_ms: int, *, batch: int = ARCHIVE_BATCH,
                   busy_timeout_s: float = 5.0, stop: Callable[[], bool] | None = None) -> int:
    """SQLite only (Postgres: `archive_trades_pg`). Move Coinbase `trades` rows with `ts < cutoff_ms` into
    `archive_path` (created with the live table's own schema), `batch` rows at a time, oldest first; returns how many
    moved. Each batch is copied (insert-or-ignore) and committed in the archive, every copied `trade_id` is checked to
    be there, and only then deleted from the live database in its own short transaction. An interrupted run leaves
    rows in both files, never in neither, and a re-run finishes it. `stop()`, checked before each batch, ends the run
    early (a shutdown). No other table is touched. With nothing older than the cutoff, the archive file is not even
    created."""
    live = sqlite3.connect(db_path, isolation_level=None, timeout=busy_timeout_s)
    arch = None
    moved = 0
    try:
        live.execute("PRAGMA synchronous=FULL")
        select = f"SELECT {_TRADE_COLS} FROM trades WHERE ts < ? ORDER BY ts LIMIT ?"  # noqa: S608 - constant column list, values bound
        while stop is None or not stop():
            rows = live.execute(select, (cutoff_ms, batch)).fetchall()
            if not rows:
                break
            if arch is None:
                arch = sqlite3.connect(archive_path, isolation_level=None, timeout=busy_timeout_s)
                arch.execute("PRAGMA synchronous=FULL")
                for (ddl,) in live.execute("SELECT sql FROM sqlite_master WHERE tbl_name = 'trades' AND sql IS NOT "
                                           "NULL ORDER BY type DESC"):  # the table, then its indexes
                    arch.execute(ddl.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1)
                                 .replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1))
            ids = json.dumps([r[0] for r in rows])
            arch.execute("BEGIN IMMEDIATE")
            arch.executemany(f"INSERT OR IGNORE INTO trades({_TRADE_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)  # noqa: S608 - constant column list, values bound
            arch.execute("COMMIT")
            have = arch.execute("SELECT count(*) FROM trades WHERE trade_id IN (SELECT value FROM json_each(?))",
                                (ids,)).fetchone()[0]
            if have != len(rows):
                raise RuntimeError(f"archive check failed: {have} of {len(rows)} copied trades found; nothing deleted")
            live.execute("BEGIN IMMEDIATE")
            try:
                live.execute("DELETE FROM trades WHERE trade_id IN (SELECT value FROM json_each(?))", (ids,))
                live.execute("COMMIT")
            except BaseException:
                live.execute("ROLLBACK")
                raise
            moved += len(rows)
    finally:
        live.close()
        if arch is not None:
            arch.close()
    return moved


# One Postgres batch: rows leave `trades` and land in `trades_archive` in one transaction, so a row is always in
# exactly one of them (no copy-check-delete as between SQLite's two files). A trade already in the archive (only after
# importing an old archive file) keeps its archived copy.
_PG_ARCHIVE_BATCH = text(f"""
with moved as (
  delete from trades where trade_id in (select trade_id from trades where ts < :cutoff order by ts limit :batch)
  returning {_TRADE_COLS}),
ins as (insert into trades_archive ({_TRADE_COLS}) select {_TRADE_COLS} from moved on conflict (trade_id) do nothing)
select count(*) from moved""")  # noqa: S608 - constant column list, values bound


def archive_trades_pg(engine: Engine, cutoff_ms: int, *, batch: int = ARCHIVE_BATCH,
                      stop: Callable[[], bool] | None = None) -> int:
    """Postgres: move Coinbase `trades` rows with `ts < cutoff_ms` to `trades_archive`, `batch` rows per transaction,
    oldest first; returns how many moved. `stop()` is checked before each batch. New trades keep being written
    meanwhile (row locks only)."""
    moved = 0
    while stop is None or not stop():
        with engine.begin() as c:
            # A bulk move at a quiet minute: it may run longer than the app's statement timeout.
            c.execute(text(f"set local statement_timeout = {int(urls.PG_TIMEOUTS.migration_statement_ms)}"))
            n = int(c.execute(_PG_ARCHIVE_BATCH, {"cutoff": cutoff_ms, "batch": batch}).scalar() or 0)
        if n == 0:
            break
        moved += n
    return moved


__all__ = ["APP_SCHEMA", "KEEPALIVES", "RESEARCH_SCHEMA", "DbTarget", "archive_trades", "archive_trades_pg",
           "describe", "ensure_schema", "is_lock_timeout", "is_postgres", "lock_holders", "lock_key", "migrate", "online_backup", "pg_connect_args",
           "pg_engine", "read_engine", "write_engine"]
