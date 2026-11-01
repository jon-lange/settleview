"""Migration 0013: the perps tables (positions, orders, triggers, funding ledger; KTD13)."""

import sqlite3

import pytest
from alembic import command

from btc_signal_engine.adapters.sqlite.db import migrate
from tests.storage.test_strategy_tables import HEAD, _cfg, _cols, _tables

PERP_TABLES = {"perp_positions", "perp_orders", "perp_triggers", "perp_funding"}
AGENT_TABLES = {"agent_sessions", "agent_journal", "agent_prompts", "agents",
                "agent_wake_inputs"}  # added later, by 0014, 0016 and 0021


def _schema(con) -> dict[str, list[tuple]]:
    """Every table older than the perps (0013), with its columns and indexes, to show 0013 and later leave them as
    they were (but for the indexes later migrations add on purpose, LATER_INDEXES)."""
    return {t: [tuple(r) for r in con.execute(f"pragma table_info({t})")]
            + sorted(tuple(r)[1:] for r in con.execute(f"pragma index_list({t})") if r[1] not in LATER_INDEXES)
            for t in sorted(_tables(con) - PERP_TABLES - AGENT_TABLES - {"trades_archive"})}


LATER_INDEXES = {"ix_trading_orders_order_id", "ix_trading_brackets_session"}  # 0020, 0022


def _order(con, coid: str, position_id: str = "p1") -> None:
    con.execute(
        "insert into perp_orders (id, env, position_id, client_order_id, role, side, price, count, time_in_force,"
        " reduce_only, filled_count, outcome, created_ms, updated_ms)"
        " values (?, 'perp_demo', ?, ?, 'entry', 'bid', 7.8123, 3, 'immediate_or_cancel', 0, 0, 'sending', 1, 1)",
        (f"o-{coid}-{position_id}", position_id, coid))


def _funding(con, env: str, ticker: str, funding_ms: int) -> None:
    con.execute(
        "insert into perp_funding (env, ticker, funding_ms, funding_rate, amount) values (?, ?, ?, 0.0001, -0.0123)",
        (env, ticker, funding_ms))


def test_fresh_database_has_the_perp_tables_and_a_second_run_changes_nothing(tmp_path):
    path = tmp_path / "fresh.db"
    migrate(path)
    con = sqlite3.connect(path)
    assert _tables(con) >= PERP_TABLES
    assert {"env", "ticker", "subaccount", "direction", "size", "avg_entry", "committed_collateral",
            "leverage_chosen", "state", "close_reason", "realized_pnl", "fees", "funding_total", "opened_ms",
            "closed_ms", "updated_ms"} <= set(_cols(con, "perp_positions"))
    assert {"exchange_trigger_id", "stop_loss_price", "take_profit_price", "state", "exchange_updated_time",
            "fired_leg", "fired_order_id"} <= set(_cols(con, "perp_triggers"))
    assert con.execute("select version_num from alembic_version").fetchone()[0] == HEAD
    before = _schema(con)
    con.close()

    migrate(path)
    con = sqlite3.connect(path)
    assert _schema(con) == before
    assert con.execute("select version_num from alembic_version").fetchone()[0] == HEAD


def test_upgrade_from_0012_adds_only_the_perp_tables_and_downgrade_removes_them(tmp_path):
    path = tmp_path / "up.db"
    migrate(path)
    command.downgrade(_cfg(path), "0012")  # a database as 0012 left it
    con = sqlite3.connect(path)
    assert not (PERP_TABLES & _tables(con))
    before = _schema(con)
    con.close()

    migrate(path)
    con = sqlite3.connect(path)
    assert _tables(con) >= PERP_TABLES
    assert _schema(con) == before  # existing tables untouched
    assert con.execute("select version_num from alembic_version").fetchone()[0] == HEAD


def test_a_second_funding_row_for_the_same_env_ticker_and_time_is_rejected(tmp_path):
    path = tmp_path / "f.db"
    migrate(path)
    con = sqlite3.connect(path)
    _funding(con, "perp_prod", "KXBTCPERP", 1_793_232_000_000)
    _funding(con, "perp_prod", "KXBTCPERP", 1_793_260_800_000)  # the next interval
    _funding(con, "perp_demo", "KXBTCPERP", 1_793_232_000_000)  # the other environment
    with pytest.raises(sqlite3.IntegrityError):
        _funding(con, "perp_prod", "KXBTCPERP", 1_793_232_000_000)
    assert con.execute("select count(*) from perp_funding").fetchone()[0] == 3


def test_a_duplicate_client_order_id_is_rejected(tmp_path):
    path = tmp_path / "o.db"
    migrate(path)
    con = sqlite3.connect(path)
    _order(con, "c-1")
    with pytest.raises(sqlite3.IntegrityError):
        _order(con, "c-1", position_id="p2")
    assert con.execute("select count(*) from perp_orders").fetchone()[0] == 1
