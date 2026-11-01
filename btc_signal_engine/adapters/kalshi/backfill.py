"""Backfill settled KXBTC15M markets into `kalshi_markets` (U4; KTD7, KTD8).

The `settled` listing filter returns markets whose object status reads `finalized`.
Rows are upserted by ticker, so re-runs are idempotent and a market first stored while
open is updated once it settles. Pass the logged cursor back in to resume a long run.
"""

from __future__ import annotations

import logging
from typing import Any

from btc_signal_engine.adapters.sqlite.store import SqliteStore
from btc_signal_engine.adapters.kalshi.client import iso_to_ms
from btc_signal_engine.application.ports import KalshiGateway
from btc_signal_engine.domain.models.numbers import to_float

log = logging.getLogger(__name__)

def _float(v: Any) -> float | None:
    return to_float(v)


def market_row(m: dict[str, Any]) -> dict[str, Any]:
    """A Kalshi market object as a `kalshi_markets` row (timestamps in ms)."""
    return {
        "ticker": m["ticker"],
        "open_ts": iso_to_ms(m["open_time"]),
        "close_ts": iso_to_ms(m["close_time"]),
        "status": m["status"],
        "floor_strike": _float(m.get("floor_strike")),
        "expiration_value": _float(m.get("expiration_value")),
        "result": m.get("result") or None,
        "rules_primary": m.get("rules_primary"),
    }


async def backfill_kalshi(store: SqliteStore, gateway: KalshiGateway, *, status: str = "settled",
                          cursor: str | None = None, max_pages: int | None = None) -> int:
    """Page the listing from `cursor` (start when None) into `kalshi_markets`; returns markets stored."""
    stored = pages = 0
    while max_pages is None or pages < max_pages:
        markets, cursor = await gateway.markets(status=status, cursor=cursor)
        if markets:
            store.insert("kalshi_markets", [market_row(m) for m in markets], ignore_conflicts=False)
        stored += len(markets)
        pages += 1
        log.info("kalshi backfill: page %d, %d markets stored, cursor=%s", pages, stored, cursor)
        if not cursor or not markets:
            break
    return stored
