"""Polymarket US live trading feed (the application's `TradingStream` for env "pm_prod"): WebSocket first, REST
polling stays the backup (TradingService re-polls on its own cadence and on every `resync`).

* Private WebSocket (official SDK `client.ws.private()`): SUBSCRIPTION_TYPE_ORDER (snapshot + execution updates) ->
  `order` events (cumulative fills, so a repeat is a no-op in the service) and `fill` events; SUBSCRIPTION_TYPE_POSITION
  -> the gateway's `stream_positions` (display; the local position book is driven by order fills); SUBSCRIPTION_TYPE_ACCOUNT_BALANCE -> the gateway's balance (auto sessions sized by
  balance). Order ids map back to the app's client_order_ids through the gateway.
* Quotes: every market-data update (markets WebSocket, `PolymarketMarketData`) for a watched market becomes a `quote`
  event (at most every `quote_every_ms` per market unless the price changed). The markets the service watches are
  passed on to the market stream so they stay subscribed.
* Health, with hysteresis (docs/solutions/2026-10-31-pm-quiet-book-vs-dead-book-feed.md: a quiet book flipped the
  feed ~600 times in 4 h under the old "a message within 2 s" rule). `connected` = the private socket is up AND every
  market the app needs now is subscribed on a connection that is up (the market data's HARD problems): losing either
  takes the feed down at once. A SOFT problem (an open window's book not servable: the market data's rule, e.g. a
  silent connection or prints arriving without book updates) takes it down only once it has lasted DOWN_AFTER_MS
  without a break. Once down, it comes back up only after UP_AFTER_MS with no problem at all (the first up after
  start is immediate). A servable quiet book is no problem, so it never toggles the feed. While down, the service
  polls on its fast cadence (TradingService). Entries never depend on this state: every ticket prices from the
  market data's own servable check. Any change emits a `health` event; every (re)connect emits `resync` so the
  service polls once for what it missed. Reconnects with exponential backoff (1 s doubling to 30 s).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from typing import Any

from btc_signal_engine.adapters.polymarket_us.market_data import ws_error_is_fatal, PolymarketMarketData, PolymarketMarketStream
from btc_signal_engine.adapters.polymarket_us.markets import is_series, num
from btc_signal_engine.adapters.polymarket_us.trading import FILL_TYPES, PolymarketTrading, normalize_order

log = logging.getLogger(__name__)

PRIVATE_SUBS = ("SUBSCRIPTION_TYPE_ORDER", "SUBSCRIPTION_TYPE_POSITION", "SUBSCRIPTION_TYPE_ACCOUNT_BALANCE")
DOWN_AFTER_MS = 10_000  # a soft problem (a book not servable) must last this long to take the feed down
UP_AFTER_MS = 5_000  # after a down, the feed must be problem-free this long to come back up


class PolymarketTradingStream:
    def __init__(self, ws_factory: Callable[[], Any], gateway: PolymarketTrading, market_data: PolymarketMarketData,
                 on_event: Callable[[dict], None], *, market_stream: PolymarketMarketStream | None = None,
                 env: str = "pm_prod", clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
                 sleep: Callable[[float], Any] = asyncio.sleep, quote_every_ms: int = 250,
                 backoff_s: tuple[float, float] = (1.0, 30.0)) -> None:
        self.env = env
        self._factory = ws_factory
        self._gw = gateway
        self._md = market_data
        self._ms = market_stream
        self._cb = on_event
        self._clock = clock_ms
        self._sleep = sleep
        self._quote_every = quote_every_ms
        self._backoff = backoff_s
        self._want: set[str] = set()
        self._last_quote: dict[str, tuple[tuple, int]] = {}
        self._connected = False
        self._closed = False
        self._last_msg_ms: int | None = None
        self._last_health: tuple = (False, True)
        self._down = True  # the latched state health() reports as `stale` (hysteresis, module docstring)
        self._ever_up = False
        self._bad_since: int | None = None  # first check of the current run of problems
        self._ok_since: int | None = None  # first check of the current run without any
        self._why = "starting"
        self._md.listeners.append(self._on_market)

    def __repr__(self) -> str:
        return f"PolymarketTradingStream(env={self.env!r})"

    # -- TradingStream ------------------------------------------------------------------------------
    def subscribe_tickers(self, tickers: Iterable[str]) -> None:
        self._want = {t for t in tickers if is_series(t)}
        if self._ms is not None:
            self._ms.want(self.env, self._want)

    def health(self) -> dict:
        """{connected, last_msg_ms, stale (latched with hysteresis), reason (why it is down, or the soft problem
        being waited out)}; see the module docstring."""
        md = self._md.health()
        now = self._clock()
        connected = self._connected and md["connected"]
        problems = list(md.get("problems") or [])
        bad = (not connected) or md["stale"]
        if bad:
            self._ok_since = None
            self._bad_since = now if self._bad_since is None else self._bad_since
        else:
            self._bad_since = None
            self._ok_since = now if self._ok_since is None else self._ok_since
        if not self._down:
            if not connected or (bad and now - self._bad_since >= DOWN_AFTER_MS):
                self._down = True
                self._why = ("private WebSocket down" if not self._connected
                             else "; ".join(md.get("hard") or problems) or "market data down")
        elif not bad and (not self._ever_up or now - self._ok_since >= UP_AFTER_MS):
            self._down, self._ever_up, self._why = False, True, ""
        last = max([x for x in (self._last_msg_ms, md["last_msg_ms"]) if x is not None], default=None)
        reason = self._why if self._down else ("; ".join(problems)[:300] if bad else "")
        return {"connected": connected, "last_msg_ms": last, "stale": self._down, "reason": reason[:300]}

    def close(self) -> None:
        self._closed = True
        if self._on_market in self._md.listeners:
            self._md.listeners.remove(self._on_market)

    # -- events ---------------------------------------------------------------------------------------
    def _emit(self, ev: dict) -> None:
        try:
            self._cb({**ev, "env": self.env})
        except Exception:
            log.warning("polymarket stream consumer failed", exc_info=True)

    def _health_changed(self) -> None:
        h = self.health()
        if (h["connected"], h["stale"]) != self._last_health:
            self._last_health = (h["connected"], h["stale"])
            self._emit({"type": "health", **h})

    def _on_market(self, slug: str) -> None:
        self._health_changed()
        if slug not in self._want:
            return
        q = self._md.quote(slug)
        if not q:
            return
        now = self._clock()
        key = (q["yes_bid"], q["yes_ask"])
        last = self._last_quote.get(slug)
        if last is not None and last[0] == key and now - last[1] < self._quote_every:
            return
        self._last_quote[slug] = (key, now)
        self._emit({"type": "quote", **{k: q[k] for k in ("ticker", "yes_bid", "yes_ask", "no_bid", "no_ask")},
                    "ts_ms": q["ts_ms"]})

    def on_message(self, msg: dict) -> None:
        """One decoded private-WebSocket message."""
        self._last_msg_ms = self._clock()
        if not isinstance(msg, dict):
            return
        snap = msg.get("orderSubscriptionSnapshot") or msg.get("ordersSnapshot")
        if isinstance(snap, dict):
            for o in snap.get("orders") or []:
                self._order(o)
        upd = msg.get("orderSubscriptionUpdate") or msg.get("orderUpdate")
        if isinstance(upd, dict) and isinstance(upd.get("execution"), dict):
            ex = upd["execution"]
            o = ex.get("order") or {}
            self._order(o)
            if ex.get("type") in FILL_TYPES and is_series(o.get("marketSlug")):
                self._gw.observe_trade_id(ex.get("tradeId"))
                self._emit({"type": "fill", "order_id": o.get("id"), "client_order_id": self._gw.coid_for(o.get("id")),
                            "ticker": o.get("marketSlug"), "trade_id": ex.get("tradeId") or ex.get("id"),
                            "count": num(ex.get("lastShares")) or 0.0, "yes_price": num(ex.get("lastPx")),
                            "fee": num(ex.get("commissionNotionalCollected")), "is_taker": ex.get("aggressor"),
                            "ts_ms": self._clock()})
        pos = msg.get("positionSubscription") or msg.get("positionSubscriptionUpdate") or msg.get("positionUpdate")
        if isinstance(pos, dict):
            after = pos.get("afterPosition") or {}
            slug = (after.get("marketMetadata") or {}).get("slug") or (
                (pos.get("beforePosition") or {}).get("marketMetadata") or {}).get("slug")
            if slug:
                self._gw.on_position(slug, num(after.get("netPosition")) or 0.0)
        for key in ("accountBalancesSnapshot", "accountBalanceSubscriptionSnapshot"):
            bal = msg.get(key)
            if isinstance(bal, dict) and bal.get("balances"):
                self._gw.on_balance(num(bal["balances"][0].get("currentBalance")))
        upd_b = msg.get("accountBalancesUpdate") or msg.get("accountBalanceSubscriptionUpdate")
        if isinstance(upd_b, dict):
            after = ((upd_b.get("balanceChange") or {}).get("afterBalance")) or {}
            self._gw.on_balance(num(after.get("currentBalance")))

    def _order(self, o: dict) -> None:
        if not isinstance(o, dict) or not is_series(o.get("marketSlug")) or not o.get("id"):
            return
        self._gw.observe_order(o)  # authoritative cumulative fills -> the gateway's local position book
        self._emit({"type": "order", "order": normalize_order(o, None, self._gw.coid_for(o["id"])),
                    "ts_ms": self._clock()})

    # -- connection -----------------------------------------------------------------------------------
    async def run(self) -> None:
        delay = self._backoff[0]
        while not self._closed:
            started = self._clock()
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("polymarket private websocket stopped (%s); reconnecting in %.0f s",
                            type(exc).__name__, delay)
            self._connected = False
            self._health_changed()
            if self._closed:
                break
            if self._clock() - started > 60_000:
                delay = self._backoff[0]
            await self._sleep(delay)
            delay = min(delay * 2, self._backoff[1])

    async def run_once(self) -> None:
        ws = self._factory()
        failed: list[str] = []
        ws.on("message", self.on_message)
        def on_error(e=None) -> None:
            if e is not None and not ws_error_is_fatal(e):
                log.warning("polymarket private websocket server message: %s", str(e)[:200])
                return
            failed.append(type(e).__name__ if e is not None else "error")

        ws.on("error", on_error)
        ws.on("close", lambda *a: failed.append("closed"))
        await ws.connect()
        for i, typ in enumerate(PRIVATE_SUBS):
            await ws.subscribe(f"pmpriv{i}", typ)
        self._connected = True
        self._emit({"type": "resync", "reason": "connected"})
        self._health_changed()
        try:
            while not self._closed:
                if failed:
                    raise ConnectionError(failed[-1])
                self._health_changed()
                await self._sleep(0.5)
        finally:
            self._connected = False
            try:
                await ws.close()
            except Exception:
                log.debug("polymarket private websocket close failed", exc_info=True)
