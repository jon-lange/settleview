"""Completed-candle structure, patterns (ported from BTC Flow `rules.js` analyze()) and consolidation ranges."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from btc_signal_engine.domain.features import RANGE_LENGTHS

MINUTE_MS = 60_000
PATTERN_MIN_CANDLES = 4  # hammer / shooting star compare with the close three candles back


@dataclass(frozen=True, slots=True)
class Candles:
    """Contiguous candles as parallel arrays; `t` is each candle's start in UTC ms."""

    t: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741
    c: np.ndarray
    v: np.ndarray

    def __len__(self) -> int:
        return len(self.t)


def aggregate(one: Candles, minutes: int) -> Candles:
    """Aggregate contiguous 1m candles into aligned `minutes` candles, keeping only complete ones."""
    period = minutes * MINUTE_MS
    aligned = np.flatnonzero(one.t % period == 0)
    start = int(aligned[0]) if len(aligned) else len(one)
    n = (len(one) - start) // minutes
    end = start + n * minutes

    def grid(a: np.ndarray) -> np.ndarray:
        return a[start:end].reshape(n, minutes)

    return Candles(t=grid(one.t)[:, 0], o=grid(one.o)[:, 0], h=grid(one.h).max(axis=1), l=grid(one.l).min(axis=1),
                   c=grid(one.c)[:, -1], v=grid(one.v).sum(axis=1))


def candle_features(one: Candles) -> dict[str, float | None]:
    """Features of the last completed 1m candle and the consolidation range before it."""
    out: dict[str, float | None] = {}
    n = len(one)
    out.update(_structure(one) if n >= 1 else dict.fromkeys(
        ("body_ratio", "upper_wick_ratio", "lower_wick_ratio", "range_pct", "last_close_1m", "last_volume_1m")))
    out.update(_patterns(one) if n >= PATTERN_MIN_CANDLES else dict.fromkeys(
        ("bull_engulf", "bear_engulf", "hammer", "shooting_star")))
    for k in RANGE_LENGTHS:
        if n >= k + 1:
            hi, lo = float(one.h[-k - 1:-1].max()), float(one.l[-k - 1:-1].min())
            out[f"range_high_{k}"] = hi
            out[f"range_low_{k}"] = lo
            out[f"range_width_pct_{k}"] = (hi - lo) / lo * 100
            out[f"range_avg_vol_{k}"] = float(one.v[-k - 1:-1].mean())
        else:
            out.update(dict.fromkeys((f"range_high_{k}", f"range_low_{k}", f"range_width_pct_{k}",
                                      f"range_avg_vol_{k}")))
    return out


def _structure(one: Candles) -> dict[str, float | None]:
    o, h, l, c = (float(one.o[-1]), float(one.h[-1]), float(one.l[-1]), float(one.c[-1]))
    rng = h - l
    return {
        "body_ratio": abs(c - o) / rng if rng else 0.0,
        "upper_wick_ratio": (h - max(o, c)) / rng if rng else 0.0,
        "lower_wick_ratio": (min(o, c) - l) / rng if rng else 0.0,
        "range_pct": rng / c * 100,
        "last_close_1m": c,
        "last_volume_1m": float(one.v[-1]),
    }


def _patterns(one: Candles) -> dict[str, float | None]:
    o, h, l, c = (float(one.o[-1]), float(one.h[-1]), float(one.l[-1]), float(one.c[-1]))
    po, pc = float(one.o[-2]), float(one.c[-2])
    prior_c = float(one.c[-4])
    body, rng = abs(c - o), h - l
    upper, lower = h - max(o, c), min(o, c) - l
    small_body = body > 0 and body / rng <= 0.35
    return {
        "bull_engulf": int(pc < po and c > o and o <= pc and c >= po),
        "bear_engulf": int(pc > po and c < o and o >= pc and c <= po),
        "hammer": int(small_body and lower >= 2 * body and upper <= body and pc < prior_c),
        "shooting_star": int(small_body and upper >= 2 * body and lower <= body and pc > prior_c),
    }
