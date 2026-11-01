"""Kalshi KXBTC15M quotes at decision time and published results (U8, KTD7, KTD8).

Market timestamps arrive as ISO strings and prices as dollar strings ("0.5100");
the gateway adapter converts timestamps to ms before they reach this module.

Live prices (U18, revised 2026-10-28): the /markets list (and /markets/{ticker}) were measured lagging the order book
by about 4 s, so the Coach saw asks that were already gone. The list is now used only to DISCOVER the current
window's market (ticker, strike, open/close); its price comes from the Kalshi WebSocket order book stream when that
quote is fresh, otherwise from the REST order book endpoint. Each recorded quote says which (`source`).
The same holds for each window's OPENING quote (`windows.yes_ask/no_ask`, "the price you'd pay"): the list
discovers the market, the book prices it. Settled results and history stay on the REST /markets endpoints.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from btc_signal_engine.application.ports import KalshiGateway, Repository
from btc_signal_engine.application.settlement import SettlementService
from btc_signal_engine.domain.models.numbers import to_float

QUOTE_TIMEOUT_MS = 30_000
LIVE_MAX_AGE_MS = 2_000  # a stream quote older than this is not used (the order book endpoint is asked instead)
DISCOVERY_MS = 15_000  # how often the market list is re-read for the current window's metadata


StreamQuote = Callable[[str, int], dict | None]


class KalshiSync:
    def __init__(self, gateway: KalshiGateway, repo: Repository, settlement: SettlementService) -> None:
        self._gw = gateway
        self._repo = repo
        self._settlement = settlement
        self._live_market: tuple[int, dict] | None = None  # (read at, current window's market from the list)
        self._recorded: dict[str, int] = {}  # ticker -> ts of the last recorded quote (at most one a second)

    async def _book_quote(self, ticker: str, now: int, stream_quote: StreamQuote | None) -> tuple[dict, str]:
        """(quote, source): the WebSocket book's quote when fresh, else the REST order book's (stamped `now`)."""
        q = stream_quote(ticker, now) if stream_quote else None
        if q is None or q.get("ts") is None or now - q["ts"] > LIVE_MAX_AGE_MS:
            return {**await self._gw.orderbook_quote(ticker), "ts": now}, "orderbook"
        return q, "ws"

    async def capture_quote(self, boundary: int, now: int, stream_quote: StreamQuote | None = None) -> bool:
        """Store the first two-sided book quote of the market opening at `boundary`. False = try again.
        The list only finds the market; its prices lag the book and are never used."""
        if now - boundary > QUOTE_TIMEOUT_MS:
            return True  # give up: EV for this window stays unavailable
        markets, _ = await self._gw.markets(status="open", limit=10)
        for m in markets:
            if m.get("open_ts") != boundary:
                continue
            q, _ = await self._book_quote(m["ticker"], now, stream_quote)
            yes, no = q.get("yes_ask"), q.get("no_ask")
            if yes is None or no is None:
                return False
            self._repo.update("windows", {"horizon": 900, "boundary": boundary}, {
                "kalshi_ticker": m["ticker"], "kalshi_strike": m.get("floor_strike"), "yes_ask": yes, "no_ask": no,
            })
            return True
        return False

    async def current_market(self, now: int) -> dict | None:
        """The market whose window contains `now` (discovery only: ticker, strike, open/close; never its prices)."""
        cached = self._live_market
        if cached and cached[1]["open_ts"] <= now < cached[1]["close_ts"] and now - cached[0] < DISCOVERY_MS:
            return cached[1]
        markets, _ = await self._gw.markets(status="open", limit=5)
        for m in markets:
            if m.get("open_ts", 0) <= now < m.get("close_ts", 0):
                self._live_market = (now, m)
                return m
        self._live_market = None
        return None

    async def poll_live(self, now: int, stream_quote: StreamQuote | None = None) -> dict | None:
        """Current window's live quote (U18): stored as a time series and returned for the 15m card.

        `stream_quote(ticker, now)` gives the WebSocket book's quote ({yes_bid, yes_ask, no_bid, no_ask, ts}) when
        it is fresh, else None; then the REST order book is read. `ts` is when the price was known to be current."""
        m = await self.current_market(now)
        if m is None:
            return None
        q, source = await self._book_quote(m["ticker"], now, stream_quote)
        # `last` (last trade price) is not recorded: the only source was the lagging list. Volume is informational.
        row = {"ticker": m["ticker"], "ts": int(q["ts"]), "yes_bid": q.get("yes_bid"), "yes_ask": q.get("yes_ask"),
               "no_bid": q.get("no_bid"), "no_ask": q.get("no_ask"), "last": None,
               "volume": to_float(m.get("volume_fp")), "source": source}
        if row["ts"] - self._recorded.get(m["ticker"], 0) >= 1000:
            self._repo.insert("kalshi_quotes", [row])
            self._recorded = {m["ticker"]: row["ts"]}
        return {**row, "strike": m.get("floor_strike"), "open_ts": m.get("open_ts"), "close_ts": m.get("close_ts")}

    async def sync_settled(self, now: int, limit: int = 20) -> int:
        """Record recently finalized markets (Play settles and scores from them) and override 15m windows. The store
        work (a `windows` read per market, every 30 s) runs in a worker thread, never on the event loop."""
        markets, _ = await self._gw.markets(status="settled", limit=limit)
        return await asyncio.to_thread(self._record_settled, markets, now)

    def _record_settled(self, markets: list[dict], now: int) -> int:
        applied = 0
        for m in markets:
            if m.get("result") not in ("yes", "no"):
                continue
            exp = m.get("expiration_value")
            self._repo.insert("kalshi_markets", [{
                "ticker": m["ticker"], "open_ts": m["open_ts"], "close_ts": m["close_ts"], "status": m.get("status", "finalized"),
                "floor_strike": m.get("floor_strike"), "expiration_value": to_float(exp),
                "result": m["result"], "rules_primary": m.get("rules_primary"),
            }], ignore_conflicts=False)
            if self._settlement.apply_kalshi_result(m["open_ts"], m["result"], m["ticker"], m.get("floor_strike"), now):
                applied += 1
        return applied
