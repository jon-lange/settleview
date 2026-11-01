import sqlite3

import pytest

from btc_signal_engine.adapters.sqlite.db import migrate, read_engine, write_engine
from btc_signal_engine.adapters.sqlite.store import SqliteStore
from btc_signal_engine.adapters.sqlite.tables import metadata


def test_migration_creates_every_table_and_is_idempotent(tmp_path):
    path = tmp_path / "m.db"
    migrate(path)
    migrate(path)  # second run is a no-op
    names = {r[0] for r in sqlite3.connect(path).execute("select name from sqlite_master where type='table'")}
    assert set(metadata.tables) <= names
    assert "alembic_version" in names


@pytest.fixture
def store(db_path):
    """SQLite's own settings: always SQLite, also on a Postgres run."""
    return SqliteStore(write_engine(db_path), read_engine(db_path))


def test_wal_mode_is_enabled(store, db_path):
    store.add_trades([])  # touch the write engine so pragmas apply
    with store._write.connect() as c:
        assert c.exec_driver_sql("PRAGMA journal_mode").scalar().lower() == "wal"


def test_audit_bracket_index_on_fresh_and_upgraded_databases(tmp_path):
    from alembic import command

    from tests.storage.test_strategy_tables import HEAD, _cfg, _indexes

    path = tmp_path / "a.db"
    migrate(path)
    assert "ix_trading_audit_bracket_id" in _indexes(sqlite3.connect(path), "trading_audit")
    command.downgrade(_cfg(path), "0010")
    con = sqlite3.connect(path)
    assert "ix_trading_audit_bracket_id" not in _indexes(con, "trading_audit")
    con.close()
    migrate(path)  # a database at 0010, without the index
    con = sqlite3.connect(path)
    assert "ix_trading_audit_bracket_id" in _indexes(con, "trading_audit")
    assert con.execute("select version_num from alembic_version").fetchone()[0] == HEAD


def test_trading_orders_order_id_index_on_fresh_and_upgraded_databases(tmp_path):
    """Migration 0020 (B4#18): a fresh database has the index from 0007's table definition; a database at 0019
    without it gets it; the downgrade removes it."""
    from alembic import command

    from tests.storage.test_strategy_tables import HEAD, _cfg, _indexes

    path = tmp_path / "o.db"
    migrate(path)
    assert "ix_trading_orders_order_id" in _indexes(sqlite3.connect(path), "trading_orders")
    command.downgrade(_cfg(path), "0019")
    con = sqlite3.connect(path)
    assert "ix_trading_orders_order_id" not in _indexes(con, "trading_orders")
    con.close()
    migrate(path)
    con = sqlite3.connect(path)
    assert "ix_trading_orders_order_id" in _indexes(con, "trading_orders")
    assert con.execute("select version_num from alembic_version").fetchone()[0] == HEAD
    plan = " ".join(r[3] for r in con.execute("explain query plan select * from trading_orders where order_id = 'x'"))
    assert "ix_trading_orders_order_id" in plan
    con.close()


@pytest.mark.postgres
def test_trading_orders_order_id_index_on_postgres(pg_admin_url):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text

    from btc_signal_engine.adapters.sqlite.db import MIGRATIONS_DIR
    from tests.conftest import fresh_pg_database
    from tests.storage.test_strategy_tables import HEAD
    url = fresh_pg_database(pg_admin_url, "settleview_test_0020")
    migrate(url)
    e = create_engine(url, connect_args={"options": "-c search_path=app"})
    try:
        def names() -> set[str]:
            with e.connect() as c:
                return {i["name"] for i in inspect(c).get_indexes("trading_orders", schema="app")}

        assert "ix_trading_orders_order_id" in names()
        with e.begin() as c:  # the downgrade and upgrade on this server, the way migrate() runs them
            cfg = Config()
            cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
            cfg.attributes["connection"] = c
            command.downgrade(cfg, "0019")
        assert "ix_trading_orders_order_id" not in names()
        migrate(url)
        assert "ix_trading_orders_order_id" in names()
        with e.connect() as c:
            assert c.execute(text("select version_num from alembic_version")).scalar() == HEAD
    finally:
        e.dispose()


INDEXES_0022 = (("ix_trading_brackets_session", "trading_brackets"), ("ix_agent_journal_kind", "agent_journal"))


def test_session_and_journal_kind_indexes_on_fresh_and_upgraded_databases(tmp_path):
    """Migration 0022: a fresh database has both indexes from the table definitions; a database at 0021 without them
    gets them; the downgrade removes them; the agent reads use them."""
    from alembic import command

    from tests.storage.test_strategy_tables import HEAD, _cfg, _indexes

    path = tmp_path / "k.db"
    migrate(path)
    con = sqlite3.connect(path)
    assert all(name in _indexes(con, table) for name, table in INDEXES_0022)
    con.close()
    command.downgrade(_cfg(path), "0021")
    con = sqlite3.connect(path)
    assert not any(name in _indexes(con, table) for name, table in INDEXES_0022)
    con.close()
    migrate(path)
    con = sqlite3.connect(path)
    assert all(name in _indexes(con, table) for name, table in INDEXES_0022)
    assert con.execute("select version_num from alembic_version").fetchone()[0] == HEAD == "0022"
    plan = " ".join(r[3] for r in con.execute(
        "explain query plan select * from trading_brackets where session_id in ('a', 'b')"))
    assert "ix_trading_brackets_session" in plan
    plan = " ".join(r[3] for r in con.execute(
        "explain query plan select * from agent_journal where kind = 'rule' and id > 5 order by id"))
    assert "ix_agent_journal_kind" in plan
    con.close()


