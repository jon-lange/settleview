"""The Agent panel's live feed as AG-UI events (Agent mode U7; R17, KTD12). docs.ag-ui.com, event reference read
2026-10-30; no AG-UI package is used, the JSON shapes are written here.

Transport: server-sent events through `FeedHub` (append-only, nothing merged, last 200 per slow client). Each frame is
`data: <event json>` and the event's `type` names it. On connect a stream gets STATE_SNAPSHOT first.

Mapping (threadId = the agent session id; one run = one wake, runId = "<session id>:<wake number>"):
- the wake's start (`wake_started`, the runner's start_fn) opens the run: RUN_STARTED with `trigger` (the event types
  that woke it) and `server_ms` (when it started); without that hook the first delta or the row opens it.
- the runner's live deltas (`live`): "thinking" pieces are a reasoning message (REASONING_START,
  REASONING_MESSAGE_START role "reasoning", REASONING_MESSAGE_CONTENT, REASONING_MESSAGE_END, REASONING_END);
  "text" pieces are TEXT_MESSAGE_START (role "assistant") / _CONTENT / _END; a "tool_use" delta (a tool block
  started) closes the open message.
- each model tool call, as the runner runs it (`ObservedTools`): TOOL_CALL_START, TOOL_CALL_ARGS (the arguments as
  JSON, the audit note inside), TOOL_CALL_END, TOOL_CALL_RESULT (role "tool", content = the result JSON).
- the wake's journal row (`wake_row`): any tool call not yet sent, the wake's decision row(s) as CUSTOM "decision"
  (record_decision's, or the runner's UNRECORDED one), then RUN_FINISHED (result = status, decision,
  estimated cost, tokens, turns, trigger) or RUN_ERROR (threadId, runId, message, code = the status) for invalid_key /
  unavailable / bad_request / misconfigured / cancelled; then the state is refreshed.
- the session view (budget, positions, time left, state, prompt versions): STATE_SNAPSHOT on connect, then
  STATE_DELTA (RFC 6902 JSON Patch against the last state sent) after every change and every 15 s while quiet.
- CUSTOM (name, value): "heartbeat" ({server_ms}, every 15 s on a quiet stream: `: ping` comments are invisible to
  EventSource), "decision" (a decision row), "session_state" (running / paused / ended, with the reason and
  pause_code), "note" (a human note queued, then delivered), "tool_call" (an external agent's tool call through the
  tool route), "feed_trimmed" (n events dropped for this slow client).
STEP_STARTED / STEP_FINISHED are not sent: the runner exposes no per-turn boundary.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any

from btc_signal_engine.adapters.http.push import FeedHub
from btc_signal_engine.application.agent_tools import DECISION_PAGE

log = logging.getLogger(__name__)

ERROR_STATUSES = frozenset({"invalid_key", "unavailable", "bad_request", "misconfigured", "cancelled"})
REASONING, TEXT = "reasoning", "text"


def _ptr(key: str) -> str:
    return str(key).replace("~", "~0").replace("/", "~1")


def json_patch(old: Any, new: Any, path: str = "") -> list[dict]:
    """RFC 6902 operations turning `old` into `new`: objects compared key by key, anything else replaced whole."""
    if isinstance(old, dict) and isinstance(new, dict):
        ops: list[dict] = []
        for k in old:
            if k not in new:
                ops.append({"op": "remove", "path": f"{path}/{_ptr(k)}"})
        for k, v in new.items():
            if k not in old:
                ops.append({"op": "add", "path": f"{path}/{_ptr(k)}", "value": v})
            else:
                ops.extend(json_patch(old[k], v, f"{path}/{_ptr(k)}"))
        return ops
    if old == new and type(old) is type(new):
        return []
    return [{"op": "replace", "path": path, "value": new}]


def _dumps(x: Any) -> str:
    return json.dumps(x, separators=(",", ":"), default=str)


def _jsonable(x: Any) -> Any:
    return json.loads(_dumps(x))


class _Run:
    def __init__(self, sid: str, wake_id: int) -> None:
        self.thread, self.id = sid, f"{sid}:{wake_id}"
        self.open: tuple[str, str] | None = None  # (kind, messageId) of the message being streamed
        self.messages = 0
        self.tool_blocks = 0  # tool_use blocks the model started (live deltas)
        self.tools_sent = 0  # tool calls sent as events, in the wake's order
        self.last_text: str | None = None


class AgUiFeed:
    def __init__(self, hub: FeedHub, state_fn: Callable[[], dict],
                 decisions_fn: Callable[..., list[dict]] | None = None,
                 now_fn: Callable[[], int] | None = None) -> None:
        """`state_fn()` -> the shared state (JSON-able dict) the panel mirrors. `decisions_fn(session_id,
        after_id=)` -> the session's decision rows (AgentTools.decisions), when the registry records them. `now_fn()`
        -> the server's clock in ms (the app's clock; default the system clock)."""
        self.hub = hub
        self._state_fn = state_fn
        self._now = now_fn or (lambda: int(time.time() * 1000))
        self._decisions_fn = decisions_fn
        self._decision_seen: dict[str, int] = {}  # session id -> last decision row id sent
        self.state: dict = {}
        self._runs: dict[str, _Run] = {}  # session id -> its open run
        self._session_key: tuple | None = None

    def _emit(self, type_: str, **fields: Any) -> None:
        self.hub.publish({"type": type_, **fields})

    def custom(self, name: str, value: Any) -> None:
        self._emit("CUSTOM", name=name, value=_jsonable(value))

    # -- state --------------------------------------------------------------------------------------------------------
    def first(self) -> list[dict]:
        """A new stream's first events: the state now (refreshed for the streams already open first)."""
        self.refresh()
        return [{"type": "STATE_SNAPSHOT", "snapshot": self.state}]

    def heartbeat(self) -> dict:
        """A quiet stream's event every 15 s (the hub's heartbeat), visible to EventSource unlike `: ping`."""
        return {"type": "CUSTOM", "name": "heartbeat", "value": {"server_ms": self._now()}}

    def refresh(self) -> None:
        try:
            new = _jsonable(self._state_fn())
        except Exception:
            log.warning("agent panel state failed", exc_info=True)
            return
        delta = json_patch(self.state, new)
        self.state = new
        if delta:
            self._emit("STATE_DELTA", delta=delta)

    def session_changed(self, row: dict | None = None) -> None:
        """AgentSessions' notify hook (the row may hold the token hash: only the state and reasons are read)."""
        row = row or {}
        key = (row.get("id"), row.get("state"), row.get("pause_reason"), row.get("pause_code"), row.get("end_reason"))
        if row.get("id") and key != self._session_key:
            self._session_key = key
            self.custom("session_state", {"session_id": row["id"], "state": row.get("state"),
                                          "pause_reason": row.get("pause_reason"),
                                          "pause_code": row.get("pause_code"),
                                          "end_reason": row.get("end_reason")})
        self.refresh()

    def new_decisions(self, sid: str) -> None:
        """Send each decision row of `sid` not sent yet as CUSTOM "decision" (value = the row: {id, ...}), oldest
        first; a full page (DECISION_PAGE rows) reads on from its last id until caught up."""
        if self._decisions_fn is None:
            return
        while True:
            seen = self._decision_seen.get(sid, 0)
            try:
                rows = self._decisions_fn(sid, after_id=seen)
            except Exception:
                log.warning("agent decision rows could not be read", exc_info=True)
                return
            for r in rows:
                self._decision_seen[sid] = max(self._decision_seen.get(sid, 0), int(r["id"]))
                self.custom("decision", {"session_id": sid, **r})
            if len(rows) < DECISION_PAGE or self._decision_seen.get(sid, 0) <= seen:
                return

    # -- a wake as a run -----------------------------------------------------------------------------------------------
    def _run(self, sid: str, wake_id: int, trigger: list[str] | None = None, started: int | None = None) -> _Run:
        run = self._runs.get(sid)
        if run is not None and run.id != f"{sid}:{wake_id}":
            self._close(run)
            run = None
        if run is None:
            run = self._runs[sid] = _Run(sid, wake_id)
            self._emit("RUN_STARTED", threadId=run.thread, runId=run.id, trigger=trigger,
                       server_ms=started if started is not None else self._now())
        return run

    def wake_started(self, row: dict) -> None:
        """The runner's `start_fn`: a wake begins; opens its run with the trigger's event types."""
        sid, rec = row.get("session_id"), row.get("data") or {}
        if not sid or "wake_id" not in rec:
            return
        self._run(sid, rec["wake_id"], [e.get("type") for e in rec.get("trigger") or []], row.get("ts"))

    def _close_message(self, run: _Run) -> None:
        if run.open is None:
            return
        kind, mid = run.open
        if kind == REASONING:
            self._emit("REASONING_MESSAGE_END", messageId=mid)
            self._emit("REASONING_END", messageId=mid)
        else:
            self._emit("TEXT_MESSAGE_END", messageId=mid)
        run.open = None

    def _open_message(self, run: _Run, kind: str) -> str:
        if run.open is not None and run.open[0] == kind:
            return run.open[1]
        self._close_message(run)
        run.messages += 1
        mid = f"{run.id}:m{run.messages}"
        if kind == REASONING:
            self._emit("REASONING_START", messageId=mid)
            self._emit("REASONING_MESSAGE_START", messageId=mid, role="reasoning")
        else:
            self._emit("TEXT_MESSAGE_START", messageId=mid, role="assistant")
            run.last_text = mid
        run.open = (kind, mid)
        return mid

    def live(self, sid: str, wake_id: int, kind: str, text: str) -> None:
        """The runner's `live_fn`: one streamed piece of the model's turn."""
        run = self._run(sid, wake_id)
        if kind == "thinking":
            mid = self._open_message(run, REASONING)
            if text:
                self._emit("REASONING_MESSAGE_CONTENT", messageId=mid, delta=text)
        elif kind == "text":
            mid = self._open_message(run, TEXT)
            if text:
                self._emit("TEXT_MESSAGE_CONTENT", messageId=mid, delta=text)
        elif kind == "tool_use":
            self._close_message(run)
            run.tool_blocks += 1
        else:
            log.warning("agent live delta of unknown kind %r", kind)

    def _tool_events(self, run: _Run, name: str, args: Any, result: Any) -> None:
        run.tools_sent += 1
        tid = f"{run.id}:t{run.tools_sent}"
        start = {"toolCallId": tid, "toolCallName": name}
        if run.last_text is not None:
            start["parentMessageId"] = run.last_text
        self._emit("TOOL_CALL_START", **start)
        self._emit("TOOL_CALL_ARGS", toolCallId=tid, delta=_dumps(args))
        self._emit("TOOL_CALL_END", toolCallId=tid)
        if result is not None:
            self._emit("TOOL_CALL_RESULT", messageId=f"{tid}:result", toolCallId=tid, content=_dumps(result),
                       role="tool")

    def tool_called(self, sid: str, name: str, args: Any, out: Any) -> None:
        """A tool call the runner just ran (`ObservedTools`). Only model tool calls count: the wake's own inline
        observe comes before any tool block and is not one."""
        run = self._runs.get(sid)
        if run is None or run.tools_sent >= run.tool_blocks:
            return
        self._close_message(run)
        self._tool_events(run, name, args, out)

    def wake_row(self, row: dict) -> None:
        """The runner's `notify_fn`: a wake's journal row; ends the run."""
        sid, rec = row.get("session_id"), row.get("data") or {}
        if not sid or "wake_id" not in rec:
            return
        trigger = [e.get("type") for e in rec.get("trigger") or []]
        run = self._run(sid, rec["wake_id"], trigger, int(row.get("ts") or 0) - int(rec.get("duration_ms") or 0))
        self._close_message(run)
        for entry in (rec.get("tools") or [])[run.tools_sent:]:
            self._tool_events(run, entry.get("name"), entry.get("args"), entry.get("result"))
        self.new_decisions(sid)
        status = rec.get("status")
        if status in ERROR_STATUSES:
            self._emit("RUN_ERROR", threadId=run.thread, runId=run.id,
                       message=rec.get("error") or f"The wake ended: {status}.", code=status)
        else:
            result = {k: rec.get(k) for k in ("status", "decision", "est_usd", "tokens", "turns", "duration_ms",
                                              "stop_reason", "refusal", "reason")}
            result["trigger"] = trigger
            self._emit("RUN_FINISHED", threadId=run.thread, runId=run.id, result=_jsonable(result))
        self._runs.pop(sid, None)
        self.refresh()

    def _close(self, run: _Run) -> None:
        """A run left open (its row never came): closed as an error so the panel never shows it running."""
        self._close_message(run)
        self._emit("RUN_ERROR", threadId=run.thread, runId=run.id, message="This wake ended without a record.",
                   code="lost")


