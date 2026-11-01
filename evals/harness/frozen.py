"""Frozen mode: replay a recorded wake. The model gets the recorded wake message (verbatim from `agent_wake_inputs`
from 2026-11-01; reconstructed before: events exact, memory rebuilt, observe missing) and each tool call is answered
with the recorded result of the same call (same tool, same arguments, first unused). A call that is not in the
recording ends the wake as `diverged`: the item is reported as diverged, never as failed (the recording cannot say
what the world would have answered). No store, no gate, no venue: nothing can be sent.

The system prompt is today's: docs/agent/default-system-prompt.md (git is the source of truth) + the item's user
layer if any + the app's TOOL_NOTE; the tool specs are today's registry. A frozen run therefore asks "what does the
current prompt and model do on that recorded input".
"""

from __future__ import annotations

import json
from typing import Any

from btc_signal_engine.application.agent_runner import TOOL_NOTE, cost_usd
from btc_signal_engine.application.agent_tools import TOOLS
from evals import REPO
from evals.harness.models import ScriptedModel, replay_turns

SYSTEM_PROMPT = REPO / "docs" / "agent" / "default-system-prompt.md"
MAX_TURNS, MAX_TOKENS, EFFORT = 6, 16_000, "medium"


def specs() -> list[dict]:
    return [{"name": t.name, "description": t.description, "input_schema": t.schema, "kind": t.kind}
            for t in sorted(TOOLS, key=lambda t: t.name)]


def system_prompt(user_layer: str = "") -> str:
    return "\n\n".join(x for x in (SYSTEM_PROMPT.read_text(encoding="utf-8"), user_layer, TOOL_NOTE) if x)


def _same(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


async def run(item, model, *, trial: int = 0, seed: int = 0) -> dict:
    frame = item.frame
    rec = frame["recorded"]
    recorded = [c for c in rec.get("tools") or [] if c.get("code") != "not_run"]
    used: set[int] = set()
    tools: list[dict] = []
    state = {"diverged": None, "ended": False}
    usage = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "usd": 0.0, "turns": 0}
    m = model or ScriptedModel(replay_turns(rec.get("tools") or [], rec.get("final_text")))
    name = getattr(m, "name", "fake")
    model_id = item.model

    async def execute(tool: str, args: dict) -> tuple[str, bool]:
        hit = next((i for i, c in enumerate(recorded) if i not in used and c["name"] == tool
                    and _same(c.get("args") or {}, args)), None)
        if hit is None:
            state["diverged"] = {"call": len(tools), "name": tool}
            out = {"ok": False, "code": "diverged", "reason": "This call is not in the recording."}
            tools.append({"name": tool, "args": args, "ok": False, "code": "diverged", "result": out})
            return json.dumps(out), True
        used.add(hit)
        c = recorded[hit]
        tools.append({k: c.get(k) for k in ("name", "args", "ok", "code", "result")})
        if tool == "end_session" and c.get("ok"):
            state["ended"] = True
        res = c.get("result")
        return (res if isinstance(res, str) else json.dumps(res)), c.get("ok") is False

    def gate(_ctx: int, _chars: int) -> str | None:
        if state["diverged"] is not None:
            return "diverged"
        if state["ended"]:
            return "session_stopped"
        return None

    def on_turn(t) -> None:
        u = t.usage
        usage["input"] += int(u.get("input_tokens") or 0)
        usage["output"] += int(u.get("output_tokens") or 0)
        usage["cache_write"] += int(u.get("cache_creation_input_tokens") or 0)
        usage["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
        usage["usd"] += cost_usd(model_id, u)
        if not t.unstreamed:
            usage["turns"] += 1

    out = await m.run_wake(system_prompt(item.scenario.get("user_layer", "")), frame["input"]["message"], specs(),
                           execute, session_id=f"eval-{item.id}-{trial}", model=model_id, effort=EFFORT,
                           max_turns=MAX_TURNS, max_tokens=MAX_TOKENS, max_budget_usd=1.0, on_turn=on_turn,
                           gate=gate)
    pending = list(tools)
    for c in out.tool_calls:  # emitted but never run (the runtime refused it, or the wake stopped first)
        args = c.get("input") or {}
        hit = next((e for e in pending if e["name"] == c.get("name") and _same(e["args"], args)), None)
        if hit is not None:
            pending.remove(hit)
            continue
        tools.append({"name": c.get("name"), "args": args, "ok": False, "code": "not_run",
                      "result": "The runtime did not run this call."})
    status = "diverged" if state["diverged"] else out.status
    if status != "diverged" and rec.get("status") in ("session_stopped", "cancelled") and name == "fake":
        status = rec["status"]  # the replayed recording ends as it did
    return {"item_id": item.id, "trial": trial, "seed": seed, "mode": "frozen", "model": model_id, "subject": name,
            "status": status, "tools": tools, "final_text": out.text, "turns": usage["turns"],
            "tokens": {k: usage[k] for k in ("input", "output", "cache_write", "cache_read")},
            "est_usd": round(usage["usd"], 6), "decisions": [], "diverged": state["diverged"],
            "fidelity": frame.get("fidelity"), "retries": 0, "fallbacks": [],
            "context": {**(frame.get("context") or {}), **item.context}, "world": {}}
