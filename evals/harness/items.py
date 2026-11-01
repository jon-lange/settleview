"""Eval items: one TOML file per scenario (golden, adversarial), optionally with a JSONL frame (an exported wake).

    id = "golden-real-4098-self-stop"        # unique across sets
    title = "..."                            # one line
    category = "mode_conflict"               # the behaviour it covers (the report's per-category rows)
    safety = true                            # a safety category: pass^k, zero tolerated failures
    source = "real"                          # real (from the journal export) | crafted
    mode = "frozen"                          # frozen (recorded tool results) | simulated (fakes + the real gates)
    frame = "frames/4098.jsonl"              # frozen: the exported wake (evals.export), relative to the file
    graders = ["records_decision_each_wake"] # must APPLY (a not_applicable listed grader fails the item); every
                                             # other grader also runs and must not fail
    [context]                                # merged over the frame's context: what the agent knew (hand-labelled)
    [recorded_verdicts]                      # frozen + fake model: the recorded run's known verdicts (a real
                                             # failure kept as a negative fixture); a live run must pass them all
    [scenario]                               # simulated: the session and world (see harness/sim.py)
    [[reference]]                            # simulated + fake model: the reference solution, one table per turn
    text = "..."                             #   the turn's text
    tools = [{name = "observe", input = {}}] #   its tool calls (`not_run = true`: emitted, refused by the runtime)
    [[mutants]]                              # a broken subject: these turns must make every grader in `breaks` FAIL
    name = "follows_injection"
    breaks = ["no_entry_in_rules_mode"]
    turns = [{tools = [...]}, {text = "..."}]
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals import ROOT
from evals.graders import GRADERS

MODES = ("frozen", "simulated")
SOURCES = ("real", "crafted")
DATASETS = ROOT / "datasets"


class ItemError(ValueError):
    """A malformed item file: named, never skipped."""


@dataclass
class Item:
    id: str
    title: str
    category: str
    set: str
    source: str
    mode: str
    path: Path
    safety: bool = False
    graders: list[str] = field(default_factory=list)
    context: dict = field(default_factory=dict)
    recorded_verdicts: dict[str, str] = field(default_factory=dict)
    scenario: dict = field(default_factory=dict)
    reference: list[dict] = field(default_factory=list)
    mutants: list[dict] = field(default_factory=list)
    frame: dict | None = None

    @property
    def model(self) -> str:
        if self.frame is not None:
            return str((self.frame.get("meta") or {}).get("model") or "claude-haiku-5-5")
        return str(self.scenario.get("model", "claude-haiku-5-5"))


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_item(path: Path, set_name: str | None = None) -> Item:
    try:
        d = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ItemError(f"{path}: {e}") from None
    for k in ("id", "title", "category", "source", "mode"):
        if k not in d:
            raise ItemError(f"{path}: missing {k!r}")
    if d["mode"] not in MODES:
        raise ItemError(f"{path}: mode must be one of {MODES} (got {d['mode']!r})")
    if d["source"] not in SOURCES:
        raise ItemError(f"{path}: source must be one of {SOURCES} (got {d['source']!r})")
    unknown = [g for g in [*d.get("graders", []), *d.get("recorded_verdicts", {})]
               + [g for m in d.get("mutants", []) for g in m.get("breaks", [])] if g not in GRADERS]
    if unknown:
        raise ItemError(f"{path}: unknown grader(s) {unknown}")
    item = Item(id=d["id"], title=d["title"], category=d["category"], set=set_name or path.parent.name,
                source=d["source"], mode=d["mode"], path=path, safety=bool(d.get("safety", False)),
                graders=list(d.get("graders", [])), context=dict(d.get("context", {})),
                recorded_verdicts=dict(d.get("recorded_verdicts", {})), scenario=dict(d.get("scenario", {})),
                reference=list(d.get("reference", [])), mutants=list(d.get("mutants", [])))
    if "frame" in d:
        frames = read_jsonl(path.parent / d["frame"])
        if len(frames) != 1:
            raise ItemError(f"{path}: frame {d['frame']} must hold exactly one wake (has {len(frames)})")
        item.frame = frames[0]
    if item.mode == "frozen" and item.frame is None:
        raise ItemError(f"{path}: a frozen item needs a frame")
    if item.mode == "simulated" and not item.reference:
        raise ItemError(f"{path}: a simulated item needs a [[reference]] solution (it shows the item can pass)")
    for m in item.mutants:
        if not m.get("name") or not m.get("breaks") or not m.get("turns"):
            raise ItemError(f"{path}: a mutant needs name, breaks and turns")
    return item


def load_set(path: Path) -> list[Item]:
    """Every item file in a dataset directory (sorted by name); a duplicate id is an error."""
    if not path.is_dir():
        raise ItemError(f"no dataset directory {path}")
    items = [load_item(p, path.name) for p in sorted(path.glob("*.toml"))]
    seen: dict[str, Path] = {}
    for it in items:
        if it.id in seen:
            raise ItemError(f"duplicate item id {it.id!r} in {seen[it.id]} and {it.path}")
        seen[it.id] = it.path
    return items


def recorded_trial(frame: dict, context: dict | None = None) -> dict[str, Any]:
    """An exported wake as a trial (the recorded run itself): what the live report card grades."""
    rec = frame.get("recorded") or {}
    return {"item_id": frame.get("item_id"), "mode": "recorded", "model": rec.get("model"),
            "status": rec.get("status"), "tools": list(rec.get("tools") or []), "final_text": rec.get("final_text"),
            "turns": rec.get("turns"), "est_usd": rec.get("est_usd"), "tokens": rec.get("tokens"),
            "decisions": [], "context": {**(frame.get("context") or {}), **(context or {})}, "world": {}}
