"""The retry / reconnect policy on tenacity (adapters/backoff.py; review fix plan 2026-10-31 B3#10): full jitter, a
429's Retry-After as the floor, a reconnect loop that never gives up and starts over, and the venue clients that use it
(same body on every Kalshi re-send). tests/test_venue_retries.py pins every call site's exact waits. No network."""

from __future__ import annotations

import asyncio
import email.utils
import sys
from decimal import Decimal

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from tenacity import RetryCallState

from btc_signal_engine.adapters.backoff import (RETRY_AFTER_MAX_S, header, jittered, rate_limit_pause, reconnecting,
                                                retry_after_s, start_over, wait_at, wait_venue)
from btc_signal_engine.adapters.kalshi.margin import MarginAuth, MarginClient
from btc_signal_engine.adapters.kalshi.trading import KalshiTrading, TradingAuth
from tests.kalshi.margin_fake import MarginFake


def run(coro):
    return asyncio.run(coro)


def state_at(attempt_number: int) -> RetryCallState:
    state = RetryCallState(retry_object=None, fn=None, args=(), kwargs={})
    state.attempt_number = attempt_number
    return state


def answered(resp, attempt_number=1) -> RetryCallState:
    state = state_at(attempt_number)
    state.set_result(resp)
    return state


def failed(exc, attempt_number=1) -> RetryCallState:
    state = state_at(attempt_number)
    try:
        raise exc
    except type(exc):
        state.set_exception(sys.exc_info())
    return state


def test_jittered_ceilings_double_to_the_cap(jitter_top):
    w = jittered(1.0, 30.0)
    assert [wait_at(w, n) for n in range(1, 9)] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]


def test_jittered_draws_stay_inside_their_window():
    w = jittered(1.0, 30.0)
    for n in range(1, 10):
        ceiling = min(30.0, 2 ** (n - 1))
        assert all(0.0 <= wait_at(w, n) <= ceiling for _ in range(50))


def test_two_reconnect_loops_do_not_wait_in_lockstep():
    w = jittered(1.0, 30.0)
    waits = [(wait_at(w, n), wait_at(w, n)) for n in range(1, 7)]
    assert any(x != y for x, y in waits)  # the default draw is random: lockstep reconnects are what B3#10 removed


def test_retry_after_reads_seconds_and_http_dates_and_ignores_garbage():
    assert retry_after_s("3") == 3.0 and retry_after_s(" 1.5 ") == 1.5 and retry_after_s("0") == 0.0
    when = email.utils.formatdate(1_000_010, usegmt=True)
    assert retry_after_s(when, now_s=lambda: 1_000_000) == 10.0
    assert retry_after_s(email.utils.formatdate(999_000, usegmt=True), now_s=lambda: 1_000_000) == 0.0  # past
    for bad in (None, "", "soon", "nan", "inf"):
        assert retry_after_s(bad) is None
    assert retry_after_s("-4") == 0.0


def test_a_429_waits_at_least_its_retry_after_capped(jitter_bottom):
    w = wait_venue()
    assert w(answered(httpx.Response(429, headers={"Retry-After": "3"}))) == 3.0
    assert w(answered(httpx.Response(429, headers={"Retry-After": "600"}))) == RETRY_AFTER_MAX_S
    assert rate_limit_pause(4.0) == 4.0 and rate_limit_pause(None) == 1.0 and rate_limit_pause(99) == RETRY_AFTER_MAX_S


def test_the_request_schedules_at_the_top_of_their_windows(jitter_top):
    w = wait_venue()
    assert w(answered(httpx.Response(429, headers={"Retry-After": "3"}))) == 4.0  # jitter only on top (1 s)
    assert w(answered(httpx.Response(429))) == 1.0  # no header: 1 s ceiling first, longer than a 5xx
    assert w(answered(httpx.Response(429), 5)) == 10.0  # capped at 10 s
    assert w(answered(httpx.Response(503))) == 0.2  # a 5xx or a transport error: 0.2 s
    assert w(failed(httpx.ConnectError("x"), 2)) == 0.4
    assert w(failed(httpx.ConnectError("x"), 9)) == 5.0  # capped at 5 s
    assert wait_venue(1.0, 30.0)(answered(httpx.Response(503), 3)) == 4.0  # a slow reader's schedule
    assert rate_limit_pause(4.0) == 5.0 and rate_limit_pause(None) == 2.0


