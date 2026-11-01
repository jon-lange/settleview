"""Kalshi's public trade prints from the production market-data WebSocket, kept for Play's "crowd, last minute" signal.

Fed by the market stream (`KalshiStream(private=False, trades=True)`): pass `TradePrints.on_event` as (or chain it
into) that stream's callback. `recent_trades(ticker)` = the prints of the last `keep_ms`, oldest first. Descriptive
only: it does not vouch that no print was missed (domain/strategy/crowd.py says what the summary means).

Each print is stamped `ts_ms` = the LOCAL clock when it arrived (the app's clock, the one the crowd summary's `now`
comes from) and keeps Kalshi's matching-engine time as `exchange_ts_ms`. Block trades (matched off the book) are
dropped.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable


class TradePrints:
    def __init__(self, *, clock_ms: Callable[[], int] = lambda: int(time.time() * 1000), keep_ms: int = 120_000) -> None:
        self._clock = clock_ms
        self._keep = keep_ms
        self._trades: dict[str, deque] = {}

    def on_event(self, ev: dict) -> None:
        if ev.get("type") != "trade":
            return
        t = ev.get("ticker")
        if not t or ev.get("is_block_trade") or ev.get("yes_price") is None or not ev.get("count"):
            return
        now = self._clock()
        q = self._trades.setdefault(t, deque())
        q.append({"trade_id": ev.get("trade_id"), "ticker": t, "yes_price": float(ev["yes_price"]),
                  "count": float(ev["count"]), "taker_book_side": ev.get("taker_book_side"), "ts_ms": now,
                  "exchange_ts_ms": ev.get("ts_ms")})
        for k in list(self._trades):  # drop old prints (and markets with none left)
            d = self._trades[k]
            while d and d[0]["ts_ms"] < now - self._keep:
                d.popleft()
            if not d:
                del self._trades[k]

    def recent_trades(self, ticker: str) -> list[dict]:
        """The kept prints for `ticker` (last `keep_ms`), oldest first, as copies."""
        return [dict(x) for x in self._trades.get(ticker, ())]
