"""One-second aggregator: turns live trades and book updates into per-second rows (KTD4, KTD5, KTD6).

Pure logic driven by explicit timestamps; the caller passes `now` to `flush`.
Second S is final once `now >= S + 1s + lateness_ms`, which leaves room for trades
that arrive slightly late while keeping `ingest_ts` well inside the KTD5 2s budget.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import chain

from btc_signal_engine.domain.models.events import BookSecond, IndexSecond, Trade
from btc_signal_engine.domain.settlement.index import SECOND_MS, index_seconds

_SEEN_KEEP = 10_000  # how many trade IDs below the newest are remembered for dedupe


@dataclass(frozen=True, slots=True)
class BookMetrics:
    """Top of book and depth at one moment; `ts` is when the book state was observed.

    `bid_size` / `ask_size` are BTC resting at the best bid / ask.
    """

    ts: int
    bid: float
    ask: float
    bid_depth_10: float
    ask_depth_10: float
    bid_depth_25: float
    ask_depth_25: float
    bid_size: float | None = None
    ask_size: float | None = None


def book_metrics(bids: Iterable[tuple[float, float]], asks: Iterable[tuple[float, float]], ts: int) -> BookMetrics:
    """Best bid/ask with their sizes and BTC resting within 10 / 25 bps of mid. Levels must be best-first.

    Iteration stops at the first level outside 25 bps, so only the top of the book is read.
    """
    bid_it, ask_it = iter(bids), iter(asks)
    first_bid, first_ask = next(bid_it), next(ask_it)
    bid, ask = float(first_bid[0]), float(first_ask[0])
    mid = (bid + ask) / 2

    def depth(first: tuple[float, float], rest: Iterable[tuple[float, float]], sign: int) -> tuple[float, float]:
        d10 = d25 = 0.0
        for price, size in chain((first,), rest):
            dist = sign * (float(price) - mid) / mid
            if dist > 25e-4:
                break
            d25 += float(size)
            if dist <= 10e-4:
                d10 += float(size)
        return d10, d25

    b10, b25 = depth(first_bid, bid_it, -1)
    a10, a25 = depth(first_ask, ask_it, 1)
    return BookMetrics(ts, bid, ask, b10, a10, b25, a25, float(first_bid[1]), float(first_ask[1]))


class SecondAggregator:
    """Dedupes trades and emits one BookSecond plus the IndexSecond rows per completed second."""

    def __init__(self, carry: IndexSecond | None = None, lateness_ms: int = 1_000, book_stale_ms: int = 5_000) -> None:
        self._carry = carry
        self._lateness = lateness_ms
        self._book_stale = book_stale_ms
        self._next: int | None = None  # first second not yet emitted
        self._trades: dict[int, list[Trade]] = {}
        self._books: dict[int, BookMetrics] = {}
        self._last_book: BookMetrics | None = None
        self._seen: set[int] = set()
        self._max_id: int | None = None
        self._floor = -1  # IDs at or below this are treated as already seen

    def _start(self, ts: int) -> None:
        if self._next is None:
            self._next = ts - ts % SECOND_MS

    def add_trade(self, t: Trade) -> bool:
        """Record a trade; returns False for a duplicate trade ID (which must not be stored again)."""
        if t.trade_id <= self._floor or t.trade_id in self._seen:
            return False
        self._seen.add(t.trade_id)
        self._max_id = t.trade_id if self._max_id is None else max(self._max_id, t.trade_id)
        if len(self._seen) > 2 * _SEEN_KEEP:
            self._floor = self._max_id - _SEEN_KEEP
            self._seen = {i for i in self._seen if i > self._floor}
        self._start(t.ts)
        sec = t.ts - t.ts % SECOND_MS
        if sec >= self._next:  # trades for an already emitted second are stored but too late for the index
            self._trades.setdefault(sec, []).append(t)
        return True

    def add_book(self, m: BookMetrics) -> None:
        self._start(m.ts)
        sec = m.ts - m.ts % SECOND_MS
        if sec >= self._next:
            self._books[sec] = m  # the last observation within the second wins

    def flush(self, now: int) -> list[BookSecond | IndexSecond]:
        """Rows for every second that is complete as of `now`, in time order."""
        out: list[BookSecond | IndexSecond] = []
        if self._next is None:
            return out
        while self._next + SECOND_MS + self._lateness <= now:
            sec = self._next
            self._next += SECOND_MS
            if sec in self._books:
                self._last_book = self._books.pop(sec)
            book = self._last_book
            if book is not None and sec + SECOND_MS - book.ts > self._book_stale:
                book = None
            if book is not None:
                out.append(BookSecond(sec, now, book.bid, book.ask, book.bid_depth_10, book.ask_depth_10,
                                      book.bid_depth_25, book.ask_depth_25, book.bid_size, book.ask_size))
            mids = {sec: (book.bid + book.ask) / 2} if book is not None else None
            rows = index_seconds(self._trades.pop(sec, []), sec, sec + SECOND_MS, carry=self._carry, mids=mids)
            if rows:
                self._carry = rows[-1]
            out.extend(rows)
        return out
