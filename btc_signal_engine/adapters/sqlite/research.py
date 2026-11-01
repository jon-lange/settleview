"""The research recorders' data on either store: SQLite (`<research dir>/polymarket_us.db`) or Postgres (schema
`research` of the app's database).

- `ResearchDb`: read-only access for the app's freshness alerts and coverage line and the containers' healthchecks.
  SQLite: the file opened with `mode=ro` on every call (never a write, never a lock held between calls), the newest
  quote from the largest rowid (the tables are append-only: an index lookup, not a scan of millions of rows).
  Postgres: read-only transactions, the newest quote from the receive-time index.
- `research_write_engine` / `create_research_tables`: the recorders' writer.
- `readonly_engine`: the one read helper for research scripts: a SQLite file read-only, or Postgres as the read-only
  `settleview_research` role (search_path research, app).
"""

from __future__ import annotations

import time
from pathlib import Path
from collections.abc import Iterable

from sqlalchemy import Engine, Table, create_engine, func, inspect, select, text
from sqlalchemy.pool import NullPool

from btc_signal_engine.adapters.polymarket_us.markets import slug_for
from btc_signal_engine.adapters.sqlite.db import RESEARCH_SCHEMA, ensure_schema, lock_key, made, pg_engine
from btc_signal_engine.adapters.sqlite.research_tables import pm_trades, pm_windows, pm_ws_quotes, research_metadata
from btc_signal_engine.adapters.sqlite.urls import DbTarget, is_postgres

DB_NAME = "polymarket_us.db"
# recorder -> (table, time column): the quotes recorder's WebSocket rows and the settlement recorder's window rows
NEWEST = {"pm-quotes": (pm_ws_quotes, "recv_ms"), "pm-settle": (pm_windows, "fetched_ms")}
TRADES = "pm-trades"  # newest_ms's extra key: the newest recorded print (not a recorder of its own)
SPAN_NAMES = {300_000: "5m", 900_000: "15m", 3_600_000: "1h"}


def _sqlite_ro(path: Path, timeout_s: float) -> Engine:
    """A read-only SQLite engine that holds no connection between calls (NullPool)."""
    return create_engine(f"sqlite:///file:{path.resolve().as_posix()}?mode=ro&uri=true", poolclass=NullPool,
                         connect_args={"timeout": timeout_s}, hide_parameters=True)


