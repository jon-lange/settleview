"""One retry and reconnect policy for every venue path: exponential backoff with FULL jitter, and a 429's Retry-After.

Review fix plan 2026-10-31, B3#10 (and P2 "one retry helper with jitter"). Before this, four request-retry loops and
six reconnect loops each had their own fixed schedule (0.2 s, 0.4 s; 1 s doubling to 30 s) with no jitter: after a
network blip every Polymarket pool socket and both Kalshi streams reconnected in lockstep, and a 429 was retried after
0.2 s whatever the venue asked for.

* `full_jitter(attempt, base_s, cap_s)`: a uniform draw from [0, min(cap, base * 2**attempt)] (AWS Architecture Blog,
  "Exponential Backoff And Jitter": full jitter spreads synchronized clients best for the least total wait).
* `retry_after_s(value)`: an HTTP Retry-After header (delta-seconds or an HTTP-date) in seconds, or None.
* `retry_delay(attempt, status, retry_after)`: the wait before re-sending a request. A 429 waits at least what the
  venue asked (Retry-After, capped at RETRY_AFTER_MAX_S so an order path never sleeps for minutes; the venue's
  answer is still the floor up to that cap) plus a jittered share of the 429 backoff; without the header a 429 backs
  off from RATE_LIMIT_BASE_S (1 s), longer than a transport error or a 5xx (REQUEST_BASE_S, 0.2 s).
* `Backoff`: a reconnect loop's state (`next()` the jittered wait, `reset()` after a connection that lasted).
* `resend_while_busy(send, ...)`: the one loop for a SAFE read (market data, history, the read-only account link)
  that the venue answered 429 or 5xx: re-sent up to `attempts` times with `retry_delay`'s waits; the caller raises on
  the last response's status. Order paths keep their own loops (the order's identity rules differ per venue).

This module decides HOW LONG to wait. Whether a request may be re-sent at all stays with each client: the Kalshi
rule (AGENTS.md, "Venue APIs") is that our own retry loop is the only one and a re-send carries the SAME body (same
client_order_id); a Polymarket create is never re-sent. `rng` is injectable so tests are exact.
"""

from __future__ import annotations

import email.utils
import logging
import math
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

log = logging.getLogger(__name__)

REQUEST_BASE_S = 0.2  # a transport error or a 5xx: 0.2 s, 0.4 s, 0.8 s ... (before jitter)
REQUEST_CAP_S = 5.0
RATE_LIMIT_BASE_S = 1.0  # a 429 without Retry-After: 1 s, 2 s, 4 s ... (before jitter)
RATE_LIMIT_CAP_S = 10.0
RETRY_AFTER_MAX_S = 10.0  # a Retry-After longer than this is waited only this long (an order path never stalls)

Rng = Callable[[], float]


def _uniform() -> float:
    """The default draw in [0, 1) (looked up at call time: tests replace it)."""
    return random.random()  # noqa: S311 - backoff jitter, not cryptography


def full_jitter(attempt: int, base_s: float, cap_s: float, rng: Rng | None = None) -> float:
    """A wait drawn uniformly from [0, min(cap_s, base_s * 2**attempt)] (attempt 0 = the first retry)."""
    ceiling = min(float(cap_s), float(base_s) * (2 ** max(0, int(attempt))))
    return max(0.0, (rng or _uniform)() * ceiling)


def retry_after_s(value: Any, now_s: Callable[[], float] = time.time) -> float | None:
    """A Retry-After header value in seconds (>= 0): delta-seconds ("3", "1.5") or an HTTP-date. None when absent or
    unreadable (never an error: the caller falls back to its own backoff)."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        secs = float(text)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
        if when is None:
            return None
        secs = when.timestamp() - now_s()
    if math.isnan(secs) or math.isinf(secs):
        return None
    return max(0.0, secs)


def header(headers: Mapping[str, Any] | None, name: str = "Retry-After") -> Any:
    """A header from any mapping (httpx, aiohttp, a plain dict), case-insensitively; None when absent."""
    if not headers:
        return None
    got = headers.get(name) if hasattr(headers, "get") else None
    if got is not None:
        return got
    low = name.lower()
    for k, v in getattr(headers, "items", lambda: [])():
        if str(k).lower() == low:
            return v
    return None


def retry_delay(attempt: int, *, status: int | None = None, retry_after: float | None = None,
                base_s: float | None = None, cap_s: float | None = None, rng: Rng | None = None) -> float:
    """The wait before re-sending a request after attempt number `attempt` (0-based) failed with `status` (None or 0
    = a transport error). See the module docstring. `base_s` / `cap_s` replace the schedule (a slow backfill reader
    backs off from 1 s to 30 s for every status); a Retry-After still sets the floor."""
    draw = rng or _uniform
    if status == 429 and retry_after is not None:  # the venue's wait is the floor (capped); jitter only on top
        return min(retry_after, RETRY_AFTER_MAX_S) + draw() * (base_s or RATE_LIMIT_BASE_S)
    if base_s is not None:
        return full_jitter(attempt, base_s, cap_s if cap_s is not None else base_s * 32, draw)
    if status == 429:
        return full_jitter(attempt, RATE_LIMIT_BASE_S, RATE_LIMIT_CAP_S, draw)
    return full_jitter(attempt, REQUEST_BASE_S, REQUEST_CAP_S, draw)


class Backoff:
    """A reconnect loop's wait: full jitter over base_s doubling to cap_s. `next()` returns the wait and moves on;
    `reset()` starts over (after a connection that stayed up). `ceiling` is the current upper bound (for logs)."""

    def __init__(self, base_s: float, cap_s: float, *, rng: Rng | None = None) -> None:
        if base_s < 0 or cap_s < base_s:
            raise ValueError("backoff needs 0 <= base_s <= cap_s")
        self.base_s, self.cap_s = float(base_s), float(cap_s)
        self._rng = rng
        self.attempt = 0

    @property
    def ceiling(self) -> float:
        return min(self.cap_s, self.base_s * (2 ** self.attempt))

    def next(self) -> float:
        wait = full_jitter(self.attempt, self.base_s, self.cap_s, self._rng)
        if self.ceiling < self.cap_s:
            self.attempt += 1
        return wait

    def reset(self) -> None:
        self.attempt = 0


async def resend_while_busy(send: Callable[[], Awaitable[Any]], *, sleep: Callable[[float], Awaitable[Any]],
                            what: str, attempts: int = 8, base_s: float = 1.0, cap_s: float = 30.0,
                            logger: logging.Logger | None = None) -> Any:
    """Call `send()` (one safe, read-only request; any pacing goes inside it) until its response is neither a 429 nor
    a 5xx, at most `attempts` times, waiting `retry_delay` (full jitter over base_s doubling to cap_s, and at least a
    429's Retry-After) after each busy answer. Returns the last response; the caller raises on its status."""
    resp = None
    for attempt in range(attempts):
        resp = await send()
        if resp.status_code != 429 and resp.status_code < 500:
            break
        delay = retry_delay(attempt, status=resp.status_code, base_s=base_s, cap_s=cap_s,
                            retry_after=retry_after_s(header(getattr(resp, "headers", None))))
        (logger or log).warning("%s -> HTTP %s; retrying in %.1fs", what, resp.status_code, delay)
        await sleep(delay)
    return resp
