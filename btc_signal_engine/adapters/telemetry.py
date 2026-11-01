"""OpenTelemetry instruments the adapters record into, on the OpenTelemetry API only.

Until bootstrap/telemetry.py installs the SDK (only when an OTLP endpoint is configured) every span and instrument
here is a no-op, so the adapters record unconditionally. What is recorded never carries content: no prompts, model
output, tool arguments or results, order tickets or headers; names, counts, codes, durations and token numbers only.

GenAI (an agent wake): one `invoke_agent settleview` span per wake with the OTel GenAI attributes
(gen_ai.operation.name, gen_ai.provider.name / gen_ai.system = anthropic, gen_ai.request.model, gen_ai.usage.*_tokens,
gen_ai.response.finish_reasons) plus `gen_ai.usage.cost` (USD as the Claude Code CLI reports it; not in the semantic
conventions yet), and one `execute_tool <name>` child span per tool call with its name and result (ok / error).
`gen_ai.usage.input_tokens` counts every input token, cached ones included (the conventions' definition); the cache
reads and writes are also given on their own.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.trace import SpanKind, Status, StatusCode

SCOPE = "settleview"
PROVIDER = "anthropic"
AGENT_NAME = "settleview"
TOKEN_BUCKETS = (1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304)
DURATION_BUCKETS_S = (0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56, 5.12, 10.24, 20.48, 40.96, 81.92)

_tracer = trace.get_tracer(SCOPE)
_meter = metrics.get_meter(SCOPE)

# -- agent wakes (adapters/agent_sdk/harness.py) -----------------------------------------------------------------------
token_usage = _meter.create_histogram(
    "gen_ai.client.token.usage", unit="{token}", description="Tokens used per agent wake, by type (input / output).",
    explicit_bucket_boundaries_advisory=TOKEN_BUCKETS)
operation_duration = _meter.create_histogram(
    "gen_ai.client.operation.duration", unit="s", description="Agent wake duration (one Claude Code CLI run).",
    explicit_bucket_boundaries_advisory=DURATION_BUCKETS_S)
agent_wakes = _meter.create_counter("settleview.agent.wakes", unit="{wake}",
                                    description="Agent wakes finished, by status (done, turn_cap, spend_cap, error).")
agent_cost = _meter.create_counter("settleview.agent.cost", unit="USD",
                                   description="Agent wake cost as the Claude Code CLI reports it.")
agent_tool_calls = _meter.create_counter("settleview.agent.tool_calls", unit="{call}",
                                         description="Agent tool calls, by tool and result (ok / error).")

# -- the database writer (adapters/sqlite/writer.py) -------------------------------------------------------------------
writer_flush = _meter.create_histogram(
    "settleview.writer.flush.duration", unit="s", description="One writer batch commit (its own transaction).",
    explicit_bucket_boundaries_advisory=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5))
writer_batch = _meter.create_histogram(
    "settleview.writer.batch.size", unit="{write}", description="Writes per committed or failed batch.",
    explicit_bucket_boundaries_advisory=(1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 5000))
writer_retries = _meter.create_counter(
    "settleview.writer.retries", unit="{attempt}",
    description="Failed writer batch attempts, by kind (transient: the database unreachable or busy, retried; data: "
                "a refused batch, bisected).")
writer_dead_lettered = _meter.create_counter(
    "settleview.writer.dead_lettered", unit="{write}",
    description="Writes moved to the dead-letter file, by reason (data, overflow, shutdown); replayable.")


def _base(model: str) -> dict[str, Any]:
    return {"gen_ai.operation.name": "invoke_agent", "gen_ai.provider.name": PROVIDER, "gen_ai.system": PROVIDER,
            "gen_ai.request.model": model}


class WakeSpan:
    """One agent wake's `invoke_agent` span and metrics: start it around the CLI run, then `finish` or `fail` it."""

    def __init__(self, *, model: str, session_id: str, max_tokens: int, max_turns: int, effort: str) -> None:
        self.model = model
        self._t0 = time.monotonic()
        self._done = False
        self.span = _tracer.start_span(
            f"invoke_agent {AGENT_NAME}", kind=SpanKind.INTERNAL, record_exception=False,
            set_status_on_exception=False,
            attributes={**_base(model), "gen_ai.agent.name": AGENT_NAME, "gen_ai.conversation.id": session_id,
                        "gen_ai.request.max_tokens": int(max_tokens), "settleview.agent.max_turns": int(max_turns),
                        "settleview.agent.effort": str(effort)})
        self.context = trace.set_span_in_context(self.span)

    def tool(self, name: str) -> ToolSpan:
        return ToolSpan(name, self)

    def finish(self, *, status: str, usage: Mapping[str, int] | None, cost_usd: float | None, turns: int,
               stop_reason: str | None, models: Sequence[str], tool_calls: int, detail: Mapping[str, Any]) -> None:
        u = {k: int(v or 0) for k, v in (usage or {}).items()}
        cache_read, cache_write = u.get("cache_read_input_tokens", 0), u.get("cache_creation_input_tokens", 0)
        tokens_in = u.get("input_tokens", 0) + cache_read + cache_write
        tokens_out = u.get("output_tokens", 0)
        attrs: dict[str, Any] = {
            "gen_ai.usage.input_tokens": tokens_in, "gen_ai.usage.output_tokens": tokens_out,
            "gen_ai.usage.cache_read.input_tokens": cache_read,
            "gen_ai.usage.cache_creation.input_tokens": cache_write,
            "settleview.agent.status": status, "settleview.agent.turns": int(turns),
            "settleview.agent.tool_calls": int(tool_calls)}
        if models:
            attrs["gen_ai.response.model"] = models[0] if len(models) == 1 else ",".join(models)
        if stop_reason:
            attrs["gen_ai.response.finish_reasons"] = (str(stop_reason),)
        if cost_usd is not None:
            attrs["gen_ai.usage.cost"] = float(cost_usd)
        for k in ("cli_version", "terminal_reason", "subtype"):
            if detail.get(k):
                attrs[f"settleview.agent.{k}"] = str(detail[k])
        self.span.set_attributes(attrs)
        self.span.set_status(Status(StatusCode.OK))
        base = _base(self.model)
        if usage:
            token_usage.record(tokens_in, {**base, "gen_ai.token.type": "input"})
            token_usage.record(tokens_out, {**base, "gen_ai.token.type": "output"})
        if cost_usd:
            agent_cost.add(float(cost_usd), {"gen_ai.request.model": self.model})
        self._end(status, None)

    def fail(self, exc: BaseException) -> None:
        """The wake raised (an API error, a misconfiguration, a cancel): its type only, never its message."""
        kind = type(exc).__name__
        self.span.set_attribute("error.type", kind)
        self.span.set_status(Status(StatusCode.ERROR, kind))
        self._end("cancelled" if kind == "CancelledError" else "error", kind)

    def _end(self, status: str, error: str | None) -> None:
        if self._done:
            return
        self._done = True
        attrs = _base(self.model) | ({"error.type": error} if error else {})
        operation_duration.record(time.monotonic() - self._t0, attrs)
        agent_wakes.add(1, {"settleview.agent.status": status, "gen_ai.request.model": self.model})
        self.span.end()


