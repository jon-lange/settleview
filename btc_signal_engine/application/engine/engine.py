"""The decision engine (U7, KTD4, KTD11, KTD14).

One code path for live and replay: callers feed `on_event` with trades and per-second
book rows in order, and call `on_boundary(T)` once per whole minute after the lateness
budget has passed. Every minute stores a feature snapshot; each horizon whose boundary
falls on T opens its settlement window and records one decision per deciding ruleset
and per baseline.
"""

from __future__ import annotations

from collections.abc import Callable

from btc_signal_engine.application.ports import Repository
from btc_signal_engine.application.rulesets import RulesetService
from btc_signal_engine.application.settlement import SettlementService
from btc_signal_engine.domain.features import FEATURE_LIB_VERSION
from btc_signal_engine.domain.features.state import FeatureState
from btc_signal_engine.domain.models.decisions import Call
from btc_signal_engine.domain.models.events import BookSecond, Trade
from btc_signal_engine.domain.strategies.base import Context
from btc_signal_engine.domain.strategies.baselines import BASELINES, baseline_call, baseline_hash
from btc_signal_engine.domain.strategies.ruleset import evaluate

FlagsFn = Callable[[int], frozenset[str]]


class Engine:
    def __init__(self, repo: Repository, rulesets: RulesetService, settlement: SettlementService,
                 horizons: tuple[int, ...], *, flags: FlagsFn | None = None,
                 rv_p95: Callable[[], float | None] | None = None) -> None:
        self._repo = repo
        self._rulesets = rulesets
        self._settlement = settlement
        self._horizons = horizons
        self._flags = flags or (lambda _t: frozenset())
        self._rv_p95 = rv_p95 or (lambda: None)
        self.state = FeatureState()
        self._backfilled_seen = False

    def on_event(self, ev) -> None:
        if isinstance(ev, Trade):
            self._backfilled_seen = self._backfilled_seen or ev.backfilled
            self.state.on_trade(ev)
        elif isinstance(ev, BookSecond):
            self.state.on_book(ev)

    def horizons_at(self, t: int) -> list[int]:
        return [h for h in self._horizons if t % (h * 1000) == 0]

    def on_boundary(self, t: int, now: int, *, run_id: int = 0, deciding: dict[int, list[dict]] | None = None,
                    open_windows: bool = True) -> list[dict]:
        """Snapshot at t; decide every horizon whose boundary is t. Returns the decision rows written."""
        features, availability = self.state.snapshot(t)
        backfilled = "book_unavailable" in availability.values()
        self._repo.insert("snapshots", [{"ts": t, "feature_version": FEATURE_LIB_VERSION, "features": features,
                                         "availability": availability, "backfilled": backfilled}])
        rows: list[dict] = []
        flags = self._flags(t)
        rv_p95 = self._rv_p95()
        for h in self.horizons_at(t):
            if open_windows:
                self._settlement.open_window(h, t, features.get("last_price"), now)
            ctx = Context(horizon=h, flags=flags, rv_p95=rv_p95, prev_actual=self._prev_actual(h, t))
            rulesets = deciding[h] if deciding is not None else self._rulesets.deciding(h)
            for rs in rulesets:
                ev = evaluate(rs["spec"], features, availability, ctx)
                rows.append(self.decision_row(h, t, rs["hash"], ev.call, ev.explanation, ev.conditions, ev.flags,
                                      features, run_id, now))
            for name in BASELINES:
                call, sentence = baseline_call(name, features, ctx)
                rows.append(self.decision_row(h, t, baseline_hash(name), call, sentence, (), (), features, run_id, now))
        self._repo.insert("decisions", rows)
        return rows

    def record_missed(self, t: int, now: int) -> list[dict]:
        """A boundary passed while the app was down or asleep: write MISSED, never compute late."""
        rows = []
        for h in self.horizons_at(t):
            for rs in self._rulesets.deciding(h):
                rows.append(self.decision_row(h, t, rs["hash"], Call.MISSED, "The app was not running when this window opened.",
                                      (), ("missed",), {}, 0, now))
        self._repo.insert("decisions", rows)
        return rows

    def _prev_actual(self, h: int, t: int) -> str | None:
        prev = self._repo.select("windows", {"horizon": h, "boundary": t - h * 1000})
        return prev[0]["actual"] if prev else None

    @staticmethod
    def decision_row(h, t, rs_hash, call, explanation, conditions, flags, features, run_id, now) -> dict:
        return {"horizon": h, "boundary": t, "ruleset_hash": rs_hash, "run_id": run_id,
                "call": Call(call).value, "explanation": explanation, "conditions": list(conditions),
                "flags": list(flags), "snapshot_ts": t, "reference_price": features.get("open_avg"),
                "created_ts": now}
