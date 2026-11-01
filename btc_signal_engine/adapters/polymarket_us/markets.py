"""Polymarket US BTC Up/Down markets (5-minute, 15-minute and 1-hour windows): slugs, windows and settlement terms.

Slugs: `cpc-btc-updown-{5m|15m|1h}-YYYY-MM-DD-HHMMz` (window START in UTC); the window is [start, start + span).
Every span settles the same way; only 15m lines up with Kalshi KXBTC15M (same strike and fixing).
Terms (`GET /v1/market/slug/{slug}`, public gateway, via the SDK's `markets.retrieve_by_slug`): `assetPriceTerms`
{priceToBeat (strike, CF Benchmarks BRTI 60 s average at the open), settlementPrice (same at the close)}. Result:
Up ("yes") when settlementPrice >= priceToBeat (flat = Up), identical to Kalshi KXBTC15M since 2026-09-24
(docs/research/polymarket-us.md: 3,293/3,293 windows).
Resolution (`resolution`): a resolved market reads status MARKET_STATUS_RESOLVED with `outcomePrices` ["1","0"] = Up
(YES) won, ["0","1"] = Down (NO) won (a JSON string in the API; the research backfill saw it match settlementPrice >=
priceToBeat on every stored row). Anything else is not resolved yet.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from btc_signal_engine.domain.markets import by_venue_span, market, tradable_spans, venue_markets

log = logging.getLogger(__name__)

FAMILY = "cpc-btc-updown"  # every span's slug starts with FAMILY + "-" (each span's series is in domain/markets.py)
SPANS_MS = {m.span: m.window_ms for m in venue_markets("polymarket")}  # every span the table knows (1h: never traded)
LIVE_SPANS = tradable_spans("polymarket")  # spans the gateway trades and the market stream follows (current + next)
SERIES = market("polymarket_15m").series
WINDOW_MS = SPANS_MS["15m"]
_SPAN_OF = {m.series: m.span for m in venue_markets("polymarket")}  # slug prefix -> span
_SLUG = re.compile(rf"^({'|'.join(map(re.escape, _SPAN_OF))})-(\d{{4}})-(\d{{2}})-(\d{{2}})-(\d{{2}})(\d{{2}})z$")


def series_for(span: str) -> str:
    """The slug prefix of one span's markets (e.g. "cpc-btc-updown-5m"); an unknown span raises (UnknownMarket)."""
    return by_venue_span("polymarket", span).series


def span_of(slug: str) -> str | None:
    m = _SLUG.match(slug or "")
    return _SPAN_OF[m.group(1)] if m else None


def num(x: Any) -> float | None:
    """A Polymarket Amount ({"value": "0.55", ...}) or plain number/string -> float (None when absent/invalid)."""
    if isinstance(x, dict):
        x = x.get("value")
    try:
        return None if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return None


def slug_for(start_ms: int, span: str = "15m") -> str:
    """The `span` market whose window contains `start_ms` (floored to the window)."""
    w = SPANS_MS[span]
    start = start_ms - start_ms % w
    return f"{series_for(span)}-" + dt.datetime.fromtimestamp(start / 1000, dt.UTC).strftime("%Y-%m-%d-%H%Mz")


def current_slugs(now_ms: int, spans: tuple[str, ...] = LIVE_SPANS) -> list[str]:
    """Each span's current window's market and the next one (subscribed ahead so the boundary has no gap)."""
    out = []
    for span in spans:
        w = SPANS_MS[span]
        start = now_ms - now_ms % w
        out += [slug_for(start, span), slug_for(start + w, span)]
    return out


def window_of(slug: str) -> tuple[int, int] | None:
    """(open ms, close ms) of a series slug (any span), or None when it is not one."""
    m = _SLUG.match(slug or "")
    if not m:
        return None
    y, mo, d, h, mi = (int(g) for g in m.groups()[1:])
    start = int(dt.datetime(y, mo, d, h, mi, tzinfo=dt.UTC).timestamp() * 1000)
    return start, start + SPANS_MS[_SPAN_OF[m.group(1)]]


def is_series(slug: Any) -> bool:
    return isinstance(slug, str) and window_of(slug) is not None


RESOLVED = "MARKET_STATUS_RESOLVED"


def resolution(m: dict | None) -> dict:
    """A `GET /v1/market/slug/{slug}` market -> {status, result: "yes" | "no" | None, final}. Final only when the
    market says it is resolved and its outcome prices are exactly 1/0 or 0/1 (YES = Up first)."""
    m = m or {}
    status = m.get("status")
    op = m.get("outcomePrices")
    if isinstance(op, str):
        try:
            op = json.loads(op)
        except ValueError:
            op = None
    result = None
    if isinstance(op, list) and len(op) == 2:
        a, b = (num(x) for x in op)
        result = "yes" if (a, b) == (1.0, 0.0) else "no" if (a, b) == (0.0, 1.0) else None
    final = status == RESOLVED and result is not None
    return {"status": status, "result": result if final else None, "final": final}


def result_reader(markets_fn: Callable[[str], Awaitable[dict]]) -> Callable[[str], Awaitable[dict]]:
    """`markets_fn(slug)` (the SDK's public `markets.retrieve_by_slug`) -> an async `(slug) -> {status, result,
    final, source}` for settlement. Raises on a failed read (the caller retries)."""
    async def read(slug: str) -> dict:
        got = await markets_fn(slug)
        return {"ticker": slug, **resolution((got or {}).get("market")), "source": "polymarket_market_resolution"}
    return read


class MarketTerms:
    """Strike (priceToBeat) per slug from the public market endpoint, cached.

    `markets_fn(slug)` is the SDK's `client.markets.retrieve_by_slug` (public gateway; no key is used). A strike never
    changes once published, so it is kept; a missing one is re-asked at most every `retry_s`."""

    def __init__(self, markets_fn: Callable[[str], Awaitable[dict]], *, retry_s: float = 10.0,
                 clock_s: Callable[[], float] = time.monotonic) -> None:
        self._fn = markets_fn
        self._retry_s = retry_s
        self._clock = clock_s
        self._strike: dict[str, float] = {}
        self._asked: dict[str, float] = {}

    async def _market(self, slug: str) -> dict | None:
        now = self._clock()
        if now - self._asked.get(slug, -1e18) < self._retry_s:
            return None
        self._asked[slug] = now
        if len(self._asked) > 256:
            self._asked.pop(next(iter(self._asked)))
        try:
            got = await self._fn(slug)
        except Exception as exc:  # never logs the response body
            log.warning("polymarket market %s read failed (%s)", slug, type(exc).__name__)
            return None
        return (got or {}).get("market") or {}

    async def strike(self, slug: str) -> float | None:
        if not is_series(slug):
            return None
        if slug in self._strike:
            return self._strike[slug]
        m = await self._market(slug)
        ptb = num(((m or {}).get("assetPriceTerms") or {}).get("priceToBeat"))
        if ptb is not None and ptb > 0:
            self._strike[slug] = ptb
        return self._strike.get(slug)

    def cached_strike(self, slug: str) -> float | None:
        return self._strike.get(slug)
