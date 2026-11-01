"""Incremental feature state (U5). Deterministic, no clock: everything is driven by event timestamps.

The caller applies the KTD5 lateness filter on `ingest_ts`; this state only enforces that `snapshot(T)` reads
events with `ts < T` and 1m candles whose close time is ≤ T. Events may arrive out of order.
"""

from __future__ import annotations

import numpy as np

from btc_signal_engine.domain.features import FEATURES, UNAVAILABLE_BOOK, UNAVAILABLE_WARMUP
from btc_signal_engine.domain.features.book import OFI_LOOKBACK_S, book_features, ofi_features
from btc_signal_engine.domain.features.candles import MINUTE_MS, Candles, aggregate, candle_features
from btc_signal_engine.domain.features.flow import flow_features
from btc_signal_engine.domain.features.momentum import kc_z, momentum_features, price_features
from btc_signal_engine.domain.features.trend import trend_features
from btc_signal_engine.domain.features.volatility import volatility_features
from btc_signal_engine.domain.models.events import BookSecond, Trade

SECOND_MS = 1_000
LOOKBACK_S = 901
"""Seconds of buckets a snapshot reads: momentum_900 / rv_900 need the price as of T−900s, i.e. second T−901."""
FUTURE_SLACK_MS = 60_000
"""snapshot(T) is exact while no event at or after T + this slack has been seen (pruning stays behind the window)."""
BUCKET_RETAIN_S = LOOKBACK_S + FUTURE_SLACK_MS // SECOND_MS + 40
CANDLE_HISTORY_MIN = 14 * 60
"""Completed 1m candles a snapshot uses (KTD14 hydration covers this)."""
CANDLE_RETAIN_MIN = CANDLE_HISTORY_MIN + 120
BOOK_MAX_AGE_S = 5
"""A book second older than this before T does not count; book features are then `book_unavailable`."""
PRUNE_EVERY_S = 60

_BOOK_NAMES = frozenset(n for n, spec in FEATURES.items() if not spec.backfillable)

Key = tuple[int, int]  # (ts, trade_id): orders trades within a bucket or candle


