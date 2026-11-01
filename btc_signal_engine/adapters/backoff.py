"""The retry and reconnect policy for every venue path, on tenacity (https://tenacity.readthedocs.io).

Review fix plan 2026-10-31, B3#10 made every retry and reconnect wait exponential with FULL jitter (AWS Architecture
Blog, "Exponential Backoff And Jitter") and made a 429 honour its Retry-After; 2026-11-01 moved the loops onto tenacity
(owner-approved: no hand-rolled retry framework). What is ours is only the policy:

* `jittered(base_s, cap_s)`: tenacity's `wait_random_exponential`, which is full jitter: attempt n waits a uniform
  draw from [0, min(cap_s, base_s * 2**(n-1))].
* `wait_venue(...)`: a tenacity custom wait (its documented extension point, a `wait_base`) for a re-sent request.
  A 429 waits at least what the venue asked (Retry-After, capped at RETRY_AFTER_MAX_S so an order path never sleeps
  for minutes) plus `wait_random` over the 429 base on top; without the header a 429 backs off from
  RATE_LIMIT_BASE_S (1 s), longer than a transport error or a 5xx (REQUEST_BASE_S, 0.2 s). `base_s` / `cap_s`
  replace that schedule for every status (a slow reader backs off from 1 s to 30 s); a Retry-After stays the floor.
* `resend_while_busy(send, ...)`: an `AsyncRetrying` for a SAFE read (market data, history, the read-only account
  link) the venue answered 429 or 5xx; it returns the last response and the caller raises on its status.
* `reconnecting(...)`: the `AsyncRetrying` of a long-lived reconnect supervisor: never gives up on its own, stops
  only when its owner closes, re-runs after a failure or a normal end, never after a cancellation. `start_over(a)`
  restarts the attempt count after a connection that lasted (tenacity has no reset: the next wait is the first
  window again, so a fleet of long-lived sockets still never reconnects in lockstep).
* `wait_at(wait, n)`: a tenacity wait for attempt n where no Retrying loop runs (a refused subscription's retry time,
  the writer's flush cadence, an async generator's reconnect).

Whether a request may be re-sent at all stays with each client (AGENTS.md, "Venue APIs"): our loop is the only one
(the SDK's retries are off) and a Kalshi re-send carries the SAME body (same client_order_id); a Polymarket create is
never re-sent. Tests make draws exact by patching tenacity's waits (tests/conftest.py: `jitter_top`, `jitter_bottom`).
"""

from __future__ import annotations

import email.utils
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from tenacity import (AsyncRetrying, AttemptManager, RetryCallState, retry_if_exception_type, retry_if_result,
                      stop_after_attempt)
from tenacity.wait import wait_base, wait_random, wait_random_exponential

log = logging.getLogger(__name__)

REQUEST_BASE_S = 0.2  # a transport error or a 5xx: 0.2 s, 0.4 s, 0.8 s ... (before jitter)
REQUEST_CAP_S = 5.0
RATE_LIMIT_BASE_S = 1.0  # a 429 without Retry-After: 1 s, 2 s, 4 s ... (before jitter)
RATE_LIMIT_CAP_S = 10.0
RETRY_AFTER_MAX_S = 10.0  # a Retry-After longer than this is waited only this long (an order path never stalls)


def jittered(base_s: float, cap_s: float) -> wait_random_exponential:
    """Full jitter: attempt n waits uniform [0, min(cap_s, base_s * 2**(n-1))] (tenacity's own strategy)."""
    return wait_random_exponential(multiplier=float(base_s), max=float(cap_s))


def wait_at(wait: Callable[[RetryCallState], float], attempt_number: int = 1) -> float:
    """The wait `wait` gives attempt `attempt_number` (1 = the first retry), for a schedule kept outside a loop."""
    state = RetryCallState(retry_object=None, fn=None, args=(), kwargs={})  # type: ignore[arg-type]
    state.attempt_number = max(1, int(attempt_number))
    return float(wait(state))


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


def busy(resp: Any) -> bool:
    """A response worth re-sending the same request for: a 429 or a 5xx."""
    status = getattr(resp, "status_code", 0)
    return status == 429 or status >= 500


