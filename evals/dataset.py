"""The eval sets as Inspect datasets (JSONL, Inspect's native format; `json_dataset` with a strict `record_to_sample`).

`datasets/golden.jsonl` and `datasets/adversarial.jsonl`: one item a line.

    id          unique across sets                     title     one line
    category    the behaviour it covers (per-category metrics)
    safety      a safety category: pass^k, zero tolerated failures (the `min` reducer)
    source      real (from the journal export) | crafted
    mode        frozen (recorded tool results) | simulated (the real runner and gates on the fakes)
    frame       frozen: an exported wake, `frames/<journal id>.jsonl` (evals.export), relative to datasets/
    graders     must APPLY (a listed grader that is not applicable fails the item); every other grader also runs
                and must not fail
    context     merged over the frame's context: what the agent knew (hand-labelled)
    recorded_verdicts   frozen, replayed by the reference subject: the recording's known verdicts (a real failure
                kept as a negative fixture); any other subject must pass them all
    scenario    simulated: the session and world (evals/sim.py)
    reference   simulated: the reference solution, one object a turn: {text?, tools?: [{name, input, not_run?}]}
                (`"${bracket}"`: the scenario's rule position)
    mutants     [{name, breaks, turns}]: a broken subject; every grader in `breaks` must FAIL (the `mutants` task)
    notes       free text (why a label or a known verdict is what it is)

`datasets/replay/*.jsonl`, `datasets/holdout/*.jsonl`: exported wakes (`python -m evals.export`), each line a frozen
item with no hand labels. A malformed line, an unknown grader or a missing reference is an error, never skipped.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from inspect_ai.dataset import MemoryDataset, Sample, json_dataset

from evals import DATASETS
from evals.graders import GRADERS

MODES = ("frozen", "simulated")
SOURCES = ("real", "crafted")
KEYS = frozenset({"id", "title", "category", "safety", "source", "mode", "frame", "graders", "context",
                  "recorded_verdicts", "scenario", "reference", "mutants", "notes"})
ITEM_SETS = ("golden", "adversarial")
FRAME_SETS = ("replay", "holdout")
DEFAULT_MODEL = "claude-haiku-5-5"


class ItemError(ValueError):
    """A malformed item: named, never skipped."""


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _frame(rel: str) -> dict:
    frames = read_jsonl(DATASETS / rel)
    if len(frames) != 1:
        raise ItemError(f"frame {rel} must hold exactly one wake (has {len(frames)})")
    return frames[0]


def item_model(meta: dict) -> str:
    if meta.get("frame") is not None:
        return str((meta["frame"].get("meta") or {}).get("model") or DEFAULT_MODEL)
    return str((meta.get("scenario") or {}).get("model", DEFAULT_MODEL))


def check(r: dict, where: str) -> dict:
    """A validated item record (the frame read in); raises ItemError."""
    unknown_keys = set(r) - KEYS
    if unknown_keys:
        raise ItemError(f"{where}: unknown key(s) {sorted(unknown_keys)}")
    for k in ("id", "title", "category", "source", "mode"):
        if k not in r:
            raise ItemError(f"{where}: missing {k!r}")
    if r["mode"] not in MODES:
        raise ItemError(f"{where}: mode must be one of {MODES} (got {r['mode']!r})")
    if r["source"] not in SOURCES:
        raise ItemError(f"{where}: source must be one of {SOURCES} (got {r['source']!r})")
    named = [*r.get("graders", []), *r.get("recorded_verdicts", {}),
             *(g for m in r.get("mutants", []) for g in m.get("breaks", []))]
    unknown = [g for g in named if g not in GRADERS]
    if unknown:
        raise ItemError(f"{where}: unknown grader(s) {unknown}")
    meta = {"safety": False, "graders": [], "context": {}, "recorded_verdicts": {}, "scenario": {}, "reference": [],
            "mutants": [], "notes": [], **r}
    if isinstance(meta.get("frame"), str):
        meta["frame"] = _frame(meta["frame"])
    if meta["mode"] == "frozen" and not meta.get("frame"):
        raise ItemError(f"{where}: a frozen item needs a frame")
    if meta["mode"] == "simulated" and not meta["reference"]:
        raise ItemError(f"{where}: a simulated item needs a reference solution (it shows the item can pass)")
    for m in meta["mutants"]:
        if not m.get("name") or not m.get("breaks") or not m.get("turns"):
            raise ItemError(f"{where}: a mutant needs name, breaks and turns")
    return meta


def frame_item(frame: dict) -> dict:
    """An exported wake as an item (replay / holdout: no hand labels)."""
    meta = frame.get("meta") or {}
    return {"id": frame["item_id"], "title": f"replay of journal {frame['source']['journal_id']}",
            "category": f"replay/{meta.get('primary_trigger')}", "source": "real", "mode": "frozen", "frame": frame}


def to_sample(meta: dict, set_name: str) -> Sample:
    frame = meta.get("frame")
    text = frame["input"]["message"] if frame else meta["title"]
    return Sample(id=meta["id"], input=text, metadata={**meta, "set": set_name, "model": item_model(meta)})


def _record_to_sample(set_name: str):
    def convert(r: dict[str, Any]) -> Sample:
        return to_sample(check(r, f"{set_name}.jsonl {r.get('id')!r}"), set_name)
    return convert


def load(sets: list[str] | tuple[str, ...]) -> MemoryDataset:
    """The named sets as one dataset; a duplicate id across them is an error."""
    samples: list[Sample] = []
    for s in sets:
        if s in ITEM_SETS:
            samples += list(json_dataset(str(DATASETS / f"{s}.jsonl"), sample_fields=_record_to_sample(s), name=s))
        elif s in FRAME_SETS:
            d = DATASETS / s
            if not d.is_dir():
                raise ItemError(f"no dataset directory {d}")
            for f in sorted(d.glob("*.jsonl")):
                samples += [to_sample(check(frame_item(fr), f"{f.name} {fr.get('item_id')!r}"), s)
                            for fr in read_jsonl(f)]
        else:
            raise ItemError(f"unknown set {s!r}; sets: {ITEM_SETS + FRAME_SETS}")
    ids = [str(x.id) for x in samples]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise ItemError(f"duplicate item ids across sets: {dup}")
    return MemoryDataset(samples, name="+".join(sets))


def load_file(path: Path, name: str = "recorded") -> MemoryDataset:
    """An export file (`python -m evals.export`) as frozen items: the `recorded` task grades them as recorded."""
    rows = read_jsonl(Path(path))
    return MemoryDataset([to_sample(check(frame_item(fr), f"{Path(path).name} {fr.get('item_id')!r}"), name)
                          for fr in rows], name=name)
