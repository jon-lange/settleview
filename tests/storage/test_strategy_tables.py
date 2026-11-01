"""Migration 0009: strategy-mode tables and the `strategy` column on the trading tables."""

import sqlite3

from alembic import command
from alembic.config import Config
from sqlalchemy import insert, select

from btc_signal_engine.adapters.sqlite.db import MIGRATIONS_DIR, migrate, write_engine
from btc_signal_engine.adapters.sqlite.tables import REVISION_0009, trading_brackets

NEW_TABLES = {t.name for t in REVISION_0009}
HEAD = "0022"  # the newest migration (0022: session and journal-kind indexes)
STRATEGY_TABLES = ("trading_brackets", "trading_orders", "trading_audit", "trading_sessions")


def _cfg(path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return cfg


def _tables(con) -> set[str]:
    return {r[0] for r in con.execute("select name from sqlite_master where type='table'")}


def _cols(con, table) -> dict[str, tuple]:
    return {r[1]: r for r in con.execute(f"pragma table_info({table})")}


def _indexes(con, table) -> set[str]:
    return {r[1] for r in con.execute(f"pragma index_list({table})")}


def _insert_pre_0009_rows(con) -> None:
    con.execute(
        "insert into trading_brackets (id, env, kind, ticker, side, window_end, stake, contracts, limit_price,"
        " take_profit, stop, max_cost, ticket_hash, state, filled, held, tp_filled, created_ts, updated_ts)"
        " values ('b1', 'shadow', 'entry', 'KXBTC15M-X', 'UP', 900000, 5, 10, 0.5, 0.6, 0.4, 5.1, 'h', 'open',"
        " 10, 10, 0, 1, 1)")
    con.execute(
        "insert into trading_orders (client_order_id, bracket_id, role, ticker, side, action, count, price, tif,"
        " status, filled, created_ts, updated_ts)"
        " values ('c1', 'b1', 'entry', 'KXBTC15M-X', 'yes', 'buy', 10, 0.5, 'immediate_or_cancel', 'executed',"
        " 10, 1, 1)")
    con.execute("insert into trading_audit (ts, event, env) values (1, 'approve', 'shadow')")
    con.execute(
        "insert into trading_sessions (id, env, pool, minutes, started_ts, ends_ts, committed)"
        " values ('s1', 'shadow', 10, 30, 1, 2, 0)")
    con.commit()


def test_fresh_database_has_new_tables_column_and_index(tmp_path):
    path = tmp_path / "fresh.db"
    migrate(path)
    con = sqlite3.connect(path)
    assert _tables(con) >= NEW_TABLES
    for t in STRATEGY_TABLES:
        col = _cols(con, t)["strategy"]
        assert col[3] == 1  # NOT NULL
        assert col[4].strip("'\"") == "taker"
    assert "ix_trading_brackets_strategy_state" in _indexes(con, "trading_brackets")
    assert con.execute("select version_num from alembic_version").fetchone()[0] == HEAD


def test_downgrade_to_0008_removes_everything_and_keeps_rows(tmp_path):
    path = tmp_path / "down.db"
    migrate(path)
    con = sqlite3.connect(path)
    _insert_pre_0009_rows(con)
    con.close()

    command.downgrade(_cfg(path), "0008")
    con = sqlite3.connect(path)
    assert not (NEW_TABLES & _tables(con))
    for t in STRATEGY_TABLES:
        assert "strategy" not in _cols(con, t)
    idx = _indexes(con, "trading_brackets")
    assert "ix_trading_brackets_strategy_state" not in idx
    assert {"ix_trading_brackets_state", "ix_trading_brackets_created"} <= idx
    assert con.execute("select count(*) from trading_brackets").fetchone()[0] == 1
    assert con.execute("select version_num from alembic_version").fetchone()[0] == "0008"


def test_upgrade_from_0008_with_rows_stamps_taker(tmp_path):
    path = tmp_path / "up.db"
    migrate(path)
    command.downgrade(_cfg(path), "0008")  # a database as 0008 left it: no strategy column
    con = sqlite3.connect(path)
    assert "strategy" not in _cols(con, "trading_brackets")
    _insert_pre_0009_rows(con)
    con.close()

    migrate(path)
    con = sqlite3.connect(path)
    for t in STRATEGY_TABLES:
        assert [r[0] for r in con.execute(f"select strategy from {t}")] == ["taker"]
    assert _tables(con) >= NEW_TABLES
    assert "ix_trading_brackets_strategy_state" in _indexes(con, "trading_brackets")
    con.close()

    # Code that does not name a strategy (today's trading service) still inserts, and gets the taker's.
    eng = write_engine(path)
    with eng.begin() as c:
        c.execute(insert(trading_brackets).values(
            id="b2", env="shadow", kind="entry", ticker="KXBTC15M-X", side="DOWN", window_end=900000, stake=5,
            contracts=10, limit_price=0.5, take_profit=0.6, stop=0.4, max_cost=5.1, ticket_hash="h", state="open",
            created_ts=2, updated_ts=2))
        c.execute(insert(trading_brackets).values(
            id="b3", env="shadow", kind="hold", ticker="KXBTC15M-X", side="UP", window_end=900000, stake=5,
            contracts=10, limit_price=0.9, take_profit=0.0, stop=0.0, max_cost=9.1, ticket_hash="h", state="open",
            created_ts=3, updated_ts=3, strategy="final_minute"))
        rows = dict(c.execute(select(trading_brackets.c.id, trading_brackets.c.strategy)).all())
    eng.dispose()
    assert rows == {"b1": "taker", "b2": "taker", "b3": "final_minute"}


def test_strategy_windows_key_is_account_and_boundary(tmp_path):
    path = tmp_path / "pk.db"
    migrate(path)
    con = sqlite3.connect(path)
    pk = sorted((r[5], r[1]) for r in con.execute("pragma table_info(strategy_windows)") if r[5])
    assert [name for _, name in pk] == ["account_id", "boundary"]
