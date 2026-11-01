"""Migration 0012: a nullable `actor` column on trading_audit ("pc", "phone:<8 chars>" or "system"; R13)."""

import sqlite3

from alembic import command

from btc_signal_engine.adapters.sqlite.db import migrate
from tests.storage.test_strategy_tables import HEAD, _cfg, _cols


def test_fresh_database_has_a_nullable_actor_column(tmp_path):
    path = tmp_path / "fresh.db"
    migrate(path)
    cols = _cols(sqlite3.connect(path), "trading_audit")
    assert "actor" in cols and cols["actor"][3] == 0  # nullable


def test_upgrade_from_0011_keeps_rows_with_a_null_actor_and_downgrade_removes_it(tmp_path):
    path = tmp_path / "up.db"
    migrate(path)
    command.downgrade(_cfg(path), "0011")
    con = sqlite3.connect(path)
    assert "actor" not in _cols(con, "trading_audit")
    con.execute("insert into trading_audit (ts, event, env, reason) values (7, 'approved', 'prod', 'old row')")
    con.commit()
    con.close()
    migrate(path)
    con = sqlite3.connect(path)
    assert "actor" in _cols(con, "trading_audit")
    assert con.execute("select ts, event, env, reason, actor from trading_audit").fetchall() == [
        (7, "approved", "prod", "old row", None)]
    assert "ix_trading_audit_bracket_id" in {r[1] for r in con.execute("pragma index_list(trading_audit)")}
    assert con.execute("select version_num from alembic_version").fetchone()[0] == HEAD
