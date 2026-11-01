"""The eval API budget: $300 a calendar month (user, 2026-10-31), enforced by the runner.

Every live run appends to `evals/results/spend.jsonl` (gitignored; one JSON object a line): a `reserve` line with the
run's estimate before the first trial and an `actual` line with what it really cost when it ends. The month's spend
is the sum of `actual` lines plus the `reserve` of any run that never wrote one (a crashed run counts at its
estimate). A run refuses to start when its estimate exceeds its own `--budget-usd`, or when the month's spend plus
the estimate exceeds the cap; during the run it stops before a trial that would take it past `--budget-usd`
(the remaining trials are `skipped_budget`, and the run is DEGRADED). Fake-model runs cost nothing and write nothing.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, UTC
from pathlib import Path

from evals import ROOT

MONTH_CAP_USD = 300.0
SPEND_FILE = ROOT / "results" / "spend.jsonl"
# Conservative per-trial estimates (about 3x the journal's p90 wake: Haiku $0.0035, Opus $0.13; 2026-10-31 inventory)
PER_TRIAL_USD = {"claude-haiku-5-5": 0.012, "claude-sonnet-5-5": 0.06, "claude-opus-5-5": 0.40}


class BudgetError(RuntimeError):
    """The run would break its own budget or the month's cap; nothing was spent."""


def month_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m")


def _lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def month_spent(month: str | None = None, path: Path | None = None) -> float:
    month = month or month_of(time.time())
    rows = [r for r in _lines(path or SPEND_FILE) if r.get("month") == month]
    actual = {r["run_id"]: float(r["usd"]) for r in rows if r.get("kind") == "actual"}
    reserved = {r["run_id"]: float(r["usd"]) for r in rows if r.get("kind") == "reserve"}
    return round(sum(actual.values()) + sum(v for k, v in reserved.items() if k not in actual), 6)


def estimate(models: list[str], per_trial: float | None = None) -> float:
    """The run's estimate: one entry per trial (its model)."""
    total = 0.0
    for m in models:
        if per_trial is not None:
            total += per_trial
        elif m in PER_TRIAL_USD:
            total += PER_TRIAL_USD[m]
        else:
            raise BudgetError(f"no cost estimate for model {m!r}; set est_usd_per_trial in the pipeline")
    return round(total, 6)


def check(est: float, budget_usd: float, *, cap: float = MONTH_CAP_USD, path: Path | None = None,
          now: float | None = None) -> float:
    """Refuse a run that does not fit; returns the month's spend so far."""
    if budget_usd <= 0:
        raise BudgetError("a live run needs --budget-usd above $0")
    if est > budget_usd + 1e-9:
        raise BudgetError(f"the run's estimate ${est:.2f} is over its budget ${budget_usd:.2f}")
    spent = month_spent(month_of(now or time.time()), path)
    if spent + est > cap + 1e-9:
        raise BudgetError(f"the month's eval spend ${spent:.2f} + this run's estimate ${est:.2f} is over the "
                          f"${cap:.0f} monthly cap")
    return spent


def record(run_id: str, pipeline: str, kind: str, usd: float, *, path: Path | None = None,
           now: float | None = None) -> None:
    if kind not in ("reserve", "actual"):
        raise ValueError(f"unknown spend kind {kind!r}")
    ts = now or time.time()
    path = path or SPEND_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": round(ts, 3), "month": month_of(ts), "run_id": run_id, "pipeline": pipeline,
                            "kind": kind, "usd": round(usd, 6)}) + "\n")
