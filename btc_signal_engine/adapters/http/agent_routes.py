"""HTTP routes for Agent mode (docs/plans/2026-10-30-1402-feat-agent-mode-mcp-plan.md U7; R1, R4, R15-R20, KTD9,
KTD12). Registered only when the app's agent flag is on. Never returns the API key, a token hash or key material.

Refusals are HTTP 200 {"ok": false, "code", "reason"}; a malformed body is 422; PC-only actions answer a phone 403
"Only from the PC itself." (the access middleware's actor, `from_pc`); an unknown session id in a
/api/agent/sessions/{id}/... path is 404 {ok: false, code: not_found}.

Multi-agent (docs/plans/2026-10-31-0010-feat-multi-agent-plan.md U5; full shapes in docs/api.md): any number of
sessions run at once, each names its session:
  GET  /api/agent/sessions        ?ended=0-50 -> {ok, sessions: [row], summary, server_ms} (agent_fleet_view)
  GET  /api/agent/fleet           {ok, summary, overlaps, server_ms}
  GET  /api/agent/sessions/{id}   {ok, session (view), row, pending_proposals, positions, server_ms}
  POST /api/agent/sessions/{id}/stop | pause | resume {confirm?} | note {text} | rename {name}
  GET  /api/agent/sessions/{id}/journal ?after_id=&limit= | decisions ?after_id= -> {ok, session_id, rows (oldest
                                  first), more, next_after_id}: after_id 0 the newest rows; > 0 the oldest after it
                                  (page on from next_after_id while more)
  GET  /api/agent/sessions/{id}/proposals | stream (SSE: that session's events only)
  POST /api/agent/stop_all        {} -> {ok, stopped: [{id, name, ok, code, reason}]}: PC or phone
  GET  /api/agent/global_limits   POST (PC only) {max_loss_day?, api_day?: dollars or null} (KD3)
  GET  /api/agent/agents          ?archived= -> {ok, agents: [{id, name, market, ..., runs, last_run, net}]} (L0)
  GET  /api/agent/agents/{id}/journal ?before_id=&limit=&kinds=a,b -> {ok, agent_id, rows (newest first, each with
                                  session_id and agent_id), next_before_id, runs: [run summary]}
  POST /api/agent/agents/{id}/rename {name}
  POST /api/agent/agents/{id}/archive (PC only) -> {ok, agent}: Record's "Retire"; agent_busy while it runs
  GET  /api/agent/journal/all     the same paging and filters over every agent (the fleet's audit)
The single-session wrappers (POST stop / resume / note, GET journal / decisions without a session, the all-sessions
stream, status's `session`, prompts' `session_versions`) were removed with the fleet UI (2026-10-31): every call
names its session.

  GET  /api/agent/status          {ok, in_app {available, reason} (no key / key invalid at the last check), external
                                  {available}, key (its view: never the key), models [{id, usd_per_mtok}], venues {key:
                                  {label, strategy, mode, rules, also_traded_by, next_agent}}, rules [{id, markets,
                                  market_labels, label, description, status {status, reason, at, by, since}}],
                                  allow_production, live_count, prompts (current versions), daily (the shared daily
                                  total), global_limits {limits, used}, wake_cap, settle_warnings (positions settling
                                  > 60 min after the close: "check the venue"), server_ms (the server's clock)}
  POST /api/agent/launch          {kind, market (one key; or DEPRECATED venues: [one key]), name?, budget, per_trade, minutes | ends_ts (exactly one), spend_cap?,
                                  model?, sell_on_stop?, rules? (rule ids; default none),
                                  one_bet_per_window? (default true), level ("auto" | "assist", required: missing
                                  or unknown is refused, code invalid_level), proposal_ttl_s? (assist only, 10-60,
                                  default 20), confirm: "START"} -> {ok, id, session, also_traded_by, token?
                                  (external, shown once)}; another live session does not refuse a launch;
                                  a phone may launch only within the user's per-trade limit
  GET  /api/agent/scorecard       the rule scorekeeper (rule_scores.scorecard): per rule and per (rule, market) the
                                  status, the live tally, the 30-trade bar and the backtest reference; `agents`: per
                                  session (by agent) its tally, API spend and net after API
  POST /api/agent/rules/status    {rule, status: "retired" | "active", reason?, confirm?} (PC only) -> {ok, changed, rule};
                                  reactivating needs confirm "REACTIVATE <rule>"; journaled into every live session arming it
                                  and sent as CUSTOM "rule_status"
  GET  /api/agent/prompts         both layers (current text and version) + history
  GET  /api/agent/prompts/version ?layer=&version= -> one version's full text
  POST /api/agent/prompts/save    {layer, text}      (PC only) -> {ok, changed, prompt}
  POST /api/agent/prompts/restore {layer, version}   (PC only)
  POST /api/agent/prompts/reset   {layer}            (PC only)
  GET  /api/agent/proposals       ?session_id= -> {ok, session_id, pending, recent, server_ms} (assist proposals; an
                                  unknown session_id is 404 not_found; without session_id: every live session's
                                  pending, recent []). A session's stream
                                  (/sessions/{id}/stream) carries CUSTOM "proposal_new" / "proposal_resolved"
  POST /api/agent/proposals/approve {id} -> the order's answer + {proposal}: the user only (PC or phone; a request
                                  carrying X-Agent-Token is refused 403 agent_cannot_approve); sent through the
                                  slide's prepare -> OrderGate -> approve path with a person present
  POST /api/agent/proposals/skip  {id} -> {ok, proposal}: the user only; nothing is sent
Tool endpoints for external agents (MCP, U8), on the agent listener (BTCFLOW_AGENT_PORT) only (actor "agent-api",
never the PC or a person; the other listeners answer 403 agent_port, access.py):
  GET  /api/agent/tools           the registry: [{name, description, input_schema, kind}]
  POST /api/agent/call/{tool}     body = the tool's arguments; header X-Agent-Token. Every call (reads too) needs the
                                  token of a live external session, else 403 and nothing is prepared. The agent actor
                                  is set only after the token checks out (AgentTools.call), never from a header or
                                  field.
  GET  /api/agent/events/wait     ?timeout_s<=25: not available yet (external sessions have no event engine yet)
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from fastapi import Body, FastAPI, Header, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from btc_signal_engine.adapters.http.access import AGENT_API, from_pc, human, only_pc, only_people
from btc_signal_engine.application.actor import actor
from btc_signal_engine.application.agent_prompts import AgentPrompts, PromptError
from btc_signal_engine.application.agent_proposals import AgentProposals
from btc_signal_engine.application.agent_rules import catalog as rule_catalog
from btc_signal_engine.application.agent_runner import PRICES
from btc_signal_engine.application.agent_fleet_view import FleetView
from btc_signal_engine.application.agent_sessions import JOURNAL, SESSIONS, VENUE_LABELS, VENUES, AgentSessions
from btc_signal_engine.application.rule_scores import BRACKETS, REACTIVATE, scorecard, tally, trade_of
from btc_signal_engine.application.ports import LlmInvalidKey, LlmUnavailable

log = logging.getLogger(__name__)

LAUNCH_CONFIRM = "START"
RESUME_CONFIRM = "RESUME"
TOKEN_HEADER = "X-Agent-Token"  # noqa: S105 - a header name, not a secret
WAIT_MAX_S = 25
NOTE_MAX = 1000


class LaunchBody(BaseModel):
    # Field types are checked by AgentSessions.launch (each refusal names the field); only the phrase is checked here.
    kind: Any = None
    agent_id: Any = None  # run this existing agent again (L0); absent: `name` makes a new one, neither the default
    market: Any = None  # ONE market key (multi-agent: one agent = one market); a list is refused (one_market)
    venues: Any = None  # DEPRECATED: the old field, accepted only as a list of exactly one market
    name: Any = None  # optional; default "<market label> agent" (then " 2", " 3" ...); unique among live sessions
    budget: Any = None
    per_trade: Any = None
    ends_ts: Any = None
    minutes: Any = None
    spend_cap: Any = None
    model: Any = None
    sell_on_stop: Any = False
    rules: Any = Field(default_factory=list)  # entry rule ids (agent_rules); unknown ids are refused at launch
    one_bet_per_window: Any = True  # at most one rule entry per settlement instant across venues (False: sampling)
    level: Any = None  # "auto" | "assist": required (AgentSessions refuses a missing or unknown one: invalid_level)
    proposal_ttl_s: Any = None  # assist only: how long a proposal waits (10-60 s; default 20)
    confirm: str | None = Field(None, max_length=50)


class RuleStatusBody(BaseModel):
    rule: str = Field(max_length=32)
    status: str = Field(max_length=16)
    reason: str | None = Field(None, max_length=300)
    confirm: str | None = Field(None, max_length=50)


class ResumeBody(BaseModel):
    confirm: str | None = Field(None, max_length=50)


class ProposalBody(BaseModel):
    id: str = Field(min_length=1, max_length=64)


class RenameBody(BaseModel):
    name: Any = None  # checked by AgentSessions.rename (1-40 characters, unique among live sessions)


class NoteBody(BaseModel):
    text: str = Field(min_length=1, max_length=NOTE_MAX)


class SaveBody(BaseModel):
    layer: Literal["system", "user"]
    text: str


class RestoreBody(BaseModel):
    layer: Literal["system", "user"]
    version: int = Field(ge=0)


class ResetBody(BaseModel):
    layer: Literal["system", "user"]


def refuse(code: str, reason: str) -> dict:
    return {"ok": False, "code": code, "reason": reason}


class KeyState:
    """What the app knows about the user's Anthropic key: present, and valid at the last check (never the key)."""

    def __init__(self, present: bool) -> None:
        self.present = present
        self.valid: bool | None = None
        self.checked_ts: int | None = None
        self.reason: str | None = None

    def view(self) -> dict:
        return {"present": self.present, "valid": self.valid, "checked_ts": self.checked_ts, "reason": self.reason}


