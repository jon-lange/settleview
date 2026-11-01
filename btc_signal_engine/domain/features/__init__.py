"""Feature library (U5): pure, deterministic features as of a boundary timestamp.

`FEATURE_LIB_VERSION` is part of every ruleset hash (KTD10); bump it whenever any feature's output changes,
and regenerate `tests/fixtures/golden_features.json`.

`FEATURES` documents every name a snapshot returns. Per-window names are `<feature>_<window seconds>`.
A feature is `backfillable` when it can be computed from trades alone (backfilled periods have no book data).
"""

from __future__ import annotations

from dataclasses import dataclass

FEATURE_LIB_VERSION = "2"
"""2: added ofi_{5,15,30,60}, microprice_bps, intensity_ratio, kc_z (existing features unchanged)."""

WINDOWS: tuple[int, ...] = (5, 15, 30, 60, 180, 300, 900)
"""Rolling windows in seconds for the per-second families."""

ACCEL_WINDOWS: tuple[int, ...] = (5, 15, 30, 60, 180, 300)
"""Windows for features comparing the last window with the prior equal window (needs 2W ≤ 900s of buckets)."""

TIMEFRAMES: tuple[str, ...] = ("1m", "5m", "15m")
TIMEFRAME_MINUTES: dict[str, int] = {"1m": 1, "5m": 5, "15m": 15}
RANGE_LENGTHS: tuple[int, ...] = (10, 20, 45)
"""Consolidation lengths N (completed 1m candles) for breakout strategy E."""

OFI_WINDOWS: tuple[int, ...] = (5, 15, 30, 60)
"""Windows (seconds) for order-flow imbalance over per-second best-level changes."""

INTENSITY_HALF_LIFE_S = 10.0
"""Half-life of the exponentially decayed trade-count intensities behind intensity_ratio."""
INTENSITY_MIN_COVERED_S = 30
"""Seconds of seen data (3 half-lives) before intensity_ratio is available."""

EMA_FAST, EMA_SLOW = 9, 21
EMA_MIN_CANDLES = 50
"""Candles required on a timeframe before its EMA features are available (as in BTC Flow `rules.js`)."""


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    name: str
    window_s: int | None
    backfillable: bool
    unit: str
    description: str


