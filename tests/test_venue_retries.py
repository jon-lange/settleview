"""Characterization of every venue retry and reconnect path's waits, written before the move from the hand-rolled
helper to tenacity (2026-11-01) and kept as the regression net after it.

The process-wide `random` is seeded, and the expected waits are computed from a second generator on the same seed:
each wait is one uniform draw scaled to the full-jitter window min(cap, base * 2**n) (AWS "Exponential Backoff And
Jitter"), and a 429 with a Retry-After waits min(Retry-After, 10 s) plus a draw scaled to the 429 base. So these
tests pin the exact waits, the same body on every Kalshi re-send, when a loop gives up and what it raises, and that a
reconnect loop starts its backoff over after a connection that lasted. No network.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from decimal import Decimal

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from btc_signal_engine.adapters.kalshi.client import KalshiClient
from btc_signal_engine.adapters.kalshi.margin import MarginAuth, MarginClient, MarginUnknown
from btc_signal_engine.adapters.kalshi.stream import KalshiStream
from btc_signal_engine.adapters.kalshi.trading import KalshiTrading, OrderRejected, TradingAuth
from btc_signal_engine.bootstrap.app import startup_read
from tests.kalshi.margin_fake import MarginFake

SEED = 20261101
TICKER = "KXBTC15M-26NOV011200-00"
FILLED = {"order_id": "o1", "client_order_id": "c1", "fill_count": "1.00", "remaining_count": "0.00",
          "average_fill_price": "0.5000", "average_fee_paid": "0.0200"}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def draws():
    """Seed the process's `random`; return a generator on the same seed (the draws the waits must be built from)."""
    state = random.getstate()
    random.seed(SEED)
    yield random.Random(SEED)
    random.setstate(state)


def jitter(r: random.Random, n: int, base: float, cap: float) -> float:
    return r.random() * min(cap, base * 2 ** n)


_YIELD = asyncio.sleep  # the real one, kept before any test replaces asyncio.sleep


class Sleeps(list):
    async def __call__(self, s: float) -> None:
        self.append(float(s))
        await _YIELD(0)  # a reconnect loop must not starve the test's own polling


# -- Kalshi orders: our loop is the only one, the same body every time ----------------------------------------------
def kalshi(handler, sleep):
    return KalshiTrading(TradingAuth("k", ed25519.Ed25519PrivateKey.generate()), env="demo",
                         transport=httpx.MockTransport(handler), sleep=sleep)


def place(gw):
    return gw.place_limit(ticker=TICKER, side="yes", action="buy", count=1, price=0.5, client_order_id="c1")


def scripted(answers, bodies):
    def handler(r: httpx.Request) -> httpx.Response:
        bodies.append(r.content)
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    return handler


def test_kalshi_order_waits_after_a_transport_error_and_a_429_with_retry_after(draws):
    bodies, sleeps = [], Sleeps()
    answers = [httpx.ConnectError("reset"), httpx.Response(429, headers={"Retry-After": "3"}),
               httpx.Response(201, json=FILLED)]
    got = run(place(kalshi(scripted(answers, bodies), sleeps)))
    assert got["order_id"] == "o1"
    assert len(bodies) == 3 and len(set(bodies)) == 1 and b'"client_order_id":"c1"' in bodies[0].replace(b" ", b"")
    assert sleeps == [jitter(draws, 0, 0.2, 5.0), 3.0 + draws.random() * 1.0]


def test_kalshi_order_waits_after_a_5xx_and_a_429_without_retry_after(draws):
    bodies, sleeps = [], Sleeps()
    answers = [httpx.Response(503), httpx.Response(429), httpx.Response(201, json=FILLED)]
    run(place(kalshi(scripted(answers, bodies), sleeps)))
    assert len(set(bodies)) == 1 and len(bodies) == 3
    assert sleeps == [jitter(draws, 0, 0.2, 5.0), jitter(draws, 1, 1.0, 10.0)]


def test_kalshi_order_caps_a_long_retry_after(draws):
    bodies, sleeps = [], Sleeps()
    answers = [httpx.Response(429, headers={"Retry-After": "600"}), httpx.Response(201, json=FILLED)]
    run(place(kalshi(scripted(answers, bodies), sleeps)))
    assert sleeps == [10.0 + draws.random() * 1.0]


def test_kalshi_order_gives_up_after_three_sends_raising_the_last_answer(draws):
    bodies, sleeps = [], Sleeps()
    answers = [httpx.Response(503), httpx.Response(502), httpx.Response(500)]
    with pytest.raises(httpx.HTTPStatusError) as e:
        run(place(kalshi(scripted(answers, bodies), sleeps)))
    assert e.value.response.status_code == 500
    assert len(bodies) == 3 and len(set(bodies)) == 1
    assert sleeps == [jitter(draws, 0, 0.2, 5.0), jitter(draws, 1, 0.2, 5.0)]  # no wait after the last send


