"""The pipeline runner: load a pipeline (pipelines/*.toml), its item sets, run k trials per item in each item's mode
with the fake model (default) or the live one, grade every trial, run the item mutants and the grader mutation
checks, write `results/<pipeline>-<ts>.jsonl` and the summary Markdown, and enforce the API budget.

A pipeline file:
    name = "pr"
    description = "..."
    model = "fake"            # fake | live (a live pipeline refuses to run without --live: never a silent fake)
    trials = 1                # k
    sets = ["golden", "adversarial"]
    seed = 20261031
    mutants = true            # run the items' [[mutants]] (fake model only) and require each caught
    budget_usd = 0.0          # the default --budget-usd of a live run
    est_usd_per_trial = 0.012 # optional: overrides the per-model estimate
    scheduled = "ci"          # ci | "not scheduled yet"
    [[claims]]                # what the run is evidence for; each gets an Adjudication line
    id = "conduct"
    text = "..."
    kind = "items_pass" | "safety_pass_k" | "graders_validated" | "mutants_caught"
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

from evals import ROOT
from evals.graders import FAIL, GRADERS, NA, grade
from evals.graders.mutations import validate_graders
from evals.harness import budget, frozen, sim
from evals.harness.items import DATASETS, Item, ItemError, load_set, read_jsonl
from evals.harness.report import summary

RESULTS = ROOT / "results"
CLAIM_KINDS = ("items_pass", "safety_pass_k", "graders_validated", "mutants_caught")


@dataclass
class Pipeline:
    name: str
    path: Path
    description: str = ""
    model: str = "fake"
    trials: int = 1
    sets: list[str] = field(default_factory=list)
    seed: int = 0
    mutants: bool = False
    budget_usd: float = 0.0
    est_usd_per_trial: float | None = None
    scheduled: str = "not scheduled yet"
    claims: list[dict] = field(default_factory=list)
    bars: dict = field(default_factory=dict)


def load_pipeline(path: Path) -> Pipeline:
    d = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    if d.get("model", "fake") not in ("fake", "live"):
        raise ValueError(f"{path}: model must be fake or live (got {d.get('model')!r})")
    for c in d.get("claims", []):
        if c.get("kind") not in CLAIM_KINDS:
            raise ValueError(f"{path}: claim {c.get('id')!r} has unknown kind {c.get('kind')!r}; kinds: {CLAIM_KINDS}")
    if int(d.get("trials", 1)) < 1:
        raise ValueError(f"{path}: trials must be at least 1")
    return Pipeline(name=d["name"], path=Path(path), description=d.get("description", ""), model=d.get("model", "fake"),
                    trials=int(d.get("trials", 1)), sets=list(d.get("sets", [])), seed=int(d.get("seed", 0)),
                    mutants=bool(d.get("mutants", False)), budget_usd=float(d.get("budget_usd", 0.0)),
                    est_usd_per_trial=d.get("est_usd_per_trial"),
                    scheduled=str(d.get("scheduled", "not scheduled yet")),
                    claims=list(d.get("claims", [])), bars=dict(d.get("bars", {})))


def frame_item(frame: dict, set_name: str, path: Path) -> Item:
    meta = frame.get("meta") or {}
    return Item(id=frame["item_id"], title=f"replay of journal {frame['source']['journal_id']}",
                category=f"replay/{meta.get('primary_trigger')}", set=set_name, source="real", mode="frozen",
                path=path, frame=frame)


def load_items(p: Pipeline, datasets: Path = DATASETS) -> list[Item]:
    items: list[Item] = []
    for s in p.sets:
        d = datasets / s
        items += load_set(d)
        for f in sorted(d.glob("*.jsonl")):  # replay / holdout: exported wakes, each a frozen item
            items += [frame_item(fr, s, f) for fr in read_jsonl(f)]
    ids = [i.id for i in items]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise ItemError(f"duplicate item ids across sets: {dup}")
    return items


def seed_for(base: int, item_id: str, trial: int) -> int:
    return int(hashlib.sha256(f"{base}:{item_id}:{trial}".encode()).hexdigest()[:8], 16)


def run_trial(item: Item, model, trial: int, seed: int, turns: list[dict] | None = None) -> dict:
    if item.mode == "frozen":
        return asyncio.run(frozen.run(item, model, trial=trial, seed=seed))
    return sim.run(item, model, trial=trial, seed=seed, turns=turns)


def run_live_trial(item: Item, model, trial: int, seed: int) -> dict:
    """A live trial; a model outage is retried once (the trial is then marked: a retry makes the run DEGRADED)."""
    from btc_signal_engine.application.ports import LlmUnavailable
    try:
        return run_trial(item, model, trial, seed)
    except LlmUnavailable:
        t = run_trial(item, model, trial, seed)
        t["retries"] = int(t.get("retries", 0)) + 1
        return t


def judge(item: Item, trial: dict, *, fake: bool, valid: set[str]) -> dict:
    """Grade one trial against the item's expectations. Only validated graders decide; listed graders must apply."""
    verdicts = grade(trial)
    problems = []
    for g in item.graders:
        if verdicts[g]["status"] == NA:
            problems.append(f"{g}: expected to apply, got not_applicable ({verdicts[g]['evidence']})")
    for g, v in verdicts.items():
        if g not in valid:
            continue
        want = item.recorded_verdicts.get(g, "pass") if fake and item.mode == "frozen" else "pass"
        if v["status"] == FAIL and want != "fail":
            problems.append(f"{g}: {v['evidence']}")
        elif want == "fail" and v["status"] != FAIL:
            problems.append(f"{g}: the recorded run is a known failure, the grader now says {v['status']}")
    outcome = "diverged" if trial.get("diverged") else ("pass" if not problems else "fail")
    return {"verdicts": verdicts, "problems": problems, "outcome": outcome}


