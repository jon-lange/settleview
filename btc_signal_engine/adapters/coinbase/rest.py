"""Coinbase Exchange public REST client (httpx): historical trades and candles.

Implements the `TradeHistory` port. Requests are spaced under the public rate limit,
and HTTP 429/5xx responses are retried with exponential back-off on the same page.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from btc_signal_engine.adapters.retry import header, retry_after_s, retry_delay
from btc_signal_engine.domain.models.events import Trade

log = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def iso_to_ms(s: str) -> int:
    """ISO-8601 UTC timestamp to integer milliseconds (sub-millisecond digits truncated)."""
    return (datetime.fromisoformat(s.replace("Z", "+00:00")) - _EPOCH) // timedelta(milliseconds=1)


def ms_to_iso(ms: int) -> str:
    return (_EPOCH + timedelta(milliseconds=ms)).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_trade(row: dict[str, Any]) -> Trade:
    """A REST trade. `side` is the maker side, so the aggressor bought when it is "sell".

    REST trades are historical: `ingest_ts = ts` and `backfilled=True` (KTD5).
    """
    ts = iso_to_ms(row["time"])
    return Trade(trade_id=int(row["trade_id"]), ts=ts, ingest_ts=ts, price=float(row["price"]),
                 size=float(row["size"]), buy=row["side"] == "sell", backfilled=True)


async def get_with_retry(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, Any],
    *,
    sleep: Callable[[float], Awaitable[None]],
    max_attempts: int = 8,
) -> httpx.Response:
    """GET, retrying the same request on 429/5xx: full jitter over 1s, 2s, 4s ... (max 30s), and at least a 429's
    Retry-After (adapters/retry.py)."""
    for attempt in range(max_attempts):
        resp = await client.get(path, params=params)
        if resp.status_code != 429 and resp.status_code < 500:
            break
        delay = retry_delay(attempt, status=resp.status_code, base_s=1.0, cap_s=30.0,
                            retry_after=retry_after_s(header(resp.headers)))
        log.warning("GET %s -> HTTP %s; retrying in %.1fs", path, resp.status_code, delay)
        await sleep(delay)
    resp.raise_for_status()
    return resp


class CoinbaseRest:
    BASE_URL = "https://api.exchange.coinbase.com"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        product: str = "BTC-USD",
        min_interval: float = 0.2,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client or httpx.AsyncClient(base_url=self.BASE_URL, timeout=20.0)
        self._product = product
        self._min_interval = min_interval  # 5 req/s, half the public limit
        self._sleep = sleep
        self._last = 0.0

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        wait = self._last + self._min_interval - time.monotonic()
        if wait > 0:
            await self._sleep(wait)
        self._last = time.monotonic()
        resp = await get_with_retry(self._client, f"/products/{self._product}{path}", params, sleep=self._sleep)
        return resp.json()

    async def trades_before(self, trade_id: int | None, limit: int = 1000) -> list[Trade]:
        """Trades with id < `trade_id` (latest when None), newest first."""
        params: dict[str, Any] = {"limit": limit}
        if trade_id is not None:
            params["after"] = trade_id
        return [parse_trade(r) for r in await self._get("/trades", params)]

    async def candles(self, start_ms: int, end_ms: int, granularity: int = 60) -> list[list[float]]:
        """Raw candle rows `[time_s, low, high, open, close, volume]` in [start, end] (max 300)."""
        params = {"granularity": granularity, "start": ms_to_iso(start_ms), "end": ms_to_iso(end_ms)}
        return await self._get("/candles", params)

    async def aclose(self) -> None:
        await self._client.aclose()
