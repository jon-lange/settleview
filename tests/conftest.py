"""Shared fixtures. The store runs on SQLite by default; with a Postgres server configured it runs on Postgres:

    BTCFLOW_TEST_DATABASE_URL=postgresql://postgres:<password>@localhost:5432/postgres pytest
    pytest --db-url postgresql://postgres:<password>@localhost:5432/postgres

The URL names a role that may create databases (CI: the service container's superuser). The run makes its own
database (`settleview_test`, dropped first if it exists), migrates it once, and empties every table before each test
that uses `store`. Tests marked `postgres` need that server and are skipped without it; everything else runs on both.
Tests that build their own files (`db_path`, `Runtime(Settings(db_path=...))`, the migration-history tests) stay on
SQLite in both runs.
"""

import os
import shutil
import sqlite3

import pytest

from btc_signal_engine.adapters.sqlite.db import migrate, read_engine, write_engine
from btc_signal_engine.adapters.sqlite.store import SqliteStore

TEST_DB = "settleview_test"


def pytest_addoption(parser):
    parser.addoption("--db-url", default=None,
                     help="run the store on this Postgres server (a role that may create databases); "
                          "default: $BTCFLOW_TEST_DATABASE_URL, else SQLite only")


def pytest_configure(config):
    config.addinivalue_line("markers", "postgres: needs a Postgres server (--db-url / BTCFLOW_TEST_DATABASE_URL)")


def _admin_url(config):
    raw = config.getoption("--db-url") or os.environ.get("BTCFLOW_TEST_DATABASE_URL")
    if not raw:
        return None
    from btc_signal_engine.adapters.sqlite.urls import postgres_url
    return postgres_url(raw, setting="BTCFLOW_TEST_DATABASE_URL")


def pytest_collection_modifyitems(config, items):
    if _admin_url(config) is not None:
        return
    skip = pytest.mark.skip(reason="needs a Postgres server (--db-url or BTCFLOW_TEST_DATABASE_URL)")
    for item in items:
        if "postgres" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _log_record_factory():
    """main() installs the log scrubber's record factory process-wide (bootstrap/logs.py): put the previous one back
    after each test, so one test's entry point does not change what later tests' caplog records read."""
    import logging
    factory = logging.getLogRecordFactory()
    yield
    logging.setLogRecordFactory(factory)


@pytest.fixture(scope="session")
def pg_admin_url(pytestconfig):
    """The server's admin URL, or a skip."""
    url = _admin_url(pytestconfig)
    if url is None:
        pytest.skip("needs a Postgres server (--db-url or BTCFLOW_TEST_DATABASE_URL)")
    return url


def fresh_pg_database(admin_url, name: str):
    """Drop and create database `name` on the admin URL's server; returns its URL (same role)."""
    from sqlalchemy import create_engine, text
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text(f'drop database if exists "{name}" with (force)'))
            c.execute(text(f'create database "{name}"'))
    finally:
        admin.dispose()
    return admin_url.set(database=name)


@pytest.fixture(scope="session")
def pg_url(pytestconfig):
    """The run's migrated Postgres test database, or None on a SQLite-only run."""
    admin = _admin_url(pytestconfig)
    if admin is None:
        return None
    url = fresh_pg_database(admin, TEST_DB)
    migrate(url)
    return url


@pytest.fixture(scope="session")
def _migrated_db(tmp_path_factory):
    """One database migrated to the latest schema per test run (running every migration took ~0.3 s per test)."""
    path = tmp_path_factory.mktemp("schema") / "migrated.db"
    migrate(path)
    with sqlite3.connect(path) as con:  # everything in the main file, so a plain copy is complete
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return path


@pytest.fixture
def db_path(tmp_path, _migrated_db):
    """A fresh, fully migrated database file for this test (a copy of the session's migrated one)."""
    path = tmp_path / "test.db"
    shutil.copyfile(_migrated_db, path)
    return path


def empty_pg_tables(engine) -> None:
    """Every table of the app's schema (and the research schema's, if any) emptied, ids restarting at 1."""
    from sqlalchemy import inspect, text

    from btc_signal_engine.adapters.sqlite.db import RESEARCH_SCHEMA
    from btc_signal_engine.adapters.sqlite.tables import metadata
    with engine.begin() as c:
        names = [f'app."{t.name}"' for t in metadata.sorted_tables]
        names += [f'{RESEARCH_SCHEMA}."{t}"' for t in inspect(c).get_table_names(schema=RESEARCH_SCHEMA)]
        c.execute(text(f"truncate {', '.join(names)} restart identity"))


@pytest.fixture
def store(request, pg_url):
    if pg_url is None:
        path = request.getfixturevalue("db_path")
        yield SqliteStore(write_engine(path), read_engine(path))
        return
    we, re_ = write_engine(pg_url), read_engine(pg_url)
    empty_pg_tables(we)
    try:
        yield SqliteStore(we, re_)
    finally:
        we.dispose()
        re_.dispose()


@pytest.fixture(autouse=True)
def fast_pin_hash(monkeypatch):
    """The phone PIN's PBKDF2 work factor (310,000 iterations, ~0.5 s a hash on this machine) made the access tests
    the slowest in the suite. Tests hash with 1,000 iterations; tests/api/test_access.py pins the production value."""
    from btc_signal_engine.adapters.http import access
    monkeypatch.setattr(access, "PBKDF2_ITERATIONS", 1_000)  # gitleaks:allow - a constant's name, not a secret