def last_response(state: RetryCallState) -> Any:
    """The response of the attempt `state` ended with; None after an exception (a transport error)."""
    out = state.outcome
    return None if out is None or out.failed else out.result()


class wait_venue(wait_base):  # noqa: N801 - tenacity's naming for wait strategies
    """The wait before re-sending a request (module docstring); reads the status and Retry-After of the attempt's
    response (an exception, i.e. a transport error, backs off like a 5xx)."""

    def __init__(self, base_s: float | None = None, cap_s: float | None = None) -> None:
        self.on_top = wait_random(0, base_s or RATE_LIMIT_BASE_S)
        self.override = (jittered(base_s, cap_s if cap_s is not None else base_s * 32)
                         if base_s is not None else None)
        self.rate_limited = jittered(RATE_LIMIT_BASE_S, RATE_LIMIT_CAP_S)
        self.transient = jittered(REQUEST_BASE_S, REQUEST_CAP_S)

    def __call__(self, retry_state: RetryCallState) -> float:
        resp = last_response(retry_state)
        status = getattr(resp, "status_code", None)
        if status == 429:
            after = retry_after_s(header(getattr(resp, "headers", None)))
            if after is not None:  # the venue's wait is the floor (capped); jitter only on top
                return min(after, RETRY_AFTER_MAX_S) + self.on_top(retry_state)
        if self.override is not None:
            return self.override(retry_state)
        return (self.rate_limited if status == 429 else self.transient)(retry_state)


def rate_limit_pause(retry_after: float | None) -> float:
    """A 429 answered where nothing is re-sent (a shared limiter pauses every caller): the venue's wait (1 s without
    one), capped, plus tenacity's jitter on top."""
    floor = RATE_LIMIT_BASE_S if retry_after is None else retry_after
    return min(floor, RETRY_AFTER_MAX_S) + wait_at(wait_random(0, RATE_LIMIT_BASE_S))


async def resend_while_busy(send: Callable[[], Awaitable[Any]], *, sleep: Callable[[float], Awaitable[Any]],
                            what: str, attempts: int = 8, base_s: float = 1.0, cap_s: float = 30.0,
                            logger: logging.Logger | None = None) -> Any:
    """Call `send()` (one safe, read-only request; any pacing goes inside it) until its response is neither a 429 nor
    a 5xx, at most `attempts` times, waiting `wait_venue(base_s, cap_s)` after each busy answer. Returns the last
    response; the caller raises on its status. An exception from `send()` is not retried here."""
    lg = logger or log

    def note(state: RetryCallState) -> None:
        lg.warning("%s -> HTTP %s; retrying in %.1fs", what, last_response(state).status_code, state.upcoming_sleep)

    async def once() -> Any:  # `send` may be a plain callable returning an awaitable (a lambda): await it here
        return await send()

    return await AsyncRetrying(stop=stop_after_attempt(attempts), wait=wait_venue(base_s, cap_s),
                               retry=retry_if_result(busy), sleep=sleep, before_sleep=note,
                               retry_error_callback=last_response)(once)


def reconnecting(wait: wait_base, *, closed: Callable[[], bool], sleep: Callable[[float], Awaitable[Any]],
                 before_sleep: Callable[[RetryCallState], Any] | None = None) -> AsyncRetrying:
    """A reconnect supervisor's loop: `async for attempt in reconnecting(...): with attempt: <one connection>`.
    Never stops on its own (no attempt or time limit); after each connection (it failed, or it ended) it waits
    `wait` and runs the next one, until `closed()` is true. A cancellation (or any BaseException that is not an
    Exception) is re-raised at once, never retried."""
    return AsyncRetrying(wait=wait, stop=lambda _state: closed(), sleep=sleep, before_sleep=before_sleep,
                         retry=retry_if_exception_type(Exception) | retry_if_result(lambda _r: True),
                         retry_error_callback=lambda _state: None)


def start_over(attempt: AttemptManager) -> None:
    """After a connection that lasted: the next wait is drawn from the first window again (tenacity has no reset)."""
    attempt.retry_state.attempt_number = 1


def failure(state: RetryCallState) -> BaseException | None:
    """The exception the attempt `state` ended with; None when it ended normally."""
    out = state.outcome
    return out.exception() if out is not None and out.failed else None