def test_kalshi_order_gives_up_after_three_transport_errors_raising_the_last(draws):
    bodies, sleeps = [], Sleeps()
    answers = [httpx.ConnectError("a"), httpx.ReadTimeout("b"), httpx.ConnectError("c")]
    with pytest.raises(httpx.ConnectError, match="c"):
        run(place(kalshi(scripted(answers, bodies), sleeps)))
    assert len(bodies) == 3 and len(set(bodies)) == 1
    assert sleeps == [jitter(draws, 0, 0.2, 5.0), jitter(draws, 1, 0.2, 5.0)]


def test_kalshi_order_4xx_is_answered_at_once(draws):
    bodies, sleeps = [], Sleeps()
    answers = [httpx.Response(400, json={"error": "bad"})]
    with pytest.raises(OrderRejected):
        run(place(kalshi(scripted(answers, bodies), sleeps)))
    assert len(bodies) == 1 and sleeps == []


# -- Kalshi perps: the same rule on the margin client --------------------------------------------------------------
def margin(fake, sleep):
    return MarginClient(MarginAuth("test-key", ed25519.Ed25519PrivateKey.generate(), env="perp_demo"),
                        env="perp_demo", subaccount=7, max_notional=Decimal(500), transport=fake.transport(),
                        sleep=sleep)


def create(c):
    async def go():
        try:
            return await c.create_order(ticker="KXBTCPERP1", side="bid", count=Decimal("3"), price=Decimal("7.8034"),
                                        client_order_id="5f0c2a8e-0f4e-4c39-9f57-3b1f6f2b8a10",
                                        time_in_force="immediate_or_cancel", reduce_only=False, purpose="entry")
        finally:
            await c.aclose()
    return go()


def test_margin_order_waits_after_a_5xx_and_a_429_with_retry_after(draws):
    fake, sleeps = MarginFake(), Sleeps()
    fake.queue("POST", "/margin/orders", 500, {"code": "internal"})
    fake.queue("POST", "/margin/orders", 429, {"code": "too_many_requests"}, headers={"Retry-After": "2"})
    got = run(create(margin(fake, sleeps)))
    posts = fake.requests("POST", "/margin/orders")
    assert got["order_id"] == "ord-1" and len(posts) == 3 and len({json.dumps(p["body"]) for p in posts}) == 1
    assert sleeps == [jitter(draws, 0, 0.2, 5.0), 2.0 + draws.random() * 1.0]


def test_margin_order_after_a_lost_reply_resends_the_same_body(draws):
    fake, sleeps = MarginFake(lose_first_post_reply=True), Sleeps()
    got = run(create(margin(fake, sleeps)))
    posts = fake.requests("POST", "/margin/orders")
    assert len(posts) == 2 and posts[0]["body"] == posts[1]["body"] and got["order_id"]
    assert sleeps == [jitter(draws, 0, 0.2, 5.0)]


def test_margin_order_gives_up_after_three_sends_as_unknown(draws):
    fake, sleeps = MarginFake(), Sleeps()
    for _ in range(3):
        fake.queue("POST", "/margin/orders", 503, {"code": "unavailable"})
    with pytest.raises(MarginUnknown) as e:
        run(create(margin(fake, sleeps)))
    assert e.value.status == 503 and len(fake.requests("POST", "/margin/orders")) == 3
    # two backoffs between the three sends; then create_order's own lookups for the unknown outcome (not retries)
    assert sleeps[:2] == [jitter(draws, 0, 0.2, 5.0), jitter(draws, 1, 0.2, 5.0)]
    assert sleeps[2:] == [0.5, 1.0, 1.5, 2.0]


def test_margin_redirect_is_unknown_at_once(draws):
    fake, sleeps = MarginFake(), Sleeps()
    fake.queue("POST", "/margin/orders", 302, {})
    with pytest.raises(MarginUnknown) as e:
        run(create(margin(fake, sleeps)))
    assert e.value.status == 302 and len(fake.requests("POST", "/margin/orders")) == 1
    assert sleeps == [0.5, 1.0, 1.5, 2.0]  # no backoff: only create_order's lookups for the unknown outcome


# -- safe reads: re-sent while the venue is busy ------------------------------------------------------------------
def reads(answers, sleeps, attempts=8):
    seen: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(str(r.url))
        return answers.pop(0)
    http = httpx.AsyncClient(base_url="https://kalshi.test", transport=httpx.MockTransport(handler))
    return KalshiClient(http, min_interval=0.0, max_attempts=attempts, sleep=sleeps), seen


def test_a_busy_read_is_resent_with_its_retry_after_then_jittered_waits(draws):
    sleeps = Sleeps()
    c, seen = reads([httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(500),
                     httpx.Response(200, json={"markets": [], "cursor": ""})], sleeps)
    assert run(c.markets(status="open")) == ([], None)
    assert len(seen) == 3 and len(set(seen)) == 1
    assert sleeps == [2.0 + draws.random() * 1.0, jitter(draws, 1, 1.0, 30.0)]


def test_a_read_still_busy_after_its_attempts_raises_its_status(draws):
    sleeps = Sleeps()
    c, seen = reads([httpx.Response(503)] * 3, sleeps, attempts=3)
    with pytest.raises(httpx.HTTPStatusError) as e:
        run(c.markets(status="open"))
    assert e.value.response.status_code == 503 and len(seen) == 3
    # tenacity gives up after the last send (the hand-rolled loop also slept once more before raising)
    assert sleeps == [jitter(draws, 0, 1.0, 30.0), jitter(draws, 1, 1.0, 30.0)]


