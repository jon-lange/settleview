"""Per-second settlement index (KTD6): last Coinbase trade at or before each second, carried forward.

Identical for live and backfilled data. The BBO mid is attached only as a diagnostic.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from btc_signal_engine.domain.models.events import IndexSecond, Trade

SECOND_MS = 1000


def index_seconds(
    trades: Iterable[Trade],
    start: int,
    end: int,
    carry: IndexSecond | None = None,
    mids: Mapping[int, float] | None = None,
) -> list[IndexSecond]:
    """Index rows for every whole second in [start, end).

    `trades` may arrive in any order; the latest by (ts, trade_id) within each second wins.
    `carry` is the last index row before `start`, used for seconds with no trade.
    Seconds before any known price are omitted.
    """
    if start % SECOND_MS or end % SECOND_MS:  # an explicit check: `python -O` drops asserts
        raise ValueError("index bounds must be whole seconds")
    last_in_second: dict[int, Trade] = {}
    for t in trades:
        if not start <= t.ts < end:
            continue
        sec = t.ts - t.ts % SECOND_MS
        cur = last_in_second.get(sec)
        if cur is None or (t.ts, t.trade_id) > (cur.ts, cur.trade_id):
            last_in_second[sec] = t

    price = carry.price if carry else None
    last_ts = carry.last_trade_ts if carry else None
    out: list[IndexSecond] = []
    for sec in range(start, end, SECOND_MS):
        t = last_in_second.get(sec)
        if t is not None:
            price, last_ts = t.price, t.ts
        if price is None:
            continue
        out.append(IndexSecond(ts=sec, price=price, last_trade_ts=last_ts, mid=(mids or {}).get(sec)))
    return out
