"""Postgres connection limits (docs/plans/2026-10-31-1800-review-fix-plan.md P0 #5, B4#3): every engine carries
statement / lock / idle-in-transaction timeouts, TCP keepalives, a short pool timeout and a pre-ping, and hides bound
parameters in errors. The `postgres`-marked tests check that the server enforces them."""

from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, OperationalError

from btc_signal_engine.adapters.sqlite import urls
from btc_signal_engine.adapters.sqlite.db import KEEPALIVES, migrate, pg_connect_args, pg_engine, write_engine
from btc_signal_engine.adapters.sqlite.urls import PgTimeouts, use_pg_timeouts


@pytest.fixture
def timeouts():
    """Restores the process-wide limits after a test changes them."""
    before = urls.PG_TIMEOUTS
    yield
    use_pg_timeouts(before)


def _opts(args: dict) -> dict[str, str]:
    parts = args["options"].split()
    return dict(p.split("=", 1) for p, flag in zip(parts[1::2], parts[0::2], strict=True) if flag == "-c")


def test_every_connection_gets_the_timeouts_and_keepalives():
    args = pg_connect_args("app", name="settleview-app")
    o = _opts(args)
    assert o == {"search_path": "app", "statement_timeout": "5000", "lock_timeout": "2000",
                 "idle_in_transaction_session_timeout": "30000"}
    assert {k: args[k] for k in KEEPALIVES} == {"keepalives": 1, "keepalives_idle": 10, "keepalives_interval": 5,
                                                "keepalives_count": 3}
    assert args["connect_timeout"] == 10 and args["application_name"] == "settleview-app"


def test_migrations_get_the_long_statement_timeout_but_the_same_short_lock_timeout():
    o = _opts(pg_connect_args("app", long=True))
    assert o["statement_timeout"] == "600000" and o["lock_timeout"] == "2000"
    assert _opts(pg_connect_args("research", read_only=True))["default_transaction_read_only"] == "on"


def test_the_limits_come_from_the_settings(monkeypatch, timeouts):
    from btc_signal_engine.bootstrap.config import Settings
    monkeypatch.setenv("BTCFLOW_DB_STATEMENT_TIMEOUT_MS", "7000")
    monkeypatch.setenv("BTCFLOW_DB_LOCK_TIMEOUT_MS", "1500")
    monkeypatch.setenv("BTCFLOW_DB_POOL_TIMEOUT_S", "3")
    s = Settings(_env_file=None)
    assert s.pg_timeouts == PgTimeouts(statement_ms=7000, lock_ms=1500, pool_timeout_s=3.0)
    use_pg_timeouts(s.pg_timeouts)
    o = _opts(pg_connect_args("app"))
    assert (o["statement_timeout"], o["lock_timeout"]) == ("7000", "1500")
    monkeypatch.setenv("BTCFLOW_DB_LOCK_TIMEOUT_MS", "-1")
    with pytest.raises(ValueError):
        Settings(_env_file=None)


def test_the_engine_has_a_short_pool_timeout_a_pre_ping_and_hides_parameters():
    pytest.importorskip("psycopg")
    e = pg_engine(urls.postgres_url("postgresql://u@db.invalid:5432/x"), "app")
    try:
        assert e.pool._timeout == 5.0 and e.pool._pre_ping is True
        assert e.hide_parameters is True
    finally:
        e.dispose()


def test_sqlite_errors_hide_bound_parameters(tmp_path):
    e = write_engine(tmp_path / "x.db")
    with e.begin() as c:
        c.execute(text("create table t (a integer primary key)"))
        c.execute(text("insert into t values (:a)"), {"a": 424242})
    with pytest.raises(DBAPIError) as err:
        with e.begin() as c:
            c.execute(text("insert into t values (:a)"), {"a": 424242})
    assert "424242" not in str(err.value) and "hidden" in str(err.value)
    e.dispose()


# -- enforced by a real server ----------------------------------------------------------------------------------------
@pytest.mark.postgres
def test_the_server_enforces_the_statement_and_idle_timeouts(pg_url, timeouts):
    use_pg_timeouts(PgTimeouts(statement_ms=300, lock_ms=200, idle_in_transaction_ms=400))
    e = write_engine(pg_url)
    try:
        with e.connect() as c:
            shown = {k: c.execute(text(f"show {k}")).scalar() for k in
                     ("statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout")}
            # Keepalives are the client socket's (libpq), not a server setting: SHOW tcp_keepalives_* is the server's
            libpq = c.connection.dbapi_connection.info.get_parameters()
        assert shown == {"statement_timeout": "300ms", "lock_timeout": "200ms",
                         "idle_in_transaction_session_timeout": "400ms"}
        assert {k: libpq.get(k) for k in KEEPALIVES} == {k: str(v) for k, v in KEEPALIVES.items()}
        t0 = time.monotonic()
        with pytest.raises(OperationalError, match="statement timeout"):
            with e.connect() as c:
                c.execute(text("select pg_sleep(5)"))
        assert time.monotonic() - t0 < 3
        with e.connect() as c:  # an abandoned transaction: the server ends the session
            c.execute(text("begin"))
            c.execute(text("select 1"))
            time.sleep(1.0)
            with pytest.raises(DBAPIError):
                c.execute(text("select 1"))
    finally:
        e.dispose()


