"""The error-analysis pack (research recommendation #1: read ~100 real wakes before writing any judge).

    python -m evals.review --source <sqlite file | postgres url> [--n 100] [--seed 7] [--out evals/review]
    python -m evals.review --tally evals/review/labels-<name>.csv

Builds a stratified sample of real wakes from the journal (read-only, through the same export and redaction as
evals.export, and the same deny-list scan: the pack fails rather than show anything the scan flags), and writes:
- `review-<name>.md`: one section per wake: what woke it, what it saw (summarised), each tool call and its result,
  its decision and reason, what it told the user, the outcome, and blank `First failure:` / `Pass/fail:` /
  `Category:` lines;
- `labels-<name>.csv`: the sheet you fill in (pass_fail, first_failure, failure_category, notes), one row a wake;
- `items-<name>.jsonl`: the sampled items themselves (to join labels to later, e.g. as judge calibration data).
`evals/review/` is gitignored: the pack is redacted, but it is your own trading history.

Sampling (the inventory's strata): quotas by primary trigger in proportion to the pool, every trigger at least 2
(or all it has), settled and rule-entry wakes oversampled 1.5x; at most 2 wakes per (session, 15-minute window);
then swaps so that every day, market, decision action and outcome (loss / win / none) in the pool appears at least
once. The seed makes it repeatable.

`--tally` reads a filled-in sheet and prints the failure categories by count (open coding -> the first failure
modes to write graders for).
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

from evals import ROOT
from evals.export import ExportScanError, export_items, load, open_engine, wakes, write_jsonl
from evals.redact import Redactor

OUT = ROOT / "review"
OVERSAMPLE = {"settled": 1.5, "rule_entry": 1.5}
LABEL_COLUMNS = ("n", "item_id", "journal_id", "local_time", "primary_trigger", "market", "decision", "outcome",
                 "pass_fail", "first_failure", "failure_category", "notes")


def _window(it: dict) -> tuple:
    return (it["source"]["session"], it["meta"]["ts"] // 900_000)


def outcome_class(it: dict) -> str:
    trades = (it.get("outcome") or {}).get("trades") or []
    if any(t.get("won") is False for t in trades):
        return "loss"
    if any(t.get("won") for t in trades):
        return "win"
    return "none"


def _keys(it: dict) -> dict[str, str]:
    m = it["meta"]
    return {"day": str(m.get("day")), "market": str(m.get("decision_market") or ",".join(m.get("markets") or [])),
            "decision": str(m.get("decision_action")), "outcome": outcome_class(it)}


def stratified(items: list[dict], n: int, seed: int) -> list[dict]:
    rng = random.Random(seed)  # noqa: S311 - reproducible sampling of the review pack, not cryptography
    by: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        by[str(it["meta"].get("primary_trigger"))].append(it)
    for v in by.values():
        rng.shuffle(v)
    total = len(items)
    want = {k: max(min(2, len(v)), round(n * len(v) / total * OVERSAMPLE.get(k, 1.0))) for k, v in by.items()}
    while sum(want.values()) > n:  # trim the largest quota first
        k = max(want, key=lambda x: (want[x] - min(2, len(by[x])), want[x]))
        want[k] -= 1
    picked: list[dict] = []
    per_window: Counter = Counter()
    for k, v in sorted(by.items()):
        took = 0
        for it in v:
            if took >= want[k]:
                break
            if per_window[_window(it)] >= 2:
                continue
            picked.append(it)
            per_window[_window(it)] += 1
            took += 1
    rest = [it for it in items if it not in picked]
    rng.shuffle(rest)
    while len(picked) < min(n, total) and rest:  # fill to n with random wakes (keep some random traces)
        it = rest.pop()
        if per_window[_window(it)] < 2:
            picked.append(it)
            per_window[_window(it)] += 1
    # coverage: every day / market / decision / outcome value in the pool appears at least once
    for dim in ("day", "market", "decision", "outcome"):
        have = {_keys(it)[dim] for it in picked}
        for val in sorted({_keys(it)[dim] for it in items} - have):
            cand = next((it for it in items if _keys(it)[dim] == val and it not in picked), None)
            if cand is None:
                continue
            counts = Counter(str(p["meta"].get("primary_trigger")) for p in picked)
            drop = max((p for p in picked if counts[str(p["meta"].get("primary_trigger"))] > 2),
                       key=lambda p: counts[str(p["meta"].get("primary_trigger"))], default=None)
            if drop is not None and len(picked) >= n:
                picked.remove(drop)
            picked.append(cand)
    return sorted(picked, key=lambda it: it["meta"]["ts"])


def _short(v, n: int = 160) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _args(name: str, a: dict) -> str:
    a = a or {}
    if name == "record_decision":
        keys = ("market", "action", "side", "p_estimate", "market_p", "p_source", "pass_reason")
        return ", ".join(f"{k}={a.get(k)}" for k in keys)
    if name == "update_notes":
        return f"text ({len(a.get('text') or '')} chars): {_short(a.get('text') or '', 140)}"
    return _short(a, 160)


def _result(c: dict) -> str:
    if c.get("ok"):
        return "ok" + (f" — {_short(c.get('result'), 120)}" if c.get("name") not in ("update_notes",) else "")
    return f"**{c.get('code')}** — {_short(c.get('result'), 160)}"


def _event(e: dict) -> str:
    where = "/".join(x for x in (e.get("venue"), e.get("span")) if x)
    val = e.get("latest") or e.get("first") or e.get("value")
    times = f", x{e['count']}" if e.get("count", 1) > 1 else ""
    return f"{e.get('type')} ({e.get('class')}{', ' + where if where else ''}{times}): {_short(val, 140)}"


def _saw(it: dict) -> list[str]:
    mem, obs, meta = it["input"].get("memory") or {}, it["input"].get("observe"), it["meta"]
    s = mem.get("session") or {}
    out = [f"Session: {', '.join(s.get('venues') or meta.get('markets') or [])}; rules "
           f"{', '.join(s.get('rules') or []) or 'none'}; level {s.get('level') or '-'}; budget {s.get('budget')}, "
           f"per trade {s.get('per_trade')}; model {meta.get('model')}; "
           f"user prompt v{(meta.get('prompt_versions') or {}).get('user')}"]
    decs = mem.get("recent_decisions") or []
    if decs:
        last = ", ".join(f"{d.get('action')}/{d.get('pass_reason') or '-'}" for d in decs[-3:])
        out.append(f"Last decisions: {last}")
    if mem.get("notes"):
        out.append(f"Its notes ({len(mem['notes'])} chars): {_short(mem['notes'], 300)}")
    if it["context"].get("retired_rules"):
        out.append(f"Retired since the last wake: {', '.join(it['context']['retired_rules'])}")
    if isinstance(obs, dict):
        for k, m in (obs.get("markets") or {}).items():
            mk = (m or {}).get("market") or {}
            w = (m or {}).get("window") or {}
            out.append(f"Observe {k}: UP mid {mk.get('mid_up')}, fresh {mk.get('fresh')}, "
                       f"{w.get('seconds_left')} s left, "
                       f"call {((m or {}).get('call') or {}).get('action')}")
    else:
        out.append("Observe: not stored for this wake (journaled from 2026-11-01 on); memory above is reconstructed.")
    return out


def render(items: list[dict], *, name: str, source: str, seed: int) -> str:
    out = [f"# Error analysis: {len(items)} real wakes ({name})", "",
           f"Source: `{source}` (read-only), seed {seed}. Redacted like the eval export (balances, ids, devices). "
           "Wakes from 2026-11-01 on also carry their exact input (memory and observe); earlier ones show a "
           "reconstruction.", "",
           "How to label (about a minute a wake): read top to bottom, and on the **First failure** line write the "
           "first thing that went wrong, in your own words (one line; leave it empty if nothing did). Then "
           "**Pass/fail** (pass / fail / unsure) and a short **Category** you would group it under (free text: "
           "you will merge categories later). Fill the same three columns in the CSV sheet next to this file, which "
           "the tooling reads (`python -m evals.review --tally <sheet>`). Judge conduct, not profit.", ""]
    for n, it in enumerate(items, 1):
        m, rec = it["meta"], it["recorded"]
        d = rec.get("decision") or {}
        out += [f"## {n}. {m.get('local_time')} · {m.get('decision_market') or ','.join(m.get('markets') or [])} · "
                f"{m.get('primary_trigger')} → {m.get('decision_action') or 'no decision'}"
                f"{' / ' + m['pass_reason'] if m.get('pass_reason') else ''}", "",
                f"`{it['item_id']}` · journal {it['source']['journal_id']} · session {it['source']['session']} · "
                f"status {rec.get('status')} · {rec.get('turns')} turn(s) · ${float(rec.get('est_usd') or 0):.4f}", "",
                "**What woke it:**"]
        out += [f"- {_event(e)}" for e in it["input"].get("events") or []]
        out += ["", "**What it saw:**"] + [f"- {x}" for x in _saw(it)]
        th = rec.get("thinking_summary") or []
        if th:
            out += ["", f"**Thinking (summary):** {_short(' '.join(th), 500)}"]
        out += ["", "**Tool calls:**"]
        tools = rec.get("tools") or []
        out += [f"{i}. `{c.get('name')}`({_args(c.get('name'), c.get('args'))}) → {_result(c)}"
                for i, c in enumerate(tools, 1)] or ["(none)"]
        if d:
            out += ["", f"**Decision:** {d.get('action')} {d.get('side') or ''} · pass reason {d.get('pass_reason')} · "
                        f"p {d.get('p_estimate')} vs market {d.get('market_p')} ({d.get('p_source')})",
                    f"- Thesis: {_short(d.get('thesis') or '', 600)}",
                    f"- Invalidation: {_short(d.get('invalidation') or '', 300)}"]
        out += ["", f"**Told the user:** {_short(rec.get('final_text') or '(nothing)', 900)}"]
        trades = (it.get("outcome") or {}).get("trades") or []
        out += ["", "**Outcome:** " + ("; ".join(
            f"{t.get('rule') or 'manual'} {t.get('side')} on {t.get('ticker')}: "
            f"{t.get('exit_reason') or t.get('state')}"
            f", net {t.get('net')}" for t in trades) if trades else "no trade linked to this wake")]
        out += ["", "First failure: ", "", "Pass/fail: ", "", "Category: ", "", "---", ""]
    return "\n".join(out)


def write_sheet(path: Path, items: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(LABEL_COLUMNS)
        for n, it in enumerate(items, 1):
            m = it["meta"]
            w.writerow([n, it["item_id"], it["source"]["journal_id"], m.get("local_time"), m.get("primary_trigger"),
                        m.get("decision_market") or ",".join(m.get("markets") or []),
                        f"{m.get('decision_action')}/{m.get('pass_reason') or '-'}", outcome_class(it), "", "", "", ""])


def tally(path: Path) -> str:
    with path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    done = [r for r in rows if (r.get("pass_fail") or "").strip()]
    pf = Counter((r["pass_fail"] or "").strip().lower() for r in done)
    cats = Counter((r.get("failure_category") or "").strip().lower() or "(none)" for r in done
                   if (r["pass_fail"] or "").strip().lower() != "pass")
    out = [f"{len(done)}/{len(rows)} wakes labelled: " + ", ".join(f"{k} {v}" for k, v in pf.most_common())]
    out += [f"  {n:4d}  {c}" for c, n in cats.most_common()]
    return "\n".join(out)


def build(source: str, n: int, seed: int, out: Path, label: str | None = None) -> dict:
    name = label or (Path(source).stem if "://" not in source else "postgres")
    src = load(open_engine(source), name)
    items = export_items(src, wakes(src), Redactor())
    picked = stratified(items, n, seed)
    tag = f"{name}-n{len(picked)}-seed{seed}"
    out.mkdir(parents=True, exist_ok=True)
    md, sheet, data = out / f"review-{tag}.md", out / f"labels-{tag}.csv", out / f"items-{tag}.jsonl"
    md.write_text(render(picked, name=name, source=name, seed=seed), encoding="utf-8")
    if not sheet.exists():  # never overwrite labels the user has filled in
        write_sheet(sheet, picked)
    write_jsonl(data, picked)
    return {"review": md, "sheet": sheet, "items": data, "n": len(picked),
            "strata": dict(Counter(str(i["meta"]["primary_trigger"]) for i in picked))}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m evals.review", description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", help="a SQLite file (read-only) or a Postgres URL (research role)")
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--label", default=None)
    ap.add_argument("--tally", default=None, help="a filled-in labels CSV: print the failure categories")
    a = ap.parse_args(argv)
    if a.tally:
        print(tally(Path(a.tally)))
        return 0
    if not a.source:
        ap.error("give --source (or --tally)")
    try:
        r = build(a.source, a.n, a.seed, Path(a.out), a.label)
    except ExportScanError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"{r['n']} wakes -> {r['review']}\nlabel sheet: {r['sheet']}\nstrata: {r['strata']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
