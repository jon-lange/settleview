"""SHADOW mode for Polymarket US: simulated fills on Polymarket's LIVE book (markets WebSocket), no keys, no money.

The simulated exchange is the same `ShadowExchange` the Kalshi shadow uses (same fill model: marketable orders walk
the real book level by level within their limit; resting orders fill from public trade prints with queue position
and latency, or by the book trading through them when the print feed cannot vouch for completeness), fed by
`PolymarketMarketData`: `book_fn` = the live book in YES-bid / NO-bid form (a NO bid at 1 - p for each YES offer at
p), `trades_fn` = the WebSocket prints, `market_fn` = the window from the slug. Taker fees are Polymarket's
(`polymarket_fee`: 0.0695 x C x P x (1 - P), rounded to the cent); makers pay 0. Every event it emits is stamped
env "pm_shadow". A book that is not servable (market_data.py's freshness rule) raises, so the shadow quote is refused like
production's: nothing is ever priced from the cached REST BBO.

The client in front of it is the Kalshi-shaped `KalshiTrading` over the in-memory transport with the Polymarket series
guard: the simulation exercises the application's order flow (gates, brackets, index stop, reconciliation) on the
real Polymarket book, NOT the Polymarket wire format (`PolymarketTrading` is covered by its own fake-SDK tests).

Settlement: with `markets_fn` (the SDK's public `markets.retrieve_by_slug`) the simulated /markets/{slug} answers the
real market's resolution in Kalshi's shape (resolved -> status "finalized", result yes | no), so a pm_shadow bracket
held to the close settles on Polymarket's own result and the simulated account is paid like the venue's.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from btc_signal_engine.adapters.kalshi.shadow import ShadowExchange, shadow_trading
from btc_signal_engine.adapters.kalshi.trading import KalshiTrading
from btc_signal_engine.adapters.polymarket_us.market_data import PolymarketMarketData
from btc_signal_engine.adapters.polymarket_us.markets import FAMILY, result_reader
from btc_signal_engine.domain.settlement.fees import polymarket_fee

SHADOW_ENV = "pm_shadow"


def polymarket_shadow(cache: PolymarketMarketData, *, max_order_cost: float,
                      clock_ms: Callable[[], int] = lambda: int(time.time() * 1000), place_latency_ms: int = 150,
                      cancel_latency_ms: int = 150, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                      maker_model: bool = True,
                      markets_fn: Callable[[str], Awaitable[dict]] | None = None) -> tuple[ShadowExchange, KalshiTrading]:
    """(simulated exchange, gateway) for the "pm_shadow" environment. `markets_fn`: see "Settlement" above (without
    it the simulated market never resolves and a held bracket stays pending settlement)."""
    read = result_reader(markets_fn) if markets_fn is not None else None

    async def result_fn(slug: str) -> dict | None:
        got = await read(slug) if read is not None else None
        return {"status": "finalized", "result": got["result"]} if got and got.get("final") else None

    ex = ShadowExchange(book_fn=cache.book, market_fn=cache.market, result_fn=result_fn if read else None,
                        trades_fn=cache.trades_since if maker_model else None, clock_ms=clock_ms,
                        place_latency_ms=place_latency_ms, cancel_latency_ms=cancel_latency_ms,
                        sleep=sleep, env=SHADOW_ENV, taker_fee_fn=lambda n, p: polymarket_fee(n, p))
    return ex, shadow_trading(max_order_cost=max_order_cost, exchange=ex, series=FAMILY)  # every span; each service checks its own
