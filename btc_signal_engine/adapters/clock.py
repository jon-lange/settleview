"""System clock corrected to exchange time.

A dev host's clock was measured 1.2–1.9s behind Coinbase. Boundaries, lateness and
skew checks must run on exchange time, so the clock learns its offset from live trades:
offset = median(exchange_ts − local receive time) over recent trades. The result tracks
exchange time minus typical network latency, i.e. what a well-synced machine would see.
"""

from __future__ import annotations

import statistics
import time
from collections import deque


def _system_ms() -> int:
    return time.time_ns() // 1_000_000


class ExchangeClock:
    def __init__(self, window: int = 500, system=_system_ms) -> None:
        self._samples: deque[int] = deque(maxlen=window)
        self._system = system
        self._offset = 0

    def observe(self, exchange_ts: int) -> None:
        self._samples.append(exchange_ts - self._system())
        if len(self._samples) % 25 == 0 or len(self._samples) < 25:
            self._offset = int(statistics.median(self._samples))

    @property
    def offset_ms(self) -> int:
        return self._offset

    def now_ms(self) -> int:
        return self._system() + self._offset
