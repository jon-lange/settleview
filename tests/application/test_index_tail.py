"""application/index_tail.IndexTail: the recent per-second index read incrementally answers exactly what the store
answers (rows rewritten inside the re-read window, rows landing late, old ranges, threads), while reading only the
newest seconds on a warm call. Every price here is SYNTHETIC test data."""

from __future__ import annotations

import random
import threading

from btc_signal_engine.application.index_tail import IndexTail
from btc_signal_engine.domain.models.events import IndexSecond

T0 = 1_800_000_000_000


class Counted:
    def __init__(self, store) -> None:
        self.store, self.rows, self.calls = store, 0, 0

    def index_range(self, start, end):
        got = self.store.index_range(start, end)
        self.rows += len(got)
        self.calls += 1
        return got


def put(store, start, end, price=lambda ts: 80_000.0 + (ts // 1000) % 7):
    store.add_index_seconds([IndexSecond(ts, price(ts), ts, None) for ts in range(start, end, 1000)])


def test_answers_like_the_store_through_rewrites_late_rows_and_old_ranges(store):
    put(store, T0, T0 + 2_000_000)
    counted = Counted(store)
    tail = IndexTail(counted)
    rng = random.Random(7)
    now = T0 + 1_900_000
    for step in range(200):
        now += 500
        if step % 2 == 0:
            put(store, now - 1000, now)  # the newest second lands
        if step % 7 == 0:  # a second inside the re-read window is rewritten (a late trade)
            put(store, now - 4000, now - 3000, price=lambda ts, v=1.0 + step: v)
        req = rng.choice([(now - 1_860_000, now), (now - 600_000, now + 1), (T0 + 1_000, T0 + 900_000)])
        assert tail.index_range(*req) == store.index_range(*req), (step, req)


def test_a_warm_call_reads_only_the_newest_seconds(store):
    put(store, T0, T0 + 2_000_000)
    counted = Counted(store)
    tail = IndexTail(counted)
    at = T0 + 1_990_000
    tail.index_range(at - 1_860_000, at)
    first = counted.rows
    assert first >= 1_800
    for _ in range(10):
        at += 1000
        put(store, at - 1000, at)
        before = counted.rows
        assert tail.index_range(at - 1_860_000, at) == store.index_range(at - 1_860_000, at)
        assert counted.rows - before <= 11  # the 10 s re-read, never the 31 minutes


def test_a_row_landing_later_than_the_reread_is_picked_up_by_the_full_refresh(store):
    put(store, T0, T0 + 100_000)
    put(store, T0 + 101_000, T0 + 200_000)  # second T0 + 100 s missing (a writer backlog)
    tail = IndexTail(store, reread_ms=10_000, full_every_ms=60_000)
    assert len(tail.index_range(T0, T0 + 200_000)) == 199
    put(store, T0 + 100_000, T0 + 101_000)  # it lands, 100 s late
    assert len(tail.index_range(T0, T0 + 210_000)) == 199  # within the minute: not yet seen (the documented bound)
    assert len(tail.index_range(T0, T0 + 261_000)) == 200  # the minute's full read has it


def test_threads_and_the_loop_share_it_safely(store):
    put(store, T0, T0 + 3_000_000)
    tail = IndexTail(store)
    want = store.index_range(T0, T0 + 3_000_000)
    errors = []

    def worker(k):
        try:
            for i in range(30):
                end = T0 + 2_800_000 + (i * 7 + k) % 100 * 1000
                got = tail.index_range(end - 1_860_000, end)
                assert got == [r for r in want if end - 1_860_000 <= r.ts < end]
        except Exception as e:  # noqa: BLE001 - reported below
            errors.append(e)
    ts = [threading.Thread(target=worker, args=(k,)) for k in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == []
