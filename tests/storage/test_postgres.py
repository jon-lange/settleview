"""Postgres-only behaviour (marked `postgres`: CI's Postgres job runs these; skipped without a server). The rest of the
suite also runs on Postgres there, through the `store` fixture (tests/conftest.py)."""

from __future__ import annotations

import asyncio
import math

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from btc_signal_engine.adapters.polymarket_us import recorders as rec
from btc_signal_engine.adapters.sqlite.db import archive_trades_pg, migrate, read_engine, write_engine
from btc_signal_engine.adapters.sqlite.research import ResearchDb, readonly_engine
from btc_signal_engine.adapters.sqlite.store import SqliteStore
from btc_signal_engine.bootstrap import ops
from btc_signal_engine.bootstrap.maintenance import DailyMaintenance
from btc_signal_engine.domain.models.events import Trade
from tests.conftest import empty_pg_tables, fresh_pg_database

pytestmark = pytest.mark.postgres

DAY = 86_400_000
NOW = 1_761_900_000_000


@pytest.fixture
def pg(pg_url):
    we, re_ = write_engine(pg_url), read_engine(pg_url)
    empty_pg_tables(we)
    yield SqliteStore(we, re_), pg_url
    we.dispose()
    re_.dispose()


def test_insert_ignore_returns_the_new_id_and_nothing_for_a_duplicate(pg):
    store, _ = pg
    assert store.dialect == "postgresql"
    row = {"horizon": 900, "boundary": 1, "ruleset_hash": "h", "run_id": 0, "call": "UP", "explanation": "x",
           "conditions": [], "flags": [], "created_ts": 1}
    assert store.insert("decisions", [row]) == [1]
    assert store.insert("decisions", [row]) == []
    assert len(store.select("decisions")) == 1


def test_replace_overwrites_every_column_like_insert_or_replace(pg):
    store, _ = pg
    store.insert("kalshi_markets", [{"ticker": "T", "open_ts": 1, "close_ts": 2, "status": "open", "result": None}],
                 ignore_conflicts=False)
    store.insert("kalshi_markets", [{"ticker": "T", "open_ts": 1, "close_ts": 2, "status": "settled",
                                     "result": "yes"}], ignore_conflicts=False)
    (m,) = store.select("kalshi_markets")
    assert (m["status"], m["result"], m["floor_strike"]) == ("settled", "yes", None)


def test_integers_for_booleans_and_nan_in_json_are_accepted(pg):
    store, _ = pg
    store.insert("gaps", [{"start_ts": 1, "cause": "x", "repaired": 0, "created_ts": 1}])
    store.update("gaps", {"id": 1}, {"repaired": 1})
    assert store.select("gaps", {"repaired": 1})[0]["repaired"] is True
    store.insert("data_quality", [{"ts": 1, "kind": "k", "value": math.nan, "detail": {"v": math.nan}}])
    (d,) = store.select("data_quality")
    assert math.isnan(d["value"]) and math.isnan(d["detail"]["v"])


def test_the_reader_cannot_write(pg):
    store, _ = pg
    with pytest.raises(DBAPIError):
        with store._read.begin() as c:
            c.execute(text("insert into gaps (start_ts, cause, repaired, created_ts) values (1, 'x', false, 1)"))


def _trades(ts_list):
    return [Trade(i + 1, ts, ts, 100.0, 0.1, True, False) for i, ts in enumerate(ts_list)]


def test_the_archive_moves_old_trades_in_one_transaction_per_batch(pg):
    store, _ = pg
    store.add_trades(_trades([NOW - 70 * DAY, NOW - 65 * DAY, NOW - 61 * DAY, NOW - DAY]))
    moved = archive_trades_pg(store._write, NOW - 60 * DAY, batch=2)
    assert moved == 3
    assert [t.trade_id for t in store.trades_range(0, NOW)] == [4]
    with store._write.connect() as c:
        assert c.execute(text("select count(*) from trades_archive")).scalar() == 3
    assert archive_trades_pg(store._write, NOW - 60 * DAY) == 0


def test_the_daily_job_on_postgres_archives_once_a_day_and_takes_no_file_backup(pg, tmp_path):
    store, url = pg
    store.add_trades(_trades([NOW - 70 * DAY, NOW - DAY]))
    m = DailyMaintenance(url, 60, now_ms=lambda: NOW, engine=store._write)
    m.run_once()
    m.run_once()  # the same day: nothing more
    assert len(store.trades_range(0, NOW)) == 1
    assert not (tmp_path / "backups").exists()


def test_recorders_write_the_research_schema_and_the_app_reads_it(pg):
    from tests.recorders.test_pm_recorders import NOW as T, FakeClient, _stop_after

    _, url = pg
    ticks = [0]
    asyncio.run(rec.run_quotes(url, lambda: FakeClient(), stop=_stop_after(ticks, 12), spans=("5m",), tick_s=0.0,
                               now_s=lambda: T + 0.5, server_message=lambda e: False))
    db = ResearchDb(url=url)
    newest = db.newest_ms(now_ms=T * 1000 + 500)
    # pm-trades: the newest print's venue time in the current / previous windows' markets (the slug index)
    assert newest == {"pm-quotes": T * 1000 + 500, "pm-settle": None, "pm-trades": 1_761_900_001_123}
    cov = db.coverage(T * 1000, T * 1000 + 300_000, 300_000)
    assert cov["recorded"] == 1 and cov["settled"] == 0
    assert ops.fresh("pm-quotes", 120, db, now_ms=T * 1000 + 30_500) == (True, "pm-quotes: newest row 30 s old")
    eng = readonly_engine(url)  # the research scripts' helper: both schemas, read-only
    try:
        with eng.connect() as c:
            assert c.execute(text("select count(*) from pm_ws_quotes")).scalar() == 2
            with pytest.raises(DBAPIError):
                c.execute(text("delete from pm_ws_quotes"))
    finally:
        eng.dispose()


def test_two_services_migrating_at_once_do_not_collide(pg_admin_url, tmp_path):
    """Two `ops migrate` processes on an empty database (the app and a recorder starting together): the advisory
    lock makes one wait, and the second finds the schema at head."""
    import os
    import subprocess
    import sys

    url = fresh_pg_database(pg_admin_url, "settleview_race")
    env = {**os.environ, "BTCFLOW_DATABASE_URL": url.render_as_string(hide_password=False)}
    procs = [subprocess.Popen([sys.executable, "-m", "btc_signal_engine.ops", "migrate"], cwd=tmp_path, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT) for _ in range(2)]
    for p in procs:
        out, _ = p.communicate(timeout=120)
        assert p.returncode == 0, out.decode(errors="replace")
    migrate(url)  # and once more in-process: a no-op
    eng = write_engine(url)
    try:
        with eng.connect() as c:
            assert c.execute(text("select count(*) from alembic_version")).scalar() == 1
    finally:
        eng.dispose()


def test_the_app_starts_on_postgres(pg, tmp_path):
    from btc_signal_engine.bootstrap.app import Runtime
    from btc_signal_engine.bootstrap.config import Settings

    _, url = pg
    raw = url.render_as_string(hide_password=False)
    rt = Runtime(Settings(_env_file=None, db_path=tmp_path / "btcflow.db", database_url=raw, trading_stream=False))
    assert rt.store.dialect == "postgresql"
    assert rt.maintenance.postgres and rt.recorder_watch.db_label == "the research schema"
    assert not (tmp_path / "btcflow.db").exists()  # the data is in Postgres; the folder holds logs and pid files
    rt.store._write.dispose()
    rt.store._read.dispose()