def run_mutants(item: Item, valid: set[str]) -> list[dict]:
    out = []
    for i, m in enumerate(item.mutants):
        t = sim.run(item, None, trial=i, seed=0, turns=m["turns"]) if item.mode == "simulated" else None
        if t is None:
            continue
        v = grade(t)
        missed = [g for g in m["breaks"] if v[g]["status"] != FAIL]
        out.append({"kind": "mutant", "item_id": item.id, "mutant": m["name"], "breaks": m["breaks"],
                    "caught": not missed, "missed": missed,
                    "evidence": {g: v[g]["evidence"] for g in m["breaks"]},
                    "layers": {g: v[g].get("layer") for g in m["breaks"]},
                    "tools": [(c["name"], c.get("code")) for c in t["tools"]]})
    return out


def _trial_line(run_id: str, p: Pipeline, item: Item, t: dict, j: dict) -> dict:
    decs = [c.get("args") or {} for c in t["tools"] if c["name"] == "record_decision" and c.get("ok")]
    return {"kind": "trial", "run_id": run_id, "pipeline": p.name, "item_id": item.id, "set": item.set,
            "category": item.category, "safety": item.safety, "source": item.source, "trial": t["trial"],
            "seed": t["seed"], "mode": t["mode"], "subject": t.get("subject"), "model": t["model"],
            "status": t["status"], "outcome": j["outcome"], "problems": j["problems"], "verdicts": j["verdicts"],
            "turns": t.get("turns"), "est_usd": t.get("est_usd"), "diverged": t.get("diverged"),
            "retries": t.get("retries", 0), "fallbacks": t.get("fallbacks", []),
            "fidelity": t.get("fidelity"), "tools": [[c["name"], c.get("ok"), c.get("code")] for c in t["tools"]],
            "decision_actions": [d.get("action") for d in decs],
            "end_session": any(c["name"] == "end_session" and c.get("ok") for c in t["tools"])}


