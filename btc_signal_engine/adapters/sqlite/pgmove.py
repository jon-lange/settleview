"""Copy SQLite files into Postgres for the cut-over (`python -m btc_signal_engine.ops migrate-sqlite-to-pg`, plan G6).

Sources (each opened read-only, `file:...?mode=ro`; never written):
- the app's database (`btcflow.db`) -> the schema `app`, table by table, same names;
- optionally the trade archive (`btcflow-archive.db`, table `trades`) -> `app.trades_archive`;
- optionally the recorders' database (`polymarket_us.db`) -> the schema `research`.

For each table: the target is migrated to the latest schema first (Alembic, as the app does at start); the source's
columns must all exist in the target; the rows stream from SQLite with `fetchmany` and into Postgres with `COPY`
(psycopg), one transaction per table, so memory stays bounded by one batch and a stopped copy leaves the table empty.
Then the table's id sequence is set past the largest id, and the copy is verified: equal row counts, and equal
checksums (an order-independent sum of row hashes) over sampled key ranges spread across the table. A table is
recorded as done in `<schema>._sqlite_migration` only once verified, so a re-run skips it (resumable per table).
A target table that has rows but is not recorded as done is refused unless named with `--force-table` (then emptied
first). Nothing is printed that could hold a secret (the URL is shown without its password).
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable, Iterable, Iterator

from sqlalchemy import JSON, BigInteger, Boolean, Engine, Float, Integer, String, Table, text

from btc_signal_engine.adapters.sqlite.db import APP_SCHEMA, RESEARCH_SCHEMA, migrate, pg_engine
from btc_signal_engine.adapters.sqlite.research import create_research_tables
from btc_signal_engine.adapters.sqlite.research_tables import RESEARCH_TABLES
from btc_signal_engine.adapters.sqlite.tables import TABLES

BATCH = 50_000  # rows per fetchmany / COPY chunk (bounded memory: one batch at a time)
SAMPLES = 8  # key ranges checksummed per table
SAMPLE_ROWS = 2_000  # about this many rows per sampled range
PROGRESS = "_sqlite_migration"
# Recorder tables have no primary key: their ranges go by this column.
RANGE_KEY = {"pm_ws_quotes": "recv_ms", "pm_trades": "recv_ms", "pm_quotes": "ts_ms", "pm_windows": "slug"}


class MoveError(RuntimeError):
    """A refusal or a failed check; the message says which table and why (never a value of a secret)."""


@dataclass
class TableResult:
    source: str
    table: str  # schema.table in the target
    rows: int = 0
    status: str = ""  # copied | skipped (done before) | empty | missing in source
    seconds: float = 0.0
    checked_ranges: int = 0


@dataclass
class Job:
    """One SQLite file and where its tables go."""
    path: Path
    schema: str
    tables: dict[str, Table]  # source table name -> target table definition
    rename: dict[str, str] = field(default_factory=dict)  # source name -> target name, when they differ

    def target_name(self, source: str) -> str:
        return self.rename.get(source, source)


def app_job(path: Path) -> Job:
    return Job(Path(path), APP_SCHEMA, {n: t for n, t in TABLES.items() if n != "trades_archive"})


def archive_job(path: Path) -> Job:
    return Job(Path(path), APP_SCHEMA, {"trades": TABLES["trades_archive"]}, {"trades": "trades_archive"})


def research_job(path: Path) -> Job:
    return Job(Path(path), RESEARCH_SCHEMA, dict(RESEARCH_TABLES))


def open_source(path: Path) -> sqlite3.Connection:
    """The SQLite file, read-only (an absent file is an error, never created)."""
    if not Path(path).is_file():
        raise MoveError(f"{path}: no such file")
    return sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True, timeout=30)


def _source_tables(con: sqlite3.Connection) -> dict[str, list[str]]:
    names = [r[0] for r in con.execute("select name from sqlite_master where type = 'table' and name not like "
                                       "'sqlite_%' order by name")]
    return {n: [r[1] for r in con.execute(f'pragma table_info("{n}")')] for n in names}


# -- value conversion and hashing ------------------------------------------------------------------------------------

def _converter(col) -> Callable | None:
    """SQLite stores what it is given; Postgres checks types. Integral floats in integer columns become ints, 0 / 1 in
    boolean columns become bools; anything else that does not fit is an error naming the column. A JSON column has
    NUMERIC affinity in SQLite, so a JSON number (a setting's `10`) was stored as a number: it goes back to JSON text."""
    if isinstance(col.type, JSON):
        return lambda v: json.dumps(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v
    if isinstance(col.type, Boolean):
        return lambda v: v if v is None or isinstance(v, bool) else bool(v)
    if isinstance(col.type, (Integer, BigInteger)):
        def to_int(v):
            if v is None or isinstance(v, int):
                return v
            if isinstance(v, float) and v.is_integer():
                return int(v)
            if isinstance(v, str) and v.strip().lstrip("-").isdigit():
                return int(v)
            raise MoveError(f"column {col.table.name}.{col.name}: a {type(v).__name__} that is not a whole number")
        return to_int
    if isinstance(col.type, Float):
        return lambda v: float(v) if isinstance(v, int) and not isinstance(v, bool) else v
    if isinstance(col.type, String):
        def to_text(v):  # a text column SQLite also stored numbers in (trading_brackets.setup_key)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                return v
            return str(int(v)) if isinstance(v, int) or v.is_integer() else repr(v)
        return to_text
    return None


def _norm(v) -> str:
    """A value's canonical text for checksums, the same whichever side it was read from."""
    if v is None:
        return "\x00N"
    if isinstance(v, bool):  # SQLite's 0 / 1 and Postgres's false / true are the same value
        v = int(v)
    if isinstance(v, float):
        if math.isnan(v):
            return "nan"
        return repr(float(int(v))) if v.is_integer() and abs(v) < 2**53 else repr(v)
    if isinstance(v, int):
        return repr(float(v)) if abs(v) < 2**53 else str(v)  # 1 and 1.0 are the same value
    if isinstance(v, (bytes, memoryview)):
        return bytes(v).hex()
    return str(v)


def _row_hash(row: Iterable) -> int:
    h = hashlib.blake2b("\x1f".join(_norm(v) for v in row).encode(), digest_size=8).digest()
    return int.from_bytes(h, "big")


def _multiset(rows: Iterable[Iterable]) -> tuple[int, int]:
    """(count, sum of row hashes mod 2^64): equal for the same rows in any order."""
    n, s = 0, 0
    for r in rows:
        n += 1
        s = (s + _row_hash(r)) & 0xFFFFFFFFFFFFFFFF
    return n, s


# -- the copy --------------------------------------------------------------------------------------------------------
# The SQL below is built with f-strings for identifiers only (each marked noqa S608): schema names are the constants
# APP_SCHEMA / RESEARCH_SCHEMA, table names come from tables.py / research_tables (Job.tables), PROGRESS is a constant,
# and every column name read from the SQLite file is first checked to be a column of the code-defined target table
# (move: "has columns the target lacks"). Values are always bound parameters.

def _ensure_progress(engine: Engine, schema: str) -> None:
    with engine.begin() as c:
        c.execute(text(f'create table if not exists "{schema}".{PROGRESS} (table_name text primary key, '
                       "source text not null, source_rows bigint not null, finished_ms bigint not null)"))


def _done(engine: Engine, schema: str, table: str) -> int | None:
    with engine.connect() as c:
        r = c.execute(text(f'select source_rows from "{schema}".{PROGRESS} where table_name = :t'), {"t": table}).first()  # noqa: S608 - identifiers from code, values bound
    return None if r is None else int(r[0])


def _count_pg(engine: Engine, schema: str, table: str) -> int:
    with engine.connect() as c:
        return int(c.execute(text(f'select count(*) from "{schema}"."{table}"')).scalar())  # noqa: S608 - identifiers from code, values bound


def _batches(cur: sqlite3.Cursor, size: int) -> Iterator[list[tuple]]:
    while True:
        rows = cur.fetchmany(size)
        if not rows:
            return
        yield rows


def _copy_table(src: sqlite3.Connection, engine: Engine, schema: str, source: str, target: Table, cols: list[str],
                batch: int) -> int:
    convs = [_converter(target.c[c]) for c in cols]
    quoted = ", ".join(f'"{c}"' for c in cols)
    raw = engine.raw_connection()  # psycopg's own connection: COPY
    n = 0
    try:
        with raw.cursor() as cur:
            with cur.copy(f'copy "{schema}"."{target.name}" ({quoted}) from stdin') as cp:
                scur = src.execute(f'select {quoted} from "{source}"')  # noqa: S608 - identifiers from code, values bound
                for rows in _batches(scur, batch):
                    for r in rows:
                        cp.write_row([v if f is None else f(v) for f, v in zip(convs, r, strict=True)])
                    n += len(rows)
        raw.commit()
    except BaseException:
        raw.rollback()
        raise
    finally:
        raw.close()
    return n


def _reset_sequences(engine: Engine, schema: str, target: Table) -> None:
    """Postgres's id sequences start at 1; after copying ids, start them past the largest."""
    pk = list(target.primary_key.columns)
    for col in pk:
        if len(pk) == 1 and isinstance(col.type, Integer) and col.autoincrement is not False:
            with engine.begin() as c:
                seq = c.execute(text("select pg_get_serial_sequence(:t, :c)"),
                                {"t": f'"{schema}"."{target.name}"', "c": col.name}).scalar()
                if seq:
                    c.execute(text(f'select setval(:s, coalesce((select max("{col.name}") from '  # noqa: S608 - identifiers from code, values bound
                                   f'"{schema}"."{target.name}"), 0) + 1, false)'), {"s": seq})


def _range_key(source: str, target: Table) -> str:
    if source in RANGE_KEY:
        return RANGE_KEY[source]
    return list(target.primary_key.columns)[0].name


def _sample_ranges(src: sqlite3.Connection, source: str, key: str, total: int, samples: int,
                   per: int) -> list[tuple]:
    """Key ranges [lo, hi] spread over the table, the first and last included, about `per` rows each. A numeric key
    (times, ids) is split by value, which needs no sort (the recorder tables have no index on it); a text key (uuids,
    slugs: small tables) by row position."""
    if total == 0:
        return []
    lo_all, hi_all = src.execute(f'select min("{key}"), max("{key}") from "{source}"').fetchone()  # noqa: S608 - identifiers from code, values bound
    if isinstance(lo_all, int) and isinstance(hi_all, int):
        span = hi_all - lo_all
        width = max(0, span * per // total)
        out = []
        for i in range(samples):
            lo = lo_all + span * i // max(1, samples - 1)
            hi = min(hi_all, lo + width)
            if i == samples - 1:
                lo, hi = max(lo_all, hi_all - width), hi_all
            out.append((lo, hi))
        return sorted(set(out))
    out = []
    starts = sorted({min(total - 1, round(i * (total - 1) / max(1, samples - 1))) for i in range(samples)})
    for off in starts:
        lo = src.execute(f'select "{key}" from "{source}" order by "{key}" limit 1 offset ?', (off,)).fetchone()[0]  # noqa: S608 - identifiers from code, values bound
        hi = src.execute(f'select "{key}" from "{source}" where "{key}" >= ? order by "{key}" limit 1 offset ?',  # noqa: S608 - identifiers from code, values bound
                         (lo, per - 1)).fetchone()
        out.append((lo, hi[0] if hi else src.execute(f'select max("{key}") from "{source}"').fetchone()[0]))  # noqa: S608 - identifiers from code, values bound
    return out


def _verify(src: sqlite3.Connection, engine: Engine, schema: str, source: str, target: Table, cols: list[str],
            rows: int, samples: int, per: int) -> int:
    got = _count_pg(engine, schema, target.name)
    if got != rows:
        raise MoveError(f"{schema}.{target.name}: {got} rows in Postgres, {rows} in the SQLite file")
    key = _range_key(source, target)
    quoted = ", ".join(f'"{c}"' for c in cols)
    convs = [_converter(target.c[c]) for c in cols]
    # SQLite compares text byte by byte; Postgres by the database's locale, so the same "between lo and hi" picks other
    # rows. Compare text keys in byte order ("C") on Postgres too (found on the live data's trading_settings keys).
    pg_key = f'"{key}" COLLATE "C"' if isinstance(target.c[key].type, String) else f'"{key}"'
    checked = 0
    with engine.connect() as conn:
        c = conn.execution_options(stream_results=True, max_row_buffer=BATCH)  # a server-side cursor: bounded memory
        for lo, hi in _sample_ranges(src, source, key, rows, samples, per):
            a = _multiset([v if f is None else f(v) for f, v in zip(convs, r, strict=True)] for r in src.execute(
                f'select {quoted} from "{source}" where "{key}" between ? and ?', (lo, hi)))  # noqa: S608 - identifiers from code, values bound
            b = _multiset(c.execute(text(f'select {quoted} from "{schema}"."{target.name}" '  # noqa: S608 - identifiers from code, values bound
                                         f'where {pg_key} between :lo and :hi'), {"lo": lo, "hi": hi}))
            if a != b:
                raise MoveError(f"{schema}.{target.name}: checksum differs for {key} in [{lo}, {hi}] "
                                f"(SQLite {a[0]} rows, Postgres {b[0]})")
            checked += 1
    return checked


def move(jobs: list[Job], url, *, force_tables: Iterable[str] = (), batch: int = BATCH, samples: int = SAMPLES,
         sample_rows: int = SAMPLE_ROWS, say: Callable[[str], None] = print) -> list[TableResult]:
    """Copy every job's tables; returns one result per table. Raises MoveError on a refusal or a failed check (tables
    finished before it stay done)."""
    force = set(force_tables)
    known = {j.target_name(s) for j in jobs for s in j.tables}
    unknown = force - known
    if unknown:
        raise MoveError(f"--force-table names no table to copy: {', '.join(sorted(unknown))}")
    migrate(url)  # the app's schema at head, as the app makes it at start
    engine = pg_engine(url, APP_SCHEMA, name="settleview-migrate-data", pool_size=1, long=True)
    results: list[TableResult] = []
    try:
        for job in jobs:
            if job.schema == RESEARCH_SCHEMA:
                rengine = pg_engine(url, RESEARCH_SCHEMA, name="settleview-migrate-data", pool_size=1, long=True)
                try:
                    create_research_tables(rengine, job.tables.values())
                finally:
                    rengine.dispose()
            _ensure_progress(engine, job.schema)
            src = open_source(job.path)
            try:
                have = _source_tables(src)
                for source, target in job.tables.items():
                    name = job.target_name(source)
                    res = TableResult(str(job.path.name), f"{job.schema}.{name}")
                    results.append(res)
                    if source not in have:
                        res.status = "missing in source"
                        continue
                    cols = have[source]
                    extra = [c for c in cols if c not in target.c]
                    if extra:
                        raise MoveError(f"{job.path.name}:{source} has columns the target lacks ({', '.join(extra)}): "
                                        "start the current app once on that file, or check the schema versions")
                    rows = int(src.execute(f'select count(*) from "{source}"').fetchone()[0])  # noqa: S608 - identifiers from code, values bound
                    res.rows = rows
                    done = _done(engine, job.schema, name)
                    present = _count_pg(engine, job.schema, name)
                    if done is not None and done == rows and present == rows and name not in force:
                        res.status = "skipped (done before)"
                        say(f"{res.table}: done before ({rows} rows), skipped")
                        continue
                    if present and name not in force:
                        raise MoveError(f"{res.table} already has {present} rows and is not recorded as copied: "
                                        f"refusing to write into it (--force-table {name} empties it first)")
                    t0 = time.monotonic()
                    with engine.begin() as c:
                        c.execute(text(f'delete from "{job.schema}".{PROGRESS} where table_name = :t'), {"t": name})  # noqa: S608 - identifiers from code, values bound
                        if present:
                            if name == "trading_audit":  # append-only (migration 0023): its owner may lift it here
                                c.execute(text(f'alter table "{job.schema}".trading_audit disable trigger '
                                               "trading_audit_append_only"))
                            c.execute(text(f'truncate "{job.schema}"."{name}"'))
                            if name == "trading_audit":
                                c.execute(text(f'alter table "{job.schema}".trading_audit enable trigger '
                                               "trading_audit_append_only"))
                    copied = _copy_table(src, engine, job.schema, source, target, cols, batch) if rows else 0
                    if copied != rows:
                        raise MoveError(f"{res.table}: copied {copied} rows, the source has {rows} (written to?)")
                    _reset_sequences(engine, job.schema, target)
                    res.checked_ranges = _verify(src, engine, job.schema, source, target, cols, rows, samples,
                                                 sample_rows)
                    with engine.begin() as c:
                        c.execute(text(f'insert into "{job.schema}".{PROGRESS} (table_name, source, source_rows, '  # noqa: S608 - identifiers from code, values bound
                                       "finished_ms) values (:t, :s, :n, :ms)"),
                                  {"t": name, "s": job.path.name, "n": rows, "ms": int(time.time() * 1000)})
                    res.seconds = time.monotonic() - t0
                    res.status = "copied" if rows else "empty"
                    say(f"{res.table}: {rows} rows copied and verified ({res.checked_ranges} sampled ranges) in "
                        f"{res.seconds:.1f} s")
                not_copied = sorted(set(have) - set(job.tables) - {"alembic_version"})
                if not_copied:
                    say(f"{job.path.name}: tables the app no longer defines, NOT copied (they stay in the file): "
                        f"{', '.join(not_copied)}")
            finally:
                src.close()
    finally:
        engine.dispose()
    return results
