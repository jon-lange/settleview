"""Rule building blocks shared by signal rules, filters and baselines (KTD10, KTD19)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from btc_signal_engine.domain.models.decisions import Vote

Features = dict[str, float | int | None]
Availability = dict[str, str]


@dataclass(frozen=True, slots=True)
class Param:
    """A tunable parameter with per-horizon defaults and an allowed range."""

    defaults: dict[int, Any]  # horizon seconds -> default
    lo: float | None = None
    hi: float | None = None
    kind: type = float
    choices: tuple[Any, ...] | None = None
    help: str = ""

    def default_for(self, horizon: int) -> Any:
        if horizon in self.defaults:
            return self.defaults[horizon]
        # unlisted horizon: use the nearest configured one
        nearest = min(self.defaults, key=lambda h: abs(h - horizon))
        return self.defaults[nearest]

    def validate(self, name: str, value: Any) -> Any:
        if self.kind is bool:
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be true or false")
            return value
        if self.choices is not None:
            if value not in self.choices:
                raise ValueError(f"{name} must be one of {list(self.choices)}")
            return value
        try:
            v = self.kind(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number") from None
        if (self.lo is not None and v < self.lo) or (self.hi is not None and v > self.hi):
            raise ValueError(f"{name} must be between {self.lo} and {self.hi}")
        return v


@dataclass(frozen=True, slots=True)
class RuleVote:
    vote: Vote
    sentence: str
    conditions: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Context:
    """Per-decision facts that are not snapshot features (supplied by the engine)."""

    horizon: int
    flags: frozenset[str] = field(default_factory=frozenset)  # e.g. backfilled, clock_skew, data_gap
    rv_p95: float | None = None  # trailing 7-day p95 of rv_60
    prev_actual: str | None = None  # previous window's settled direction (baseline)


class Rule(Protocol):
    name: str
    RULE_VERSION: str
    params: dict[str, Param]

    def required(self, p: dict[str, Any], horizon: int) -> set[str]: ...

    def evaluate(self, f: Features, p: dict[str, Any], ctx: Context) -> RuleVote: ...


def buy_share(f: Features, window: int) -> float | None:
    buy, sell = f.get(f"buy_vol_{window}"), f.get(f"sell_vol_{window}")
    if buy is None or sell is None or buy + sell == 0:
        return None
    return buy / (buy + sell)


def pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def abstain(sentence: str, *notes: str) -> RuleVote:
    return RuleVote(Vote.ABSTAIN, sentence, (), tuple(notes))
