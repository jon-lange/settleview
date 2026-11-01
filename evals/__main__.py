"""python -m evals <command>

    run <pipeline.toml> [--trials K] [--live --budget-usd N [--api-key-file F]] [--out DIR]
        run a pipeline (default: the fake model; free, deterministic); exit 1 when the run FAILs
    graders
        list the code graders and run their mutation checks (each must fail its broken trajectory)
    grade <export.jsonl> [--out FILE]
        grade recorded wakes (an evals.export file) with every grader: the live report card's view of history
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from evals import ROOT


def _run(a) -> int:
    from evals.harness.run import RESULTS, execute, load_pipeline, otel_emitter
    path = Path(a.pipeline)
    if not path.exists() and (ROOT / path).exists():
        path = ROOT / path
    p = load_pipeline(path)
    if a.trials is not None:
        if a.trials < 1:
            raise SystemExit("--trials must be at least 1")
        p.trials = a.trials
    if not a.live and a.budget_usd is not None:
        raise SystemExit("--budget-usd is for --live runs only")
    out = execute(p, live=a.live, budget_usd=a.budget_usd, out_dir=Path(a.out) if a.out else RESULTS,
                  key_file=a.api_key_file, otel=otel_emitter())
    print(f"{out['run_id']}: {out['status']} · Adjudication: {out['adjudication']}")
    for cid, v in out["claims"].items():
        print(f"  {cid}: {v}")
    print(f"  results: {out['results']}\n  summary: {out['summary']}")
    return 1 if out["status"] == "FAIL" else 0


def _graders(_a) -> int:
    from evals.graders import GRADERS
    from evals.graders.mutations import validate_graders
    bad = 0
    for name, (ok, ev) in validate_graders().items():
        g = GRADERS[name]
        bad += not ok
        print(f"{'ok  ' if ok else 'REJECTED'} {name} [{g.level}{', safety' if g.safety else ''}]: {ev}")
    return 1 if bad else 0


def _grade(a) -> int:
    from evals.graders import grade
    from evals.harness.items import read_jsonl, recorded_trial
    rows = read_jsonl(Path(a.file))
    counts: Counter = Counter()
    out = []
    for fr in rows:
        v = grade(recorded_trial(fr))
        counts.update((g, x["status"]) for g, x in v.items())
        out.append({"item_id": fr["item_id"], "journal_id": fr["source"]["journal_id"], "verdicts": v})
    for (g, s), n in sorted(counts.items()):
        print(f"{g:34s} {s:15s} {n}")
    if a.out:
        Path(a.out).write_text("".join(json.dumps(x) + "\n" for x in out), encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m evals")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a pipeline")
    r.add_argument("pipeline")
    r.add_argument("--live", action="store_true", help="the real Claude model (costs money; budget enforced)")
    r.add_argument("--budget-usd", type=float, default=None, help="this run's spend limit (live only)")
    r.add_argument("--api-key-file", default=None, help="a file holding the Anthropic key (else ANTHROPIC_API_KEY)")
    r.add_argument("--trials", type=int, default=None, help="k trials per item (default: the pipeline's)")
    r.add_argument("--out", default=None, help="results directory (default evals/results)")
    r.set_defaults(fn=_run)
    g = sub.add_parser("graders", help="list graders and run their mutation checks")
    g.set_defaults(fn=_graders)
    gr = sub.add_parser("grade", help="grade an export file's recorded wakes")
    gr.add_argument("file")
    gr.add_argument("--out", default=None)
    gr.set_defaults(fn=_grade)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
