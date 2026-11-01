"""Test rig for real-order trading. NO network: every exchange is an httpx.MockTransport (or the shadow transport)
backed by an in-memory simulated Kalshi book. Payload shapes follow docs.kalshi.com/openapi.yaml (V2 orders)."""

from __future__ import annotations

import json
from datetime import datetime, UTC

import httpx
from cryptography.hazmat.primitives.asymmetric import ed25519

from btc_signal_engine.adapters.kalshi.shadow import ShadowExchange
from btc_signal_engine.adapters.kalshi.trading import KalshiTrading, TradingAuth
from btc_signal_engine.application.trading import TradingConfig, TradingService

W0 = 1793206800000  # 2026-10-28 17:00Z
CLOSE = W0 + 900_000
NOW = W0 + 300_000  # 10 minutes left
TICKER = "KXBTC15M-26OCT281315-15"
WRITES = ("POST", "DELETE")


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Market:
    """A controllable book for one market: YES bids and NO bids (YES ask = 1 - NO bid), like the real API."""

    def __init__(self, yes_bid=0.55, no_bid=0.42, size=100.0, ticker=TICKER, open_ts=W0, close_ts=CLOSE):
        self.ticker, self.open_ts, self.close_ts = ticker, open_ts, close_ts
        self.status, self.result = "active", None  # settle(...) sets the official result
        self.fail_reads = 0  # the next N /markets/{ticker} reads fail (HTTP 503 from the exchange)
        self.set(yes_bid, no_bid, size)

    def settle(self, result: str, status: str = "finalized") -> None:
        """The market's official result ("yes" | "no"), as GET /markets/{ticker} reports it once determined."""
        self.status, self.result = status, result

    def set(self, yes_bid, no_bid, size=100.0, depth=None):
        self.book = {"yes": depth[0] if depth else [(yes_bid, size)], "no": depth[1] if depth else [(no_bid, size)]}

    async def book_fn(self, ticker):
        return {k: list(v) for k, v in self.book.items()}

    async def market_fn(self, ticker):
        """GET /markets/{ticker} metadata. No prices: they come from the order book (book_fn)."""
        if self.fail_reads > 0:
            self.fail_reads -= 1
            raise httpx.ConnectError("market read failed")
        return {"ticker": ticker, "status": self.status, "open_time": iso(self.open_ts), "close_time": iso(self.close_ts),
                "result": self.result}


class Exchange:
    """MockTransport around the simulator; records every request (method, path, body)."""

    def __init__(self, market: Market, *, drop_first_post_response: bool = False):
        self.market = market
        self.sim = ShadowExchange(book_fn=market.book_fn, market_fn=market.market_fn, clock_ms=lambda: NOW)
        self.seen: list[dict] = []
        self._drop = drop_first_post_response

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append({"method": request.method, "path": request.url.path,
                          "body": json.loads(request.content) if request.content else None,
                          "query": request.url.query.decode()})
        resp = await self.sim.handle_async_request(request)
        if self._drop and request.method == "POST":
            self._drop = False
            raise httpx.ReadTimeout("response lost", request=request)  # the order WAS placed
        return resp

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def writes(self) -> list[dict]:
        return [r for r in self.seen if r["method"] in WRITES]

    def posts(self) -> list[dict]:
        return [r["body"] for r in self.seen if r["method"] == "POST"]


async def _no_sleep(_):
    return None


def gateway(ex: Exchange, env: str = "prod", max_order_cost: float = 25.0) -> KalshiTrading:
    return KalshiTrading(TradingAuth("test-key", ed25519.Ed25519PrivateKey.generate()), env=env,
                         transport=ex.transport(), max_order_cost=max_order_cost, sleep=_no_sleep)


class Ids:
    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return f"id{self.n:04d}"


STRATEGY = "you"  # the Kalshi Play tab's strategy


def service(store, ex: Exchange, *, mode: str = "prod", config: TradingConfig | None = None, paused=None, notify=None,
            env_gateways=None) -> TradingService:
    store.insert("trading_settings", [{"key": f"{STRATEGY}.mode", "value": {"mode": mode}, "updated_ts": 0}],
                 ignore_conflicts=False)
    env = {"sandbox": "demo", "shadow": "shadow", "prod": "prod"}[mode]
    gws = env_gateways if env_gateways is not None else {env: gateway(ex, env)}
    return TradingService(store, gws, config or TradingConfig(enabled=True), strategy=STRATEGY,
                          paused_fn=paused, notify_fn=notify, id_fn=Ids(), token_fn=Ids())


def proposal(**kw) -> dict:
    p = {"ticker": TICKER, "side": "UP", "stake": 5.0, "max_price": 0.58, "take_profit": 0.70, "stop": 0.43,
         "window_end": CLOSE, "setup_key": str(NOW - 1000), "fair_up": 0.66}
    p.update(kw)
    return p


async def ready(svc: TradingService, now: int = NOW) -> TradingService:
    await svc.reconcile({"sandbox": "demo", "shadow": "shadow", "prod": "prod"}[svc.mode], now)
    svc.heartbeat(now)
    return svc
