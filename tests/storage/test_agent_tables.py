"""Migration 0014: the agent tables (sessions, journal, prompts)."""

import sqlite3

from alembic import command

from btc_signal_engine.adapters.sqlite.db import migrate
from tests.storage.test_strategy_tables import HEAD, _cfg, _tables

AGENT_TABLES = {"agent_sessions", "agent_journal", "agent_prompts", "agents", "agent_wake_inputs"}  # 0016, 0021


def test_upgrade_from_0013_adds_only_the_agent_tables_and_downgrade_removes_them(tmp_path):
    path = tmp_path / "up.db"
    migrate(path)
    command.downgrade(_cfg(path), "0013")  # a database as 0013 left it
    con = sqlite3.connect(path)
    assert not (AGENT_TABLES & _tables(con))
    before = _tables(con)
    assert con.execute("select version_num from alembic_version").fetchone()[0] == "0013"
    con.close()

    migrate(path)
    con = sqlite3.connect(path)
    assert _tables(con) - before == AGENT_TABLES | {"trades_archive"}  # 0017 comes back too
    assert con.execute("select version_num from alembic_version").fetchone()[0] == HEAD
    con.close()

    command.downgrade(_cfg(path), "0013")
    con = sqlite3.connect(path)
    assert _tables(con) == before
    assert con.execute("select version_num from alembic_version").fetchone()[0] == "0013"


def test_0015_adds_pause_code_to_a_database_made_by_the_first_0014_and_keeps_its_rows(tmp_path):
    path = tmp_path / "pc.db"
    migrate(path)
    command.downgrade(_cfg(path), "0014")
    con = sqlite3.connect(path)
    # Downgraded to 0014 = a database the first 0014 made (no pause_code); one paused row in it.
    con.execute("insert into agent_sessions (id, kind, state, limits, pinned, pause_reason, started_ts, ends_ts, "
                "updated_ts) values ('s1', 'in_app', 'paused', '{}', '{}', 'old', 1, 2, 1)")
    con.commit()
    con.close()

    migrate(path)
    con = sqlite3.connect(path)
    assert con.execute("select id, pause_reason, pause_code from agent_sessions").fetchall() == [("s1", "old", None)]
    assert con.execute("select version_num from alembic_version").fetchone()[0] == HEAD
    con.close()
    command.downgrade(_cfg(path), "0014")
    con = sqlite3.connect(path)
    assert "pause_code" not in {r[1] for r in con.execute("pragma table_info(agent_sessions)")}


def test_0016_backfills_an_agent_per_market_list_and_name_and_stamps_the_journal(tmp_path):
    path = tmp_path / "ag.db"
    migrate(path)
    command.downgrade(_cfg(path), "0015")
    con = sqlite3.connect(path)
    assert "agent_id" not in {r[1] for r in con.execute("pragma table_info(agent_sessions)")}
    rows = [("s1", '{"venues": ["kalshi_15m"]}', 1), ("s2", '{"venues": ["kalshi_15m"]}', 2),
            ("s3", '{"venues": ["kalshi_15m", "polymarket_15m"]}', 3), ("s4", '{"venues": ["polymarket_5m"]}', 4)]
    for sid, lim, ts in rows:
        con.execute("insert into agent_sessions (id, kind, state, limits, pinned, started_ts, ends_ts, updated_ts) "
                    "values (?, 'in_app', 'ended', ?, '{}', ?, 9, 9)", (sid, lim, ts))
        con.execute("insert into agent_journal (session_id, ts, kind, data) values (?, ?, 'state', '{}')", (sid, ts))
    con.commit()
    con.close()

    migrate(path)
    con = sqlite3.connect(path)
    agents = {r[0]: r[1:] for r in con.execute("select id, name, market, archived, created_ts from agents")}
    by = dict(con.execute("select id, agent_id from agent_sessions"))
    assert by["s1"] == by["s2"] and len({by["s1"], by["s3"], by["s4"]}) == 3  # one agent per (market list, name)
    assert agents[by["s1"]] == ("Kalshi 15m agent", "kalshi_15m", 0, 1)
    assert agents[by["s3"]] == ("Earlier runs: Kalshi 15m + Polymarket 15m", "kalshi_15m,polymarket_15m", 1, 3)
    assert agents[by["s4"]][:3] == ("Polymarket 5m agent", "polymarket_5m", 0)
    assert all(a == by[s] for s, a in con.execute("select session_id, agent_id from agent_journal"))
    con.close()
    command.downgrade(_cfg(path), "0015")
    con = sqlite3.connect(path)
    assert "agents" not in _tables(con) and "agent_id" not in {r[1] for r in con.execute(
        "pragma table_info(agent_journal)")}


def test_0021_adds_agent_wake_inputs_and_downgrade_removes_it(tmp_path):
    path = tmp_path / "wi.db"
    migrate(path)
    command.downgrade(_cfg(path), "0020")
    con = sqlite3.connect(path)
    assert "agent_wake_inputs" not in _tables(con)
    con.close()
    migrate(path)
    con = sqlite3.connect(path)
    cols = {r[1] for r in con.execute("pragma table_info(agent_wake_inputs)")}
    assert cols == {"journal_id", "session_id", "wake_id", "ts", "message", "message_sha256", "fingerprint",
                    "sensitive"}
    assert con.execute("select version_num from alembic_version").fetchone()[0] == HEAD
