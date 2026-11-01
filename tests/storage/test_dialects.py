"""One schema and one code path on SQLite and Postgres (plan G1, G2).

The first part needs no server: it compiles the Postgres SQL (DDL, inserts, upserts) with SQLAlchemy's postgresql
dialect and checks the types that differ. The second part (marked `postgres`) upgrades an empty database from nothing
to head on both dialects and compares the resulting tables, columns and indexes.
"""

from __future__ import annotations

import json
import math

import pytest
from sqlalchemy import create_mock_engine, inspect
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

from btc_signal_engine.adapters.sqlite.db import migrate, write_engine
from btc_signal_engine.adapters.sqlite.research_tables import research_metadata
from btc_signal_engine.adapters.sqlite.store import conflict_target
from btc_signal_engine.adapters.sqlite.tables import TABLES, JsonText, metadata

PG = postgresql.dialect()
LITE = sqlite.dialect()


def _ddl(meta, dialect) -> str:
    out: list[str] = []
    engine = create_mock_engine(f"{dialect.name}://", lambda sql, *a, **k: out.append(str(sql.compile(dialect=dialect))))
    meta.create_all(engine, checkfirst=False)
    return "\n".join(out)


def test_every_table_compiles_for_postgres_with_text_json_and_unbounded_varchar():
    ddl = _ddl(metadata, PG)
    for name in TABLES:
        assert f"CREATE TABLE {name} (" in ddl
    assert "VARCHAR(" not in ddl  # SQLite never enforced the lengths; Postgres does not either
    assert " JSON" not in ddl and "JSONB" not in ddl  # JSON stays text (NaN-safe, as on SQLite)
    assert "archived BOOLEAN DEFAULT '0' NOT NULL" in ddl  # a boolean default Postgres accepts
    assert "id SERIAL NOT NULL" in CreateTable(TABLES["gaps"]).compile(dialect=PG).string


def test_sqlite_ddl_is_unchanged_by_the_postgres_types():
    ddl = str(CreateTable(TABLES["coach_settings"]).compile(dialect=LITE))
    assert '"key" VARCHAR(32) NOT NULL' in ddl and "value JSON NOT NULL" in ddl


@pytest.mark.parametrize("value", [{"a": 1, "b": [1.5, None]}, [], "text", 3, None, True])
def test_json_text_round_trips_like_sqlites_json(value):
    t = JsonText()
    stored = t.process_bind_param(value, PG)
    assert isinstance(stored, str) and stored == json.dumps(value)  # None is the JSON null, as SQLAlchemy's JSON
    assert t.process_result_value(stored, PG) == value


def test_json_text_keeps_nan_and_infinity_which_postgres_json_refuses():
    back = JsonText().process_result_value(JsonText().process_bind_param({"x": math.nan, "y": math.inf}, PG), PG)
    assert math.isnan(back["x"]) and back["y"] == math.inf


def test_conflict_targets_follow_the_rows_keys():
    assert conflict_target(TABLES["index_1s"], {"ts", "price"}) == ("ts",)
    assert conflict_target(TABLES["strategy_settings"], {"strategy", "key", "value"}) == ("strategy", "key")
    # keyed by an autoincrement id the row leaves out: its unique constraint
    assert conflict_target(TABLES["decisions"], {"horizon", "boundary", "ruleset_hash", "run_id", "call"}) == (
        "horizon", "boundary", "ruleset_hash", "run_id")
    assert conflict_target(TABLES["perp_orders"], {"client_order_id", "env"}) == ("client_order_id",)
    assert conflict_target(TABLES["trading_audit"], {"ts", "event"}) == ()


def _compile(stmt) -> str:
    return str(stmt.compile(dialect=PG))


def test_store_upsert_and_insert_ignore_compile_for_postgres():
    for name, t in TABLES.items():
        ins = postgresql.insert(t)
        row = {c.name: None for c in t.columns}
        target = conflict_target(t, row)
        new = {c.name: ins.excluded[c.name] for c in t.columns if c.name not in target}
        sql = _compile(ins.on_conflict_do_update(index_elements=list(target), set_=new) if new
                       else ins.on_conflict_do_nothing(index_elements=list(target)))
        assert "ON CONFLICT (" in sql, name
        pk = list(t.primary_key.columns)[:1]
        sql = _compile(ins.values(row).on_conflict_do_nothing().returning(*pk))
        assert "ON CONFLICT DO NOTHING RETURNING" in sql, name


def test_research_tables_add_a_receive_time_index_on_postgres_only():
    pg, lite = _ddl(research_metadata, PG), _ddl(research_metadata, LITE)
    assert "ix_pm_ws_quotes_recv_ms" in pg and "ix_pm_ws_quotes_recv_ms" not in lite
    assert "ix_pm_ws_quotes_slug_ts" in pg and "ix_pm_ws_quotes_slug_ts" in lite
    assert str(CreateIndex(list(TABLES["trades_archive"].indexes)[0]).compile(dialect=PG)).startswith(
        "CREATE INDEX ix_trades_archive_ts")


# -- G2: empty -> head on both dialects ------------------------------------------------------------------------------

def _shape(engine, schema=None) -> dict[str, tuple]:
    insp = inspect(engine)
    out = {}
    for t in insp.get_table_names(schema=schema):
        if t == "_sqlite_migration":  # the move tool's own progress table, not the app's schema
            continue
        cols = tuple(sorted((c["name"], bool(c["nullable"])) for c in insp.get_columns(t, schema=schema)))
        # Postgres also lists the indexes behind UNIQUE constraints (SQLite hides its autoindexes): compare the rest.
        idx = tuple(sorted(i["name"] for i in insp.get_indexes(t, schema=schema)
                           if not i.get("duplicates_constraint")))
        pk = tuple(insp.get_pk_constraint(t, schema=schema)["constrained_columns"])
        out[t] = (cols, idx, pk)
    return out


@pytest.mark.postgres
def test_empty_to_head_gives_the_same_tables_columns_and_indexes_on_both(tmp_path, pg_admin_url):
    from tests.conftest import fresh_pg_database

    lite = tmp_path / "head.db"
    migrate(lite)
    url = fresh_pg_database(pg_admin_url, "settleview_g2")
    migrate(url)
    migrate(url)  # a second run is a no-op
    le, pe = write_engine(lite), write_engine(url)
    try:
        a, b = _shape(le), _shape(pe, "app")
        assert set(a) == set(b) == set(TABLES) | {"alembic_version"}
        for name in a:
            assert a[name] == b[name], name
        with pe.connect() as c, le.connect() as lc:
            assert c.exec_driver_sql("select version_num from app.alembic_version").scalar() == \
                lc.exec_driver_sql("select version_num from alembic_version").scalar()
            assert c.exec_driver_sql("select count(*) from pg_namespace where nspname = 'research'").scalar() == 1
    finally:
        le.dispose()
        pe.dispose()


def test_the_research_read_helper_opens_a_sqlite_file_read_only(tmp_path):
    import sqlite3

    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from btc_signal_engine.adapters.sqlite.research import readonly_engine

    path = tmp_path / "r.db"
    con = sqlite3.connect(path)
    con.execute("create table t (i integer)")
    con.execute("insert into t values (1)")
    con.commit()
    con.close()
    eng = readonly_engine(path)
    with eng.connect() as c:
        assert c.execute(text("select i from t")).scalar() == 1
        with pytest.raises(OperationalError):
            c.execute(text("insert into t values (2)"))
    with pytest.raises(FileNotFoundError):
        readonly_engine(tmp_path / "missing.db")
    assert not (tmp_path / "missing.db").exists()