def execute(p: Pipeline, *, live: bool = False, budget_usd: float | None = None, out_dir: Path = RESULTS,
            datasets: Path = DATASETS, model_factory=None, key_file: str | None = None, otel=None) -> dict:
    """Run a pipeline; returns {run_id, status, adjudication, results, summary paths}."""
    if p.model == "live" and not live:
        raise SystemExit(f"pipeline {p.name!r} runs the live model: pass --live and --budget-usd (never a silent "
                         "fake run)")
    items = load_items(p, datasets)
    ts = datetime.now(UTC)
    run_id = f"{p.name}-{ts.strftime('%Y%m%dT%H%M%SZ')}"
    checks = validate_graders()
    valid = {g for g, (ok, _) in checks.items() if ok}
    model = None
    spend_budget = budget_usd if budget_usd is not None else p.budget_usd
    est = 0.0
    if live:
        est = budget.estimate([i.model for i in items for _ in range(p.trials)], p.est_usd_per_trial)
        budget.check(est, spend_budget)
        from evals.harness.models import live_model
        model = model_factory() if model_factory else live_model(out_dir / "cli", key_file=key_file)
        budget.record(run_id, p.name, "reserve", est)
    lines: list[dict] = []
    spent = 0.0
    skipped = 0
    per_trial = (est / max(1, len(items) * p.trials)) if live else 0.0
    try:
        for item in items:
            for k in range(p.trials):
                if live and spent + per_trial > spend_budget + 1e-9:
                    skipped += 1
                    lines.append({"kind": "trial", "run_id": run_id, "pipeline": p.name, "item_id": item.id,
                                  "set": item.set, "category": item.category, "safety": item.safety, "trial": k,
                                  "outcome": "skipped_budget", "problems": [], "verdicts": {}})
                    continue
                seed = seed_for(p.seed, item.id, k)
                t = run_live_trial(item, model, k, seed) if live else run_trial(item, model, k, seed)
                spent += float(t.get("est_usd") or 0.0) if live else 0.0
                j = judge(item, t, fake=not live, valid=valid)
                line = _trial_line(run_id, p, item, t, j)
                lines.append(line)
                if otel is not None:
                    otel(line)
        if p.mutants and not live:
            for item in items:
                lines += [{"run_id": run_id, **m} for m in run_mutants(item, valid)]
    finally:
        if live:
            budget.record(run_id, p.name, "actual", spent)
    header = {"kind": "run", "run_id": run_id, "pipeline": p.name, "pipeline_file": str(p.path.name),
              "subject": "live" if live else "fake", "trials": p.trials, "seed": p.seed, "items": len(items),
              "sets": p.sets, "estimate_usd": est, "spent_usd": round(spent, 6), "skipped_budget": skipped,
              "graders": {g: {"valid": ok, "evidence": ev, "safety": GRADERS[g].safety, "level": GRADERS[g].level}
                          for g, (ok, ev) in checks.items()},
              "started": ts.isoformat(), "ended": datetime.now(UTC).isoformat()}
    out_dir.mkdir(parents=True, exist_ok=True)
    res = out_dir / f"{run_id}.jsonl"
    with res.open("w", encoding="utf-8", newline="\n") as f:
        for x in [header, *lines]:
            f.write(json.dumps(x, ensure_ascii=False, default=str) + "\n")
    md, verdict = summary(p, header, lines, items)
    (out_dir / f"{run_id}.md").write_text(md, encoding="utf-8")
    return {"run_id": run_id, "results": res, "summary": out_dir / f"{run_id}.md", **verdict}


def otel_emitter() -> Any:
    """`gen_ai.evaluation.result`-style log events when an OTLP endpoint is configured (bootstrap telemetry; no new
    package), else None. Names, labels and ids only: never a prompt, an argument or an evidence string."""
    import logging
    import os
    if not any(os.environ.get(k) for k in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT")):
        return None
    from btc_signal_engine.bootstrap import telemetry
    tel = telemetry.setup("settleview-evals", instrument=False)
    if not tel.enabled:
        return None
    from opentelemetry.sdk._logs import LoggingHandler
    lg = logging.getLogger("settleview.evals")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    lg.addHandler(LoggingHandler())

    def emit(line: dict) -> None:
        for g, v in (line.get("verdicts") or {}).items():
            lg.info("gen_ai.evaluation.result", extra=evaluation_attributes(line, g, v))
    emit.telemetry = tel  # type: ignore[attr-defined]
    return emit


def evaluation_attributes(line: dict, grader_name: str, verdict: dict) -> dict:
    return {"event.name": "gen_ai.evaluation.result", "gen_ai.evaluation.name": grader_name,
            "gen_ai.evaluation.score.label": verdict["status"],
            "gen_ai.evaluation.score.value": {"pass": 1.0, "fail": 0.0}.get(verdict["status"], -1.0),
            "settleview.eval.run_id": line["run_id"], "settleview.eval.item_id": line["item_id"],
            "settleview.eval.trial": line["trial"], "settleview.eval.outcome": line["outcome"],
            "gen_ai.request.model": line.get("model") or "", "settleview.eval.subject": line.get("subject") or ""}
