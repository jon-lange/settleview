"""Historical backfill and gap repair from Coinbase REST (U4; KTD5, KTD6, KTD9).

- `backfill` pages trades backwards from the oldest stored trade (or from now) until
  `days` before `now_ms`, rebuilds `index_1s` for the new span with `index_seconds`
  (the same function live uses, so rows are identical), then fetches 1m candles.
  It resumes from the DB's min trade id, so an interrupted run is simply re-run.
- `repair_gaps` fills open `gaps` rows: an `id_jump` fetches exactly the missing
  trade-id range; a time gap (`stale`/`restart`) fetches trades covering its window.

`store` is a `SqliteStore`, or a `BufferedStore` wrapping one: index rebuilds also take
the just-fetched trades directly, so they never depend on a pending write being visible.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from btc_signal_engine.adapters.coinbase.rest import CoinbaseRest
from btc_signal_engine.adapters.sqlite.store import SqliteStore
from btc_signal_engine.application.ports import TradeHistory
from btc_signal_engine.domain.models.events import IndexSecond, Trade
from btc_signal_engine.domain.settlement.index import index_seconds

log = logging.getLogger(__name__)

SEC = 1000
MIN = 60 * SEC
HOUR = 60 * MIN
DAY = 24 * HOUR
PAGE_SIZE = 1000
CANDLES_PER_REQUEST = 300


def _floor(ts: int, unit: int) -> int:
    return ts - ts % unit


def _trade_ts(store: SqliteStore, trade_id: int) -> int | None:
    rows = store.select("trades", {"trade_id": trade_id})
    return rows[0]["ts"] if rows else None


async def backfill_trades(store: SqliteStore, history: TradeHistory, *, since_ms: int) -> int:
    """Page trades backwards from the oldest stored id until one is at or before `since_ms`.

    Returns the number of trades fetched (already-stored ids are ignored on insert).
    """
    cursor = store.min_trade_id()
    if cursor is not None and (_trade_ts(store, cursor) or 0) <= since_ms:
        return 0
    fetched = pages = 0
    while True:
        page = await history.trades_before(cursor, PAGE_SIZE)
        if not page:
            log.info("trade backfill: history exhausted below id %s", cursor)
            break
        store.add_trades(page)
        fetched += len(page)
        pages += 1
        cursor = min(t.trade_id for t in page)
        oldest = min(t.ts for t in page)
        if pages % 50 == 1:
            log.info("trade backfill: %d trades, reached id %d (%.2f days to go)",
                     fetched, cursor, max(0, oldest - since_ms) / DAY)
        if oldest <= since_ms:
            break
    log.info("trade backfill: done, %d trades in %d pages", fetched, pages)
    return fetched


def rebuild_index(store: SqliteStore, start: int, end: int, extra: Iterable[Trade] = (),
                  chunk_ms: int = HOUR) -> int:
    """Recompute `index_1s` for [start, end) from stored trades plus `extra`, carrying the prior row.

    The first chunk is written last, so an interrupted rebuild leaves the earliest index row
    where it was and the next `backfill` run redoes the whole span instead of leaving a hole.
    """
    start, end = _floor(start, SEC), _floor(end + SEC - 1, SEC)
    extra = sorted(extra, key=lambda t: t.ts)
    carry = store.last_index_before(start)
    first: list[IndexSecond] = []
    total = 0
    for s in range(start, end, chunk_ms):
        e = min(s + chunk_ms, end)
        trades = store.trades_range(s, e) + [t for t in extra if s <= t.ts < e]
        mids = {r.ts: r.mid for r in store.index_range(s, e) if r.mid is not None}  # keep live diagnostics
        rows = index_seconds(trades, s, e, carry=carry, mids=mids)
        if s == start:
            first = rows
        else:
            store.add_index_seconds(rows)
        if rows:
            carry = rows[-1]
        total += len(rows)
    store.add_index_seconds(first)
    return total


async def backfill_candles(store: SqliteStore, rest: CoinbaseRest, start: int, end: int,
                           granularity: int = 60) -> int:
    """Store REST candles (source "rest", ts = candle open in ms) for [start, end); live rows win."""
    step = granularity * SEC
    total = 0
    for s in range(_floor(start, step), end, CANDLES_PER_REQUEST * step):
        last = min(s + CANDLES_PER_REQUEST * step, end) - step
        rows = await rest.candles(s, max(s, last), granularity)
        store.insert("candles", [
            {"granularity": granularity, "ts": int(r[0]) * SEC, "low": r[1], "high": r[2],
             "open": r[3], "close": r[4], "volume": r[5], "source": "rest"}
            for r in rows
        ])
        total += len(rows)
    return total


async def backfill(store: SqliteStore, rest: CoinbaseRest, *, days: float, now_ms: int) -> dict[str, int]:
    """Backfill `days` of trades before `now_ms`, then the index and 1m candles for the new span."""
    trades = await backfill_trades(store, rest, since_ms=now_ms - int(days * DAY))
    oldest = store.select("trades", order_by=["ts"], limit=1)
    if not oldest:
        return {"trades": trades, "index_rows": 0, "candles": 0}
    start = _floor(oldest[0]["ts"], SEC)
    # The span ends at the earliest existing index row (live data or an earlier backfill).
    first_index = store.select("index_1s", filters=[("ts", ">=", start)], order_by=["ts"], limit=1)
    if first_index:
        end = first_index[0]["ts"]
    else:
        newest = store.select("trades", order_by=["-ts"], limit=1)[0]["ts"]
        end = _floor(newest, SEC) + SEC
    if end <= start:
        return {"trades": trades, "index_rows": 0, "candles": 0}
    log.info("rebuilding index_1s for %d seconds", (end - start) // SEC)
    index_rows = rebuild_index(store, start, end)
    candles = await backfill_candles(store, rest, start, end)
    log.info("backfill: %d trades, %d index rows, %d candles", trades, index_rows, candles)
    return {"trades": trades, "index_rows": index_rows, "candles": candles}


async def _fetch_id_range(history: TradeHistory, lo: int, hi: int) -> list[Trade]:
    """Trades with lo < trade_id < hi, requesting no more than the missing count."""
    got: list[Trade] = []
    cursor = hi
    while cursor - lo > 1:
        page = [t for t in await history.trades_before(cursor, min(PAGE_SIZE, cursor - lo - 1)) if t.trade_id > lo]
        if not page:
            break
        got += page
        cursor = min(t.trade_id for t in page)
    return got


async def _fetch_time_range(history: TradeHistory, start_ts: int, cursor: int | None) -> list[Trade]:
    """Trades below id `cursor` (latest when None) back to the first one before `start_ts`."""
    got: list[Trade] = []
    while True:
        page = await history.trades_before(cursor, PAGE_SIZE)
        if not page:
            break
        got += page
        cursor = min(t.trade_id for t in page)
        if min(t.ts for t in page) < start_ts:
            break
    return got


async def repair_gaps(store: SqliteStore, history: TradeHistory) -> int:
    """Repair every open gap row; returns how many were closed.

    Open time gaps (no `end_ts` yet) are left for a later run.
    """
    closed = 0
    for gap in store.select("gaps", {"repaired": False}, order_by=["id"]):
        if gap["cause"] == "id_jump":
            lo, hi = gap["from_trade_id"], gap["to_trade_id"]
            got = await _fetch_id_range(history, lo, hi)
            anchor_ts = _trade_ts(store, hi)
        elif gap["end_ts"] is not None:
            after = store.select("trades", filters=[("ts", ">=", gap["end_ts"])], order_by=["ts", "trade_id"],
                                 limit=1)
            got = await _fetch_time_range(history, gap["start_ts"], after[0]["trade_id"] if after else None)
            anchor_ts = after[0]["ts"] if after else gap["end_ts"]
        else:
            continue
        if got:
            store.add_trades(got)
            # Rebuild through the second of the next known trade, so carried prices are corrected too.
            end = max([t.ts for t in got] + ([anchor_ts] if anchor_ts is not None else []))
            rebuild_index(store, min(t.ts for t in got), _floor(end, SEC) + SEC, extra=got)
        store.update("gaps", {"id": gap["id"]}, {"repaired": True})
        closed += 1
        log.info("gap %d (%s) repaired with %d trades", gap["id"], gap["cause"], len(got))
    return closed
