"""Gap and clock-skew detection for the live feed.

Gaps (trade-ID jumps, socket silence) go to the `gaps` table with `repaired=False`;
U4's repair job fills them from Coinbase trade history. Clock skew
(median `ingest_ts - exchange_ts` over 60s, KTD14) goes to `data_quality` as kind "skew", in ms.
All timestamps come from the caller; nothing here reads the clock.
"""

from __future__ import annotations

from statistics import median

from btc_signal_engine.application.ports import Repository
from btc_signal_engine.domain.models.events import Trade

SILENCE_MS = 15_000
SKEW_WINDOW_MS = 60_000


class GapRecorder:
    def __init__(self, store: Repository, last_trade_id: int | None = None, silence_ms: int = SILENCE_MS) -> None:
        self._store = store
        self._last_id = last_trade_id
        self._last_trade_ts: int | None = None
        self.silence_ms = silence_ms
        self._last_msg: int | None = None
        self._silent = False  # a stale gap was already recorded for the current silence
        self._skew: list[int] = []
        self._skew_start: int | None = None

    def on_trade(self, t: Trade) -> None:
        """Feed every new (deduped) trade: records an ID jump and samples clock skew."""
        if self._last_id is not None and t.trade_id > self._last_id + 1:
            self._store.insert("gaps", [{
                "start_ts": self._last_trade_ts if self._last_trade_ts is not None else t.ts,
                "end_ts": t.ts, "cause": "trade_id_jump",
                "from_trade_id": self._last_id + 1, "to_trade_id": t.trade_id - 1,
                "repaired": False, "created_ts": t.ingest_ts,
            }])
        if self._last_id is None or t.trade_id > self._last_id:
            self._last_id, self._last_trade_ts = t.trade_id, t.ts
        self._sample_skew(t)

    def _sample_skew(self, t: Trade) -> None:
        if self._skew_start is None:
            self._skew_start = t.ingest_ts
        elif t.ingest_ts - self._skew_start >= SKEW_WINDOW_MS:
            self._store.insert("data_quality", [{
                "ts": t.ingest_ts, "kind": "skew", "value": float(median(self._skew)),
                "detail": {"samples": len(self._skew)},
            }])
            self._skew, self._skew_start = [], t.ingest_ts
        self._skew.append(t.ingest_ts - t.ts)

    def on_message(self, now: int) -> None:
        """Any socket message (or a fresh connection) proves the feed is alive."""
        self._last_msg = now
        self._silent = False

    def check_silence(self, now: int) -> bool:
        """True (reconnect now) the first time the socket has been silent longer than the limit."""
        if self._silent or self._last_msg is None or now - self._last_msg <= self.silence_ms:
            return False
        self._silent = True
        self._store.insert("gaps", [{
            "start_ts": self._last_msg, "end_ts": now, "cause": "stale",
            "from_trade_id": None, "to_trade_id": None, "repaired": False, "created_ts": now,
        }])
        return True
