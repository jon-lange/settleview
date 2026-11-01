"""Migration 0010: nullable index-stop columns on trading_brackets (stop_index_below / stop_index_above, dollars)."""

import sqlite3

from alembic import command

from btc_signal_engine.adapters.sqlite.db import migrate
from tests.storage.test_strategy_tables import HEAD, _cfg, _cols, _insert_pre_0009_rows

COLS = ("stop_index_below", "stop_index_above")


def test_fresh_database_has_the_index_stop_columns(tmp_path):
    path = tmp_path / "fresh.db"
    migrate(path)
    cols = _cols(sqlite3.connect(path), "trading_brackets")
    for c in COLS:
        assert c in cols and cols[c][3] == 0  # nullable


def test_upgrade_from_0009_keeps_rows_and_downgrade_removes_the_columns(tmp_path):
    path = tmp_path / "up.db"
    migrate(path)
    command.downgrade(_cfg(path), "0009")
    con = sqlite3.connect(path)
    assert not set(COLS) & set(_cols(con, "trading_brackets"))
    _insert_pre_0009_rows(con)
    con.close()
    migrate(path)
    con = sqlite3.connect(path)
    assert set(COLS) <= set(_cols(con, "trading_brackets"))
    assert con.execute("select stop_index_below, stop_index_above from trading_brackets").fetchall() == [(None, None)]
    assert con.execute("select version_num from alembic_version").fetchone()[0] == HEAD
