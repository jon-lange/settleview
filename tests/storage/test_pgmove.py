"""The SQLite -> Postgres move tool (plan G6). Its pure parts (value conversion, checksums, sampled ranges, batching)
run everywhere; the copy itself (marked `postgres`) runs on CI's Postgres with generated data: an app database, a
trade archive file and a recorders' file, copied with a tiny batch size, then checked, re-run, refused and forced."""

from __future__ import annotations

import hashlib
import math
import random
import sqlite3

import pytest
from sqlalchemy import text

from btc_signal_engine.adapters.polymarket_us import recorders as rec
from btc_signal_engine.adapters.sqlite import pgmove
from btc_signal_engine.adapters.sqlite.db import archive_trades, migrate, read_engine, write_engine
from btc_signal_engine.adapters.sqlite.store import SqliteStore
from btc_signal_engine.adapters.sqlite.tables import TABLES
from btc_signal_engine.domain.models.events import IndexSecond, Trade

DAY = 86_400_000
NOW = 1_761_900_000_000


# -- pure parts ------------------------------------------------------------------------------------------------------

def test_values_are_converted_to_what_postgres_checks():
    to_int = pgmove._converter(TABLES["trades"].c.ts)
    assert to_int(1.0e12) == 10**12 and to_int(5) == 5 and to_int(None) is None and to_int("7") == 7
    with pytest.raises(pgmove.MoveError, match="trades.ts"):
        to_int(1.5)
    to_bool = pgmove._converter(TABLES["trades"].c.buy)
    assert to_bool(1) is True and to_bool(0) is False and to_bool(None) is None
    assert pgmove._converter(TABLES["trades"].c.price)(3) == 3.0
    assert pgmove._converter(TABLES["kalshi_markets"].c.ticker)("KXBTC15M-X") == "KXBTC15M-X"  # text passes as it is
    to_json = pgmove._converter(TABLES["trading_settings"].c.value)  # SQLite kept the JSON number 10 as a number
    assert to_json(10) == "10" and to_json(0.5) == "0.5" and to_json('{"a": 1}') == '{"a": 1}'


def test_checksums_ignore_row_order_and_the_int_or_float_form_of_a_value():
    rows = [(1, "a", 2.5, True), (2, None, 3.0, False), (3, "c", math.nan, None)]
    same = [(3, "c", math.nan, None), (1, "a", 2.5, 1), (2, None, 3, 0)]
    assert pgmove._multiset(rows) == pgmove._multiset(same)
    assert pgmove._multiset(rows) != pgmove._multiset(rows[:2] + [(3, "c", 1.0, None)])


def test_rows_stream_in_bounded_batches():
    con = sqlite3.connect(":memory:")
    con.execute("create table t (i integer)")
    con.executemany("insert into t values (?)", [(i,) for i in range(23)])
    sizes = [len(b) for b in pgmove._batches(con.execute("select i from t"), 5)]
    assert sizes == [5, 5, 5, 5, 3]


def test_sampled_ranges_cover_the_first_and_last_rows(tmp_path):
    con = sqlite3.connect(":memory:")
    con.execute("create table q (recv_ms integer, slug text)")
    con.executemany("insert into q values (?, ?)", [(1000 + 10 * i, f"s{i:04d}") for i in range(1000)])
    ranges = pgmove._sample_ranges(con, "q", "recv_ms", 1000, 8, 50)
    assert ranges[0][0] == 1000 and ranges[-1][1] == 1000 + 10 * 999 and len(ranges) == 8
    assert all(hi - lo <= 500 for lo, hi in ranges)  # about 50 rows each
    text_ranges = pgmove._sample_ranges(con, "q", "slug", 1000, 4, 10)
    assert text_ranges[0][0] == "s0000" and text_ranges[-1][1] == "s0999"


