"""The agent's model loop on Inspect AI's model API: an `AgentHarness` (btc_signal_engine.application.ports) whose
model is an Inspect `Model`. The real `AgentRunner` drives it in simulated mode (its gate, tools and journal are the
app's own), and frozen mode drives it with the recorded results; either way the model is whatever Inspect resolves:
the eval's `--model` (the Anthropic provider on the user's key) or `mockllm/model` playing a script (free, offline).

The loop is the production one's shape: one model request a turn, every tool call run through `execute` (the app's
registry validates the arguments and answers `invalid` itself, as in production, so Inspect's own argument check is
not in the way), the gate asked between turns, `max_turns` -> `turn_cap`. A call whose `parse_error` is set is not
executed: that is the runtime refusing the call's input (Inspect's provider sets it when the arguments do not parse;
a replayed recording uses it for the calls the runtime dropped, `not_run` in the journal).

The production runtime is the Claude Agent SDK (adapters/agent_sdk); this harness replaces only the transport, so an
eval asks "what do this prompt, these tools and this model do", not "does the SDK's CLI behave".
"""

from __future__ import annotations

import copy
import re
from collections.abc import Awaitable, Callable
from typing import Any

from inspect_ai.model import (ChatMessage, ChatMessageAssistant, ChatMessageSystem, ChatMessageTool,
                              ChatMessageUser, GenerateConfig, Model, ModelOutput, ModelUsage, get_model)
from inspect_ai.model._model_output import ChatCompletionChoice
from inspect_ai.tool import ToolCall, ToolCallError, ToolInfo, ToolParams
from inspect_ai.util import LimitExceededError

from btc_signal_engine.application.ports import TurnUsage, WakeResult

NOT_RUN = "The runtime did not run this call (recorded: not_run)."
USAGE = {"input_tokens": 2_000, "output_tokens": 300, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
NULL = "<null>"
VAR = re.compile(r"^\$\{(\w+)\}$")
STOP = {"tool_calls": "tool_use", "stop": "end_turn", "max_tokens": "max_tokens", "model_length": "max_tokens",
        "content_filter": "refusal"}


def tool_infos(specs: list[dict]) -> list[ToolInfo]:
    """The registry's tool specs as Inspect tool definitions (Inspect's schema model keeps the JSON Schema keywords
    it knows; the registry still validates every call against the full schema)."""
    return [ToolInfo(name=s["name"], description=s["description"], parameters=ToolParams.model_validate(s["input_schema"]))
            for s in specs]


class InspectHarness:
    """`AgentHarness` over an Inspect model. `after_tool(i)` (simulated hooks) runs after the i-th executed call."""

    live = True

    def __init__(self, model: Model, *, after_tool: Callable[[int], Awaitable[None]] | None = None) -> None:
        self.model = model
        self.after_tool = after_tool
        self.messages: list[ChatMessage] = []
        self.output: ModelOutput | None = None
        self.limit: LimitExceededError | None = None  # Inspect's token / cost limit stopped the wake

    @property
    def name(self) -> str:
        return str(self.model)

    async def check_key(self) -> None:
        return None

    async def run_wake(self, system_prompt, wake_prompt, tool_specs, execute, *, session_id, model, effort, max_turns,
                       max_tokens, max_budget_usd, on_delta=None, on_turn=None, gate=None) -> WakeResult:
        infos = tool_infos(tool_specs)
        self.messages = [ChatMessageSystem(content=system_prompt), ChatMessageUser(content=wake_prompt)]
        config = GenerateConfig(max_tokens=max_tokens, effort=effort)
        calls: list[dict] = []
        total = dict.fromkeys(USAGE, 0)
        status, stop, text, turns, n_tool, context, new_chars = "turn_cap", None, None, 0, 0, 0, 0
        for n in range(max_turns):
            if n and gate is not None:
                why = gate(context, new_chars)
                if why is not None:
                    status = why
                    break
            try:
                out = await self.model.generate(self.messages, tools=infos, config=config)
            except LimitExceededError as e:  # the runner records the wake as failed; the solver re-raises it
                self.limit = e
                raise
            self.output = out
            turns += 1
            u = out.usage or ModelUsage()
            usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                     "cache_creation_input_tokens": u.input_tokens_cache_write or 0,
                     "cache_read_input_tokens": u.input_tokens_cache_read or 0}
            for k, v in usage.items():
                total[k] += v
            stop = STOP.get(out.stop_reason, out.stop_reason)
            text = out.message.text or None
            thinking = "".join(getattr(c, "summary", None) or getattr(c, "reasoning", "") or ""
                               for c in (out.message.content if isinstance(out.message.content, list) else [])
                               if getattr(c, "type", None) == "reasoning")
            for kind, piece in (("thinking", thinking), ("text", text or "")):
                if piece and on_delta is not None:
                    on_delta(kind, piece)
            if on_turn is not None:
                on_turn(TurnUsage(usage=usage, stop_reason=stop, thinking=thinking, text=text or ""))
            self.messages.append(out.message)
            context = sum(usage.values())  # the whole context, cached or not: the gate prices the next turn on it
            uses = list(out.message.tool_calls or [])
            if not uses:
                status = "done"
                break
            new_chars = 0
            for c in uses:
                calls.append({"id": c.id, "name": c.function, "input": dict(c.arguments or {})})
                if on_delta is not None:
                    on_delta("tool_use", c.function)
                if c.parse_error:
                    result, is_error = c.parse_error, True
                else:
                    result, is_error = await execute(c.function, copy.deepcopy(dict(c.arguments or {})))
                    if self.after_tool is not None:
                        await self.after_tool(n_tool)
                    n_tool += 1
                new_chars += len(result)
                self.messages.append(ChatMessageTool(content=result, tool_call_id=c.id, function=c.function,
                                                     error=ToolCallError("unknown", result) if is_error else None))
        return WakeResult(status=status, stop_reason=stop, text=text, tool_calls=tuple(calls), turns=turns,
                          usage=total, cost_usd=0.0, detail={"harness": "inspect", "model": str(self.model)})


