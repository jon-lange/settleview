"""Database locations: a SQLite file (a `Path`) or a Postgres server (a SQLAlchemy `URL`).

`postgres_url` turns the BTCFLOW_DATABASE_URL setting into the URL the engines use. Only `postgresql://` and
`postgresql+psycopg://` are accepted (psycopg 3 is the one driver); anything else is an error, never a quiet fallback
to SQLite. Error messages name the setting, never the value (it may hold a password). Kept free of the engine code so
the light commands (ops, supervisor) can parse settings without loading Alembic.

`PgTimeouts` (here for the same reason) are the server-side limits every Postgres connection is opened with
(db.pg_engine); the composition root sets them from the settings once, with `use_pg_timeouts`.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

DbTarget = Path | URL
DRIVER = "postgresql+psycopg"


def postgres_url(raw: str, password: str | None = None, *, setting: str = "BTCFLOW_DATABASE_URL") -> URL:
    """The psycopg URL for `raw`, with `password` (from its own setting, typically a secret file) filled in."""
    try:
        url = make_url(raw.strip())
    except (ArgumentError, ValueError):  # never chained: the message could quote the URL
        raise ValueError(f"{setting} is not a database URL (expected postgresql://user@host:port/database)") from None
    if url.drivername not in ("postgresql", DRIVER):
        raise ValueError(f"{setting} must be a postgresql:// or {DRIVER}:// URL (SQLite is BTCFLOW_DB_PATH)")
    if not url.host or not url.database:
        raise ValueError(f"{setting} needs a host and a database name (postgresql://user@host:port/database)")
    if not url.username:
        raise ValueError(f"{setting} needs a user name (postgresql://user@host:port/database)")
    if password:
        if url.password:
            raise ValueError(f"{setting} already holds a password and the matching _PASSWORD setting is set: use one")
        url = url.set(password=password)
    return url.set(drivername=DRIVER)


def is_postgres(target: DbTarget) -> bool:
    return isinstance(target, URL)


def describe(target: DbTarget) -> str:
    """A log-safe name for the database: the file, or the Postgres URL without its password."""
    if is_postgres(target):
        return target.render_as_string(hide_password=True)
    return str(target)


@dataclasses.dataclass(frozen=True)
class PgTimeouts:
    """Server-side limits for every Postgres connection (milliseconds; 0 = none), so a lock, a stuck statement or a
    forgotten transaction fails within seconds instead of freezing the caller (docs/operations.md, "Postgres:
    timeouts"). Migrations and bulk moves get `migration_statement_ms` (long DDL and COPY) but keep the short
    `lock_ms`, so a migration never queues behind a long query while every later query queues behind it; a migration
    that hits it retries with backoff for up to `migration_lock_wait_s` (db.migrate) instead of failing at once."""
    statement_ms: int = 5_000  # statement_timeout
    lock_ms: int = 2_000  # lock_timeout
    idle_in_transaction_ms: int = 30_000  # idle_in_transaction_session_timeout
    migration_statement_ms: int = 600_000  # statement_timeout of migrations, the SQLite -> Postgres move, archiving
    migration_lock_wait_s: float = 120.0  # how long a migration keeps retrying a lock someone else holds (pg_dump)
    pool_timeout_s: float = 5.0  # waiting for a free pooled connection (SQLAlchemy pool_timeout)


PG_TIMEOUTS = PgTimeouts()


def use_pg_timeouts(t: PgTimeouts) -> None:
    """The limits every engine made from now on uses (set by the composition root from the settings)."""
    global PG_TIMEOUTS
    PG_TIMEOUTS = t