@pytest.mark.postgres
def test_the_server_enforces_the_lock_timeout(pg_url, timeouts):
    use_pg_timeouts(PgTimeouts(lock_ms=300))
    e = write_engine(pg_url)
    holder = create_engine(pg_url, connect_args={"options": "-c search_path=app"})
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with holder.begin() as c:
            c.execute(text("lock table gaps in access exclusive mode"))
            held.set()
            release.wait(10)

    th = threading.Thread(target=hold)
    th.start()
    try:
        assert held.wait(10)
        t0 = time.monotonic()
        with pytest.raises(OperationalError, match="lock timeout"):
            with e.begin() as c:
                c.execute(text("select count(*) from gaps"))
        assert time.monotonic() - t0 < 3
    finally:
        release.set()
        th.join()
        holder.dispose()
        e.dispose()


@pytest.mark.postgres
def test_a_migration_waits_for_another_migration_but_not_behind_a_table_lock(pg_admin_url, timeouts):
    from tests.conftest import fresh_pg_database
    url = fresh_pg_database(pg_admin_url, "settleview_test_timeouts")
    use_pg_timeouts(PgTimeouts(statement_ms=200, lock_ms=300))
    migrate(url)  # the long statement timeout: a fresh migration takes longer than 200 ms
    holder = create_engine(url, connect_args={"options": "-c search_path=app"})
    try:
        with holder.begin() as c:
            assert c.execute(text("select count(*) from alembic_version")).scalar() == 1
    finally:
        holder.dispose()


def test_the_migration_lock_wait_comes_from_the_settings_and_is_bounded(monkeypatch):
    from btc_signal_engine.bootstrap.config import Settings
    assert Settings(_env_file=None).pg_timeouts.migration_lock_wait_s == 120.0
    monkeypatch.setenv("BTCFLOW_DB_MIGRATION_LOCK_WAIT_S", "45")
    assert Settings(_env_file=None).pg_timeouts.migration_lock_wait_s == 45.0
    for bad in ("-1", "3601"):
        monkeypatch.setenv("BTCFLOW_DB_MIGRATION_LOCK_WAIT_S", bad)
        with pytest.raises(ValueError):
            Settings(_env_file=None)


# -- a migration behind the backup's pg_dump (docs/solutions/2026-11-01-migration-vs-backup-lock.md) -----------------
def _at_0018(pg_admin_url, name: str):
    """A fresh database migrated to head and then taken back to 0018, so 0019's ALTER TABLE perp_orders is pending."""
    from alembic import command
    from alembic.config import Config

    from btc_signal_engine.adapters.sqlite.db import MIGRATIONS_DIR
    from tests.conftest import fresh_pg_database
    url = fresh_pg_database(pg_admin_url, name)
    migrate(url)
    e = create_engine(url, connect_args={"options": "-c search_path=app"})
    try:
        with e.begin() as c:
            cfg = Config()
            cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
            cfg.attributes["connection"] = c
            command.downgrade(cfg, "0018")
        with e.connect() as c:
            assert c.execute(text("select version_num from alembic_version")).scalar() == "0018"
    finally:
        e.dispose()
    return url


def _hold_access_share(url, seconds: float):
    """A thread that holds ACCESS SHARE on perp_orders (what pg_dump takes on every table) as `pg_dump` would, for
    `seconds`; returns (thread, held event)."""
    holder = create_engine(url, connect_args={"options": "-c search_path=app", "application_name": "pg_dump"})
    held = threading.Event()

    def hold() -> None:
        try:
            with holder.begin() as c:
                c.execute(text("lock table perp_orders in access share mode"))
                held.set()
                time.sleep(seconds)
        finally:
            holder.dispose()

    th = threading.Thread(target=hold)
    th.start()
    assert held.wait(10)
    return th


def _version(url) -> str:
    e = create_engine(url, connect_args={"options": "-c search_path=app"})
    try:
        with e.connect() as c:
            return c.execute(text("select version_num from alembic_version")).scalar()
    finally:
        e.dispose()


@pytest.mark.postgres
def test_a_migration_waits_out_a_dump_lock_instead_of_failing(pg_admin_url, timeouts, caplog):
    url = _at_0018(pg_admin_url, "settleview_test_migrate_lock")
    # lock_timeout 300 ms per attempt, held 3 s: the old behaviour failed the first ALTER after 300 ms
    use_pg_timeouts(PgTimeouts(lock_ms=300, migration_lock_wait_s=30))
    th = _hold_access_share(url, 3.0)
    try:
        t0 = time.monotonic()
        with caplog.at_level("INFO", logger="btc_signal_engine.adapters.sqlite.db"):
            migrate(url)
        waited = time.monotonic() - t0
    finally:
        th.join()
    assert waited >= 2.0, waited  # it really waited for the holder
    assert _version(url) != "0018"
    lines = [r.getMessage() for r in caplog.records]
    assert any("waiting for a lock held by pg_dump as " in m for m in lines), lines
    assert any("done after" in m for m in lines), lines


@pytest.mark.postgres
def test_a_migration_gives_up_on_a_lock_after_its_bounded_wait(pg_admin_url, timeouts):
    url = _at_0018(pg_admin_url, "settleview_test_migrate_lock_bounded")
    use_pg_timeouts(PgTimeouts(lock_ms=200, migration_lock_wait_s=1.5))
    th = _hold_access_share(url, 6.0)
    try:
        t0 = time.monotonic()
        with pytest.raises(OperationalError, match="lock timeout"):
            migrate(url)
        assert time.monotonic() - t0 < 5
    finally:
        th.join()
    assert _version(url) == "0018"  # the failed attempts rolled back: nothing half-applied