def test_a_missing_source_file_is_an_error_and_never_created(tmp_path):
    with pytest.raises(pgmove.MoveError, match="no such file"):
        pgmove.open_source(tmp_path / "nothing.db")
    assert not (tmp_path / "nothing.db").exists()


# -- the copy (Postgres) ---------------------------------------------------------------------------------------------

def _make_sources(tmp_path) -> tuple:
    rnd = random.Random(7)
    app = tmp_path / "btcflow.db"
    migrate(app)
    store = SqliteStore(write_engine(app), read_engine(app))
    store.add_trades([Trade(i, NOW - (90 - i % 90) * DAY + i, NOW, 100 + rnd.random(), rnd.random(), i % 2 == 0,
                            i % 3 == 0) for i in range(1, 1201)])
    store.add_index_seconds([IndexSecond(NOW + i * 1000, 100 + i / 7, NOW + i * 1000 - 3, None if i % 5 else 1.5)
                             for i in range(800)])
    store.insert("decisions", [{"horizon": 900, "boundary": NOW + i, "ruleset_hash": "h", "run_id": 0, "call": "UP",
                                "explanation": "é ✓", "conditions": [{"x": i}], "flags": [], "created_ts": NOW}
                               for i in range(300)])
    store.insert("data_quality", [{"ts": 1, "kind": "k", "value": math.nan, "detail": {"v": math.inf}}])
    store.insert("agents", [{"id": "a1", "name": "A", "market": "kalshi_15m", "created_ts": 1, "archived": True,
                             "updated_ts": 1}])
    store.insert("gaps", [{"start_ts": i, "cause": "x", "repaired": i % 2, "created_ts": i} for i in range(1, 40)])
    # text keys whose order differs between byte order (SQLite) and a locale (Postgres): the sampled ranges must agree
    keys = ["Mode", "mode", "you_poly5.mode", "you-poly5.mode", "_first", "Zeta", "alpha", "you.poly", "you_kalshi.mode"]
    store.insert("trading_settings", [{"key": k, "value": i, "updated_ts": i} for i, k in enumerate(keys)])
    store._write.dispose()
    archive = tmp_path / "btcflow-archive.db"
    assert archive_trades(app, archive, NOW - 60 * DAY) > 0
    research = tmp_path / "polymarket_us.db"
    con = sqlite3.connect(research)
    con.executescript(rec.QUOTES_SCHEMA + rec.SETTLE_SCHEMA)
    con.executemany("insert into pm_ws_quotes values (?,?,?,?,?,?,?,?)",
                    [(NOW + i * 250, "5m", f"slug-{i // 40}", 0.4, 0.6, 10, 12, None) for i in range(900)])
    con.executemany("insert into pm_windows values (?,?,?,?,?,?,?,?,?)",
                    [(f"w{i}", "5m", i * 300, i * 300 + 300, 1.0, 2.0, "up", "{}", NOW + i) for i in range(30)])
    con.commit()
    con.close()
    return app, archive, research


def _digest(*paths) -> list[str]:
    return [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]


def _jobs(app, archive, research):
    return [pgmove.app_job(app), pgmove.archive_job(archive), pgmove.research_job(research)]


