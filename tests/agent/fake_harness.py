"""A scripted AgentHarness (and launch key check) for the runner tests: no subprocess, no SDK, no network.

`run_wake` plays the script one model turn per step, the way the real harness drives the CLI:
- a step is a `turn(...)`, an exception (raised), or a callable `step(request) -> turn` (may be async; it can block,
  push events or change the store);
- before every turn after the first it asks `gate(context_tokens, new_chars)` (as the PostToolUse hook does) and stops
  with the gate's reason;
- each turn streams its deltas (the given ones, else one per block: thinking, text, the tool's name), then reports
  its usage to `on_turn`, then runs its tool_use blocks through `execute` in order, recording each result text;
- it stops after a turn without tool calls ("done") or after `max_turns` ("turn_cap").
Every call is recorded as a deep copy (`requests`, one per wake) with the turns it played and the tool results it got
back, so a test can check exactly what the runner sent."""

from __future__ import annotations

import copy
import inspect
from dataclasses import dataclass, field

from btc_signal_engine.application.ports import TurnUsage, WakeResult

USAGE = {"input_tokens": 2_000, "output_tokens": 300, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def thinking(t: str) -> dict:
    return {"type": "thinking", "thinking": t}


def tool_use(id: str, name: str, input: dict) -> dict:
    return {"type": "tool_use", "id": id, "name": name, "input": input}


@dataclass
class Turn:
    blocks: list[dict]
    stop: str
    usage: dict
    deltas: tuple = ()
    message_id: str = "msg_fake"


def turn(*blocks: dict, stop: str | None = None, usage: dict | None = None, deltas: tuple = ()) -> Turn:
    """`stop` defaults to tool_use when a tool_use block is present, else end_turn. `deltas` = ((kind, text), ...)
    streamed to on_delta (default: one per block)."""
    if stop is None:
        stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
    return Turn(list(blocks), stop, dict(usage or USAGE), tuple(deltas))


@dataclass
class Request:
    system: str
    prompt: str
    tools: list[dict]
    options: dict
    turns: list[Turn] = field(default_factory=list)
    results: list[list[tuple[str, str, bool]]] = field(default_factory=list)  # per turn: (name, text, is_error)


class FakeHarness:
    def __init__(self, *script) -> None:
        self.script = list(script)
        self.requests: list[Request] = []
        self.keys_checked = 0
        self.execute = None  # the last wake's tool callback (a test can call it as the SDK's own tasks would)

    async def check_key(self) -> None:
        self.keys_checked += 1

    async def run_wake(self, system_prompt, wake_prompt, tool_specs, execute, *, session_id, model, effort, max_turns,
                       max_tokens, max_budget_usd, on_delta=None, on_turn=None, gate=None) -> WakeResult:
        req = Request(system_prompt, wake_prompt, copy.deepcopy(tool_specs),
                      {"session_id": session_id, "model": model, "effort": effort, "max_turns": max_turns,
                       "max_tokens": max_tokens, "max_budget_usd": max_budget_usd})
        self.requests.append(req)
        self.execute = execute
        context, chars, last, status = 0, 0, None, "turn_cap"
        calls: list[dict] = []
        for n in range(max_turns):
            if n and gate is not None:
                why = gate(context, chars)
                if why is not None:
                    status = why
                    break
            if not self.script:
                raise AssertionError("the fake harness's script ran out")
            step = self.script.pop(0)
            if isinstance(step, BaseException):
                raise step
            if callable(step):
                step = step(req)
                if inspect.isawaitable(step):
                    step = await step
            req.turns.append(copy.deepcopy(step))
            deltas = step.deltas or tuple(
                ("thinking", b["thinking"]) if b["type"] == "thinking" else ("text", b["text"]) if b["type"] == "text"
                else ("tool_use", b["name"]) for b in step.blocks)
            for kind, piece in deltas:
                if on_delta is not None:
                    on_delta(kind, piece)
            last = step
            if on_turn is not None:
                on_turn(TurnUsage(usage=dict(step.usage), stop_reason=step.stop,
                                  thinking="".join(b["thinking"] for b in step.blocks if b["type"] == "thinking"),
                                  text="".join(b["text"] for b in step.blocks if b["type"] == "text"),
                                  message_id=step.message_id))
            context, chars = sum(step.usage.values()), 0
            uses = [b for b in step.blocks if b["type"] == "tool_use"]
            results = []
            for u in uses:
                calls.append({"id": u["id"], "name": u["name"], "input": u["input"]})
                out, is_error = await execute(u["name"], copy.deepcopy(u["input"]))
                chars += len(out)
                results.append((u["name"], out, is_error))
            req.results.append(results)
            if not uses:
                status = "done"
                break
        return WakeResult(status=status, stop_reason=last.stop if last else None,
                          text="".join(b["text"] for b in last.blocks if b["type"] == "text") or None if last else None,
                          tool_calls=tuple(calls), turns=len(req.turns), cost_usd=0.0, detail={"fake": True})
