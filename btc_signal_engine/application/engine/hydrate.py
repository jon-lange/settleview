"""Warm the engine's feature state from stored history before live boundaries start (KTD14)."""

from __future__ import annotations

import heapq

from btc_signal_engine.application.ports import MarketStore

HOUR_MS = 3_600_000
LOOKBACK_MS = 15 * HOUR_MS  # 14h of 1m candles for the 15m EMA/pattern features, plus margin


def ordered_events(market: MarketStore, start: int, end: int, chunk_ms: int = HOUR_MS):
    """Trades and book seconds in [start, end), merged by (ts, kind) — the replay order (KTD4)."""
    for lo in range(start, end, chunk_ms):
        hi = min(lo + chunk_ms, end)
        trades = ((t.ts, 0, t.trade_id, t) for t in market.trades_range(lo, hi))
        books = ((b.ts, 1, 0, b) for b in market.book_range(lo, hi))
        for *_key, ev in heapq.merge(trades, books, key=lambda x: x[:3]):
            yield ev


def hydrate(engine, market: MarketStore, now: int, lookback_ms: int = LOOKBACK_MS) -> int:
    count = 0
    for ev in ordered_events(market, now - lookback_ms, now):
        engine.on_event(ev)
        count += 1
    return count