class ToolSpan:
    """One tool call's `execute_tool <name>` span under its wake: a context manager; `result(is_error)` before exit."""

    def __init__(self, name: str, wake: WakeSpan) -> None:
        self.name = name
        self._wake = wake
        self._ok: bool | None = None
        self._cm = _tracer.start_as_current_span(
            f"execute_tool {name}", context=wake.context, kind=SpanKind.INTERNAL, record_exception=False,
            set_status_on_exception=False,
            attributes={"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name,
                        "gen_ai.tool.type": "function", "gen_ai.provider.name": PROVIDER})

    def __enter__(self) -> ToolSpan:
        self.span = self._cm.__enter__()
        return self

    def result(self, is_error: bool) -> None:
        self._ok = not is_error

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None:
            result = "error"
            self.span.set_attribute("error.type", exc_type.__name__)
            self.span.set_status(Status(StatusCode.ERROR, exc_type.__name__))
        elif self._ok is False:
            result = "error"
            self.span.set_attribute("error.type", "tool_error")
            self.span.set_status(Status(StatusCode.ERROR, "tool_error"))
        else:
            result = "ok"
        self.span.set_attribute("settleview.tool.result", result)
        agent_tool_calls.add(1, {"gen_ai.tool.name": self.name, "settleview.tool.result": result})
        self._cm.__exit__(exc_type, exc, tb)
