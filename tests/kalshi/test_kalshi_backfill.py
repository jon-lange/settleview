"""Kalshi settled-market backfill against a recorded page (no network)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from btc_signal_engine.adapters.kalshi.backfill import backfill_kalshi
from btc_signal_engine.adapters.kalshi.client import KalshiClient

FIXTURES = Path(__file__).parents[1] / "fixtures"


def _client(handler, sleeps: list[float] | None = None) -> KalshiClient:
    async def sleep(s: float) -> None:
        if sleeps is not None:
            sleeps.append(s)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=KalshiClient.BASE_URL)
    return KalshiClient(http, sleep=sleep)


def _paged(fail_first: int | None = None, headers: dict | None = None):
    page = json.loads((FIXTURES / "kalshi_settled_page_recorded.json").read_text())
    requests: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        requests.append(params)
        if fail_first and len(requests) == 1:
            return httpx.Response(fail_first, json={"error": "busy"}, headers=headers)
        if params.get("cursor"):
            return httpx.Response(200, json={"markets": [], "cursor": ""})
        return httpx.Response(200, json=page)

    return handler, requests


def test_settled_page_stores_strike_expiration_and_result_for_each_finalized_market(store):
    handler, requests = _paged()
    n = asyncio.run(backfill_kalshi(store, _client(handler)))

    assert n == 3
    assert requests[0] == {"series_ticker": "KXBTC15M", "status": "settled", "limit": "200"}
    rows = {r["ticker"]: r for r in store.select("kalshi_markets")}
    assert len(rows) == 3
    r = rows["KXBTC15M-26OCT281330-30"]
    assert r["status"] == "finalized"
    assert r["floor_strike"] == 80381.86
    assert r["expiration_value"] == 80465.75
    assert r["result"] == "yes"
    assert r["open_ts"] == 1793207700000 and r["close_ts"] == 1793208600000
    assert r["rules_primary"].startswith("If the simple average")
    assert rows["KXBTC15M-26OCT281315-15"]["result"] == "no"

    asyncio.run(backfill_kalshi(store, _client(handler)))  # idempotent re-run
    assert len(store.select("kalshi_markets")) == 3


def test_kalshi_429_and_5xx_back_off_and_retry_the_same_page(store, jitter_top):  # the full 1 s first backoff
    for status in (429, 503):
        handler, requests = _paged(fail_first=status)
        sleeps: list[float] = []
        asyncio.run(backfill_kalshi(store, _client(handler, sleeps)))
        assert requests[0] == requests[1]  # same page retried
        assert any(s >= 1.0 for s in sleeps)
        assert len(store.select("kalshi_markets")) == 3


def test_a_kalshi_429_waits_at_least_its_retry_after(store, jitter_bottom):  # no jitter on top: the venue's own
    handler, requests = _paged(fail_first=429, headers={"Retry-After": "7"})
    sleeps: list[float] = []
    asyncio.run(backfill_kalshi(store, _client(handler, sleeps)))
    assert requests[0] == requests[1] and 7.0 in sleeps
