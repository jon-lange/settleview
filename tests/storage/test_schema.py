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
            assert c.execute(text("select version_num from alembic_version")).scalar() == "0021"
    finally:
        e.dispose()