@pytest.mark.postgres
def test_session_and_journal_kind_indexes_on_postgres(pg_admin_url):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    from btc_signal_engine.adapters.sqlite.db import MIGRATIONS_DIR
    from tests.conftest import fresh_pg_database
    url = fresh_pg_database(pg_admin_url, "settleview_test_0022")
    migrate(url)
    e = create_engine(url, connect_args={"options": "-c search_path=app"})
    try:
        def present() -> set[str]:
            with e.connect() as c:
                return {i["name"] for _, table in INDEXES_0022
                        for i in inspect(c).get_indexes(table, schema="app")} & {n for n, _ in INDEXES_0022}

        assert present() == {n for n, _ in INDEXES_0022}
        with e.begin() as c:
            cfg = Config()
            cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
            cfg.attributes["connection"] = c
            command.downgrade(cfg, "0021")
        assert present() == set()
        migrate(url)
        assert present() == {n for n, _ in INDEXES_0022}
    finally:
        e.dispose()


def _schema_diff(conn) -> list:
    """What a database migrated to head lacks or has beyond tables.py (Alembic's autogenerate comparison, in the
    connection's search_path schema)."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    opts = {"compare_type": False}  # types: SQLite reflects its own names; the DDL is covered by the dialect tests
    return compare_metadata(MigrationContext.configure(conn, opts=opts), metadata)


def test_the_initial_migration_is_frozen():
    """0001 writes its tables out: it imports nothing from the app, so changing tables.py can never change it."""
    from btc_signal_engine.adapters.sqlite.db import MIGRATIONS_DIR
    src = (MIGRATIONS_DIR / "versions" / "0001_initial.py").read_text(encoding="utf-8")
    assert "btc_signal_engine" not in src and "import" in src


def test_a_fresh_database_migrated_to_head_has_exactly_the_app_schema(tmp_path):
    """Every table, column and index of tables.py, and nothing else (the frozen 0001 plus each later revision)."""
    from sqlalchemy import create_engine
    path = tmp_path / "fresh.db"
    migrate(path)
    e = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with e.connect() as c:
            assert _schema_diff(c) == []
    finally:
        e.dispose()


@pytest.mark.postgres
def test_a_fresh_postgres_database_migrated_to_head_has_exactly_the_app_schema(pg_admin_url):
    from sqlalchemy import create_engine

    from tests.conftest import fresh_pg_database
    url = fresh_pg_database(pg_admin_url, "settleview_test_fresh_schema")
    migrate(url)
    e = create_engine(url, connect_args={"options": "-c search_path=app"})
    try:
        with e.connect() as c:
            assert _schema_diff(c) == []
    finally:
        e.dispose()


def test_check_mode_starts_only_on_a_schema_at_head(tmp_path):
    """BTCFLOW_DB_MIGRATE=check (compose: the one-off `migrate` service owns the schema): the app reads the revision
    and refuses a missing or older database instead of migrating it; `auto` (the default) migrates as before."""
    from alembic import command
    from alembic.config import Config
    from pydantic import ValidationError

    from btc_signal_engine.adapters.sqlite.db import MIGRATIONS_DIR, SchemaNotMigrated, require_head
    from btc_signal_engine.bootstrap.app import Runtime
    from btc_signal_engine.bootstrap.config import Settings
    from tests.storage.test_strategy_tables import HEAD

    db = tmp_path / "btcflow.db"
    with pytest.raises(SchemaNotMigrated, match="no database"):
        Runtime(Settings(_env_file=None, db_path=db, db_migrate="check", trading_stream=False))
    assert not db.exists()  # nothing was created
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
    command.upgrade(cfg, "0021")
    with pytest.raises(SchemaNotMigrated, match=f"revision 0021, this code needs {HEAD}"):
        require_head(db)
    migrate(db)  # the migrate job
    assert require_head(db) == HEAD
    rt = Runtime(Settings(_env_file=None, db_path=db, db_migrate="check", trading_stream=False))
    rt.store._write.dispose()
    rt.store._read.dispose()
    with pytest.raises(ValidationError):
        Settings(_env_file=None, db_migrate="never")  # no silent fallback to a default


@pytest.mark.postgres
def test_check_mode_reads_the_revision_on_postgres(pg_admin_url):
    from btc_signal_engine.adapters.sqlite.db import SchemaNotMigrated, require_head
    from tests.conftest import fresh_pg_database
    from tests.storage.test_strategy_tables import HEAD
    url = fresh_pg_database(pg_admin_url, "settleview_test_check")
    with pytest.raises(SchemaNotMigrated, match="revision none"):
        require_head(url)
    migrate(url)
    assert require_head(url) == HEAD