class AgentActions:
    """The launch / stop / pause / resume / note paths the routes run, shared with the Telegram bot (U11) so a remote
    command takes exactly the route's path (same checks, same fleet, same feed events). Every one names its session
    (multi-agent KD1). Exposed as `app.state.agent_actions` by `register_agent_routes`."""

    def __init__(self, *, sessions: AgentSessions, prompts: AgentPrompts, feed: Any,
                 now_fn: Callable[[], int], llm: Any | None, fleet: Any | None,
                 start_task: Callable[[str, Callable[[], Awaitable[Any]]], Any], limits: Any | None,
                 proposals: AgentProposals | None, key: KeyState) -> None:
        self.sessions, self.prompts, self.feed, self.now_fn = sessions, prompts, feed, now_fn
        self.llm, self.fleet, self.start_task, self.limits, self.proposals = llm, fleet, start_task, limits, proposals
        self.key = key
        self._fleet_started = False

    def in_app_unavailable(self) -> str | None:
        if self.llm is None or self.fleet is None:
            return "agent unavailable: no key (set BTCFLOW_ANTHROPIC_API_KEY in .env and restart)"
        if self.key.valid is False:
            return f"agent unavailable: invalid key ({self.key.reason})"
        return None

    @staticmethod
    def _sell_on_stop() -> dict:
        return refuse("not_available", "Sell on stop is not available yet; launch without it (positions keep "
                                       "their take profit and stops and pass to you).")

    @staticmethod
    def _no_assist() -> dict:
        return refuse("assist_unavailable", "Assist is not available in this app.")

    @staticmethod
    def _unknown_model(model: Any) -> dict:
        return refuse("unknown_model", f"Unknown model {model!r}; models are {', '.join(PRICES)}.")

    def validate(self, spec: dict, now: int) -> dict | None:
        """A dry run of every launch check except the key and the phone's limit (a saved preset): the refusal
        launch would give, or None. Nothing is stored."""
        if spec.get("sell_on_stop") is True:
            return self._sell_on_stop()
        if spec.get("level") == "assist" and self.proposals is None:
            return self._no_assist()
        if spec.get("kind") == "in_app" and spec.get("model") not in PRICES:
            return self._unknown_model(spec.get("model"))
        return self.sessions.validate(spec, now)

    def _within_your_limit(self, spec: dict) -> dict | None:
        if self.limits is None:
            return refuse("limits_unknown", "Your trade limits cannot be read here; launch from the PC.")
        try:
            per_trade_limit = self.limits.current().per_trade
        except ValueError as e:
            return refuse("limits_unreadable", f"Your trade limits cannot be read ({e}); launch from the PC.")
        pt = spec.get("per_trade")
        if per_trade_limit is not None and isinstance(pt, (int, float)) and pt > per_trade_limit + 1e-9:
            return refuse("over_your_limit", f"From the phone a session's per-trade cap must be within your "
                                             f"${per_trade_limit:.2f} per-trade limit.")
        return None

    def _start_fleet(self) -> None:
        """The fleet's one tick loop (a supervised, non-critical task), started with the first in-app session."""
        if self._fleet_started or self.fleet is None:
            return
        self._fleet_started = True
        self.start_task("agent_fleet", self.fleet.run)

    async def launch(self, spec: dict, *, from_pc: bool) -> dict:
        """Launch a session from a body without its confirm phrase (the caller checked it). Not from the PC (a phone or
        the Telegram bot): the per-trade cap must be within the user's per-trade limit. Other live sessions do not
        matter (any number run at once); the answer carries the new `id` and `also_traded_by`."""
        if spec.get("sell_on_stop") is True:
            return self._sell_on_stop()
        if not from_pc:
            bad = self._within_your_limit(spec)
            if bad is not None:
                return bad
        if spec.get("level") == "assist" and self.proposals is None:
            return self._no_assist()
        if spec.get("kind") == "in_app":
            why = self.in_app_unavailable()
            if why is not None:
                return refuse("agent_unavailable", why)
            if spec.get("model") not in PRICES:
                return self._unknown_model(spec.get("model"))
            bad = self.sessions.validate(spec, self.now_fn())  # nothing costs a key check that launch would refuse
            if bad is not None:
                return bad
            key = self.key
            try:
                await self.llm.check_key()
            except LlmInvalidKey as e:
                key.valid, key.reason, key.checked_ts = False, str(e), self.now_fn()
                return refuse("agent_unavailable", "agent unavailable: invalid key")
            except LlmUnavailable as e:
                return refuse("model_unavailable", f"agent unavailable: {e}")
            key.valid, key.reason, key.checked_ts = True, None, self.now_fn()
        now = self.now_fn()
        out = self.sessions.launch(spec, now, prompt_versions=self.prompts.versions())
        if out.get("ok") and spec.get("kind") == "in_app":
            self.fleet.attach(out["id"])  # the events engine exists before the first note can arrive
            self._start_fleet()
        if out.get("ok"):
            self.feed.refresh()
        return out

    async def stop(self, session_id: str) -> dict:
        """Stop one session (PC or phone, no confirmation: R18); the others keep running."""
        s = self.sessions.get(session_id)
        if s is None:
            return refuse("no_session", "That agent session is not running; nothing to stop.")
        now = self.now_fn()
        if s["kind"] == "in_app" and self.fleet is not None:
            return await self.fleet.stop(session_id, now)
        return self.sessions.stop(session_id, now)

    async def stop_all(self) -> dict:
        """Stop every live session: {ok, stopped: [{id, name, ok, ...}]}; ok False with code no_session when none."""
        live = self.sessions.live()
        if not live:
            return refuse("no_session", "There is no agent session to stop.")
        names = {s["id"]: self.sessions.name_of(s) for s in live}
        now = self.now_fn()
        if self.fleet is not None:
            outs = await self.fleet.stop_all(now)
        else:
            outs = [{"id": sid, **self.sessions.stop(sid, now)} for sid in names]
        return {"ok": all(o.get("ok") for o in outs),
                "stopped": [{"id": o["id"], "name": names.get(o["id"]), "ok": bool(o.get("ok")),
                             "code": o.get("code"), "reason": o.get("reason")} for o in outs]}

    def pause(self, session_id: str, why: str) -> dict:
        """Pause one running session (the user's own pause; resuming stays on the PC or the phone's RESUME)."""
        s = self.sessions.get(session_id)
        if s is None:
            return refuse("no_session", "That agent session is not running; nothing to pause.")
        if s["state"] != "running":
            return refuse("not_running", f"The agent session is already {s['state']}.")
        now = self.now_fn()
        self.sessions.pause(session_id, now, why, "other")
        return {"ok": True, "session": self.sessions.view(session_id, now)}

    def resume(self, session_id: str) -> dict:
        return self.sessions.resume(session_id, self.now_fn())

    def note(self, session_id: str, text: str) -> dict:
        s = self.sessions.get(session_id)
        if s is None:
            return refuse("no_session", "That agent session is not running or paused; no note was sent.")
        now = self.now_fn()
        data = {"text": text, "by": actor(), "state": "queued", "delivered_ts": None}
        nid = self.sessions.journal("note", data, now, session_id=session_id)
        ev = self.fleet.events(session_id) if (s["kind"] == "in_app" and self.fleet is not None) else None
        if ev is not None:
            ev.push(now, "human_note", value={"notes": [{"id": nid, "text": text}]})
        note = {"id": nid, "session_id": session_id, "ts": now, **data}
        self.feed.custom("note", note)
        return {"ok": True, "note": note}

    def rename(self, session_id: str, name: Any) -> dict:
        out = self.sessions.rename(session_id, name, self.now_fn())
        if out.get("ok"):
            self.feed.refresh()
        return out

    def answered(self, out: dict) -> dict:
        """After a proposal's answer (the card or Telegram): the panel's decision rows and state."""
        p = out.get("proposal") or {}
        if p.get("session_id"):
            self.feed.new_decisions(p["session_id"])
        self.feed.refresh()
        return out


