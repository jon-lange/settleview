"""Price-path features from the per-second index (KTD6): momentum, acceleration, last price, open_avg."""

from __future__ import annotations

import math

import numpy as np

from btc_signal_engine.domain.features import ACCEL_WINDOWS, WINDOWS


def _log_ret(idx: np.ndarray, back_to: int) -> float | None:
    """ln(price at T / price at T − back_to seconds); idx[-1] is the second before T."""
    ref = idx[-back_to - 1]
    return None if math.isnan(ref) else math.log(idx[-1] / ref)


def momentum_features(idx: np.ndarray) -> dict[str, float | None]:
    """`idx` holds the carried-forward index price per second (NaN before the first known price)."""
    out: dict[str, float | None] = {}
    for w in WINDOWS:
        out[f"momentum_{w}"] = _log_ret(idx, w)
    for w in ACCEL_WINDOWS:
        ref = idx[-2 * w - 1]
        mid = idx[-w - 1]
        out[f"accel_{w}"] = None if math.isnan(ref) else math.log(idx[-1] / mid) - math.log(mid / ref)
    return out


def price_features(idx: np.ndarray, last_trade_ts: int | None, as_of: int) -> dict[str, float | None]:
    """last_price, last_trade_age, open_avg (mean of the 60 seconds before T, rounded to cents) and known_component."""
    if last_trade_ts is None:
        return dict.fromkeys(("last_price", "last_trade_age", "open_avg", "known_component"))
    last = float(idx[-1])
    window = idx[-60:]
    open_avg = None if math.isnan(window[0]) else round(float(window.mean()), 2)
    return {
        "last_price": last,
        "last_trade_age": (as_of - last_trade_ts) / 1000,
        "open_avg": open_avg,
        "known_component": None if open_avg is None else last - open_avg,
    }


def kc_z(known_component: float | None, last_price: float | None, rv_60: float | None) -> float | None:
    """known_component in units of one minute's realized move: kc / (last_price * rv_60).

    rv_60 = sqrt(sum of squared 1s log returns over 60s) = (rv_60 / sqrt(60)) * sqrt(60), i.e. the per-second
    volatility scaled to one minute, so last_price * rv_60 is the last minute's typical move in USD.
    """
    if known_component is None or last_price is None or rv_60 is None:
        return None
    if rv_60 == 0:
        return 0.0  # a flat minute: every price equals the last, so known_component is 0 too
    return float(known_component / (last_price * rv_60))
