"""Realized volatility of 1-second index log returns."""

from __future__ import annotations

import math

import numpy as np

from btc_signal_engine.domain.features import WINDOWS


def volatility_features(idx: np.ndarray) -> dict[str, float | None]:
    """rv_W = sqrt(sum of squared 1s log returns over the last W seconds); None until the price at T−W is known."""
    out: dict[str, float | None] = {}
    for w in WINDOWS:
        path = idx[-w - 1:]
        out[f"rv_{w}"] = None if math.isnan(path[0]) else float(math.sqrt(np.square(np.diff(np.log(path))).sum()))
    return out
