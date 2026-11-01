"""The Coach's live Kalshi price: WebSocket order book first, REST order book as the fallback, never the /markets
list (measured lagging the book by ~4 s on 2026-10-28). Quotes older than 2 s are never used.

Gateway data below is SYNTHETIC except `rest_orderbook_one_sided.json`, a real public order book captured
2026-10-28 (read-only) for the client test.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from btc_signal_engine.adapters.kalshi.client import KalshiClient, book_top
from btc_signal_engine.application.kalshi_sync import KalshiSync

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "kalshi_ws"
NOW = 1_800_000_000_000 - 1_800_000_000_000 % 900_000 + 300_000
OPEN, CLOSE = NOW - 300_000, NOW + 600_000
TICKER = "SYNTHETIC-15M"


def run(coro):
    return asyncio.run(coro)


class Gateway:
    """The list says yes_ask 67c (stale); the order book says 76c (current)."""

    def __init__(self):
        self.calls: list[str] = []

    async def markets(self, *, status, cursor=None, limit=200):
        self.calls.append("list")
        return [{"ticker": TICKER, "open_ts": OPEN, "close_ts": CLOSE, "floor_strike": 80_000.0,
                 "yes_ask_dollars": "0.6700", "yes_bid_dollars": "0.6600", "no_ask_dollars": "0.3400",
                 "no_bid_dollars": "0.3300", "last_price_dollars": "0.6600", "volume_fp": "10.00"}], None

    async def market(self, ticker):
        raise AssertionError("prices must not come from /markets/{ticker}")

    async def orderbook_quote(self, ticker):
        self.calls.append("orderbook")
        return {"ticker": ticker, "yes_bid": 0.75, "yes_ask": 0.76, "no_bid": 0.24, "no_ask": 0.25}


class Settlement:
    def apply_kalshi_result(self, *a):
        return False


def test_stream_down_falls_back_to_the_order_book_not_the_list(store):
    async def go():
        gw = Gateway()
        sync = KalshiSync(gw, store, Settlement())
        q = await sync.poll_live(NOW, stream_quote=lambda t, now: None)  # stream down
        assert (q["yes_ask"], q["no_ask"], q["source"], q["ts"]) == (0.76, 0.25, "orderbook", NOW)
        assert gw.calls == ["list", "orderbook"]  # the list only discovered the market
        row = store.select("kalshi_quotes")[0]
        assert row["yes_ask"] == 0.76 and row["source"] == "orderbook"
        assert q["last"] is None and row["last"] is None  # the list's last trade price lags: not used
    run(go())


def test_fresh_stream_quote_is_used_and_a_stale_one_is_not(store):
    async def go():
        gw = Gateway()
        sync = KalshiSync(gw, store, Settlement())
        fresh = {"ticker": TICKER, "yes_bid": 0.77, "yes_ask": 0.78, "no_bid": 0.22, "no_ask": 0.23}
        q = await sync.poll_live(NOW, stream_quote=lambda t, now: {**fresh, "ts": now})
        assert (q["yes_ask"], q["source"]) == (0.78, "ws") and "orderbook" not in gw.calls
        q = await sync.poll_live(NOW + 1_000, stream_quote=lambda t, now: {**fresh, "ts": now - 2_500})
        assert (q["yes_ask"], q["source"]) == (0.76, "orderbook")
        assert gw.calls.count("list") == 1  # discovery is cached while the window lasts
        assert [r["source"] for r in store.select("kalshi_quotes", order_by=["ts"])] == ["ws", "orderbook"]
    run(go())


def test_client_reads_the_order_book_endpoint():
    payload = json.loads((FIX / "rest_orderbook_one_sided.json").read_text(encoding="utf-8"))
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json=payload)

    async def go():
        c = KalshiClient(httpx.AsyncClient(base_url=KalshiClient.BASE_URL, transport=httpx.MockTransport(handler)),
                         min_interval=0)
        q = await c.orderbook_quote("KXBTC15M-26OCT281730-30")
        await c.aclose()
        return q
    q = run(go())
    assert seen == ["/trade-api/v2/markets/KXBTC15M-26OCT281730-30/orderbook"]
    # captured late in a window: nobody bid YES, the best NO bid was 99.9c -> YES ask 0.1c
    assert q == {"ticker": "KXBTC15M-26OCT281730-30", "yes_bid": None, "no_bid": 0.999, "yes_ask": 0.001,
                 "no_ask": None}


def test_book_top_two_sided():
    q = book_top("T", {"yes_dollars": [["0.4100", "5.00"], ["0.4200", "1.00"]],
                       "no_dollars": [["0.5500", "3.00"], ["0.5700", "0.00"]]})
    assert q == {"ticker": "T", "yes_bid": 0.42, "no_bid": 0.55, "yes_ask": 0.45, "no_ask": 0.58}



# -- staleness from silence (R15) -----------------------------------------------------------------
# The order-book messages below are REAL (prod_orderbook_yes_price.jsonl, captured read-only 2026-10-28); the
# clock is a test clock so a silence can be simulated.
BOOK_TICKER = "KXBTC15M-26OCT281715-15"


class FakeWS:
    def __init__(self, msgs=()):
        self.q: asyncio.Queue = asyncio.Queue()
        for m in msgs:
            self.q.put_nowait(m if isinstance(m, str) else json.dumps(m))
        self.sent: list[dict] = []

    async def send(self, s):
        self.sent.append(json.loads(s))

    async def recv(self):
        return await self.q.get()

    async def close(self):
        pass


def book_head(private: bool) -> list[dict]:
    """The real subscribe replies (private channels only for a trading stream) and the order-book snapshot."""
    msgs = [json.loads(x) for x in (FIX / "prod_orderbook_yes_price.jsonl").read_text(encoding="utf-8").splitlines()
            if x.strip()]
    keep = ("fill", "user_orders", "orderbook_delta") if private else ("orderbook_delta",)
    return [m for m in msgs if (m["type"] == "subscribed" and m["msg"]["channel"] in keep)
            or m["type"] == "orderbook_snapshot"]


def make_stream(ws, clock, on_event, *, private=False, tickers=(BOOK_TICKER,), requote_ms=10**9):
    from cryptography.hazmat.primitives.asymmetric import ed25519

    from btc_signal_engine.adapters.kalshi.stream import KalshiStream, WsOnlyAuth

    async def connect(url, headers):
        return ws

    st = KalshiStream("prod", WsOnlyAuth("kid", ed25519.Ed25519PrivateKey.generate()), on_event, private=private,
                      connect=connect, clock_ms=lambda: clock[0], requote_ms=requote_ms)
    st.subscribe_tickers(tickers)
    return st


async def until(cond, timeout=3.0):
    for _ in range(int(timeout / 0.005)):
        if cond():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached")


async def stop(st, task):
    st.close()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


def test_a_silent_stream_with_a_subscribed_market_is_stale_after_12_s(store):
    from tests.trading.helpers import Exchange, Market, service

    async def go():
        clock, events = [NOW], []
        st = make_stream(FakeWS(book_head(private=True)), clock, events.append, private=True)
        svc = service(store, Exchange(Market()))
        svc.attach_stream("prod", st, NOW)
        st.subscribe_tickers({BOOK_TICKER})  # attach synced the service's (empty) market set
        task = asyncio.create_task(st.run())
        await until(lambda: any(e["type"] == "quote" for e in events))
        assert st.health()["last_msg_ms"] == NOW
        clock[0] = NOW + 11_000
        assert not st.health()["stale"] and svc.stream_ok("prod")
        clock[0] = NOW + 13_000
        assert st.health()["stale"] and not svc.stream_ok("prod")
        await stop(st, task)
    run(go())


def test_the_forced_requote_keeps_sending_the_book_but_not_the_silence_clock():
    async def go():
        clock, events = [NOW], []
        st = make_stream(FakeWS(book_head(private=False)), clock, events.append, requote_ms=1_000)
        task = asyncio.create_task(st.run())
        await until(lambda: any(e["type"] == "quote" for e in events))
        for k in range(1, 14):  # 13 s with no message from Kalshi; the requote fires every second
            n = len([e for e in events if e["type"] == "quote"])
            clock[0] = NOW + k * 1_000
            await until(lambda n=n: len([e for e in events if e["type"] == "quote"]) > n)
        assert st.health()["last_msg_ms"] == NOW and st.health()["stale"]
        await until(lambda: [e["stale"] for e in events if e["type"] == "health"][-1] is True)  # announced
        await stop(st, task)
    run(go())


def test_fresh_market_quote_carries_the_receive_time_and_a_silent_stream_falls_back_to_rest(tmp_path, store):
    from types import SimpleNamespace

    from btc_signal_engine.bootstrap.app import Runtime
    from btc_signal_engine.bootstrap.config import Settings

    async def go():
        rt = Runtime(Settings(_env_file=None, db_path=tmp_path / "rt.db", trading_stream=False))
        try:
            clock = [NOW]
            rt.clock = SimpleNamespace(now_ms=lambda: clock[0])
            st = make_stream(FakeWS(book_head(private=False)), clock, rt._on_market_event, requote_ms=1_000)
            rt.market_stream = st
            task = asyncio.create_task(st.run())
            await until(lambda: BOOK_TICKER in rt.market_quotes)
            q = rt.fresh_market_quote(BOOK_TICKER, NOW + 500)
            assert q is not None and q["ts"] == NOW  # when it arrived, not "now"
            clock[0] = NOW + 1_500  # one requote re-stores the same book; nothing new came from Kalshi
            await until(lambda: rt.market_quotes[BOOK_TICKER][1] == NOW + 1_500)
            assert rt.fresh_market_quote(BOOK_TICKER, NOW + 1_500)["ts"] == NOW
            for k in range(2, 14):  # requoted every second; still nothing from Kalshi
                clock[0] = NOW + k * 1_000 + 500
                await until(lambda: rt.market_quotes[BOOK_TICKER][1] == clock[0])
            assert rt.fresh_market_quote(BOOK_TICKER, clock[0]) is None  # silent 13.5 s: stale
            gw = Gateway()
            sync = KalshiSync(gw, store, Settlement())
            live = await sync.poll_live(clock[0], stream_quote=rt.fresh_market_quote)
            assert live["source"] == "orderbook" and "orderbook" in gw.calls
            await stop(st, task)
        finally:
            rt.writer.close()
    run(go())


def test_an_idle_private_stream_with_no_markets_stays_fresh_for_an_hour():
    async def go():
        clock, events = [NOW], []
        st = make_stream(FakeWS(book_head(private=True)[:2]), clock, events.append, private=True, tickers=())
        task = asyncio.create_task(st.run())
        await until(lambda: st.health()["connected"])
        clock[0] = NOW + 3_600_000
        assert st.health() == {"connected": True, "last_msg_ms": NOW, "stale": False}
        await stop(st, task)
    run(go())
