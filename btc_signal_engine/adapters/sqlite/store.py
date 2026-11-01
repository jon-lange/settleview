"""The MarketStore and Repository ports on SQLAlchemy Core, one code path for SQLite and Postgres.

Writes go through `write_conn` (the single writer); reads use the read engine.
Every insert is idempotent on its natural key: `insert` ignores a row whose key exists (ON CONFLICT DO NOTHING), and
with `ignore_conflicts=False` replaces it (ON CONFLICT ... DO UPDATE every column, what SQLite's INSERT OR REPLACE did).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import Boolean, Connection, Engine, Integer, Table, UniqueConstraint, and_, func, select, update
from sqlalchemy.dialects import postgresql, sqlite

from btc_signal_engine.adapters.sqlite.tables import TABLES, book_1s, index_1s, trades
from btc_signal_engine.domain.models.events import BookSecond, IndexSecond, Trade

_OPS = {
    "=": lambda c, v: c == v,
    "!=": lambda c, v: c != v,
    "<": lambda c, v: c < v,
    "<=": lambda c, v: c <= v,
    ">": lambda c, v: c > v,
    ">=": lambda c, v: c >= v,
    "in": lambda c, v: c.in_(list(v)),
    "is_null": lambda c, v: c.is_(None) if v else c.is_not(None),
}
_INSERTS = {"sqlite": sqlite.insert, "postgresql": postgresql.insert}

# Boolean columns per table: SQLite took 0 / 1 for them, Postgres takes only true / false, so values are made bools.
_BOOLS = {name: frozenset(c.name for c in t.columns if isinstance(c.type, Boolean)) for name, t in TABLES.items()}


def _as_bool(v: Any) -> Any:
    return v if v is None or isinstance(v, bool) else bool(v)


def _bools(table: str, row: dict[str, Any]) -> dict[str, Any]:
    cols = _BOOLS[table]
    if not cols or not any(k in cols and not isinstance(v, bool) and v is not None for k, v in row.items()):
        return row
    return {k: _as_bool(v) if k in cols else v for k, v in row.items()}


def _filter_value(table: str, col: str, op: str, val: Any) -> Any:
    if col not in _BOOLS[table] or op == "is_null":
        return val
    return [_as_bool(v) for v in val] if op == "in" else _as_bool(val)


def _default_order(t: Table) -> list:
    """Insertion order, as SQLite's rowid gave it: an autoincrement id; else the creation time, then the key; else
    the key."""
    pk = list(t.primary_key.columns)
    if len(pk) == 1 and isinstance(pk[0].type, Integer) and pk[0].autoincrement is not False:
        return pk
    created = [t.c[n] for n in ("created_ts", "created_ms") if n in t.c][:1]
    return created + pk


_ORDER = {name: _default_order(t) for name, t in TABLES.items()}


def conflict_target(t: Table, keys: Iterable[str]) -> tuple[str, ...]:
    """The key a row replaces on: the primary key when the row gives it, else the first unique constraint it gives
    in full (a table keyed by an autoincrement id that the row leaves out). () when the row gives none."""
    keys = set(keys)
    pk = tuple(c.name for c in t.primary_key.columns)
    if pk and set(pk) <= keys:
        return pk
    uniques = [tuple(c.name for c in u.columns) for u in t.constraints if isinstance(u, UniqueConstraint)]
    uniques += [(c.name,) for c in t.columns if c.unique]
    for u in uniques:
        if u and set(u) <= keys:
            return u
    return ()


class SqliteStore:
    """The app's store on either dialect (the name is kept: every port and test knows it by it)."""

    def __init__(self, write_engine: Engine, read_engine: Engine | None = None) -> None:
        self._write = write_engine
        self._read = read_engine or write_engine
        self._insert_fn = _INSERTS[write_engine.dialect.name]

    @property
    def dialect(self) -> str:
        return self._write.dialect.name

    # -- writes -----------------------------------------------------------------
    def _rows(self, table: str, rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        return [_bools(table, r) for r in rows]

    def _insert_ignore(self, table: str, rows: Sequence[dict[str, Any]], conn: Connection | None = None) -> list[int]:
        if not rows:
            return []
        t = TABLES[table]
        rows = self._rows(table, rows)
        stmt = self._insert_fn(t).on_conflict_do_nothing()
        if conn is not None:
            conn.execute(stmt, rows)
            return []
        with self._write.begin() as c:
            if len(rows) == 1:  # the new row's key (an autoincrement id); [] when the row existed
                pk = list(t.primary_key.columns)[:1]
                if not pk:
                    c.execute(stmt, rows[0])
                    return []
                return [r[0] for r in c.execute(stmt.values(rows[0]).returning(*pk))]
            c.execute(stmt, rows)
        return []

    def _upsert(self, table: str, rows: Sequence[dict[str, Any]], conn: Connection | None = None) -> None:
        if not rows:
            return
        t = TABLES[table]
        rows = self._rows(table, rows)
        stmt = self._insert_fn(t)
        target = conflict_target(t, rows[0])
        if target:
            # Every other column takes the new row's value, defaults included (excluded.* is the full proposed row):
            # the row ends as INSERT OR REPLACE left it. An autoincrement id the row left out keeps its old value.
            keep = set(target) | {c.name for c in t.primary_key.columns if c.name not in rows[0]}
            new = {c.name: stmt.excluded[c.name] for c in t.columns if c.name not in keep}
            stmt = (stmt.on_conflict_do_update(index_elements=list(target), set_=new) if new
                    else stmt.on_conflict_do_nothing(index_elements=list(target)))
        if conn is not None:
            conn.execute(stmt, rows)
            return
        with self._write.begin() as c:
            c.execute(stmt, rows)

    def add_trades(self, items: Iterable[Trade], conn: Connection | None = None) -> None:
        rows = [
            {"trade_id": t.trade_id, "ts": t.ts, "ingest_ts": t.ingest_ts, "price": t.price,
             "size": t.size, "buy": t.buy, "backfilled": t.backfilled}
            for t in items
        ]
        self._insert_ignore("trades", rows, conn)

    def add_book_seconds(self, items: Iterable[BookSecond], conn: Connection | None = None) -> None:
        rows = [
            {"ts": b.ts, "ingest_ts": b.ingest_ts, "bid": b.bid, "ask": b.ask,
             "bid_depth_10": b.bid_depth_10, "ask_depth_10": b.ask_depth_10,
             "bid_depth_25": b.bid_depth_25, "ask_depth_25": b.ask_depth_25,
             "bid_size": b.bid_size, "ask_size": b.ask_size}
            for b in items
        ]
        self._insert_ignore("book_1s", rows, conn)

    def add_index_seconds(self, items: Iterable[IndexSecond], conn: Connection | None = None) -> None:
        rows = [{"ts": i.ts, "price": i.price, "last_trade_ts": i.last_trade_ts, "mid": i.mid} for i in items]
        # Index rows can be recomputed (e.g. a late trade inside the lateness budget), so replace.
        self._upsert("index_1s", rows, conn)

    def insert(self, table: str, rows: Sequence[dict[str, Any]], *, ignore_conflicts: bool = True,
               conn: Connection | None = None) -> list[int]:
        if ignore_conflicts:
            return self._insert_ignore(table, rows, conn)
        self._upsert(table, rows, conn)
        return []

    def update(self, table: str, key: dict[str, Any], values: dict[str, Any], conn: Connection | None = None) -> int:
        t = TABLES[table]
        key, values = _bools(table, key), _bools(table, values)
        stmt = update(t).where(and_(*(t.c[k] == v for k, v in key.items()))).values(**values)
        if conn is not None:
            return conn.execute(stmt).rowcount
        with self._write.begin() as c:
            return c.execute(stmt).rowcount

    # -- reads ------------------------------------------------------------------
    def select(self, table: str, where: dict[str, Any] | None = None, *, order_by: Sequence[str] = (),
               limit: int | None = None, filters: Sequence[tuple[str, str, Any]] = ()) -> list[dict[str, Any]]:
        t = TABLES[table]
        stmt = select(t)
        conds = [t.c[k] == v for k, v in _bools(table, where or {}).items()]
        conds += [_OPS[op](t.c[col], _filter_value(table, col, op, val)) for col, op, val in filters]
        if conds:
            stmt = stmt.where(and_(*conds))
        named = set()
        for col in order_by:
            named.add(col.lstrip("-"))
            stmt = stmt.order_by(t.c[col[1:]].desc() if col.startswith("-") else t.c[col])
        # Then the table's default order (also the tie-break): neither store promises an order otherwise, and
        # Postgres moves an updated row to the end of its table, where SQLite kept its rowid.
        stmt = stmt.order_by(*(c for c in _ORDER[table] if c.name not in named))
        if limit is not None:
            stmt = stmt.limit(limit)
        with self._read.connect() as c:
            return [dict(r._mapping) for r in c.execute(stmt)]

    def trades_range(self, start: int, end: int) -> list[Trade]:
        stmt = select(trades).where(trades.c.ts >= start, trades.c.ts < end).order_by(trades.c.ts, trades.c.trade_id)
        with self._read.connect() as c:
            return [Trade(r.trade_id, r.ts, r.ingest_ts, r.price, r.size, bool(r.buy), bool(r.backfilled))
                    for r in c.execute(stmt)]

    def book_range(self, start: int, end: int) -> list[BookSecond]:
        stmt = select(book_1s).where(book_1s.c.ts >= start, book_1s.c.ts < end).order_by(book_1s.c.ts)
        with self._read.connect() as c:
            return [BookSecond(r.ts, r.ingest_ts, r.bid, r.ask, r.bid_depth_10, r.ask_depth_10,
                               r.bid_depth_25, r.ask_depth_25, r.bid_size, r.ask_size) for r in c.execute(stmt)]

    def index_range(self, start: int, end: int) -> list[IndexSecond]:
        stmt = select(index_1s).where(index_1s.c.ts >= start, index_1s.c.ts < end).order_by(index_1s.c.ts)
        with self._read.connect() as c:
            return [IndexSecond(r.ts, r.price, r.last_trade_ts, r.mid) for r in c.execute(stmt)]

    def last_index_before(self, ts: int) -> IndexSecond | None:
        stmt = select(index_1s).where(index_1s.c.ts < ts).order_by(index_1s.c.ts.desc()).limit(1)
        with self._read.connect() as c:
            r = c.execute(stmt).first()
        return IndexSecond(r.ts, r.price, r.last_trade_ts, r.mid) if r else None

    def max_trade_id(self) -> int | None:
        with self._read.connect() as c:
            return c.execute(select(func.max(trades.c.trade_id))).scalar()

    def min_trade_id(self) -> int | None:
        with self._read.connect() as c:
            return c.execute(select(func.min(trades.c.trade_id))).scalar()

    def candles(self, start: int, end: int, granularity_s: int = 60) -> list[dict]:
        """OHLC from the per-second index (same prices the settlement uses), volume from trades."""
        g = granularity_s * 1000
        out: dict[int, dict] = {}
        for r in self.index_range(start, end):
            b = r.ts - r.ts % g
            c = out.get(b)
            if c is None:
                out[b] = {"time": b // 1000, "open": r.price, "high": r.price, "low": r.price, "close": r.price,
                          "volume": 0.0}
            else:
                c["high"] = max(c["high"], r.price)
                c["low"] = min(c["low"], r.price)
                c["close"] = r.price
        stmt = (select((trades.c.ts - trades.c.ts % g).label("b"), func.sum(trades.c.size))
                .where(trades.c.ts >= start, trades.c.ts < end).group_by("b"))
        with self._read.connect() as c:
            for b, vol in c.execute(stmt):
                if b in out:
                    out[b]["volume"] = float(vol)
        return [out[k] for k in sorted(out)]

    def recent_trades(self, limit: int = 40) -> list[Trade]:
        stmt = select(trades).order_by(trades.c.ts.desc(), trades.c.trade_id.desc()).limit(limit)
        with self._read.connect() as c:
            return [Trade(r.trade_id, r.ts, r.ingest_ts, r.price, r.size, bool(r.buy), bool(r.backfilled))
                    for r in c.execute(stmt)]

    def last_trade_before(self, ts: int) -> Trade | None:
        stmt = select(trades).where(trades.c.ts < ts).order_by(trades.c.ts.desc(), trades.c.trade_id.desc()).limit(1)
        with self._read.connect() as c:
            r = c.execute(stmt).first()
        return Trade(r.trade_id, r.ts, r.ingest_ts, r.price, r.size, bool(r.buy), bool(r.backfilled)) if r else None