class ResearchDb:
    def __init__(self, research_dir: Path | None = None, *, url=None, timeout_s: float = 1.0) -> None:
        if (research_dir is None) == (url is None):
            raise ValueError("ResearchDb needs a research folder (SQLite) or a Postgres URL, not both")
        self.url = url
        self.path = Path(research_dir) / DB_NAME if research_dir is not None else None
        self.timeout_s = timeout_s
        self._pg: Engine | None = None

    @classmethod
    def for_target(cls, database: DbTarget, research_dir: Path) -> ResearchDb:
        """Where the recorders write next to this app database: Postgres's `research` schema, else the folder."""
        return cls(url=database) if is_postgres(database) else cls(research_dir)

    @property
    def label(self) -> str:
        """How alerts name the research data."""
        return f"the {RESEARCH_SCHEMA} schema" if self.url is not None else DB_NAME

    @property
    def where(self) -> str:
        return f"Postgres schema {RESEARCH_SCHEMA}" if self.url is not None else str(self.path)

    def available(self) -> bool:
        return self.url is not None or self.path.exists()

    def _engine(self) -> Engine:
        if self.url is not None:
            if self._pg is None:
                self._pg = pg_engine(self.url, RESEARCH_SCHEMA, read_only=True, name="settleview-research-read",
                                     pool_size=1)
            return self._pg
        if not self.path.exists():
            raise FileNotFoundError(str(self.path))
        return _sqlite_ro(self.path, self.timeout_s)

    def _tables(self, conn) -> set[str]:
        return set(inspect(conn).get_table_names(schema=RESEARCH_SCHEMA if self.url is not None else None))

    def newest_ms(self, now_ms: int | None = None) -> dict[str, int | None]:
        """Each recorder's newest row (NEWEST), plus TRADES: the newest recorded print (the app tells "the venue
        sends trades but no quotes" from "the quotes recorder is down" with it)."""
        with self._engine().connect() as con:
            tables = self._tables(con)
            out: dict[str, int | None] = {}
            for name, (table, col) in NEWEST.items():
                if table.name not in tables:
                    out[name] = None
                    continue
                c = table.c[col]
                if self.url is None:  # SQLite: the newest row is the largest rowid (no index on the time column)
                    stmt = select(c).where(text(f"rowid = (select max(rowid) from {table.name})"))  # noqa: S608 - table name from code (NEWEST)
                else:
                    stmt = select(func.max(c))  # Postgres: an index-only lookup (ix_..._recv_ms; pm_windows is small)
                v = con.execute(stmt).scalar()
                out[name] = None if v is None else int(v)
            out[TRADES] = self._newest_trade(con, tables, now_ms)
            return out

    def _newest_trade(self, con, tables: set[str], now_ms: int | None) -> int | None:
        """The newest recorded print's venue time (`trade_ms`): SQLite by the largest rowid; Postgres per market of
        the current and previous 5m / 15m / 1h windows on the (slug, trade_ms) index (pm_trades has no receive-time
        index there, and a max over the whole table would scan it)."""
        if pm_trades.name not in tables:
            return None
        c = pm_trades.c
        if self.url is None:
            stmt = select(func.coalesce(c.trade_ms, c.recv_ms)).where(
                text("rowid = (select max(rowid) from pm_trades)"))
            v = con.execute(stmt).scalar()
            return None if v is None else int(v)
        now = int(time.time() * 1000) if now_ms is None else now_ms
        best = None
        for span_ms, span in SPAN_NAMES.items():
            start = now - now % span_ms
            for st in (start, start - span_ms):
                v = con.execute(select(func.max(c.trade_ms)).where(c.slug == slug_for(st, span))).scalar()
                if v is not None and (best is None or v > best):
                    best = int(v)
        return best

    def coverage(self, start_ms: int, end_ms: int, span_ms: int) -> dict:
        span = SPAN_NAMES[span_ms]
        starts = list(range(-(-start_ms // span_ms) * span_ms, end_ms, span_ms))  # window starts in [start, end)
        with self._engine().connect() as con:
            tables = self._tables(con)
            missing = []
            q = pm_ws_quotes.c
            for st in starts:
                hit = None
                if "pm_ws_quotes" in tables:  # the (slug, recv_ms) index: one lookup per window
                    hit = con.execute(select(1).where(q.slug == slug_for(st, span), q.recv_ms >= st,
                                                      q.recv_ms < st + span_ms).limit(1)).first()
                if hit is None:
                    missing.append(st)
            settled = 0
            if "pm_windows" in tables:
                w = pm_windows.c
                settled = con.execute(select(func.count()).select_from(pm_windows).where(
                    w.span == span, w.window_start >= start_ms // 1000, w.window_start < end_ms // 1000,
                    w.settlement_price.is_not(None))).scalar()
            return {"windows": len(starts), "recorded": len(starts) - len(missing), "settled": int(settled or 0),
                    "missing_starts": missing}


def research_write_engine(target: DbTarget) -> Engine:
    """The recorders' writer: the SQLite file (its folder is created), or Postgres's `research` schema."""
    if is_postgres(target):
        return pg_engine(target, RESEARCH_SCHEMA, name="settleview-recorder", pool_size=1)
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    return made(create_engine(f"sqlite:///{path.as_posix()}", hide_parameters=True))


def create_research_tables(engine: Engine, tables: Iterable[Table]) -> None:
    """Create the recorder's tables where missing (existing tables and files are left as they are). On Postgres, the
    schema too, under an advisory lock: both recorders start at once."""
    with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(text("select pg_advisory_xact_lock(:k)"), {"k": lock_key("research-tables")})
            ensure_schema(conn, RESEARCH_SCHEMA)
        research_metadata.create_all(conn, tables=list(tables), checkfirst=True)


def readonly_engine(source: DbTarget) -> Engine:
    """The research scripts' read helper: `source` is a SQLite file (opened `mode=ro`) or a Postgres URL (use the
    settleview_research role; every transaction is read-only and both schemas are on the search path, research
    first). Works with `pandas.read_sql(sql, engine)` and `engine.connect()`."""
    if is_postgres(source):
        # Research queries scan a lot: the long statement timeout (urls.PgTimeouts), still bounded.
        return pg_engine(source, f"{RESEARCH_SCHEMA},app", read_only=True, name="settleview-research-script",
                         pool_size=1, long=True)
    path = Path(source)
    if not path.exists():
        raise FileNotFoundError(str(path))
    return _sqlite_ro(path, 60.0)