def test_header_lookup_is_case_insensitive_on_any_mapping():
    assert header({"retry-after": "2"}) == "2"
    assert header(httpx.Headers({"Retry-After": "5"})) == "5"
    assert header(None) is None and header({}) is None


def test_a_reconnect_loop_never_gives_up_starts_over_and_never_retries_a_cancellation(jitter_top):
    sleeps: list[float] = []
    runs = 0

    async def sleep(s):
        sleeps.append(s)

    async def go():
        nonlocal runs
        closed = lambda: runs >= 9  # noqa: E731 - reads the live count
        async for attempt in reconnecting(jittered(1.0, 30.0), closed=closed, sleep=sleep):
            with attempt:
                runs += 1
                if runs == 5:
                    start_over(attempt)  # this connection lasted
                if runs != 4:  # the fourth ends normally: it is reconnected all the same
                    raise ConnectionError("dropped")
    run(go())
    assert runs == 9 and sleeps == [1.0, 2.0, 4.0, 8.0, 1.0, 2.0, 4.0, 8.0]

    async def cancelled():
        async for attempt in reconnecting(jittered(1.0, 30.0), closed=lambda: False, sleep=sleep):
            with attempt:
                raise asyncio.CancelledError
    sleeps.clear()
    with pytest.raises(asyncio.CancelledError):
        run(cancelled())
    assert sleeps == []


def test_kalshi_resends_the_same_order_after_a_429_and_waits_its_retry_after(jitter_bottom):
    """Kalshi's rule (AGENTS.md): our retry loop is the only one and a re-send carries the same client_order_id."""
    bodies: list[bytes] = []
    slept: list[float] = []

    def handler(r: httpx.Request) -> httpx.Response:
        bodies.append(r.content)
        if len(bodies) == 1:
            return httpx.Response(429, json={"error": "slow down"}, headers={"Retry-After": "4"})
        return httpx.Response(201, json={"order_id": "o1", "client_order_id": "c1", "fill_count": "1.00",
                                         "remaining_count": "0.00", "average_fill_price": "0.5000",
                                         "average_fee_paid": "0.0200"})

    async def sleep(s: float) -> None:
        slept.append(s)
    gw = KalshiTrading(TradingAuth("k", ed25519.Ed25519PrivateKey.generate()), env="demo",
                       transport=httpx.MockTransport(handler), sleep=sleep)
    got = run(gw.place_limit(ticker="KXBTC15M-26NOV011200-00", side="yes", action="buy", count=1, price=0.5,
                             client_order_id="c1"))
    assert got["order_id"] == "o1"
    assert len(bodies) == 2 and bodies[0] == bodies[1] and b'"client_order_id":"c1"' in bodies[0].replace(b" ", b"")
    assert slept == [4.0]


def test_the_perps_client_honours_retry_after_and_resends_the_same_body(jitter_bottom):
    fake = MarginFake()
    fake.queue("POST", "/margin/orders", 429, {"code": "too_many_requests"}, headers={"Retry-After": "2"})
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)
    c = MarginClient(MarginAuth("test-key", ed25519.Ed25519PrivateKey.generate(), env="perp_demo"), env="perp_demo",
                     subaccount=7, max_notional=Decimal(500), transport=fake.transport(), sleep=sleep)

    async def go():
        try:
            return await c.create_order(ticker="KXBTCPERP1", side="bid", count=Decimal("3"), price=Decimal("7.8034"),
                                        client_order_id="5f0c2a8e-0f4e-4c39-9f57-3b1f6f2b8a10",
                                        time_in_force="immediate_or_cancel", reduce_only=False, purpose="entry")
        finally:
            await c.aclose()
    got = run(go())
    posts = fake.requests("POST", "/margin/orders")
    assert got["order_id"] == "ord-1" and len(posts) == 2 and posts[0]["body"] == posts[1]["body"]
    assert slept == [2.0]
