"""The append-only audit on Postgres with the real roles (deploy/postgres/audit.sql, migration 0023): the app's role
may SELECT and INSERT trading_audit and nothing else; the owner and a superuser meet the trigger; the maintenance
procedure (DISABLE TRIGGER inside one transaction, as the owner or a superuser) is the only way to change a row. Each
test makes its own database (and the cluster-wide roles if missing) on the throwaway test server."""

from __future__ import annotations

import statistics
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from btc_signal_engine.adapters.sqlite.db import migrate, read_engine, write_engine
from btc_signal_engine.adapters.sqlite.store import SqliteStore
from tests.conftest import fresh_pg_database

pytestmark = pytest.mark.postgres

AUDIT_SQL = Path(__file__).resolve().parents[2] / "deploy" / "postgres" / "audit.sql"
APP_PW = "audit-test-only"  # the throwaway test server's role, never a deployment's
ROW = {"ts": 1, "event": "refused", "env": "prod", "code": "LIMIT", "reason": "over the cap", "actor": "pc",
       "detail": {"ticker": "KXBTC15M-26NOV011800-00", "price": 0.57}}


def _run_file(url, path: Path) -> None:
    """A SQL file through psycopg's simple protocol (several statements, dollar-quoted bodies), as psql -f runs it."""
    eng = create_engine(url)
    raw = eng.raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(path.read_text(encoding="utf-8"))
        raw.commit()
    finally:
        raw.close()
        eng.dispose()


def _database(pg_admin_url, name: str):
    """A new database as initdb.sh makes it (the app's role owns the schemas app and research); (admin, app) URLs."""
    url = fresh_pg_database(pg_admin_url, name)
    eng = create_engine(url, isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        for role in ("settleview_app", "settleview_research"):
            c.exec_driver_sql(f"do $$ begin if not exists (select 1 from pg_roles where rolname = '{role}') then "
                              f"create role {role}; end if; end $$")
        c.exec_driver_sql(f"alter role settleview_app login password '{APP_PW}'")
        c.exec_driver_sql(f'grant connect, temporary on database "{name}" to settleview_app')
        c.exec_driver_sql("create schema app authorization settleview_app")
        c.exec_driver_sql("create schema research authorization settleview_app")
    eng.dispose()
    return url, url.set(username="settleview_app", password=APP_PW)


def _refused(engine, sql: str, match: str) -> None:
    with pytest.raises(DBAPIError, match=match), engine.begin() as c:
        c.execute(text(sql))


def _owners(engine) -> tuple:
    with engine.connect() as c:
        return tuple(c.execute(text(
            "select pg_get_userbyid(c.relowner), pg_get_userbyid(n.nspowner) from pg_class c join pg_namespace n "
            "on n.oid = c.relnamespace where c.oid = to_regclass('app.trading_audit')")).one())


@pytest.fixture
def sealed(pg_admin_url):
    """A new volume's order: audit.sql (the seal function) first, then the migrate job as the app's role."""
    admin, app = _database(pg_admin_url, "settleview_audit_sealed")
    _run_file(admin, AUDIT_SQL)
    migrate(app)
    store = SqliteStore(write_engine(app), read_engine(app))
    yield admin, store
    store._write.dispose()
    store._read.dispose()


def test_a_new_volume_is_sealed_and_the_app_can_only_add_rows(sealed):
    _, store = sealed
    assert _owners(store._read) == ("settleview_audit", "settleview_audit")
    assert store.insert("trading_audit", [ROW]) == [1]
    store.insert("trading_audit", [ROW, ROW])
    w = store._write
    _refused(w, "update trading_audit set reason = 'x' where id = 1", "permission denied")
    _refused(w, "delete from trading_audit where id = 1", "permission denied")
    _refused(w, "truncate trading_audit", "permission denied")
    _refused(w, "drop table trading_audit", "must be owner")
    _refused(w, "alter table trading_audit disable trigger all", "must be owner")
    _refused(w, "drop function trading_audit_append_only() cascade", "must be owner")
    _refused(w, "drop schema app cascade", "must be owner")
    _refused(w, "select setval(pg_get_serial_sequence('trading_audit', 'id'), 1)", "permission denied")
    with w.begin() as c:  # the app's own migrations still create (and drop) their tables in `app`
        c.execute(text("create table app.later_feature (i int)"))
        c.execute(text("drop table app.later_feature"))
    assert len(store.select("trading_audit")) == 3


def test_the_owner_meets_the_trigger_and_only_the_maintenance_procedure_changes_a_row(sealed):
    admin, store = sealed
    store.insert("trading_audit", [ROW])
    eng = create_engine(admin)
    try:
        for sql in ("update app.trading_audit set reason = 'x'", "delete from app.trading_audit",
                    "truncate app.trading_audit"):
            with pytest.raises(DBAPIError, match="append-only"), eng.begin() as c:
                c.execute(text("set local role settleview_audit"))
                c.execute(text(sql))
        _refused(eng, "update app.trading_audit set reason = 'x'", "append-only")  # a superuser too, by accident
        with eng.begin() as c:  # the documented procedure: one transaction, trigger off, change, trigger on
            c.execute(text("alter table app.trading_audit disable trigger trading_audit_append_only"))
            c.execute(text("update app.trading_audit set reason = 'corrected' where id = 1"))
            c.execute(text("alter table app.trading_audit enable trigger trading_audit_append_only"))
        _refused(eng, "delete from app.trading_audit", "append-only")
    finally:
        eng.dispose()
    assert [r["reason"] for r in store.select("trading_audit")] == ["corrected"]


def test_an_existing_volume_is_sealed_by_running_audit_sql_after_the_migration(pg_admin_url):
    admin, app = _database(pg_admin_url, "settleview_audit_existing")
    migrate(app)  # no seal function yet: the trigger holds, the app's role still owns the table
    store = SqliteStore(write_engine(app), read_engine(app))
    try:
        store.insert("trading_audit", [ROW])
        assert _owners(store._read) == ("settleview_app", "settleview_app")
        _refused(store._write, "update trading_audit set reason = 'x'", "append-only")  # the trigger, not a grant
        _run_file(admin, AUDIT_SQL)
        _run_file(admin, AUDIT_SQL)  # idempotent
        assert _owners(store._read) == ("settleview_audit", "settleview_audit")
        assert store.insert("trading_audit", [ROW]) == [2]  # the sequence still serves the app
        _refused(store._write, "delete from trading_audit", "permission denied")
    finally:
        store._write.dispose()
        store._read.dispose()


def test_the_cost_on_the_order_path(sealed, capsys):
    """An audit insert as the app on the sealed table against the same insert into an unguarded copy: the
    statement-level trigger never fires on INSERT, so there is nothing to add."""
    _, store = sealed
    with store._write.begin() as c:
        c.execute(text("create table app.audit_bench (like app.trading_audit including defaults)"))
    times: dict[str, list[float]] = {"trading_audit": [], "audit_bench": []}
    for _ in range(300):
        for table in times:
            t = time.perf_counter()
            with store._write.begin() as c:
                c.execute(text(f"insert into app.{table} (ts, event, env, reason) values (1, 'sent', 'prod', 'r')"))  # noqa: S608 - constant names
            times[table].append(time.perf_counter() - t)
    sealed_ms, plain_ms = (statistics.median(v) * 1000 for v in times.values())
    with capsys.disabled():
        print(f"\naudit insert p50: sealed {sealed_ms:.3f} ms, unguarded copy {plain_ms:.3f} ms")
    assert sealed_ms - plain_ms < 2
