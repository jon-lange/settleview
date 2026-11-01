"""EMA 9/21 alignment and stacked trend per timeframe on completed candles (TA-Lib), plus cross-timeframe agreement."""

from __future__ import annotations

import numpy as np
import talib

from btc_signal_engine.domain.features import EMA_FAST, EMA_MIN_CANDLES, EMA_SLOW, TIMEFRAMES


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def trend_features(closes: dict[str, np.ndarray]) -> dict[str, float | None]:
    """`closes` maps each timeframe to its completed candle closes, oldest first."""
    out: dict[str, float | None] = {}
    for tf in TIMEFRAMES:
        c = closes[tf]
        if len(c) < EMA_MIN_CANDLES:
            out[f"ema_align_{tf}"] = out[f"trend_{tf}"] = None
            continue
        fast = float(talib.EMA(c, EMA_FAST)[-1])
        slow = float(talib.EMA(c, EMA_SLOW)[-1])
        last = float(c[-1])
        out[f"ema_align_{tf}"] = _sign(fast - slow)
        out[f"trend_{tf}"] = 1 if last > fast > slow else -1 if last < fast < slow else 0
    votes = [out[f"ema_align_{tf}"] for tf in TIMEFRAMES]
    if any(v is None for v in votes):
        out["trend_agree"] = out["ema_votes_up"] = out["ema_votes_down"] = None
    else:
        base = votes[0]
        out["trend_agree"] = base * sum(v == base for v in votes)
        out["ema_votes_up"] = sum(v == 1 for v in votes)
        out["ema_votes_down"] = sum(v == -1 for v in votes)
    return out