def _registry() -> dict[str, FeatureSpec]:
    specs: list[FeatureSpec] = []

    def add(name: str, window_s: int | None, unit: str, description: str, backfillable: bool = True) -> None:
        specs.append(FeatureSpec(name, window_s, backfillable, unit, description))

    for w in WINDOWS:
        add(f"buy_vol_{w}", w, "BTC", f"Aggressor-buy volume over the last {w}s.")
        add(f"sell_vol_{w}", w, "BTC", f"Aggressor-sell volume over the last {w}s.")
        add(f"imbalance_{w}", w, "ratio [-1, 1]",
            f"(buy − sell)/(buy + sell) volume over the last {w}s; 0.0 when there was no volume.")
        add(f"trades_{w}", w, "count", f"Number of trades over the last {w}s.")
        add(f"momentum_{w}", w, "log return", f"ln(index price at T / index price at T−{w}s).")
        add(f"rv_{w}", w, "log return",
            f"Realized volatility: sqrt of the sum of squared 1s index log returns over the last {w}s (not annualized).")
    for w in ACCEL_WINDOWS:
        add(f"accel_{w}", 2 * w, "log return", f"momentum over the last {w}s minus momentum over the {w}s before that.")
        add(f"vol_accel_{w}", 2 * w, "BTC", f"Total volume over the last {w}s minus total volume over the {w}s before.")
    for tf in TIMEFRAMES:
        sec = TIMEFRAME_MINUTES[tf] * 60
        add(f"ema_align_{tf}", sec, "-1/0/+1",
            f"+1 if EMA{EMA_FAST} > EMA{EMA_SLOW} of completed {tf} closes, −1 if below, 0 if equal (strategy B's vote).")
        add(f"trend_{tf}", sec, "-1/0/+1",
            f"Stacked trend on completed {tf} candles: +1 if close > EMA{EMA_FAST} > EMA{EMA_SLOW}, −1 if mirrored, else 0.")
    add("trend_agree", None, "-3..+3",
        "Number of {1m,5m,15m} ema_align votes equal to ema_align_1m (1m counts itself), signed by ema_align_1m; "
        "0 when ema_align_1m is 0.")
    add("ema_votes_up", None, "count 0..3", "How many of {1m,5m,15m} ema_align are +1.")
    add("ema_votes_down", None, "count 0..3", "How many of {1m,5m,15m} ema_align are −1.")
    for name, text in (
        ("bull_engulf", "last completed 1m candle is green and its body engulfs the prior red body"),
        ("bear_engulf", "last completed 1m candle is red and its body engulfs the prior green body"),
        ("hammer", "body/range ≤ 0.35, lower wick ≥ 2×body, upper wick ≤ body, after a decline "
                   "(prior candle close < close 3 candles back)"),
        ("shooting_star", "body/range ≤ 0.35, upper wick ≥ 2×body, lower wick ≤ body, after a rise"),
    ):
        add(name, 60, "0/1", f"1 if the {text}; ported from BTC Flow rules.js.")
    add("body_ratio", 60, "ratio [0, 1]", "|close − open| / (high − low) of the last completed 1m candle; 0 if flat.")
    add("upper_wick_ratio", 60, "ratio [0, 1]", "(high − max(open, close)) / range of the last 1m candle; 0 if flat.")
    add("lower_wick_ratio", 60, "ratio [0, 1]", "(min(open, close) − low) / range of the last 1m candle; 0 if flat.")
    add("range_pct", 60, "percent", "(high − low) / close × 100 of the last completed 1m candle.")
    add("last_close_1m", 60, "USD", "Close of the last completed 1m candle.")
    add("last_volume_1m", 60, "BTC", "Volume of the last completed 1m candle.")
    for n in RANGE_LENGTHS:
        what = f"the {n} completed 1m candles before the last completed one"
        add(f"range_high_{n}", (n + 1) * 60, "USD", f"Highest high of {what}.")
        add(f"range_low_{n}", (n + 1) * 60, "USD", f"Lowest low of {what}.")
        add(f"range_width_pct_{n}", (n + 1) * 60, "percent", f"(range_high − range_low) / range_low × 100 over {what}.")
        add(f"range_avg_vol_{n}", (n + 1) * 60, "BTC", f"Mean 1m volume of {what}.")
    add("spread", 1, "USD", "ask − bid from the latest book second within 5s before T.", backfillable=False)
    add("book_imbalance_10", 1, "ratio [-1, 1]", "(bid − ask) depth within 10 bps / total, latest book second.",
        backfillable=False)
    add("depth_bid_10", 1, "BTC", "Bid depth within 10 bps of mid, latest book second.", backfillable=False)
    add("depth_ask_10", 1, "BTC", "Ask depth within 10 bps of mid, latest book second.", backfillable=False)
    for w in OFI_WINDOWS:
        add(f"ofi_{w}", w, "multiples of avg best-level depth",
            f"Order-flow imbalance (Cont, Kukanov & Stoikov 2014) over the last {w}s, from per-second best levels: "
            "sum over seconds s of e_s = [bid_s >= bid_s-1]*bid_size_s - [bid_s <= bid_s-1]*bid_size_s-1 "
            "- [ask_s <= ask_s-1]*ask_size_s + [ask_s >= ask_s-1]*ask_size_s-1, divided by the mean over those "
            "seconds of (bid_size + ask_size)/2. Positive = net demand at the top of book. Needs consecutive book "
            "seconds with sizes covering at least half the window, else book_unavailable.", backfillable=False)
    add("microprice_bps", 1, "basis points of mid",
        "Weighted mid (first-order Stoikov 2018 micro-price) minus mid, latest book second within 5s: "
        "I = bid_size/(bid_size + ask_size), micro = I*ask + (1 - I)*bid, value = (micro - mid)/mid * 1e4. "
        "Lies within +/- half the spread; positive when the ask queue is the thinner one.", backfillable=False)
    add("intensity_ratio", None, "ratio [0, 1]",
        f"Hawkes-lite aggressor pace: lambda_buy / (lambda_buy + lambda_sell), where lambda sums "
        f"0.5^(age/{INTENSITY_HALF_LIFE_S:g}s) over trade COUNTS before T (second resolution: a trade in second s "
        f"has age T - s - 0.5s; the last 900s are summed). 0.5 when there were no trades; warmup until "
        f"{INTENSITY_MIN_COVERED_S}s of data were seen.")
    add("last_price", None, "USD", "Last trade price with ts < T (the index price at T, KTD6).")
    add("last_trade_age", None, "seconds", "T minus the timestamp of the last trade before T.")
    add("open_avg", 60, "USD", "Mean of the 60 per-second index prices in [T−60s, T), rounded to cents (KTD6).")
    add("known_component", 60, "USD", "last_price − open_avg (KTD15).")
    add("kc_z", 60, "one-minute moves",
        "known_component / (last_price * rv_60): distance of the last price from open_avg in units of the last "
        "minute's realized move (rv_60 is the 60s log-return volatility, so last_price * rv_60 is about one "
        "minute's typical move in USD). 0.0 when rv_60 is 0 (flat minute).")
    return {s.name: s for s in specs}


FEATURES: dict[str, FeatureSpec] = _registry()

UNAVAILABLE_WARMUP = "warmup"
UNAVAILABLE_BOOK = "book_unavailable"