def test_a_4xx_read_is_not_resent(draws):
    sleeps = Sleeps()
    c, seen = reads([httpx.Response(404)], sleeps)
    with pytest.raises(httpx.HTTPStatusError):
        run(c.markets(status="open"))
    assert len(seen) == 1 and sleeps == []


# -- Polymarket: a 429 pauses every caller; nothing is re-sent ---------------------------------------------------
def test_a_polymarket_429_pauses_the_limiter_for_its_retry_after_plus_jitter(draws):
    from btc_signal_engine.adapters.polymarket_us.trading import PolymarketTrading
    from tests.polymarket_us.fakes import RateLimitError, client

    pauses: list[float] = []

    class Limiter:
        async def acquire(self):
            pass

        def backoff(self, s):
            pauses.append(s)

    gw = PolymarketTrading(client(), quote_fn=lambda s: None)
    gw._limiter = Limiter()
    calls = []

    def limited(headers):
        resp = httpx.Response(429, headers=headers, request=httpx.Request("POST", "https://api.polymarket.us/v1/x"))

        async def fn():
            calls.append(1)
            raise RateLimitError("slow", response=resp, body={})
        return fn
    for headers in ({"Retry-After": "4"}, {}, {"Retry-After": "120"}):
        with pytest.raises(RateLimitError):
            run(gw._call(limited(headers)))
    assert len(calls) == 3  # never re-sent
    assert pauses == [4.0 + draws.random(), 1.0 + draws.random(), 10.0 + draws.random()]


# -- startup reads ------------------------------------------------------------------------------------------------
def test_startup_read_waits_full_jitter_doubling_then_raises(draws, monkeypatch, caplog):
    from sqlalchemy.exc import OperationalError

    from btc_signal_engine.bootstrap import app as app_mod
    sleeps = Sleeps()
    monkeypatch.setattr(app_mod.asyncio, "sleep", sleeps)
    calls = []

    def read():
        calls.append(1)
        raise OperationalError("SELECT 1", {}, Exception("canceling statement due to statement timeout"))
    with pytest.raises(OperationalError):
        run(startup_read("the probe", read, attempts=4, backoff_s=2.0))
    assert len(calls) == 4
    assert sleeps == [jitter(draws, n, 2.0, 16.0) for n in range(3)]


# -- a reconnect supervisor: doubling, capped, and starting over after a connection that lasted -------------------
class WS:
    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, s):
        self.sent.append(json.loads(s))

    async def recv(self):
        m = await self.q.get()
        if m is None:
            raise ConnectionError("closed by peer")
        return m

    async def close(self):
        self.closed = True


def test_kalshi_stream_reconnects_with_full_jitter_and_starts_over_after_a_long_session(draws):
    clock = [1_793_570_400_000]
    lasting = WS()
    script: list = ["fail", "fail", lasting, "fail", "fail"]
    sleeps = Sleeps()
    connected = asyncio.Event

    async def go():
        up = connected()

        async def connect(url, headers):
            if not script:
                await asyncio.Event().wait()  # no more connects: the loop parks here
            got = script.pop(0)
            if got == "fail":
                raise ConnectionError("refused")
            up.set()
            return got
        s = KalshiStream("prod", TradingAuth("kid", ed25519.Ed25519PrivateKey.generate()), lambda e: None,
                         connect=connect, clock_ms=lambda: clock[0], requote_ms=10**9, sleep=sleeps)
        task = asyncio.create_task(s.run())
        await asyncio.wait_for(up.wait(), 3)
        clock[0] += 61_000  # it stayed up past a minute
        lasting.q.put_nowait(None)
        for _ in range(600):
            if len(sleeps) >= 5:
                break
            await asyncio.sleep(0.005)
        s.close()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    run(go())
    want = [jitter(draws, 0, 1.0, 30.0), jitter(draws, 1, 1.0, 30.0)]
    want += [jitter(draws, 0, 1.0, 30.0), jitter(draws, 1, 1.0, 30.0), jitter(draws, 2, 1.0, 30.0)]  # started over
    assert sleeps == want


def test_kalshi_stream_logs_each_drop(draws, caplog):
    caplog.set_level(logging.WARNING, logger="btc_signal_engine")
    sleeps = Sleeps()

    async def go():
        async def connect(url, headers):
            raise ConnectionError("refused")
        s = KalshiStream("prod", TradingAuth("kid", ed25519.Ed25519PrivateKey.generate()), lambda e: None,
                         connect=connect, clock_ms=lambda: 0, requote_ms=10**9, sleep=sleeps)
        task = asyncio.create_task(s.run())
        for _ in range(600):
            if len(sleeps) >= 2:
                break
            await asyncio.sleep(0.005)
        s.close()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    run(go())
    assert any("kalshi prod stream dropped (ConnectionError)" in r.getMessage() for r in caplog.records)
