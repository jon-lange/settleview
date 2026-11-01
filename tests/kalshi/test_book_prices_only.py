"""Live prices come only from the order book (WebSocket, or GET /markets/{ticker}/orderbook as the fallback).

GET /markets (list) was measured lagging the book by a median 15 s and GET /markets/{ticker} by up to 11 s
(production, 2026-10-28), so neither is ever a price source: the list only discovers markets, and /markets/{ticker}
only gives metadata (status, open/close), cached per ticker. Gateway data below is SYNTHETIC.
"""

from __future__ import annotations

import asyncio

import httpx
from cryptography.hazmat.primitives.asymmetric import ed25519

from btc_signal_engine.adapters.kalshi.shadow import ShadowExchange
from btc_signal_engine.adapters.kalshi.trading import KalshiTrading, TradingAuth
from btc_signal_engine.application.kalshi_sync import KalshiSync
from btc_signal_engine.application.settlement import SettlementService
from tests.trading.helpers import CLOSE, TICKER, W0, Market, iso

STALE = {"yes_bid_dollars": "0.6600", "yes_ask_dollars": "0.6700", "no_bid_dollars": "0.3300",
         "no_ask_dollars": "0.3400", "last_price_dollars": "0.6600"}  # what the lagging endpoints say
BOOK = {"orderbook_fp": {"yes_dollars": [["0.7400", "10"], ["0.7500", "5"]], "no_dollars": [["0.2400", "8"]]}}


def run(coro):
    return asyncio.run(coro)


class Host:
    """A demo/prod trading host: /markets/{t} carries metadata plus stale prices; the book is current."""

    def __init__(self, status="active"):
        self.paths: list[str] = []
        self.status = status

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        if path.endswith("/orderbook"):
            return httpx.Response(200, json=BOOK)
        return httpx.Response(200, json={"market": {"ticker": TICKER, "status": self.status, "open_time": iso(W0),
                                                    "close_time": iso(CLOSE), **STALE}})


def trading(host: Host, env: str, clock: list[float]) -> KalshiTrading:
    return KalshiTrading(TradingAuth("k", ed25519.Ed25519PrivateKey.generate()), env=env,
                         transport=httpx.MockTransport(host.handler), clock_s=lambda: clock[0])


def test_trading_quote_prices_come_from_the_order_book_in_demo_and_prod():
    for env, base in (("demo", "/trade-api/v2"), ("prod", "/trade-api/v2")):
        host, clock = Host(), [100.0]
        gw = trading(host, env, clock)
        q = run(gw.quote(TICKER))
        assert (q["yes_bid"], q["yes_ask"], q["no_bid"], q["no_ask"]) == (0.75, 0.76, 0.24, 0.25)
        assert (q["ticker"], q["status"], q["open_ts"], q["close_ts"]) == (TICKER, "active", W0, CLOSE)
        assert f"{base}/markets/{TICKER}/orderbook" in host.paths
        assert gw.base_url.startswith("https://external-api.demo" if env == "demo" else "https://external-api.kalshi")


def test_trading_quote_caches_metadata_and_rereads_status_after_30s():
    host, clock = Host(), [100.0]
    gw = trading(host, "prod", clock)
    meta = lambda: [p for p in host.paths if not p.endswith("/orderbook")]  # noqa: E731
    book = lambda: [p for p in host.paths if p.endswith("/orderbook")]  # noqa: E731
    run(gw.quote(TICKER))
    clock[0] += 20
    run(gw.quote(TICKER))
    assert (len(meta()), len(book())) == (1, 2)  # metadata cached, the book read every time
    host.status = "closed"
    clock[0] += 15  # 35 s since the metadata read
    q = run(gw.quote(TICKER))
    assert (len(meta()), len(book())) == (2, 3) and q["status"] == "closed"


def test_trading_auth_allows_the_order_book_read():
    auth = TradingAuth("k", ed25519.Ed25519PrivateKey.generate())
    assert "KALSHI-ACCESS-SIGNATURE" in auth.headers("GET", f"/trade-api/v2/markets/{TICKER}/orderbook")


def test_shadow_serves_the_real_book_and_metadata_without_prices():
    m = Market(yes_bid=0.55, no_bid=0.42)

    async def stale_market(ticker):
        return {**await m.market_fn(ticker), **STALE}

    async def go():
        ex = ShadowExchange(book_fn=m.book_fn, market_fn=stale_market)
        c = httpx.AsyncClient(transport=ex, base_url="https://x/trade-api/v2")
        meta = (await c.get(f"/markets/{TICKER}")).json()["market"]
        book = (await c.get(f"/markets/{TICKER}/orderbook")).json()
        await c.aclose()
        return meta, book
    meta, book = run(go())
    assert not any(k.endswith("_dollars") or k in ("yes_bid", "yes_ask", "no_bid", "no_ask") for k in meta)
    assert meta["status"] == "active" and meta["close_time"] == iso(CLOSE)
    assert book == {"orderbook_fp": {"yes_dollars": [["0.5500", "100.0000"]], "no_dollars": [["0.4200", "100.0000"]]}}


# -- the window's opening price (KalshiSync.capture_quote) --------------------------------------------
B = 1_800_000_000_000 - 1_800_000_000_000 % 900_000


class Gateway:
    def __init__(self, book=None):
        self.calls: list[str] = []
        self.book = book or {"yes_bid": 0.47, "yes_ask": 0.51, "no_bid": 0.49, "no_ask": 0.53}

    async def markets(self, *, status, cursor=None, limit=200):
        self.calls.append("list")
        return [{"ticker": "T", "open_ts": B, "close_ts": B + 900_000, "floor_strike": 100.0,
                 "yes_ask_dollars": "0.6700", "no_ask_dollars": "0.3400"}], None  # stale list prices

    async def market(self, ticker):
        raise AssertionError("prices must not come from /markets/{ticker}")

    async def orderbook_quote(self, ticker):
        self.calls.append("orderbook")
        return {"ticker": ticker, **self.book}


def test_window_opening_price_comes_from_the_order_book_not_the_list(store):
    svc = SettlementService(store, store)
    svc.open_window(900, B, 100.0, now=B)
    gw = Gateway()
    assert run(KalshiSync(gw, store, svc).capture_quote(B, now=B + 2_000))
    w = store.select("windows")[0]
    assert (w["yes_ask"], w["no_ask"], w["kalshi_ticker"], w["kalshi_strike"]) == (0.51, 0.53, "T", 100.0)
    assert gw.calls == ["list", "orderbook"]


def test_window_opening_price_prefers_a_fresh_stream_quote(store):
    svc = SettlementService(store, store)
    svc.open_window(900, B, 100.0, now=B)
    gw = Gateway()
    fresh = {"ticker": "T", "yes_bid": 0.48, "yes_ask": 0.52, "no_bid": 0.48, "no_ask": 0.52}
    assert run(KalshiSync(gw, store, svc).capture_quote(B, B + 2_000, lambda t, now: {**fresh, "ts": now}))
    assert (store.select("windows")[0]["yes_ask"], gw.calls) == (0.52, ["list"])


def test_window_opening_price_waits_for_a_two_sided_book(store):
    svc = SettlementService(store, store)
    svc.open_window(900, B, 100.0, now=B)
    gw = Gateway(book={"yes_bid": None, "yes_ask": 0.51, "no_bid": 0.49, "no_ask": None})
    sync = KalshiSync(gw, store, svc)
    assert run(sync.capture_quote(B, now=B + 2_000)) is False
    assert run(sync.capture_quote(B, now=B + 40_000)) is True  # gave up
    assert store.select("windows")[0]["yes_ask"] is None