class ObservedTools:
    """The runner's tool registry with each call reported to the feed (the registry itself is unchanged)."""

    def __init__(self, tools: Any, feed: AgUiFeed) -> None:
        self._tools, self._feed = tools, feed

    def __getattr__(self, name: str) -> Any:
        return getattr(self._tools, name)

    async def call(self, name: str, args: Any, now: int, *, session_id: str | None = None, **kw: Any) -> dict:
        out = await self._tools.call(name, args, now, session_id=session_id, **kw)
        if session_id is not None and kw.get("by_rule") is None:  # a rule's entry is not the model's tool call
            try:
                self._feed.tool_called(session_id, name, args, out)
            except Exception:
                log.warning("agent feed tool hook failed", exc_info=True)
        return out


class FleetFeeds:
    """The feed router for many sessions (multi-agent U5): every event reaches the all-sessions feed (`main`, the
    deprecated /api/agent/stream, whose state is the latest session's) and, when that session's own stream exists, the
    session's feed (/api/agent/sessions/{id}/stream: its events only, its own STATE_SNAPSHOT / STATE_DELTA). A
    session's feed is made on its first stream (`for_session`) and dropped once its session is over and nobody
    listens (`prune`), so memory and refresh work grow with open streams, not with sessions. The runner's and the
    routes' hooks call this exactly as they call one AgUiFeed."""

    def __init__(self, main: AgUiFeed, make: Callable[[str], AgUiFeed], *,
                 live_fn: Callable[[str], bool] = lambda sid: True) -> None:
        """`make(session_id)` -> a fresh AgUiFeed (its own hub) whose state is that session's panel; `live_fn(id)`:
        the session is still running or paused (an ended one's feed goes once nobody listens)."""
        self.main = main
        self.hub = main.hub
        self._make = make
        self._live = live_fn
        self._feeds: dict[str, AgUiFeed] = {}

    def for_session(self, sid: str) -> AgUiFeed:
        f = self._feeds.get(sid)
        if f is None:
            f = self._feeds[sid] = self._make(sid)
        return f

    def _sub(self, sid: Any) -> AgUiFeed | None:
        return self._feeds.get(sid) if isinstance(sid, str) else None

    def prune(self) -> None:
        for sid in [sid for sid, f in self._feeds.items() if f.hub.subscribers == 0 and not self._live(sid)]:
            del self._feeds[sid]

    # -- AgUiFeed's surface, routed ----------------------------------------------------------------------------------
    def first(self) -> list[dict]:
        return self.main.first()

    def heartbeat(self) -> dict:
        return self.main.heartbeat()

    def custom(self, name: str, value: Any) -> None:
        self.main.custom(name, value)
        sub = self._sub(value.get("session_id") if isinstance(value, dict) else None)
        if sub is not None:
            sub.custom(name, value)

    def refresh(self) -> None:
        self.main.refresh()
        for f in list(self._feeds.values()):
            if f.hub.subscribers:
                f.refresh()
        self.prune()

    def session_changed(self, row: dict | None = None) -> None:
        self.main.session_changed(row)
        sub = self._sub((row or {}).get("id"))
        if sub is not None:
            sub.session_changed(row)
        for _sid, f in list(self._feeds.items()):  # another session changed: the others' "also traded by" and totals
            if f is not sub and f.hub.subscribers:
                f.refresh()
        self.prune()

    def new_decisions(self, sid: str) -> None:
        self.main.new_decisions(sid)
        sub = self._sub(sid)
        if sub is not None:
            sub.new_decisions(sid)

    def wake_started(self, row: dict) -> None:
        self.main.wake_started(row)
        sub = self._sub(row.get("session_id"))
        if sub is not None:
            sub.wake_started(row)

    def live(self, sid: str, wake_id: int, kind: str, text: str) -> None:
        self.main.live(sid, wake_id, kind, text)
        sub = self._sub(sid)
        if sub is not None:
            sub.live(sid, wake_id, kind, text)

    def tool_called(self, sid: str, name: str, args: Any, out: Any) -> None:
        self.main.tool_called(sid, name, args, out)
        sub = self._sub(sid)
        if sub is not None:
            sub.tool_called(sid, name, args, out)

    def wake_row(self, row: dict) -> None:
        self.main.wake_row(row)
        sub = self._sub(row.get("session_id"))
        if sub is not None:
            sub.wake_row(row)

    async def close(self) -> None:
        await self.main.hub.close()
        for f in self._feeds.values():
            await f.hub.close()
