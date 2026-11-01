"""The subject's model: the scripted fake (default: free, deterministic, no network) or the real Claude model
(`--live`).

`ScriptedModel` implements the app's `AgentHarness` port the way tests/agent/fake_harness.py does (one scripted turn
per model request, the gate asked between turns, each tool_use block run through `execute`), plus what the evals
need: a block marked `not_run` is emitted but never executed (the runtime dropping a parallel duplicate, as the CLI
did in 55 recorded wakes), `${name}` placeholders in tool inputs are filled from `vars` (a simulated bracket id), and
an `after_tool(index)` hook lets the simulated world change between calls (a pause, the kill switch).

`live_model()` is the production harness (adapters/agent_sdk SdkHarness: the Claude Agent SDK and its CLI) on the
user's Anthropic key. Only `--live` builds it; the PR pipeline and the tests never do.
"""

from __future__ import annotations

import copy
import inspect
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from btc_signal_engine.application.ports import TurnUsage, WakeResult

USAGE = {"input_tokens": 2_000, "output_tokens": 300, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
VAR = re.compile(r"^\$\{(\w+)\}$")


NULL = "<null>"  # TOML has no null: a script value "<null>" is sent as JSON null


def _fill(v: Any, env: dict) -> Any:
    if isinstance(v, str):
        if v == NULL:
            return None
        m = VAR.match(v)
        if m:
            if m.group(1) not in env:
                raise KeyError(f"script placeholder ${{{m.group(1)}}} has no value (known: {sorted(env)})")
            return env[m.group(1)]
        return v
    if isinstance(v, dict):
        return {k: _fill(x, env) for k, x in v.items()}
    if isinstance(v, list):
        return [_fill(x, env) for x in v]
    return v


class ScriptedModel:
    """Plays `turns` (each {text?, thinking?, tools?: [{name, input, not_run?}]}); runs out -> an AssertionError."""

    name = "fake"
    live = False

    def __init__(self, turns: list[dict], *, vars: dict | None = None,
                 after_tool: Callable[[int], Any] | None = None, usage: dict | None = None) -> None:
        self.turns = copy.deepcopy(list(turns))
        self.vars = dict(vars or {})
        self.after_tool = after_tool
        self.usage = dict(usage or USAGE)
        self.requests: list[dict] = []

    async def check_key(self) -> None:
        return None

    async def run_wake(self, system_prompt, wake_prompt, tool_specs, execute, *, session_id, model, effort, max_turns,
                       max_tokens, max_budget_usd, on_delta=None, on_turn=None, gate=None) -> WakeResult:
        self.requests.append({"system": system_prompt, "prompt": wake_prompt, "tools": [t["name"] for t in tool_specs],
                              "model": model, "max_turns": max_turns})
        script = list(self.turns)
        status, calls, last, n_tool, played = "turn_cap", [], None, 0, 0
        context = 0
        for n in range(max_turns):
            if n and gate is not None:
                why = gate(context, 0)
                if why is not None:
                    status = why
                    break
            if not script:
                status = "done"
                break
            step = script.pop(0)
            played += 1
            text, thinking = step.get("text") or "", step.get("thinking") or ""
            uses = [dict(u, input=_fill(u.get("input") or {}, self.vars)) for u in step.get("tools") or []]
            for kind, piece in (("thinking", thinking), ("text", text)):
                if piece and on_delta is not None:
                    on_delta(kind, piece)
            for u in uses:
                if on_delta is not None:
                    on_delta("tool_use", u["name"])
            stop = "tool_use" if uses else "end_turn"
            if on_turn is not None:
                on_turn(TurnUsage(usage=dict(self.usage), stop_reason=stop, thinking=thinking, text=text,
                                  message_id=f"fake_{played}"))
            context = sum(self.usage.values())
            last = (text, stop)
            for i, u in enumerate(uses):
                calls.append({"id": f"fake_{played}_{i}", "name": u["name"], "input": u["input"]})
                if u.get("not_run"):
                    continue
                await execute(u["name"], copy.deepcopy(u["input"]))
                if self.after_tool is not None:
                    r = self.after_tool(n_tool)
                    if inspect.isawaitable(r):
                        await r
                n_tool += 1
            if not uses:
                status = "done"
                break
        return WakeResult(status=status, stop_reason=last[1] if last else None,
                          text=(last[0] or None) if last else None,
                          tool_calls=tuple(calls), turns=played, cost_usd=0.0, detail={"fake": True})


def replay_turns(tools: list[dict], final_text: str | None) -> list[dict]:
    """A recorded wake as a script: its calls in one turn (in the recorded order; the runtime-dropped ones marked
    not_run), then its closing text."""
    turn = {"tools": [{"name": c["name"], "input": c.get("args") or {}, "not_run": c.get("code") == "not_run"}
                      for c in tools]}
    out = [turn] if tools else []
    out.append({"text": final_text or ""})
    return out


def live_model(data_dir: Path, *, key_file: str | None = None) -> Any:
    """The production SDK harness on the user's key: the key comes from `key_file` or ANTHROPIC_API_KEY (never a
    .env file). Raises when there is no key."""
    from btc_signal_engine.adapters.agent_sdk.harness import SdkHarness
    key = Path(key_file).read_text(encoding="utf-8").strip() if key_file else os.environ.get("ANTHROPIC_API_KEY", "")
    if not key.strip():
        raise SystemExit("--live needs an Anthropic key: --api-key-file <file> or ANTHROPIC_API_KEY")
    h = SdkHarness(key, data_dir)
    h.live = True  # type: ignore[attr-defined]
    h.name = "live"  # type: ignore[attr-defined]
    return h