def register_agent_routes(app: FastAPI, *, sessions: AgentSessions, tools: Any, prompts: AgentPrompts,
                          feed: Any, now_fn: Callable[[], int], llm: Any | None, fleet: Any | None,
                          start_task: Callable[[str, Callable[[], Awaitable[Any]]], Any],
                          limits: Any | None = None, daily_fn: Callable[[int], dict] | None = None,
                          proposals: AgentProposals | None = None,
                          session_state_fn: Callable[[str], dict] | None = None) -> KeyState:
    """`llm` / `fleet`: None without an API key (in-app launches refused "no key"). `start_task(name, factory)` runs
    the fleet's tick loop as a supervised, non-critical task (started with the first in-app launch). `feed`: the
    feed router (agent_feed.FleetFeeds: the all-sessions stream and one per session; a plain AgUiFeed also works,
    without per-session streams). `limits`: the user's play limits (`current()`), for a phone launch. `daily_fn(now)`:
    the shared daily total. `proposals`: the assist proposals (None: an assist launch is refused, code
    assist_unavailable). `session_state_fn(id)`: one session's panel state (its per-session stream and status).
    Returns the key state (the status line)."""
    key = KeyState(llm is not None)
    actions = AgentActions(sessions=sessions, prompts=prompts, feed=feed, now_fn=now_fn, llm=llm, fleet=fleet,
                           start_task=start_task, limits=limits, proposals=proposals, key=key)
    app.state.agent_actions = actions  # the Telegram bot's launch / stop / pause / note (U11)
    in_app_unavailable = actions.in_app_unavailable
    view = FleetView(sessions, proposals=proposals, fleet=fleet)

    def venues() -> dict:
        """Each agent market: its label, trading strategy, that strategy's mode now (None: not in this app), its
        rules and the live sessions on it (KD2's "also traded by")."""
        out = {}
        for k, strategy in VENUES.items():
            svc = sessions.services.get(strategy)
            out[k] = {"label": VENUE_LABELS[k], "strategy": strategy, "mode": getattr(svc, "mode", None),
                      "rules": [r["id"] for r in rule_catalog() if k in r["markets"]],
                      "also_traded_by": sessions.also_traded_by(k), "next_agent": sessions.next_agent(k)}
        return out

    def settle_warnings(now: int) -> list[dict]:
        """Every agent venue's positions settling too long after the close (code review #7): shown, never closed."""
        out, seen = [], set()
        for k, strategy in VENUES.items():
            fn = getattr(sessions.services.get(strategy), "settle_warnings", None)
            if fn is not None and strategy not in seen:
                seen.add(strategy)
                out += [{**w, "venue": k, "venue_label": VENUE_LABELS[k]} for w in fn(now)]
        return out

    def rules_view() -> list[dict]:
        book = sessions.rule_book
        return [{**r, "status": book.status(r["id"])} for r in rule_catalog()]

    def not_found(sid: str) -> JSONResponse:
        return JSONResponse(refuse("not_found", f"There is no agent session {sid!r}."), status_code=404)

    # -- status ---------------------------------------------------------------------------------------------------
    @app.get("/api/agent/status")
    async def agent_status() -> dict:
        now = now_fn()
        daily: dict | None = None
        if daily_fn is not None:
            try:
                daily = daily_fn(now)
            except Exception as e:  # shown as an error, never as zero
                daily = {"error": f"The daily total could not be read: {e}"}
        why = in_app_unavailable()
        return {"ok": True, "in_app": {"available": why is None, "reason": why}, "external": {"available": True},
                "key": key.view(),
                "models": [{"id": m, "usd_per_mtok": {"input": p.input, "output": p.output,
                                                      "cache_read": p.cache_read, "cache_write": p.cache_write}}
                           for m, p in PRICES.items()],
                "venues": venues(), "rules": rules_view(), "allow_production": sessions.allow_production,
                "live_count": len(sessions.live()),
                "prompts": prompts.versions(), "daily": daily,
                "global_limits": {"limits": sessions.global_limits(), "used": sessions.global_usage(now)},
                "wake_cap": fleet.scheduler.cap if fleet is not None else None,
                "settle_warnings": settle_warnings(now), "server_ms": now}

    # -- the fleet (multi-agent U5) ------------------------------------------------------------------------------
    @app.get("/api/agent/sessions")
    async def agent_sessions(ended: int = Query(0, ge=0, le=50)) -> dict:
        now = now_fn()
        rows = view.rows(now, include_ended=ended)
        return {"ok": True, "sessions": rows, "summary": view.summary(now, rows), "server_ms": now}

    @app.get("/api/agent/fleet")
    async def agent_fleet() -> dict:
        now = now_fn()
        return {"ok": True, "summary": view.summary(now), "overlaps": sessions.overlaps(), "server_ms": now}

    @app.get("/api/agent/sessions/{sid}")
    async def agent_session_status(sid: str = Path(max_length=40)):
        now = now_fn()
        row = sessions.row(sid)
        if row is None:
            return not_found(sid)
        out = {"ok": True, "session": sessions.view(sid, now), "row": view.row(row, now),
               "pending_proposals": proposals.pending(sid) if proposals is not None else [], "server_ms": now}
        if session_state_fn is not None:
            out["positions"] = session_state_fn(sid).get("positions", [])
        return out

    @app.post("/api/agent/sessions/{sid}/stop")
    async def agent_session_stop(sid: str = Path(max_length=40)):
        if sessions.row(sid) is None:
            return not_found(sid)
        return await actions.stop(sid)

    @app.post("/api/agent/sessions/{sid}/pause")
    async def agent_session_pause(sid: str = Path(max_length=40)):
        if sessions.row(sid) is None:
            return not_found(sid)
        return actions.pause(sid, "Paused by you.")

    @app.post("/api/agent/sessions/{sid}/resume")
    async def agent_session_resume(request: Request, sid: str = Path(max_length=40), body: ResumeBody | None = None):
        if sessions.row(sid) is None:
            return not_found(sid)
        if not from_pc(request) and (body is None or body.confirm != RESUME_CONFIRM):
            return refuse("confirm_required", f"Type {RESUME_CONFIRM} to resume the session from the phone.")
        return actions.resume(sid)

    @app.post("/api/agent/sessions/{sid}/note")
    async def agent_session_note(body: NoteBody, sid: str = Path(max_length=40)):
        if sessions.row(sid) is None:
            return not_found(sid)
        return actions.note(sid, body.text)

    @app.post("/api/agent/sessions/{sid}/rename")
    async def agent_session_rename(body: RenameBody, sid: str = Path(max_length=40)):
        if sessions.row(sid) is None:
            return not_found(sid)
        return actions.rename(sid, body.name)

    @app.get("/api/agent/sessions/{sid}/journal")
    async def agent_session_journal(sid: str = Path(max_length=40), after_id: int = Query(0, ge=0),
                                    limit: int = Query(200, ge=1, le=500)):
        if sessions.row(sid) is None:
            return not_found(sid)
        rows, more = sessions.journal_after(sid, after_id=after_id, limit=limit)
        return {"ok": True, "session_id": sid, "rows": rows, "more": more,
                "next_after_id": rows[-1]["id"] if rows else after_id}

    @app.get("/api/agent/sessions/{sid}/decisions")
    async def agent_session_decisions(sid: str = Path(max_length=40), after_id: int = Query(0, ge=0)):
        if sessions.row(sid) is None:
            return not_found(sid)
        rows, more = tools.decisions_after(sid, after_id=after_id)
        return {"ok": True, "session_id": sid, "rows": rows, "more": more,
                "next_after_id": rows[-1]["id"] if rows else after_id}

    @app.get("/api/agent/sessions/{sid}/proposals")
    async def agent_session_proposals(sid: str = Path(max_length=40)):
        if proposals is None:
            return no_proposals()
        if sessions.row(sid) is None:
            return not_found(sid)
        return {"ok": True, "session_id": sid, "pending": proposals.pending(sid), "recent": proposals.recent(sid),
                "server_ms": now_fn()}

    @app.get("/api/agent/sessions/{sid}/stream")
    async def agent_session_stream(sid: str = Path(max_length=40)):
        if sessions.row(sid) is None:
            return not_found(sid)
        for_session = getattr(feed, "for_session", None)
        if for_session is None:
            return refuse("not_available", "Per-session streams are not available in this app.")
        f = for_session(sid)
        return f.hub.response(f.first, idle=f.refresh, heartbeat=f.heartbeat)

    @app.post("/api/agent/stop_all")
    async def agent_stop_all() -> dict:
        # PC or phone, no typed phrase (stopping only lowers risk, R18); the screen asks "Stop all?" first
        return await actions.stop_all()

    @app.get("/api/agent/global_limits")
    async def agent_global_limits() -> dict:
        now = now_fn()
        return {"ok": True, "limits": sessions.global_limits(), "used": sessions.global_usage(now), "server_ms": now}

    @app.post("/api/agent/global_limits")
    async def agent_set_global_limits(request: Request, body: dict[str, Any] = Body(...)):
        if not from_pc(request):
            return only_pc()
        out = sessions.set_global_limits(body, now_fn())
        if out.get("ok"):
            sessions.review(now_fn())  # a limit already reached pauses every agent at once
            feed.refresh()
        return out

    # -- agents (L0) and the journal across runs (U6b) ---------------------------------------------------------
    def kinds_of(kinds: str | None) -> list[str] | None:
        return [k for k in kinds.split(",") if k] if kinds else None

    def page(agent_id: str | None, before_id: int | None, limit: int, kinds: str | None) -> dict:
        try:
            out = sessions.journal_page(agent_id=agent_id, before_id=before_id, limit=limit, kinds=kinds_of(kinds))
        except ValueError as e:
            return refuse("invalid", str(e))
        return {"ok": True, "agent_id": agent_id, **out, "server_ms": now_fn()}

    @app.get("/api/agent/agents")
    async def agent_agents(archived: bool = Query(False)) -> dict:
        now = now_fn()
        return {"ok": True, "agents": sessions.agent_list(now, archived=archived), "server_ms": now}

    @app.get("/api/agent/agents/{agent_id}/journal")
    async def agent_agent_journal(agent_id: str = Path(max_length=40), before_id: int | None = Query(None, ge=1),
                                  limit: int = Query(100, ge=1, le=500),
                                  kinds: str | None = Query(None, max_length=400)):
        if sessions.agent(agent_id) is None:
            return JSONResponse(refuse("not_found", f"There is no agent {agent_id!r}."), status_code=404)
        return page(agent_id, before_id, limit, kinds)

    @app.post("/api/agent/agents/{agent_id}/rename")
    async def agent_agent_rename(body: RenameBody, agent_id: str = Path(max_length=40)):
        if sessions.agent(agent_id) is None:
            return JSONResponse(refuse("not_found", f"There is no agent {agent_id!r}."), status_code=404)
        out = sessions.rename_agent(agent_id, body.name, now_fn())
        if out.get("ok"):
            feed.refresh()
        return out

    @app.post("/api/agent/agents/{agent_id}/archive")
    async def agent_agent_archive(request: Request, agent_id: str = Path(max_length=40)):
        # Record's "Retire": PC only (it cannot be undone from the app)
        if not from_pc(request):
            return only_pc()
        if sessions.agent(agent_id) is None:
            return JSONResponse(refuse("not_found", f"There is no agent {agent_id!r}."), status_code=404)
        out = sessions.archive_agent(agent_id, now_fn())
        if out.get("ok"):
            feed.refresh()
        return out

    @app.get("/api/agent/journal/all")
    async def agent_journal_all(before_id: int | None = Query(None, ge=1), limit: int = Query(100, ge=1, le=500),
                                kinds: str | None = Query(None, max_length=400)):
        return page(None, before_id, limit, kinds)

    # -- launch ------------------------------------------------------------------------------------------------------
    @app.post("/api/agent/launch")
    async def agent_launch(body: LaunchBody, request: Request) -> dict:
        if body.confirm != LAUNCH_CONFIRM:
            return refuse("confirm_required", f"Type {LAUNCH_CONFIRM} to launch an agent session.")
        spec = body.model_dump(exclude={"confirm"}, exclude_none=False)
        for k in ("agent_id", "market", "venues", "name"):  # absent stays absent (launch tells them apart)
            if spec.get(k) is None:
                spec.pop(k, None)
        return await actions.launch(spec, from_pc=from_pc(request))

    @app.get("/api/agent/scorecard")
    async def agent_scorecard() -> dict:
        return {"ok": True, **scorecard(sessions.repo, sessions.rule_book), "agents": agent_tallies(sessions),
                "server_ms": now_fn()}

    @app.post("/api/agent/rules/status")
    async def agent_rule_status(body: RuleStatusBody, request: Request):
        if not from_pc(request):
            return only_pc()
        book = sessions.rule_book
        try:
            cur = book.status(body.rule)
        except ValueError as e:
            return refuse("invalid", str(e))
        if body.status not in ("retired", "active"):
            return refuse("invalid", f"status must be retired or active (got {body.status!r}).")
        if body.status == "active":
            phrase = f"{REACTIVATE} {body.rule}"
            if (body.confirm or "").strip() != phrase:
                return refuse("confirm_required", f"Type {phrase} to reactivate rule {body.rule}; it starts a fresh "
                                                  "30-trade trial.")
        if cur["status"] == body.status:
            return {"ok": True, "changed": False, "rule": cur}
        now = now_fn()
        reason = (body.reason or "").strip() or ("Retired by you." if body.status == "retired" else
                                                  "Reactivated by you: a fresh 30-trade trial.")
        st = book.set(body.rule, body.status, now, by="user", reason=reason)
        change = {"rule": body.rule, **st}
        for s in sessions.live():  # every live session that arms the rule journals the change
            if body.rule in (s["limits"].get("rules") or []):
                sessions.journal("rule_status", change, now, session_id=s["id"])
        feed.custom("rule_status", change)
        feed.refresh()
        return {"ok": True, "changed": True, "rule": st}

    # -- prompts --------------------------------------------------------------------------------------------------
    def prompt_view() -> dict:
        # each session's own versions are on its view (prompt_versions)
        return {"ok": True, "system": prompts.current("system"), "user": prompts.current("user"),
                "history": {"system": prompts.history("system"), "user": prompts.history("user")}}

    def prompt_write(fn: Callable[[], dict], request: Request):
        if not from_pc(request):
            return only_pc()
        try:
            out = fn()
        except PromptError as e:
            return refuse(e.code, e.reason)
        feed.refresh()
        return {**out, **{k: v for k, v in prompt_view().items() if k != "ok"}}

    @app.get("/api/agent/prompts")
    async def agent_prompts() -> dict:
        return prompt_view()

    @app.get("/api/agent/prompts/version")
    async def agent_prompt_version(layer: Literal["system", "user"], version: int = Query(..., ge=0)) -> dict:
        try:
            return {"ok": True, "layer": layer, "version": version, "text": prompts.text(layer, version)}
        except PromptError as e:
            return refuse(e.code, e.reason)

    @app.post("/api/agent/prompts/save")
    async def agent_prompt_save(body: SaveBody, request: Request):
        return prompt_write(lambda: prompts.save(body.layer, body.text, now_fn()), request)

    @app.post("/api/agent/prompts/restore")
    async def agent_prompt_restore(body: RestoreBody, request: Request):
        return prompt_write(lambda: prompts.restore(body.layer, body.version, now_fn()), request)

    @app.post("/api/agent/prompts/reset")
    async def agent_prompt_reset(body: ResetBody, request: Request):
        return prompt_write(lambda: prompts.reset(body.layer, now_fn()), request)

    # -- assist proposals: the user's answer ---------------------------------------------------------------------
    def no_proposals() -> dict:
        return refuse("assist_unavailable", "Assist proposals are not available in this app.")

    def not_the_agent(request: Request, x_agent_token: str | None) -> JSONResponse | None:
        if not human(request):  # the paired PC or a phone's PIN session, never just the absence of a header
            return only_people()
        if x_agent_token is not None:
            return JSONResponse(refuse("agent_cannot_approve", "Only you can answer a proposal; an agent never can. "
                                                               "Nothing was sent."), status_code=403)
        return None

    answered = actions.answered

    @app.get("/api/agent/proposals")
    async def agent_proposals(session_id: str | None = Query(None, max_length=40)) -> dict:
        if proposals is None:
            return no_proposals()
        if session_id is None:  # every live session's pending proposals (the fleet's cards answer any of them)
            pending = [p for s in sessions.live() for p in proposals.pending(s["id"])]
            return {"ok": True, "session_id": None, "pending": pending, "recent": [], "server_ms": now_fn()}
        if sessions.row(session_id) is None:
            return not_found(session_id)
        return {"ok": True, "session_id": session_id, "pending": proposals.pending(session_id),
                "recent": proposals.recent(session_id), "server_ms": now_fn()}

    @app.post("/api/agent/proposals/approve")
    async def agent_proposal_approve(body: ProposalBody, request: Request,
                                     x_agent_token: str | None = Header(None, max_length=200)):
        if proposals is None:
            return no_proposals()
        return not_the_agent(request, x_agent_token) or answered(await proposals.approve(body.id, now_fn()))

    @app.post("/api/agent/proposals/skip")
    async def agent_proposal_skip(body: ProposalBody, request: Request,
                                  x_agent_token: str | None = Header(None, max_length=200)):
        if proposals is None:
            return no_proposals()
        return not_the_agent(request, x_agent_token) or answered(await proposals.skip(body.id, now_fn()))

    # -- the live feed ---------------------------------------------------------------------------------------------
    # -- tool endpoints for external agents (MCP, U8): the agent listener only -----------------------------------------
    def no_session_403() -> JSONResponse:
        return JSONResponse(refuse("no_agent_session", "No agent session: every tool call needs the token of the "
                                                       "running external agent session you launched. Nothing was "
                                                       "prepared."), status_code=403)

    def agent_port_403() -> JSONResponse:
        return JSONResponse(refuse("agent_port", "Agent tools are served on the agent port (BTCFLOW_AGENT_PORT) "
                                                 "only."), status_code=403)

    def on_agent_listener(request: Request) -> bool:
        return request.scope.get("state", {}).get("actor") == AGENT_API

    @app.get("/api/agent/tools")
    async def agent_tools(request: Request):
        if not on_agent_listener(request):
            return agent_port_403()
        return {"ok": True, "tools": [{**t, "kind": tools.kind(t["name"])} for t in tools.tools()]}

    @app.post("/api/agent/call/{tool}")
    async def agent_call(tool: str, request: Request, x_agent_token: str | None = Header(None, max_length=200)):
        if not on_agent_listener(request):
            return agent_port_403()
        sid = sessions.session_for_token(x_agent_token) if x_agent_token else None
        if sid is None:  # the agent listener is reachable by anything that reaches the port: the token is the proof
            return no_session_403()
        try:
            args = await request.json()
        except ValueError:
            return JSONResponse(refuse("invalid", "The body must be the tool's arguments as a JSON object."),
                                status_code=422)
        kind = tools.kind(tool)
        now = now_fn()
        out = await tools.call(tool, args, now, session_id=sid if kind is not None else None)
        if sid is not None and kind in ("write", "session"):
            feed.custom("tool_call", {"session_id": sid, "name": tool, "args": args, "ok": bool(out.get("ok")),
                                      "code": out.get("code"), "result": out})
            feed.new_decisions(sid)
            feed.refresh()
        return out

    @app.get("/api/agent/events/wait")
    async def agent_events_wait(request: Request, timeout_s: float = Query(25, gt=0, le=WAIT_MAX_S)):
        if not on_agent_listener(request):
            return agent_port_403()
        return refuse("not_available", "Waiting for events is not available for external agents yet; poll "
                                       "observe and get_journal instead.")

    return key


