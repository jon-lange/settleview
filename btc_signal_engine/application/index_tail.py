"""The recent per-second BTC index, read incrementally (review fix plan 2026-10-31 P1, B5#7).

The Coach's tick (twice a second), the Play tabs' fair odds and "price to beat" chart (every phone poll, every agent
fleet tick) and the Coach's odds series each asked the store for up to 46 minutes of `index_1s` (about 2,800 rows) on
every call. `IndexTail.index_range` answers the same question from a cache of the newest rows and reads only what may
have changed since its last read:

- a request that ends at or after the cache's end minus `reread_ms` (a recent one) reads [cache end - `reread_ms`,
  end) and replaces those rows: an index second can still be rewritten (a late trade inside the aggregator's
  lateness budget) or land late (the writer flushes every few seconds), so the last `reread_ms` are always read
  again;
- every `full_every_ms` (by the requests' end times) the whole range is read again, so a row that landed later than
  `reread_ms` (a writer backlog after a database outage) is picked up within that time;
- a request reaching back before the cache's start, or an old range (another window's series), is read from the store
  as before (an older start re-seeds the cache).

The cache keeps the newest `keep_ms` (50 minutes: the Coach series' 31-minute history before a 15-minute window). It is
shared by the event loop and the sync routes' worker threads: the store read runs outside the lock, the merge inside.
The answer is exactly what `MarketStore.index_range(start, end)` returns, ts-sorted, start <= ts < end.
"""

from __future__ import annotations

import bisect
import threading

from btc_signal_engine.application.ports import MarketStore
from btc_signal_engine.domain.models.events import IndexSecond

REREAD_MS = 10_000
FULL_EVERY_MS = 60_000
KEEP_MS = 50 * 60_000


class IndexTail:
    def __init__(self, market: MarketStore, *, reread_ms: int = REREAD_MS, full_every_ms: int = FULL_EVERY_MS,
                 keep_ms: int = KEEP_MS) -> None:
        self._market = market
        self.reread_ms, self.full_every_ms, self.keep_ms = int(reread_ms), int(full_every_ms), int(keep_ms)
        self._lock = threading.Lock()
        self._rows: list[IndexSecond] = []  # ts-sorted, covering [self._lo, self._hi)
        self._ts: list[int] = []
        self._lo = self._hi = 0
        self._full_at: int | None = None  # the end of the last full read
        self.reads = 0  # store reads made (tests, the perf budget)

    def index_range(self, start: int, end: int) -> list[IndexSecond]:
        if end <= start:
            return []
        with self._lock:
            if self._full_at is not None and end < self._hi - self.reread_ms:
                old = True  # another window's range: the store, and the cache stays as it is
            else:
                old = False
                cached = self._full_at is not None and start >= self._lo
                full = not cached or end - self._full_at >= self.full_every_ms
                read_from = start if full else max(start, self._hi - self.reread_ms)
        if old:
            self.reads += 1
            return list(self._market.index_range(start, end))
        self.reads += 1
        got = self._market.index_range(read_from, end)
        with self._lock:
            if full or read_from < self._lo or read_from > self._hi:  # (re)seed with what was read
                self._rows, self._lo, self._hi = list(got), read_from, end
                self._full_at = end
            elif end >= self._hi - self.reread_ms:  # contiguous with the cache: [read_from, end) replaced
                i = bisect.bisect_left(self._ts, read_from)
                j = bisect.bisect_left(self._ts, end)
                self._rows = [*self._rows[:i], *got, *self._rows[j:]]
                self._hi = max(self._hi, end)
            # else: another thread already cached newer rows; they answer
            self._ts = [r.ts for r in self._rows]
            floor = self._hi - self.keep_ms
            if self._lo < floor:
                k = bisect.bisect_left(self._ts, floor)
                del self._rows[:k], self._ts[:k]
                self._lo = floor
            if start >= self._lo:
                i = bisect.bisect_left(self._ts, start)
                j = bisect.bisect_left(self._ts, end)
                return self._rows[i:j]
        # only under a concurrent re-seed or trim: what was read covers the request when it started at `start`
        return list(got) if read_from == start else list(self._market.index_range(start, end))
