"""Order-flow features over the per-second buckets: volume, imbalance, trade count, volume acceleration,
and the decayed buy/sell trade-count intensity ratio."""

from __future__ import annotations

import numpy as np

from btc_signal_engine.domain.features import (
    ACCEL_WINDOWS,
    INTENSITY_HALF_LIFE_S,
    INTENSITY_MIN_COVERED_S,
    WINDOWS,
)

_decay_cache: dict[int, np.ndarray] = {}


def _decay(n: int) -> np.ndarray:
    """Weights for n seconds, oldest first: a trade in the second ending at T - k s has age k - 0.5 s."""
    w = _decay_cache.get(n)
    if w is None:
        ages = np.arange(n, 0, -1, dtype=np.float64) - 0.5
        w = _decay_cache[n] = np.power(0.5, ages / INTENSITY_HALF_LIFE_S)
    return w


def flow_features(buy: np.ndarray, sell: np.ndarray, count: np.ndarray, covered_s: int,
                  buy_count: np.ndarray | None = None) -> dict[str, float | None]:
    """Arrays hold one entry per second, the last being the second just before T.

    `covered_s` is how many of the most recent seconds lie inside the data the state has seen;
    a window longer than that is warm-up (None). `buy_count` (aggressive-buy trades per second) enables
    intensity_ratio; the sell count is `count - buy_count`.
    """
    out: dict[str, float | None] = {}
    for w in WINDOWS:
        ok = covered_s >= w
        b = float(buy[-w:].sum()) if ok else None
        s = float(sell[-w:].sum()) if ok else None
        out[f"buy_vol_{w}"] = b
        out[f"sell_vol_{w}"] = s
        out[f"trades_{w}"] = int(count[-w:].sum()) if ok else None
        out[f"imbalance_{w}"] = None if not ok else (0.0 if b + s == 0 else (b - s) / (b + s))
    total = buy + sell
    for w in ACCEL_WINDOWS:
        ok = covered_s >= 2 * w
        out[f"vol_accel_{w}"] = float(total[-w:].sum() - total[-2 * w:-w].sum()) if ok else None
    if buy_count is not None:
        out["intensity_ratio"] = intensity_ratio(buy_count, count, covered_s)
    return out


def intensity_ratio(buy_count: np.ndarray, count: np.ndarray, covered_s: int) -> float | None:
    """lambda_buy / (lambda_buy + lambda_sell) with lambda = sum of 0.5^(age / half-life) over trades."""
    if covered_s < INTENSITY_MIN_COVERED_S:
        return None
    w = _decay(len(count))
    lb = float(np.dot(w, buy_count))
    ls = float(np.dot(w, count - buy_count))
    return 0.5 if lb + ls == 0 else lb / (lb + ls)
