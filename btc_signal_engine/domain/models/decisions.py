"""Decision, pick and outcome value types shared by every layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Call(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    SKIP = "SKIP"
    MISSED = "MISSED"


class Vote(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    ABSTAIN = "ABSTAIN"


class Result(StrEnum):
    PENDING = "PENDING"
    WIN = "WIN"
    LOSS = "LOSS"
    TIE = "TIE"
    UNRESOLVED = "UNRESOLVED"
    NOT_SCORED = "NOT_SCORED"


class ScoreSource(StrEnum):
    PROXY = "proxy"
    KALSHI = "kalshi"


@dataclass(frozen=True, slots=True)
class Decision:
    """One ruleset's call for one horizon window starting at `boundary`."""

    horizon: int
    boundary: int
    ruleset_hash: str
    call: Call
    explanation: str
    conditions: tuple[str, ...] = ()
    snapshot_ts: int | None = None
    reference_price: float | None = None
    flags: tuple[str, ...] = ()
    run_id: int = 0  # 0 = live; >0 = backtest run

    @property
    def settles_at(self) -> int:
        return self.boundary + self.horizon * 1000


@dataclass(frozen=True, slots=True)
class Settlement:
    """The KTD6 averages for one window. Either average may be missing (coverage rule)."""

    horizon: int
    boundary: int
    open_avg: float | None
    close_avg: float | None

    @property
    def resolvable(self) -> bool:
        return self.open_avg is not None and self.close_avg is not None


@dataclass(frozen=True, slots=True)
class Outcome:
    result: Result
    actual: Call | None  # UP / DOWN that actually happened, None when unresolved
    source: ScoreSource | None
    tie: bool = False
    detail: dict = field(default_factory=dict)