def agent_tallies(sessions: AgentSessions, limit: int = 100) -> list[dict]:
    """The scorecard's "by agent" lines: each of the last `limit` sessions (live first) with its closed trades'
    tally (rule_scores.tally), API spend (estimated) and net after API."""
    rows = sessions.repo.select(SESSIONS, order_by=["-started_ts"], limit=limit)
    if not rows:
        return []
    by: dict[str, list[dict]] = {}
    for b in sessions.repo.select(BRACKETS, filters=[("session_id", "in", [r["id"] for r in rows])]):
        by.setdefault(b["session_id"], []).append(b)
    out = []
    for r in rows:
        r = sessions.get(r["id"]) or r
        market = sessions.market_key(r)
        t = tally([trade_of({"rule": None, "market": market}, b) for b in by.get(r["id"], [])])
        api = round(float((r.get("usage") or {}).get("api_usd") or 0.0), 4)
        out.append({"id": r["id"], "name": sessions.name_of(r), "market": market,
                    "market_label": VENUE_LABELS.get(market or "", market), "state": r["state"],
                    "level": (r.get("limits") or {}).get("level"), "rules": (r.get("limits") or {}).get("rules") or [],
                    "started_ts": r["started_ts"], "ended_ts": r.get("ended_ts"), "live": t,
                    "api_usd": api, "net_after_api": round(float(t["net_usd"]) - api, 4)})
    out.sort(key=lambda x: (x["state"] == "ended", -x["started_ts"]))
    return out


