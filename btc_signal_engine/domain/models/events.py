"""Normalized market events. All timestamps are integer UTC milliseconds."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Trade:
    """One exchange trade. `buy` is True when the aggressor (taker) bought."""

    trade_id: int
    ts: int
    ingest_ts: int
    price: float
    size: float
    buy: bool
    backfilled: bool = False


@dataclass(frozen=True, slots=True)
class BookSecond:
    """Per-second best bid/ask and depth metrics, emitted once per second by the aggregator.

    `ts` is the start of the second; `ingest_ts` is when the aggregate was emitted (KTD5).
    Depth is BTC resting within 10 / 25 basis points of the mid on each side.
    `bid_size` / `ask_size` are BTC resting at the best bid / ask (None for rows recorded before
    they were captured, migration 0004); order-flow imbalance and microprice need them.
    """

    ts: int
    ingest_ts: int
    bid: float
    ask: float
    bid_depth_10: float
    ask_depth_10: float
    bid_depth_25: float
    ask_depth_25: float
    bid_size: float | None = None
    ask_size: float | None = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def imbalance_10(self) -> float:
        total = self.bid_depth_10 + self.ask_depth_10
        return 0.0 if total == 0 else (self.bid_depth_10 - self.ask_depth_10) / total


@dataclass(frozen=True, slots=True)
class IndexSecond:
    """Settlement index price for one second (KTD6).

    `price` is the last trade at or before the end of the second, carried forward;
    `last_trade_ts` is when that trade happened, used for the 5-second staleness rule.
    """

    ts: int
    price: float
    last_trade_ts: int
    mid: float | None = None


MarketEvent = Trade | BookSecond