class FeatureState:
    """Rolling per-second buckets (900s+), 1m candles (14h+) and recent book seconds."""

    def __init__(self) -> None:
        # second -> [buy_vol, sell_vol, count, last_price, last_key, buy_count]
        self._buckets: dict[int, list] = {}
        # minute start ms -> [open, high, low, close, volume, open_key, close_key]
        self._candles: dict[int, list] = {}
        self._books: dict[int, BookSecond] = {}
        self._first_ts: int | None = None
        self._max_sec: int | None = None
        self._bucket_cutoff = -(2**62)  # seconds below this were pruned
        self._candle_cutoff = -(2**62)  # minute starts below this were pruned
        self._last_prune_sec = -(2**62)
        self._pruned_trade: tuple[Key, float] | None = None  # latest trade among pruned buckets
        self._pruned_candle: tuple[Key, float] | None = None  # latest trade among pruned candles

    def copy(self) -> FeatureState:
        """An independent copy (the bucket and candle rows are copied too, since ingestion updates them in place), so
        a snapshot can be computed off the loop thread while ingestion keeps writing to the original."""
        out = FeatureState.__new__(FeatureState)
        out.__dict__.update(self.__dict__)
        out._buckets = {k: v.copy() for k, v in self._buckets.items()}
        out._candles = {k: v.copy() for k, v in self._candles.items()}
        out._books = dict(self._books)
        return out

    # ingestion -----------------------------------------------------------------------------------------------

    def on_trade(self, t: Trade) -> None:
        sec = t.ts // SECOND_MS
        key: Key = (t.ts, t.trade_id)
        if self._first_ts is None or t.ts < self._first_ts:
            self._first_ts = t.ts
        self._advance(sec)

        if sec < self._bucket_cutoff:
            self._pruned_trade = _later(self._pruned_trade, key, t.price)
        else:
            b = self._buckets.get(sec)
            if b is None:
                self._buckets[sec] = [t.size if t.buy else 0.0, 0.0 if t.buy else t.size, 1, t.price, key,
                                      1 if t.buy else 0]
            else:
                b[0 if t.buy else 1] += t.size
                b[2] += 1
                b[5] += 1 if t.buy else 0
                if key >= b[4]:
                    b[3], b[4] = t.price, key

        minute = t.ts - t.ts % MINUTE_MS
        if minute < self._candle_cutoff:
            self._pruned_candle = _later(self._pruned_candle, key, t.price)
            return
        c = self._candles.get(minute)
        if c is None:
            self._candles[minute] = [t.price, t.price, t.price, t.price, t.size, key, key]
            return
        c[1] = max(c[1], t.price)
        c[2] = min(c[2], t.price)
        c[4] += t.size
        if key < c[5]:
            c[0], c[5] = t.price, key
        if key >= c[6]:
            c[3], c[6] = t.price, key

    def on_book(self, b: BookSecond) -> None:
        sec = b.ts // SECOND_MS
        self._advance(sec)
        if sec >= self._bucket_cutoff:
            self._books[sec] = b

    def _advance(self, sec: int) -> None:
        if self._max_sec is not None and sec <= self._max_sec:
            return
        self._max_sec = sec
        if sec - self._last_prune_sec < PRUNE_EVERY_S:
            return
        self._last_prune_sec = sec
        self._bucket_cutoff = sec - BUCKET_RETAIN_S
        for s in [s for s in self._buckets if s < self._bucket_cutoff]:
            b = self._buckets.pop(s)
            self._pruned_trade = _later(self._pruned_trade, b[4], b[3])
        for s in [s for s in self._books if s < self._bucket_cutoff]:
            del self._books[s]
        self._candle_cutoff = (sec * SECOND_MS) - (sec * SECOND_MS) % MINUTE_MS - CANDLE_RETAIN_MIN * MINUTE_MS
        for m in [m for m in self._candles if m < self._candle_cutoff]:
            c = self._candles.pop(m)
            self._pruned_candle = _later(self._pruned_candle, c[6], c[3])

    # snapshot ------------------------------------------------------------------------------------------------

    def snapshot(self, as_of: int) -> tuple[dict[str, float | None], dict[str, str]]:
        """Features as of boundary `as_of` (UTC ms, whole second) and the reason for each unavailable one."""
        if as_of % SECOND_MS:
            raise ValueError("as_of must be a whole second")
        t_sec = as_of // SECOND_MS
        if self._max_sec is not None and self._max_sec >= t_sec + FUTURE_SLACK_MS // SECOND_MS:
            raise ValueError("as_of is too far behind the newest event; state has already pruned its lookback")

        features: dict[str, float | None] = {}
        buy, sell, count, buy_count, idx, last_key = self._seconds(t_sec)
        covered = 0 if self._first_ts is None else max(0, t_sec - self._first_ts // SECOND_MS)
        features.update(flow_features(buy, sell, count, covered, buy_count))
        features.update(momentum_features(idx))
        features.update(volatility_features(idx))
        features.update(price_features(idx, None if last_key is None else last_key[0], as_of))
        features["kc_z"] = kc_z(features["known_component"], features["last_price"], features["rv_60"])

        one = self._one_minute(as_of)
        features.update(candle_features(one))
        features.update(trend_features({"1m": one.c, "5m": aggregate(one, 5).c, "15m": aggregate(one, 15).c}))
        features.update(book_features(self._latest_book(t_sec)))
        books = self._books.get
        features.update(ofi_features([books(s) for s in range(t_sec - OFI_LOOKBACK_S, t_sec)]))

        availability = {
            name: UNAVAILABLE_BOOK if name in _BOOK_NAMES else UNAVAILABLE_WARMUP
            for name, value in features.items() if value is None
        }
        return features, availability

    def _seconds(self, t_sec: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, Key | None]:
        """Per-second buy/sell volume, trade and buy-trade counts and the carried-forward index price over
        [T−LOOKBACK_S, T)."""
        start = t_sec - LOOKBACK_S
        carry = self._pruned_trade
        older = [s for s in self._buckets if s < start]
        if older:
            b = self._buckets[max(older)]
            carry = _later(carry, b[4], b[3])
        price = np.nan if carry is None else carry[1]
        last_key = None if carry is None else carry[0]

        buy = np.zeros(LOOKBACK_S)
        sell = np.zeros(LOOKBACK_S)
        count = np.zeros(LOOKBACK_S, dtype=np.int64)
        buy_count = np.zeros(LOOKBACK_S, dtype=np.int64)
        idx = np.empty(LOOKBACK_S)
        get = self._buckets.get
        for i in range(LOOKBACK_S):
            b = get(start + i)
            if b is not None:
                buy[i], sell[i], count[i], buy_count[i] = b[0], b[1], b[2], b[5]
                price, last_key = b[3], b[4]
            idx[i] = price
        return buy, sell, count, buy_count, idx, last_key

    def _one_minute(self, as_of: int) -> Candles:
        """Contiguous completed 1m candles (close ≤ T) over the last 14h; quiet minutes are flat at the prior close."""
        last_start = as_of - as_of % MINUTE_MS - MINUTE_MS
        first_start = last_start - (CANDLE_HISTORY_MIN - 1) * MINUTE_MS
        carry = self._pruned_candle
        older = [m for m in self._candles if m < first_start]
        if older:
            c = self._candles[max(older)]
            carry = _later(carry, c[6], c[3])
        close = None if carry is None else carry[1]

        rows: list[tuple[int, float, float, float, float, float]] = []
        get = self._candles.get
        for m in range(first_start, last_start + 1, MINUTE_MS):
            c = get(m)
            if c is not None:
                rows.append((m, c[0], c[1], c[2], c[3], c[4]))
                close = c[3]
            elif close is not None:
                rows.append((m, close, close, close, close, 0.0))
        if not rows:
            empty = np.empty(0)
            return Candles(np.empty(0, dtype=np.int64), empty, empty, empty, empty, empty)
        arr = np.array([r[1:] for r in rows], dtype=np.float64)
        return Candles(t=np.array([r[0] for r in rows], dtype=np.int64), o=arr[:, 0].copy(), h=arr[:, 1].copy(),
                       l=arr[:, 2].copy(), c=arr[:, 3].copy(), v=arr[:, 4].copy())

    def _latest_book(self, t_sec: int) -> BookSecond | None:
        for s in range(t_sec - 1, t_sec - 1 - BOOK_MAX_AGE_S, -1):
            b = self._books.get(s)
            if b is not None:
                return b
        return None


def _later(current: tuple[Key, float] | None, key: Key, price: float) -> tuple[Key, float]:
    return (key, price) if current is None or key >= current[0] else current
