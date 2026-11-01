"""Read-only Kalshi portfolio client (U19), implementing `PortfolioGateway`.

Only GET /portfolio/fills, /portfolio/positions and /portfolio/balance are implemented
(https://docs.kalshi.com/api-reference/portfolio/). There is no request method other than
`_get`, and the signing auth refuses non-GET requests, so this client cannot place or
cancel orders. Payloads are returned as documented; fills gain `ts_ms` from `created_time`.

Requests go through Kalshi's official SDK (adapters/kalshi/backend.py; retries off, signed by `KalshiAuth`, which
refuses anything but GET); a test's httpx `transport` keeps the original httpx requests.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from btc_signal_engine.adapters.kalshi.auth import KalshiAuth, load_private_key
from btc_signal_engine.adapters.kalshi.backend import Backend, HttpxBackend, SdkBackend
from btc_signal_engine.adapters.kalshi.client import iso_to_ms
from btc_signal_engine.adapters.retry import header, retry_after_s, retry_delay

log = logging.getLogger(__name__)


class KalshiPortfolio:
    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

    def __init__(
        self,
        auth: KalshiAuth,
        *,
        base_url: str = BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        min_interval: float = 1.0,
        max_attempts: int = 5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._backend: Backend = (
            HttpxBackend(httpx.AsyncClient(base_url=base_url, auth=auth, transport=transport, timeout=20.0))
            if transport is not None else SdkBackend(base_url, signer=auth, timeout=20.0))
        self._min_interval = min_interval
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._last = 0.0

    async def _get(self, op: str, **params: Any) -> Any:
        for attempt in range(self._max_attempts):
            wait = self._last + self._min_interval - time.monotonic()
            if wait > 0:
                await self._sleep(wait)
            self._last = time.monotonic()
            resp = await getattr(self._backend, op)(**params)
            if resp.status_code != 429 and resp.status_code < 500:
                break
            delay = retry_delay(attempt, status=resp.status_code, base_s=1.0, cap_s=30.0,  # full jitter
                                retry_after=retry_after_s(header(getattr(resp, "headers", None))))
            log.warning("%s -> HTTP %s; retrying in %.1fs", op, resp.status_code, delay)
            await self._sleep(delay)
        resp.raise_for_status()
        return resp.json()

    async def fills(self, *, min_ts: int | None = None, cursor: str | None = None,
                    limit: int = 200) -> tuple[list[dict], str | None]:
        """One page of fills. `min_ts` is a Unix timestamp in seconds."""
        params: dict[str, Any] = {}
        if min_ts is not None:
            params["min_ts"] = int(min_ts)  # may arrive as a stored REAL; Kalshi wants whole seconds
        if cursor:
            params["cursor"] = cursor
        params["limit"] = limit
        data = await self._get("get_fills", **params)
        fills = data.get("fills", [])
        for f in fills:
            if f.get("created_time"):
                f["ts_ms"] = iso_to_ms(f["created_time"])
        return fills, data.get("cursor") or None

    async def positions(self, *, cursor: str | None = None, limit: int = 200) -> tuple[list[dict], str | None]:
        """One page of open (unsettled) market positions."""
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        data = await self._get("get_positions", **params)
        return data.get("market_positions", []), data.get("cursor") or None

    async def balance(self) -> dict:
        return await self._get("get_balance")

    async def aclose(self) -> None:
        await self._backend.aclose()


def portfolio_from_settings(key_id: str | None, private_key_path: str | Path | None, *,
                            base_url: str = KalshiPortfolio.BASE_URL) -> KalshiPortfolio | None:
    """The account link, or None (not linked) unless both the key id and key path are configured."""
    if not key_id or not private_key_path:
        return None
    return KalshiPortfolio(KalshiAuth(key_id, load_private_key(private_key_path)), base_url=base_url)
