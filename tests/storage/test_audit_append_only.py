"""trading_audit is append-only in the database (migration 0023): the store adds rows; UPDATE and DELETE are refused
by the database itself, on SQLite and (through the `store` fixture in the Postgres job) on Postgres.
tests/storage/test_audit_postgres.py checks the Postgres roles (deploy/postgres/audit.sql)."""

import sqlite3

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from btc_signal_engine.adapters.sqlite.db import migrate
from tests.storage.test_strategy_tables import HEAD, _cfg

ROW = {"ts": 1, "event": "sent", "env": "prod", "reason": "r", "detail": {"p": 0.5}, "actor": "pc"}


def test_rows_are_added_but_never_changed_or_removed(store):
    assert store.insert("trading_audit", [ROW]) == [1]
    store.insert("trading_audit", [{**ROW, "ts": 2}], ignore_conflicts=False)  # Copilot's insert: still a new row
    for sql in ("update trading_audit set reason = 'x' where id = 1", "delete from trading_audit where id = 2",
                "delete from trading_audit"):
        with pytest.raises(DBAPIError, match="append-only"), store._write.begin() as c:
            c.execute(text(sql))
    assert [r["reason"] for r in store.select("trading_audit")] == ["r", "r"]


def test_the_triggers_come_and_go_with_the_migration(tmp_path):
    path = tmp_path / "a.db"
    migrate(path)
    command.downgrade(_cfg(path), "0022")
    con = sqlite3.connect(path)
    con.execute("insert into trading_audit (ts, event, env) values (1, 'sent', 'prod')")
    con.execute("update trading_audit set reason = 'before 0023 a row could change'")
    con.commit()
    con.close()
    migrate(path)
    con = sqlite3.connect(path)
    assert con.execute("select version_num from alembic_version").fetchone()[0] == HEAD
    with pytest.raises(sqlite3.IntegrityError, match="append-only: DELETE refused"):
        con.execute("delete from trading_audit")
    assert con.execute("select reason from trading_audit").fetchall() == [("before 0023 a row could change",)]