# -- scripts: a reference solution, a recording or a mutant played by mockllm -----------------------------------------
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


def replay_turns(tools: list[dict], final_text: str | None) -> list[dict]:
    """A recorded wake as a script: its calls in one turn (in the recorded order; the runtime-dropped ones marked
    not_run), then its closing text."""
    turn = {"tools": [{"name": c["name"], "input": c.get("args") or {}, "not_run": c.get("code") == "not_run"}
                      for c in tools]}
    return [*([turn] if tools else []), {"text": final_text or ""}]


def scripted_model(turns: list[dict], *, vars: dict | None = None, usage: dict | None = None) -> Model:
    """`mockllm/model` playing `turns` ({text?, tools?: [{name, input, not_run?}]}), one turn a request; a script that
    runs out is an error (a reference solution must end the wake)."""
    env = dict(vars or {})
    use = {**USAGE, **(usage or {})}
    script = copy.deepcopy(list(turns))
    played = {"n": 0}

    def next_output(_input, _tools, _choice, _config) -> ModelOutput:
        if played["n"] >= len(script):
            raise RuntimeError(f"the script ran out after {len(script)} turn(s): a reference must end the wake")
        step = script[played["n"]]
        played["n"] += 1
        calls = [ToolCall(id=f"call_{played['n']}_{i}", function=u["name"], arguments=_fill(u.get("input") or {}, env),
                          parse_error=NOT_RUN if u.get("not_run") else None)
                 for i, u in enumerate(step.get("tools") or [])]
        msg = ChatMessageAssistant(content=step.get("text") or "", tool_calls=calls or None, model="mockllm/model")
        return ModelOutput(model="mockllm/model", choices=[ChatCompletionChoice(
            message=msg, stop_reason="tool_calls" if calls else "stop")],
            usage=ModelUsage(input_tokens=use["input_tokens"], output_tokens=use["output_tokens"],
                             input_tokens_cache_write=use["cache_creation_input_tokens"],
                             input_tokens_cache_read=use["cache_read_input_tokens"],
                             total_tokens=use["input_tokens"] + use["output_tokens"]))

    model = get_model("mockllm/model", custom_outputs=next_output, memoize=False)
    model.script_vars = env  # type: ignore[attr-defined]  # simulated mode fills ${bracket} once the world exists
    return model