@pytest.mark.postgres
def test_the_move_copies_verifies_resumes_and_leaves_the_files_untouched(tmp_path, pg_admin_url):
    from tests.conftest import fresh_pg_database

    app, archive, research = _make_sources(tmp_path)
    before = _digest(app, archive, research)
    url = fresh_pg_database(pg_admin_url, "settleview_move")
    lines: list[str] = []
    res = {r.table: r for r in pgmove.move(_jobs(app, archive, research), url, batch=7, sample_rows=50,
                                           say=lines.append)}
    assert _digest(app, archive, research) == before  # read-only, every file
    src = sqlite3.connect(f"file:{app.as_posix()}?mode=ro", uri=True)
    eng = write_engine(url)
    try:
        with eng.connect() as c:
            for name in ("trades", "index_1s", "decisions", "data_quality", "agents", "gaps", "trading_settings"):
                n = src.execute(f"select count(*) from {name}").fetchone()[0]
                assert c.execute(text(f"select count(*) from app.{name}")).scalar() == n == res[f"app.{name}"].rows
            assert res["app.trades_archive"].rows > 0 and res["research.pm_ws_quotes"].rows == 900
            assert res["research.pm_trades"].status == "empty" and res["research.pm_quotes"].status == "empty"
            assert c.execute(text("select archived from app.agents")).scalar() is True
            # SQLite stores a NaN REAL as NULL, so the source holds NULL and the copy must too (found on Postgres 17)
            assert src.execute("select value from data_quality").fetchone()[0] is None
            assert c.execute(text("select value from app.data_quality")).scalar() is None
            assert c.execute(text("select explanation from app.decisions limit 1")).scalar() == "é ✓"
        # sequences start past the copied ids
        store = SqliteStore(eng, read_engine(url))
        assert store.insert("decisions", [{"horizon": 900, "boundary": 1, "ruleset_hash": "z", "run_id": 0,
                                           "call": "UP", "explanation": "", "conditions": [], "flags": [],
                                           "created_ts": 1}]) == [301]
        store._read.dispose()
        with eng.begin() as c:  # take the probe row out again: a table written to after its copy is refused, not skipped
            c.execute(text("delete from app.decisions where id = 301"))
        # a re-run skips every verified table
        again = pgmove.move(_jobs(app, archive, research), url, batch=7, say=lines.append)
        assert {r.status for r in again if r.rows} == {"skipped (done before)"}
        # a changed row is caught by the checksum
        with eng.begin() as c:
            c.execute(text("update app.gaps set cause = 'changed' where start_ts = 1"))
        con = pgmove.open_source(app)
        with pytest.raises(pgmove.MoveError, match="checksum differs"):
            pgmove._verify(con, eng, "app", "gaps", TABLES["gaps"], ["id", "start_ts", "end_ts", "cause",
                                                                       "from_trade_id", "to_trade_id", "repaired",
                                                                       "created_ts"], 39, 8, 50)
        con.close()
    finally:
        src.close()
        eng.dispose()


@pytest.mark.postgres
def test_a_target_with_rows_is_refused_unless_forced(tmp_path, pg_admin_url):
    from tests.conftest import fresh_pg_database

    app, _, _ = _make_sources(tmp_path)
    url = fresh_pg_database(pg_admin_url, "settleview_move_refuse")
    migrate(url)
    eng = write_engine(url)
    try:
        with eng.begin() as c:
            c.execute(text("insert into app.gaps (start_ts, cause, repaired, created_ts) values (1, 'mine', false, 1)"))
        with pytest.raises(pgmove.MoveError, match="app.gaps already has 1 rows"):
            pgmove.move([pgmove.app_job(app)], url, say=lambda s: None)
        with pytest.raises(pgmove.MoveError, match="names no table"):
            pgmove.move([pgmove.app_job(app)], url, force_tables=["nope"], say=lambda s: None)
        pgmove.move([pgmove.app_job(app)], url, force_tables=["gaps"], say=lambda s: None)
        with eng.connect() as c:
            assert c.execute(text("select count(*) from app.gaps where cause = 'mine'")).scalar() == 0
            assert c.execute(text("select count(*) from app.gaps")).scalar() == 39
    finally:
        eng.dispose()


def test_text_columns_take_the_numbers_sqlite_stored_in_them():
    from btc_signal_engine.adapters.sqlite.tables import metadata

    conv = pgmove._converter(metadata.tables["trading_brackets"].c["setup_key"])
    assert conv(1793245877390) == "1793245877390" and conv(17.0) == "17"
    assert conv("pnw_cushion:1793246400000:1793245877390") == "pnw_cushion:1793246400000:1793245877390"
    assert conv(None) is None
