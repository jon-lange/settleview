"""Kalshi public market-data client, implementing the `KalshiGateway` port.

No auth is needed for market reads. Requests go through Kalshi's official SDK (adapters/kalshi/backend.py, unsigned,
its retries off); a caller's httpx client (tests) keeps the original httpx requests. Requests are spaced under the
basic read limit and HTTP 429/5xx responses are retried here with exponential back-off on the same request.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from btc_signal_engine.adapters.kalshi.backend import Backend, HttpxBackend, SdkBackend
from btc_signal_engine.adapters.backoff import resend_while_busy
from btc_signal_engine.domain.markets import market
from btc_signal_engine.domain.timestamps import iso_to_ms

log = logging.getLogger(__name__)

__all__ = ["iso_to_ms"]  # re-exported for the research scripts that read it from here (domain/timestamps.py)


def book_top(ticker: str, ob: dict) -> dict:
    """orderbook_fp {"yes_dollars": [[price, size], ...] (YES bids), "no_dollars": [...] (NO bids)} -> top of book.
    A YES ask is 1 - the best NO bid, and a NO ask is 1 - the best YES bid."""
    def best(key: str) -> float | None:
        px = [float(p) for p, s in (ob.get(key) or []) if float(s) > 0]
        return max(px) if px else None

    def ok(x: float | None) -> float | None:
        return round(x, 4) if x is not None and 0.0 < x < 1.0 else None
    yb, nb = best("yes_dollars"), best("no_dollars")
    return {"ticker": ticker, "yes_bid": ok(yb), "no_bid": ok(nb), "yes_ask": ok(None if nb is None else 1 - nb),
            "no_ask": ok(None if yb is None else 1 - yb)}


def _normalize(m: dict) -> dict:
    """Add the ms timestamps the KalshiGateway port promises (`open_ts`, `close_ts`)."""
    if m.get("open_time"):
        m["open_ts"] = iso_to_ms(m["open_time"])
    if m.get("close_time"):
        m["close_ts"] = iso_to_ms(m["close_time"])
    return m


class KalshiClient:
    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        series_ticker: str = market("kalshi_15m").series,
        min_interval: float = 0.1,
        max_attempts: int = 8,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._backend: Backend = (HttpxBackend(client) if client is not None
                                  else SdkBackend(self.BASE_URL, signer=None, timeout=20.0))
        self._series = series_ticker
        self._min_interval = min_interval
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._last = 0.0

    async def _get(self, op: str, **params: Any) -> Any:
        async def send() -> Any:
            wait = self._last + self._min_interval - time.monotonic()
            if wait > 0:
                await self._sleep(wait)
            self._last = time.monotonic()
            return await getattr(self._backend, op)(**params)

        resp = await resend_while_busy(send, sleep=self._sleep, what=op, attempts=self._max_attempts, logger=log)
        resp.raise_for_status()
        return resp.json()

    async def markets(self, *, status: str, cursor: str | None = None, limit: int = 200) -> tuple[list[dict], str | None]:
        """One page of the series' markets. `status` is a filter value (`open`, `settled`, ...)."""
        params: dict[str, Any] = {"series_ticker": self._series, "status": status, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        data = await self._get("get_markets", **params)
        return [_normalize(m) for m in data.get("markets", [])], data.get("cursor") or None

    async def market(self, ticker: str) -> dict:
        return _normalize((await self._get("get_market", ticker=ticker))["market"])

    async def orderbook_quote(self, ticker: str) -> dict:
        """Top of the market's live order book (GET /markets/{ticker}/orderbook): {ticker, yes_bid, yes_ask, no_bid,
        no_ask}. Used for live prices instead of the /markets list and /markets/{ticker}, which were measured
        lagging the book by several seconds (2026-10-28)."""
        return book_top(ticker, (await self._get("get_orderbook", ticker=ticker)).get("orderbook_fp") or {})

    async def aclose(self) -> None:
        await self._backend.aclose()