def _deliver(sessions: AgentSessions, feed: Any, events: list[dict], ts: int) -> None:
    """Mark every human note in `events` delivered at `ts` (once) and tell the feed."""
    ids = []
    for e in events:
        if e.get("type") != "human_note":
            continue
        for v in (e.get("first"), e.get("latest")):
            ids += [n.get("id") for n in ((v or {}).get("notes") or []) if isinstance(n, dict)]
    for nid in dict.fromkeys(i for i in ids if isinstance(i, int)):
        got = sessions.repo.select(JOURNAL, {"id": nid}, limit=1)
        if not got or got[0]["kind"] != "note" or (got[0]["data"] or {}).get("state") == "delivered":
            continue
        data = {**got[0]["data"], "state": "delivered", "delivered_ts": ts}
        sessions.repo.update(JOURNAL, {"id": nid}, {"data": data})
        feed.custom("note", {"id": nid, "session_id": got[0]["session_id"], "ts": got[0]["ts"], **data})


def wake_start_hook(sessions: AgentSessions, feed: Any) -> Callable[[dict], None]:
    """The runner's `start_fn`: the run opens on the feed (with its trigger), and the notes that woke it are marked
    delivered now."""
    def hook(row: dict) -> None:
        feed.wake_started(row)
        _deliver(sessions, feed, (row.get("data") or {}).get("trigger") or [], int(row.get("ts") or 0))
    return hook


def wake_hook(sessions: AgentSessions, feed: Any) -> Callable[[dict], None]:
    """The runner's `notify_fn`: the wake row goes to the feed, and every note the wake took that is not marked yet
    (its new_events; its trigger when no start hook ran) is marked delivered at the wake's start."""
    def hook(row: dict) -> None:
        if row.get("kind") == "rule_status":  # the scorekeeper retired a rule: to the panel
            feed.custom("rule_status", row.get("data") or {})
            feed.refresh()
            return
        if row.get("kind") == "rule":  # an armed rule's entry attempt: its decision row (if any) to the panel
            feed.new_decisions(row["session_id"])
            feed.refresh()
            return
        feed.wake_row(row)
        rec = row.get("data") or {}
        started = int(row.get("ts") or 0) - int(rec.get("duration_ms") or 0)
        _deliver(sessions, feed, [*(rec.get("trigger") or []), *(rec.get("new_events") or [])], started)
    return hook
