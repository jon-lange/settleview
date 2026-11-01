"""Fair odds that a window settles UP, under a driftless random walk (Coach).

Settlement (KTD6): a window [T, E) settles UP iff `close_avg >= K`, where `close_avg` is the mean
of the 60 per-second index prices with timestamps E−60s … E−1s and K is the window's `open_avg`
(for 15m this is the same number Kalshi uses as the strike, up to the Coinbase-vs-Kalshi index
basis; we use our own open_avg so price and strike come from the same index).

Model. Let `at` be now (a whole second), S the current index price, σ the per-second dollar
volatility. The index row stamped `ts` carries the price at the end of that second, so a closing
second with `ts >= at` is still unknown and lies d = (ts − at)/1000 + 1 seconds ahead. Each
unknown price is S + σ·W(d) with W a standard Brownian motion (no drift). Closing seconds already
recorded (ts < at) are known; their sum is A and their count is m. With n = 60 − m unknown seconds
at consecutive offsets g, g+1, …, g+n−1:

    sum of unknown prices  ~  Normal(n·S, σ²·V),   V = Σ_i Σ_j min(d_i, d_j) = n²·g + (n−1)·n·(2n−1)/6

    P(UP) = P(A + sum >= 60·K) = Φ( (A + n·S − 60·K) / (σ·√V) )

When every closing second is known (n = 0), P(UP) is 1 if A >= 60·K else 0 (an exact tie is UP).

Volatility: σ = S · sqrt(mean of squared 1-minute log returns over the last 30 minutes / 60),
floored at S · SIGMA_FLOOR_FRAC so a quiet stretch never makes the model certain. One-minute
returns are used because the per-second index is the last trade, which flips between bid and ask
("bid-ask bounce"); per-second returns made a calm market look ~5x more volatile and pulled every
probability toward 50%. The engine's lean is deliberately not mixed in: the lean is a rule tally, not a
calibrated probability, and keeping the odds a pure function of price, strike, time and volatility
keeps every number auditable. The engine's view is shown alongside instead.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Sequence
from operator import itemgetter

_TS = itemgetter(0)

CLOSE_SECONDS = 60
SIGMA_FLOOR_FRAC = 2e-5
"""Minimum per-second volatility as a fraction of price (≈ $1.60/s at $80k; quiet BTC is ~$3–8/s)."""


def sigma_per_second(price: float | None, rv_60: float | None) -> float | None:
    """Per-second dollar volatility σ = price × rv_60 / √60, floored; None while warming up."""
    if price is None or rv_60 is None or price <= 0:
        return None
    return max(price * rv_60 / math.sqrt(60), price * SIGMA_FLOOR_FRAC)


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _sum_var(at: int, window_end: int, m: int) -> float:
    """V of the module doc: the variance (in sigma^2 units) of the sum of the 60 - m unknown closing prices."""
    n = CLOSE_SECONDS - m
    if n <= 0:
        return 0.0
    first_unknown_ts = max(at, window_end - CLOSE_SECONDS * 1000 + m * 1000)
    g = (first_unknown_ts - at) / 1000 + 1
    return n * n * g + (n - 1) * n * (2 * n - 1) / 6


def _sum_sd(sigma: float, v: float, index_sd: float = 0.0) -> float:
    if index_sd > 0:
        return math.sqrt(sigma * sigma * v + CLOSE_SECONDS * CLOSE_SECONDS * index_sd * index_sd)
    return sigma * math.sqrt(v)


def close_avg_dist(*, price: float, sigma: float, at: int, window_end: int, observed: Sequence[float] = (),
                   index_sd: float = 0.0) -> tuple[float, float]:
    """(mean, sd) in dollars of the settlement price, the 60-second closing average, under the same model as
    prob_up: mean = (A + n*S) / 60, sd = sqrt(sigma^2 * V + 3600 * index_sd^2) / 60. So
    prob_up = Phi((mean - strike) / sd) whenever sd > 0: a band drawn from these agrees with the odds."""
    observed = list(observed)[:CLOSE_SECONDS]
    m = len(observed)
    mean = (sum(observed) + (CLOSE_SECONDS - m) * price) / CLOSE_SECONDS
    return mean, _sum_sd(sigma, _sum_var(at, window_end, m), index_sd) / CLOSE_SECONDS


def prob_up(*, price: float, strike: float, sigma: float, at: int, window_end: int,
            observed: Sequence[float] = (), index_sd: float = 0.0) -> float:
    """P(window settles UP). `observed` = index prices of closing seconds with ts < `at` (see module doc).

    `index_sd`: standard deviation (dollars) of the error between our index and the settlement index, averaged
    over the closing minute (Final Minute, KTD14). It adds 60^2 * index_sd^2 to the variance of the 60-second
    sum, so P(UP) = Phi((A + n*S - 60*K) / sqrt(sigma^2 * V + 3600 * index_sd^2)). 0 keeps the original model."""
    observed = list(observed)[:CLOSE_SECONDS]
    m = len(observed)
    n = CLOSE_SECONDS - m
    a = sum(observed)
    target = CLOSE_SECONDS * strike
    if n == 0 and index_sd <= 0:
        return 1.0 if a >= target - 1e-9 else 0.0
    sd = _sum_sd(sigma, _sum_var(at, window_end, m), index_sd)
    mean_gap = a + n * price - target
    if sd <= 0:
        return 1.0 if mean_gap >= 0 else 0.0
    return _phi(mean_gap / sd)


VOL_MINUTES = 30
MIN_VOL_RETURNS = 10
MAX_PRICE_AGE_MS = 5_000
PRICE_SMOOTH_S = 10
"""S is the mean of the last 10 per-second prices: the last trade bounces between bid and ask, and near
the close a $5 tick moved the odds by 5-8c, so one-tick odds were noise (entries/exits seconds apart)."""
HISTORY_MS = (VOL_MINUTES + 1) * 60_000 + 2_000
"""How much index history before `at` callers must supply."""


def realized_vol(prices: Sequence[float]) -> float | None:
    """rv = sqrt(Σ (ln p_i / p_{i−1})²) over consecutive per-second prices (same definition as rv_60)."""
    if len(prices) < 2:
        return None
    return math.sqrt(sum(math.log(b / a) ** 2 for a, b in zip(prices, prices[1:], strict=False) if a > 0 and b > 0))


def minute_sigma(at: int, rows: Sequence[tuple[int, float]], end: int) -> float | None:
    """Per-second dollar volatility from 1-minute log returns over the last VOL_MINUTES (bounce-robust).

    `rows[:end]` are (ts, price) with ts < at, sorted. Each minute mark takes the latest price at or
    before it; at least MIN_VOL_RETURNS returns are required.
    """
    samples: list[float] = []
    hi = end
    for k in range(VOL_MINUTES + 1):
        mark = at - k * 60_000
        i = bisect_left(rows, mark, 0, hi, key=_TS) - 1  # the latest row before the mark (a bisection, not a walk)
        if i < 0:
            break
        samples.append(rows[i][1])
        hi = i + 1
    rets = [math.log(a / b) for a, b in zip(samples, samples[1:], strict=False) if a > 0 and b > 0]
    if len(rets) < MIN_VOL_RETURNS:
        return None
    price = rows[end - 1][1]
    return max(price * math.sqrt(sum(r * r for r in rets) / len(rets) / 60), price * SIGMA_FLOOR_FRAC)


def fair_from_index(at: int, rows: Sequence[tuple[int, float]], open_avg: float, window_end: int) -> dict | None:
    """Fair odds at whole second `at` from recorded per-second index rows `(ts, price)` sorted by ts.

    Returns fair_up, price (S), sigma (per second), at, window_end and the settlement average's mean_close /
    sd_close (close_avg_dist), so a chart's band and the odds come from one computation.
    Uses only rows with ts < at: S = the latest row (must be within 5s), σ from 1-minute returns over
    the last 30 minutes (callers pass HISTORY_MS of rows), observed closing seconds with missing rows carried forward from the
    previous row (or S). None when the data is not there yet. Live advice and paper replay both use
    this function, so the record is scored with exactly the odds the screen would have shown.
    """
    end = bisect_left(rows, at, key=_TS)  # first index with ts >= at
    if end == 0 or rows[end - 1][0] < at - MAX_PRICE_AGE_MS:
        return None
    recent = [p for ts, p in rows[max(0, end - PRICE_SMOOTH_S):end] if ts >= at - PRICE_SMOOTH_S * 1000]
    price = sum(recent) / len(recent)
    sigma = minute_sigma(at, rows, end)
    if sigma is None:
        return None
    close_start = window_end - CLOSE_SECONDS * 1000
    observed: list[float] = []
    if at > close_start:
        c = bisect_left(rows, close_start, 0, end, key=_TS)  # the first closing second's row (no copy of `rows`)
        by_ts = {ts: p for ts, p in rows[c:end]}
        prev = rows[c - 1][1] if c > 0 else price
        for s in range(close_start, min(at, window_end), 1000):
            prev = by_ts.get(s, prev)
            observed.append(prev)
    fair = prob_up(price=price, strike=open_avg, sigma=sigma, at=at, window_end=window_end, observed=observed)
    mean_close, sd_close = close_avg_dist(price=price, sigma=sigma, at=at, window_end=window_end, observed=observed)
    return {"fair_up": fair, "price": price, "sigma": sigma, "at": at, "window_end": window_end,
            "mean_close": mean_close, "sd_close": sd_close}
