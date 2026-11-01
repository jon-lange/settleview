"""The one retry / reconnect policy (adapters/retry.py, review fix plan 2026-10-31 B3#10): full jitter, a 429's
Retry-After as the floor, and the venue clients that use it (same body on every Kalshi re-send). No network."""

from __future__ import annotations

import asyncio
import email.utils
from decimal import Decimal

import httpx
from cryptography.hazmat.primitives.asymmetric import ed25519

from btc_signal_engine.adapters import retry
from btc_signal_engine.adapters.kalshi.margin import MarginAuth, MarginClient
from btc_signal_engine.adapters.kalshi.trading import KalshiTrading, TradingAuth
from btc_signal_engine.adapters.retry import (RETRY_AFTER_MAX_S, Backoff, full_jitter, header, retry_after_s,
                                              retry_delay)
from tests.kalshi.margin_fake import MarginFake


def run(coro):
    return asyncio.run(coro)


def test_full_jitter_draws_from_zero_to_the_exponential_ceiling():
    assert full_jitter(0, 1.0, 30.0, lambda: 0.0) == 0.0
    assert full_jitter(0, 1.0, 30.0, lambda: 0.999) == 0.999
    assert full_jitter(3, 1.0, 30.0, lambda: 0.5) == 4.0  # half of 8 s
    assert full_jitter(10, 1.0, 30.0, lambda: 1.0) == 30.0  # capped


def test_backoff_doubles_its_ceiling_to_the_cap_and_resets():
    b = Backoff(1.0, 30.0, rng=lambda: 1.0)  # the top of each draw: the ceilings themselves
    assert [b.next() for _ in range(8)] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]
    b.reset()
    assert b.next() == 1.0 and b.ceiling == 2.0


def test_two_reconnect_loops_do_not_wait_in_lockstep():
    a, b = Backoff(1.0, 30.0), Backoff(1.0, 30.0)
    waits = [(a.next(), b.next()) for _ in range(6)]
    assert any(x != y for x, y in waits)  # the default draw is random: lockstep reconnects are what B3#10 removed


def test_retry_after_reads_seconds_and_http_dates_and_ignores_garbage():
    assert retry_after_s("3") == 3.0 and retry_after_s(" 1.5 ") == 1.5 and retry_after_s("0") == 0.0
    when = email.utils.formatdate(1_000_010, usegmt=True)
    assert retry_after_s(when, now_s=lambda: 1_000_000) == 10.0
    assert retry_after_s(email.utils.formatdate(999_000, usegmt=True), now_s=lambda: 1_000_000) == 0.0  # past
    for bad in (None, "", "soon", "nan", "inf"):
        assert retry_after_s(bad) is None
    assert retry_after_s("-4") == 0.0


def test_a_429_waits_at_least_its_retry_after_capped_and_otherwise_backs_off_longer_than_a_5xx():
    assert retry_delay(0, status=429, retry_after=3.0, rng=lambda: 0.0) == 3.0
    assert retry_delay(0, status=429, retry_after=3.0, rng=lambda: 0.5) == 3.5  # jitter only on top
    assert retry_delay(0, status=429, retry_after=600.0, rng=lambda: 0.0) == RETRY_AFTER_MAX_S
    assert retry_delay(0, status=429, rng=lambda: 1.0) == 1.0  # no header: 1 s ceiling first
    assert retry_delay(0, status=503, rng=lambda: 1.0) == 0.2  # a 5xx or a transport error: 0.2 s
    assert retry_delay(1, rng=lambda: 1.0) == 0.4
    assert retry_delay(2, status=503, base_s=1.0, cap_s=30.0, rng=lambda: 1.0) == 4.0  # a slow reader's schedule


def test_header_lookup_is_case_insensitive_on_any_mapping():
    assert header({"retry-after": "2"}) == "2"
    assert header(httpx.Headers({"Retry-After": "5"})) == "5"
    assert header(None) is None and header({}) is None


def test_kalshi_resends_the_same_order_after_a_429_and_waits_its_retry_after(monkeypatch):
    """Kalshi's rule (AGENTS.md): our retry loop is the only one and a re-send carries the same client_order_id."""
    monkeypatch.setattr(retry, "_uniform", lambda: 0.0)
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


def test_the_perps_client_honours_retry_after_and_resends_the_same_body(monkeypatch):
    monkeypatch.setattr(retry, "_uniform", lambda: 0.0)
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
