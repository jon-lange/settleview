"""Agent tool registry (Agent mode, docs/plans/2026-10-30-1402-feat-agent-mode-mcp-plan.md, U4; R7-R12, KTD2, KTD4).

Every agent tool is defined once here (name, plain-English description, strict JSON input schema, kind, handler) and
called in-process through `AgentTools.call` by the in-app runner (U6) and the agent HTTP routes behind the MCP server
(U7/U8). `tools()` is the list in name order with deterministic JSON (a prompt-cached prefix).

Rules:
- `call(name, args, now, session_id=...)` validates `args` against the tool's schema first: an unknown tool, a missing
  or extra argument, a wrong type or an unknown market / side / order type is `invalid` and nothing is prepared.
- `session_id` is the session the caller has already proven (the runner's own session; the route's token-checked
  one); it is never read from tool arguments. Reads run for anyone; writes and session tools run only when that
  session is the running (or paused) one, as its agent (`actor.as_agent`), else `no_agent_session`.
- Markets are the agent's keys (kalshi_15m, polymarket_15m, polymarket_5m). Only the session's own markets are shown
  by observe and may be traded (`venue_not_allowed` otherwise); with no session every market is shown. A session
  sees only its own positions and the user's, never another agent session's (multi-agent KD2); the lock and the
  request_id memo are per session, so sessions never wait on each other.
- Writes carry `note` and `request_id`. All writes and session tools of a session run under one lock; a repeated
  request_id with the same tool and arguments (the note aside) returns the first answer marked `repeat` and sends
  nothing again; reused for a different tool or different arguments it is refused `request_id_reused` and nothing is
  done. Each writes one journal row (kind "tool").
- place_entry = prepare -> the agent's bounds -> approve in one call (KTD2): the re-priced ticket is refused when its
  limit price is above `max_price` or its worst case (max_cost) above `max_worst_case`, its token discarded, nothing
  sent; otherwise approved at once through the ordinary order gate (where the session's own check runs).
- place_entry's optional `btc_stop` is the bracket's BTC index stop, set in the same order ticket (one atomic path:
  the entry is never live without it); refused when BTC is already at or past it.
- Rules (agent_rules): while the session has any rule armed, the agent's opening tools (place_entry,
  place_perp_entry) are refused `rules_own_entries` (one execution owner, no LLM in the open path); only the runner's
  rule executor opens, calling place_entry with `by_rule`. Exits, take profit, BTC stop, decisions, notes and alarms
  stay the agent's.
- A bracket a rule opened (a "rule" journal row with its bracket_id) keeps its code exits against the agent
  (`rule_stop_locked`): its BTC stop may only be tightened (moved toward BTC: up for UP, down for DOWN), never loosened
  or removed, and it has NO take-profit order (the rule's entry is sent `ride_to_settlement`: exactly one buy, then it
  is paid by the market's official result), which the agent cannot add. The agent has no contract-stop tool.
  A person in the app keeps full control (their changes go through TradingService directly, never through here).
- Data the app does not have yet is answered "not available yet", never estimated.
- record_decision (session tool, once per wake) writes one journal row of kind "decision": the agent's structured
  decision plus ts, the runner's wake id and the market's window and UP mid now (null without a market). A wake
  that ends without one gets the runner's UNRECORDED row (`unrecorded_decision`). `decisions()` reads them for the UI.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

from btc_signal_engine.application.actor import as_agent
from btc_signal_engine.application.agent_events import ALARM_KINDS, EVENT_CLASS, AgentEvents
from btc_signal_engine.application.agent_sessions import BRACKETS, JOURNAL, LIVE, VENUE_LABELS, VENUES, AgentSessions
from btc_signal_engine.application.play import STAKE_CEILING, PlayService
from btc_signal_engine.application.trading import TradingService
from btc_signal_engine.domain.markets import market as market_spec
from btc_signal_engine.domain.strategy import play as P

OBSERVE_SERIES_POINTS = 30
NOTES_MAX = 6000
JOURNAL_RESULT_MAX = 2000
JOURNAL_ARG_MAX = 500
EPS = 1e-9
PERPS_LATER = "Perps for agents come in a later step."

# -- schema pieces ------------------------------------------------------------------------------------------------
MARKET = {"type": "string", "enum": list(VENUES),
          "description": "The market: kalshi_15m (Kalshi 15 min), polymarket_15m or polymarket_5m (Polymarket US)."}
SIDE = {"type": "string", "enum": ["UP", "DOWN"], "description": "UP = BTC settles at or above the strike."}
STAKE = {"type": "number", "exclusiveMinimum": 0, "maximum": STAKE_CEILING,
         "description": "Dollars to spend; contracts are sized to fit it including the venue's fee."}
ORDER_TYPE = {"type": "string", "enum": ["market", "limit"],
              "description": "market = buy at the best offers now, up to the ask plus your market protection; "
                             "limit = buy at max_price or less, refused if the ask is above it."}
PRICE = {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1}
NOTE = {"type": "string", "minLength": 1, "maxLength": 500,
        "description": "Audit note: why, the evidence, and what would prove it wrong."}
REQUEST_ID = {"type": "string", "minLength": 1, "maxLength": 64,
              "description": "Your unique id for this action; repeating it returns the first answer, nothing resent."}
BRACKET_ID = {"type": "string", "minLength": 1, "maxLength": 64, "description": "The position's id (positions[].id)."}

# record_decision: the structured end of every wake (the UI's belief line, per-window cards and scorecard).
ACTIONS = ("ENTRY", "REDUCE", "EXIT", "HEDGE", "HOLD", "NO_TRADE")
PASS_ACTIONS = ("HOLD", "NO_TRADE")
P_SOURCES = ("market_implied", "model", "experimental", "unknown")
PASS_REASONS = ("NO_VALIDATED_EDGE", "INSUFFICIENT_EDGE", "STALE_DATA", "RISK_LIMIT", "SETTLEMENT_UNCERTAINTY",
                "EXECUTION_RISK", "UNSUPPORTED_MODEL", "UNKNOWN_ORDER_STATE", "INSUFFICIENT_TIME",
                "NO_MATERIAL_CHANGE")
THESIS_MAX = 600
UNRECORDED = "UNRECORDED"  # server-only action: a wake that ended without record_decision
UNRECORDED_NOTE = "the agent did not record a decision"
OPENING_TOOLS = ("place_entry", "place_perp_entry")
RULES_OWN_ENTRIES = "Entries in this session are opened only by the armed rules; you supervise."


def _prob(what: str) -> dict:
    return {"type": ["number", "null"], "minimum": 0, "maximum": 1, "description": what}


def _short(what: str, lo: int = 1, hi: int = 300, nullable: bool = False) -> dict:
    return {"type": ["string", "null"] if nullable else "string", "minLength": lo, "maxLength": hi,
            "description": what}


DECISION_PROPS = {
    "market": {"type": ["string", "null"], "enum": [*VENUES, None],
               "description": "The market this decision is about, or null for a portfolio-level decision."},
    "action": {"type": "string", "enum": list(ACTIONS), "description": "What you decided this wake."},
    "side": {"type": ["string", "null"], "enum": ["UP", "DOWN", None],
             "description": "The side acted on or favoured, or null."},
    "p_estimate": _prob("Your probability that UP settles, 0-1; null when unknown."),
    "p_low": _prob("Low end of your uncertainty range for p_estimate, or null."),
    "p_high": _prob("High end of your uncertainty range for p_estimate, or null."),
    "p_source": {"type": "string", "enum": list(P_SOURCES),
                 "description": "Where p_estimate comes from: market_implied, model, experimental or unknown."},
    "market_p": _prob("The market price (UP, 0-1) you compared against, or null."),
    "edge_cents": {"type": ["number", "null"],
                   "description": "Conservative net edge per contract in cents after fees, or null when not "
                                  "calculable. Never invent it."},
    "pass_reason": {"type": ["string", "null"], "enum": [*PASS_REASONS, None],
                    "description": "Required for NO_TRADE; allowed for HOLD; null otherwise."},
    "invalidation": _short("What would prove this decision wrong."),
    "thesis": _short("The decision's reason in a few sentences.", hi=THESIS_MAX),
    "next_check": _short("When or on what you will look again, or null.", nullable=True),
}
DECISION_KEYS = tuple(DECISION_PROPS)


def decision_rules(args: dict) -> str | None:
    """The cross-field rules the schema cannot say, or None."""
    action, why = args["action"], args["pass_reason"]
    if action == "NO_TRADE" and why is None:
        return "A NO_TRADE decision needs pass_reason (its main reason)."
    if why is not None and action not in PASS_ACTIONS:
        return f"pass_reason is only for NO_TRADE or HOLD (got action {action})."
    lo, p, hi = args["p_low"], args["p_estimate"], args["p_high"]
    for a, b, names in ((lo, p, "p_low <= p_estimate"), (p, hi, "p_estimate <= p_high"), (lo, hi, "p_low <= p_high")):
        if a is not None and b is not None and a > b + EPS:
            return f"Must have {names} (got {a} > {b})."
    return None


def unrecorded_decision(now: int, wake_id: int | None, wake_status: str | None) -> dict:
    """The row the runner journals when a wake ended without record_decision (so the UI never shows a silent gap)."""
    return {**{k: None for k in DECISION_KEYS}, "action": UNRECORDED, "ts": now, "wake_id": wake_id,
            "source": "runner", "window_start": None, "window_end": None, "mid_up": None, "note": UNRECORDED_NOTE,
            "wake_status": wake_status}


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


_TYPES: dict[str, Callable[[Any], bool]] = {
    "object": lambda v: isinstance(v, dict), "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str), "boolean": lambda v: isinstance(v, bool), "null": lambda v: v is None,
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)}


def validate(schema: dict, v: Any, path: str = "args") -> str | None:
    """The first way `v` breaks `schema` (the keywords this registry uses), or None."""
    types = schema.get("type")
    types = [types] if isinstance(types, str) else types
    if types and not any(_TYPES[t](v) for t in types):
        return f"{path} must be {' or '.join(types)} (got {v!r})."
    if "enum" in schema and v not in schema["enum"]:
        return f"{path} must be one of {', '.join(map(str, schema['enum']))} (got {v!r})."
    if isinstance(v, str) and (len(v) < schema.get("minLength", 0) or len(v) > schema.get("maxLength", len(v))):
        return f"{path} must be {schema.get('minLength', 0)}-{schema.get('maxLength')} characters."
    if _TYPES["number"](v):
        lo, hi = schema.get("minimum"), schema.get("maximum")
        xlo, xhi = schema.get("exclusiveMinimum"), schema.get("exclusiveMaximum")
        if (lo is not None and v < lo) or (hi is not None and v > hi) or (xlo is not None and v <= xlo) or (
                xhi is not None and v >= xhi):
            return f"{path} is out of range (got {v!r})."
    if isinstance(v, dict) and "properties" in schema:
        props = schema["properties"]
        extra = [k for k in v if k not in props]
        if extra and schema.get("additionalProperties") is False:
            return f"{path} has an unknown argument {extra[0]!r}."
        for k in schema.get("required", []):
            if k not in v:
                return f"{path} is missing {k!r}."
        for k, x in v.items():
            err = validate(props[k], x, f"{path}.{k}") if k in props else None
            if err:
                return err
    if isinstance(v, list):
        if len(v) > schema.get("maxItems", len(v)):
            return f"{path} has more than {schema['maxItems']} items."
        if "items" in schema:
            for i, x in enumerate(v):
                err = validate(schema["items"], x, f"{path}[{i}]")
                if err:
                    return err
    return None


@dataclass(frozen=True)
class Tool:
    name: str
    kind: str  # read | write | session
    description: str
    schema: dict
    handler: Callable[..., Awaitable[dict]]  # (tools, ctx, args, now) -> JSON-serialisable dict


@dataclass(frozen=True)
class Ctx:
    """Who is calling: `sid` the proven session id (or None); `live` its row while running or paused; `view` its view
    (also once ended); `markets` the markets it may see and trade."""

    sid: str | None
    live: dict | None
    view: dict | None
    markets: tuple[str, ...]
    wake_id: int | None = None  # the runner's wake this call belongs to, when it says
    by_rule: str | None = None  # the armed rule this call executes (the runner's rule executor only)


def _invalid(reason: str) -> dict:
    return {"ok": False, "code": "invalid", "reason": reason}


def _locked(reason: str) -> dict:
    return {"ok": False, "code": "rule_stop_locked", "reason": reason}


def _not_available(reason: str) -> dict:
    return {"ok": False, "code": "not_available", "reason": reason}


def _c(x: float) -> str:
    return f"{x * 100:.0f}¢"


TICKET_KEYS = ("ticker", "side", "price_mode", "stake", "contracts", "sized_at", "limit_price", "cost_at_sized",
               "max_cost", "take_profit", "window_end", "mode", "sentence")
POSITION_KEYS = ("id", "ticker", "side", "state", "contracts", "filled", "held", "entry_avg", "entry_cost",
                 "take_profit", "stop_level", "bid", "unrealized", "advice", "outlook", "created_ts", "window_end")
ENTRY_KEYS = ("bid", "ask", "ideal_entry", "max_price", "take_profit", "suggested_stop", "stop_distance", "contracts",
              "max_loss", "if_wins", "if_loses", "buyable", "market")
CALL_KEYS = ("action", "headline", "reason", "lean", "mid_up", "fair_up", "model_lean", "agree")


class AgentTools:
    def __init__(self, sessions: AgentSessions, play: PlayService, *,
                 events_fn: Callable[[str], AgentEvents | None] = lambda sid: None,
                 odds_fn: Callable[[str, int], dict | None] | None = None) -> None:
        """`events_fn(session_id)` -> that live session's event engine (alarms, snooze) or None. `odds_fn(market, now)` ->
        the market's recorded odds history for the current window (e.g. the Coach's series for kalshi_15m), or None
        when the app does not record it for that market."""
        self.sessions = sessions
        self.play = play
        self._events_fn = events_fn
        self._odds_fn = odds_fn
        self._tools = {t.name: t for t in TOOLS}
        self._locks: dict[str, asyncio.Lock] = {}
        self._memo: dict[tuple[str, str], tuple[str, str, dict]] = {}  # (sid, request_id) -> (tool, args key, answer)

    # -- the registry ---------------------------------------------------------------------------------------------
    def tools(self) -> list[dict]:
        """Every tool as the model sees it ({name, description, input_schema}), in name order."""
        return [{"name": t.name, "description": t.description, "input_schema": t.schema}
                for t in sorted(self._tools.values(), key=lambda t: t.name)]

    def tools_json(self) -> str:
        return json.dumps(self.tools(), sort_keys=True, separators=(",", ":"))

    def forget(self, session_id: str) -> None:
        """An ended session: its lock and request_id memo go (memory stays bounded with many sessions over a day)."""
        lock = self._locks.get(session_id)
        if lock is not None and not lock.locked():
            del self._locks[session_id]
        for k in [k for k in self._memo if k[0] == session_id]:
            del self._memo[k]

    def kind(self, name: str) -> str | None:
        t = self._tools.get(name)
        return t.kind if t else None

    def notes(self, session_id: str) -> str | None:
        """The session's latest notes (update_notes), for the runner's memory block; None before any."""
        rows = self.sessions.repo.select(JOURNAL, {"session_id": session_id, "kind": "notes"}, order_by=["-id"],
                                         limit=1)
        return rows[0]["data"]["text"] if rows else None

    def decisions(self, session_id: str, after_id: int = 0, limit: int = 200) -> list[dict]:
        """The session's decision rows (record_decision, or the runner's UNRECORDED), oldest first: the latest
        `limit` with id > `after_id`, each {id, **data}."""
        rows = self.sessions.repo.select(JOURNAL, {"session_id": session_id, "kind": "decision"},
                                         filters=[("id", ">", after_id)], order_by=["-id"], limit=limit)
        return [{"id": r["id"], **r["data"]} for r in reversed(rows)]

    async def call(self, name: str, args: Any, now: int, *, session_id: str | None = None,
                   wake_id: int | None = None, by_rule: str | None = None) -> dict:
        """`by_rule`: the armed rule this call executes (the runner's rule executor only; never from a tool call)."""
        tool = self._tools.get(name)
        if tool is None:
            return _invalid(f"Unknown tool {name!r}.")
        err = validate(tool.schema, args)
        if err:
            return _invalid(err)
        ctx = replace(self._ctx(session_id, now), wake_id=wake_id, by_rule=by_rule)
        if tool.kind == "read":
            return await tool.handler(self, ctx, args, now)
        if ctx.live is None:
            return {"ok": False, "code": "no_agent_session",
                    "reason": "No agent session of yours is running; nothing was done."}
        sid = ctx.sid
        lock = self._locks.setdefault(sid, asyncio.Lock())
        async with lock:
            rid = args.get("request_id")
            key = json.dumps({k: v for k, v in args.items() if k not in ("note", "request_id")}, sort_keys=True,
                             default=str)
            seen = self._memo.get((sid, rid)) if rid is not None else None
            repeat = seen is not None
            armed = (ctx.live.get("limits") or {}).get("rules") or []
            if name in OPENING_TOOLS and armed and (by_rule is None or by_rule not in armed):
                repeat = False
                out = {"ok": False, "code": "rules_own_entries", "reason": RULES_OWN_ENTRIES}
            elif seen is not None and seen[:2] != (name, key):
                out = {"ok": False, "code": "request_id_reused",
                       "reason": f"request_id {rid!r} was already used for a different action ({seen[0]}); nothing "
                                 "was done. Use a new request_id for a new action."}
            elif seen is not None:
                out = {**seen[2], "repeat": True}
            else:
                with as_agent(sid):
                    out = await tool.handler(self, ctx, args, now)
                if rid is not None:
                    self._memo[(sid, rid)] = (name, key, out)
            self.sessions.journal("tool", self._journal_row(name, args, out, repeat), now, session_id=sid)
        return out

    def _ctx(self, session_id: str | None, now: int) -> Ctx:
        if session_id is None:
            return Ctx(None, None, None, tuple(VENUES))
        active = self.sessions.get(session_id)
        live = active if active is not None and active["state"] in LIVE else None
        view = self.sessions.view(session_id, now)
        if view is None:
            return Ctx(session_id, None, None, ())
        return Ctx(session_id, live, view, tuple(view["limits"]["venues"]))

    @staticmethod
    def _journal_row(name: str, args: dict, out: dict, repeat: bool) -> dict:
        short = {k: (v[:JOURNAL_ARG_MAX] + "…" if isinstance(v, str) and len(v) > JOURNAL_ARG_MAX else v)
                 for k, v in args.items()}
        text = json.dumps(out, default=str)
        result = out if len(text) <= JOURNAL_RESULT_MAX else {"truncated": text[:JOURNAL_RESULT_MAX]}
        return {"name": name, "args": short, "ok": bool(out.get("ok")), "code": out.get("code"), "result": result,
                "repeat": repeat}

    # -- helpers ----------------------------------------------------------------------------------------------------
    def _refuse_market(self, ctx: Ctx, key: str) -> dict | None:
        if key in ctx.markets:
            return None
        allowed = ", ".join(ctx.markets) or "none"
        return {"ok": False, "code": "venue_not_allowed",
                "reason": f"{VENUE_LABELS[key]} is not one of this session's markets ({allowed}); nothing was done."}

    def _view(self, key: str, now: int, stake: float | None = None) -> dict:
        m = market_spec(key)
        return self.play.now_view(now, m.venue, span=m.span, stake=stake)

    def _svc(self, key: str) -> TradingService | None:
        v = self.play.venues.get(market_spec(key).play_key)
        return v.svc if v is not None else None

    def _owner(self, ctx: Ctx, session_id: str | None, bracket_id: str) -> str:
        mine = ctx.sid is not None and session_id == ctx.sid and bracket_id not in (ctx.view or {}).get(
            "taken_over", [])
        return "agent" if mine else "you"

    def _trim(self, ctx: Ctx, key: str, v: dict) -> dict:
        if not v.get("available"):
            return {"available": False, "reason": v.get("reason"), "window": v["window"], "btc": v["btc"]}
        svc = self._svc(key)
        sessions_of = {b["id"]: b.get("session_id") for b in svc.live_brackets()} if svc else {}
        # KD2: an agent sees only its own positions and the user's, never another agent session's
        positions = [{**{k: p.get(k) for k in POSITION_KEYS},
                      "owner": self._owner(ctx, sessions_of.get(p["id"]), p["id"])} for p in v["positions"]
                     if ctx.sid is None or sessions_of.get(p["id"]) in (None, ctx.sid)]
        w = v["window"]
        return {"available": True,
                "window": {k: w[k] for k in ("start", "end", "seconds_left", "ticker", "entry_open",
                                              "entry_cutoff_ms", "phase")},
                "market": v["market"], "call": {k: v["call"].get(k) for k in CALL_KEYS},
                "signals": [{k: s[k] for k in ("key", "name", "lean", "value", "up_usd", "down_usd") if k in s}
                            for s in v["signals"]],
                "cone": v["cone"], "btc": v["btc"],
                "entry": {side: {k: v["entry"][side].get(k) for k in ENTRY_KEYS} for side in ("UP", "DOWN")},
                "positions": positions,
                "btc_series": P.thin_series([(t, p) for t, p in v["btc_series"]], OBSERVE_SERIES_POINTS),
                "btc_levels": v["btc_levels"],
                "mode": {k: v["mode"][k] for k in ("mode", "label", "real_money", "ready", "reason", "killed")},
                "limits": v["limits"],
                "stake": {k: v["stake"][k] for k in ("value", "max", "cap", "sentence")},
                "ready": v["ready"], "reason": v["reason"]}

    def _session_block(self, ctx: Ctx) -> dict | None:
        s = ctx.view
        if s is None:
            return None
        parts = self.sessions.budget_parts(s["id"])
        total = s["limits"]["budget"]
        used = round(parts["realized"] + parts["at_risk"], 4)
        usage = s["usage"] or {}
        ev = self._events_fn(ctx.sid) if ctx.live is not None else None
        return {"id": s["id"], "state": s["state"], "pause_reason": s["pause_reason"], "end_reason": s["end_reason"],
                "modes": {k: "LIVE" if s["pinned"].get(VENUES[k]) == "prod" else "PAPER"
                          for k in s["limits"]["venues"]},
                "budget": {"total": total, **parts, "used": used, "left": round(total - used, 4)},
                "per_trade": s["limits"]["per_trade"], "seconds_left": s["seconds_left"], "ends_ts": s["ends_ts"],
                "api_usd": usage.get("api_usd", 0), "spend_cap": s["limits"].get("spend_cap"),
                "wakes": usage.get("wakes", 0), "rules": list(s["limits"].get("rules") or []),
                "alarms": self._alarms_out(ev.alarms()) if ev else None, "snoozed": ev.snoozed() if ev else None}

    @staticmethod
    def _alarms_out(alarms: list[dict]) -> list[dict]:
        out = []
        for a in alarms:
            x = {"kind": a["kind"], "value": a["value"]}
            if a.get("venue"):
                x["market"] = next(k for k in VENUES if (market_spec(k).venue, market_spec(k).span)
                                   == (a["venue"], a["span"]))
            out.append(x)
        return out

    @staticmethod
    def _proposal(v: dict, args: dict, *, ride: bool = False) -> dict:
        """The trading proposal for an agent entry from the market's view, or a refusal (`ok` False). `ride` (rule
        entries): no take-profit order at all (`ride_to_settlement`), paid by the official result."""
        if not v.get("available"):
            return {"ok": False, "code": "unavailable", "reason": v.get("reason")}
        m, side = v.get("market") or {}, args["side"]
        tp = v["entry"][side].get("take_profit")
        if not m.get("ticker") or not m.get("fresh") or (tp is None and not ride):
            return {"ok": False, "code": "no_quote", "reason": "There is no fresh price for this market right now; "
                                                               "nothing was done."}
        p = {"ticker": m["ticker"], "side": side, "stake": float(args["stake"]),
             **({"ride_to_settlement": True} if ride else {"take_profit": tp}),
             "window_end": v["window"]["end"], "fair_up": v["call"].get("fair_up"), "price_mode": args["order_type"]}
        if args["order_type"] == "limit":
            p["max_price"] = float(args["max_price"])
        stop = args.get("btc_stop")
        if stop is not None:
            btc = (v.get("btc") or {}).get("btc")
            if btc is None:
                return {"ok": False, "code": "no_index", "reason": "There is no live BTC price to check the BTC stop "
                                                                  "against; nothing was done."}
            if (side == "UP" and stop >= btc - EPS) or (side == "DOWN" and stop <= btc + EPS):
                return _invalid(f"btc_stop ${stop:,.2f} is already reached (BTC is ${btc:,.2f}): an UP stop is below "
                                "BTC, a DOWN stop above. Nothing was done.")
            p["stop_index_below" if side == "UP" else "stop_index_above"] = float(stop)
        return {"ok": True, "proposal": p}

    def _bracket_market(self, ctx: Ctx, bracket_id: str) -> tuple[str | None, dict | None]:
        rows = self.sessions.repo.select(BRACKETS, {"id": bracket_id}, limit=1)
        key = next((k for k, s in VENUES.items() if rows and rows[0].get("strategy") == s), None)
        if key is None or self._svc(key) is None:
            return None, {"ok": False, "code": "not_found", "reason": "There is no such position; nothing was done."}
        return key, self._refuse_market(ctx, key)

    # -- reads ------------------------------------------------------------------------------------------------------
    async def observe(self, ctx: Ctx, args: dict, now: int) -> dict:
        stake = args.get("stake") or ((ctx.view or {}).get("limits") or {}).get("per_trade")
        views = {k: self._view(k, now, stake) for k in ctx.markets}
        first = next(iter(views.values()), None)
        return {"ok": True, "now": now, "markets": {k: self._trim(ctx, k, v) for k, v in views.items()},
                "balances": first["balances"] if first else self.play.balances(now),
                "session": self._session_block(ctx)}

    async def get_signals(self, ctx: Ctx, args: dict, now: int) -> dict:
        key = args["market"]
        bad = self._refuse_market(ctx, key)
        if bad:
            return bad
        v = self._trim(ctx, key, self._view(key, now))
        if not v["available"]:
            return {"ok": False, "code": "unavailable", "reason": v["reason"]}
        return {"ok": True, "market": key, "call": v["call"], "signals": v["signals"], "flips": None,
                "flips_note": "Flip history is not available yet: the app does not record light flips yet. The "
                              "current leans are above."}

    async def get_odds(self, ctx: Ctx, args: dict, now: int) -> dict:
        key = args["market"]
        bad = self._refuse_market(ctx, key)
        if bad:
            return bad
        odds = self._odds_fn(key, now) if self._odds_fn is not None else None
        if odds is None:
            return _not_available(f"Odds history for {VENUE_LABELS[key]} is not available yet: the app does not "
                                  "record it for this market yet. observe has the prices now.")
        return {"ok": True, "market": key, "odds": odds}

    async def get_btc_series(self, ctx: Ctx, args: dict, now: int) -> dict:
        key = args["market"]
        bad = self._refuse_market(ctx, key)
        if bad:
            return bad
        v = self._view(key, now)
        return {"ok": True, "market": key, "window": {k: v["window"][k] for k in ("start", "end", "seconds_left")},
                "btc": v["btc"], "btc_series": v["btc_series"], "btc_levels": v["btc_levels"], "cone": v.get("cone")}

    async def get_scoreboard(self, ctx: Ctx, args: dict, now: int) -> dict:
        key = args["market"]
        bad = self._refuse_market(ctx, key)
        if bad:
            return bad
        m = market_spec(key)
        if not args.get("this_session_only"):
            return {"ok": True, "market": key, "this_session_only": False,
                    "scoreboard": self.play.scoreboard(now, m.venue, span=m.span)}
        svc = self._svc(key)
        if ctx.view is None or svc is None:
            return {"ok": False, "code": "no_agent_session", "reason": "There is no session to score."}
        ids = {b["id"] for b in self.sessions.repo.select(BRACKETS, {"session_id": ctx.sid})}
        trades = [t for t in svc.trades(0, now, 500) if t["id"] in ids]
        return {"ok": True, "market": key, "this_session_only": True, "scoreboard": P.scoreboard(trades, now)}

    async def get_journal(self, ctx: Ctx, args: dict, now: int) -> dict:
        if ctx.view is None:
            return {"ok": False, "code": "no_agent_session", "reason": "There is no session journal to read."}
        rows = self.sessions.journal_rows(ctx.sid, after_id=args.get("after_id", 0), limit=args.get("limit", 50))
        return {"ok": True, "rows": [{k: r[k] for k in ("id", "ts", "kind", "data")} for r in rows]}

    async def quote_entry(self, ctx: Ctx, args: dict, now: int) -> dict:
        key = args["market"]
        bad = self._refuse_market(ctx, key)
        if bad:
            return bad
        if (args["order_type"] == "limit") != ("max_price" in args):
            return _invalid("A limit order needs max_price (its limit); a market order takes none.")
        prop = self._proposal(self._view(key, now, args["stake"]), args)
        if not prop["ok"]:
            return prop
        svc = self._svc(key)
        if ctx.live is not None:  # the session's own check runs in the gate, as it would for the order
            with as_agent(ctx.sid):
                r = await svc.preview(prop["proposal"], now)
        else:
            r = await svc.preview(prop["proposal"], now)
        if not r["ok"]:
            return r
        return {"ok": True, "market": key, "would_send": r["would_send"], "gate": r["gate"],
                "ticket": {k: r["ticket"].get(k) for k in TICKET_KEYS}}

    # -- writes (run as the session's agent, under its lock) ---------------------------------------------------------
    async def place_entry(self, ctx: Ctx, args: dict, now: int) -> dict:
        key = args["market"]
        bad = self._refuse_market(ctx, key)
        if bad:
            return bad
        prop = self._proposal(self._view(key, now, args["stake"]), args, ride=ctx.by_rule is not None)
        if not prop["ok"]:
            return prop
        svc = self._svc(key)
        prep = await svc.prepare(prop["proposal"], now)
        if not prep["ok"]:
            return prep
        t = prep["ticket"]
        ticket = {k: t.get(k) for k in TICKET_KEYS}
        why = []
        if t["limit_price"] > args["max_price"] + EPS:
            how = (f"a market buy sized at the {_c(t['sized_at'])} ask may pay up to {_c(t['limit_price'])}"
                   if t["price_mode"] == "market" else f"its limit is {_c(t['limit_price'])}")
            why.append(f"{how}, above your max_price {_c(args['max_price'])}")
        if t["max_cost"] > args["max_worst_case"] + EPS:
            why.append(f"its worst case is ${t['max_cost']:.2f}, above your max_worst_case "
                       f"${args['max_worst_case']:.2f}")
        if why:
            await svc.approve(prep["token"], "no", t, now)  # the token dies here: nothing can be sent from it
            return {"ok": False, "code": "outside_your_bounds", "ticket": ticket,
                    "reason": "Re-priced now, " + "; and ".join(why) + ". Nothing was sent."}
        out = await svc.approve(prep["token"], "yes", t, now)
        return {**out, "ticket": ticket}

    async def place_exit(self, ctx: Ctx, args: dict, now: int) -> dict:
        key, bad = self._bracket_market(ctx, args["bracket_id"])
        if bad:
            return bad
        svc = self._svc(key)
        prep = await svc.prepare_exit(args["bracket_id"], now)
        if not prep["ok"]:
            return prep
        return await svc.approve(prep["token"], "yes", prep["ticket"], now)

    def _rule_bracket(self, ctx: Ctx, bracket_id: str) -> bool:
        """The bracket was opened by one of this session's armed rules."""
        rows = self.sessions.repo.select(JOURNAL, {"session_id": ctx.sid, "kind": "rule"})
        return any((r["data"] or {}).get("ok") and (r["data"] or {}).get("bracket_id") == bracket_id for r in rows)

    async def set_take_profit(self, ctx: Ctx, args: dict, now: int) -> dict:
        key, bad = self._bracket_market(ctx, args["bracket_id"])
        if bad:
            return bad
        if self._rule_bracket(ctx, args["bracket_id"]):
            return _locked("This position was opened by a rule that holds to settlement with no take-profit order; "
                           "the agent cannot add or change one. Nothing was changed.")
        return await self._svc(key).set_take_profit(args["bracket_id"], args["price"], now)

    async def set_btc_stop(self, ctx: Ctx, args: dict, now: int) -> dict:
        key, bad = self._bracket_market(ctx, args["bracket_id"])
        if bad:
            return bad
        level = args["level"]
        if self._rule_bracket(ctx, args["bracket_id"]):
            b = self.sessions.repo.select(BRACKETS, {"id": args["bracket_id"]}, limit=1)[0]
            up = b["side"] == "UP"
            cur = b.get("stop_index_below") if up else b.get("stop_index_above")
            if level is None:
                return _locked("This position was opened by a rule: its code BTC stop cannot be removed by the "
                               "agent. Nothing was changed.")
            if cur is not None and ((up and level < cur - EPS) or (not up and level > cur + EPS)):
                return _locked(f"This position was opened by a rule: its BTC stop (${cur:,.2f}) may only be "
                               f"tightened toward BTC ({'higher' if up else 'lower'}), not moved to ${level:,.2f}. "
                               "Nothing was changed.")
        return await self._svc(key).set_index_stop(args["bracket_id"], level, now)

    async def perps_later(self, ctx: Ctx, args: dict, now: int) -> dict:
        return _not_available(PERPS_LATER)

    # -- session tools ------------------------------------------------------------------------------------------------
    async def set_alarms(self, ctx: Ctx, args: dict, now: int) -> dict:
        ev = self._events_fn(ctx.sid)
        if ev is None:
            return _not_available("Alarms are not available yet: no event engine is attached to this session.")
        alarms = []
        for a in args["alarms"]:
            x = {"kind": a["kind"], "value": a["value"]}
            if "market" in a:
                bad = self._refuse_market(ctx, a["market"])
                if bad:
                    return bad
                m = market_spec(a["market"])
                x.update(venue=m.venue, span=m.span)
            alarms.append(x)
        try:
            ev.set_alarms(alarms)
        except ValueError as e:
            return _invalid(str(e))
        return {"ok": True, "alarms": self._alarms_out(ev.alarms())}

    async def snooze(self, ctx: Ctx, args: dict, now: int) -> dict:
        ev = self._events_fn(ctx.sid)
        if ev is None:
            return _not_available("Snooze is not available yet: no event engine is attached to this session.")
        try:
            return {"ok": True, "snoozed": ev.snooze(until_ms=args.get("until_ms"),
                                                     until_event=args.get("until_event"))}
        except ValueError as e:
            return _invalid(str(e))

    async def update_notes(self, ctx: Ctx, args: dict, now: int) -> dict:
        self.sessions.journal("notes", {"text": args["text"]}, now, session_id=ctx.sid)
        return {"ok": True, "chars": len(args["text"])}

    async def record_decision(self, ctx: Ctx, args: dict, now: int) -> dict:
        key = args["market"]
        if key is not None:
            bad = self._refuse_market(ctx, key)
            if bad:
                return bad
        err = decision_rules(args)
        if err:
            return _invalid(err)
        at = {"window_start": None, "window_end": None, "mid_up": None}
        if key is not None:  # the market's window and UP mid now, from the same view as observe
            v = self._view(key, now)
            w = v.get("window") or {}
            at = {"window_start": w.get("start"), "window_end": w.get("end"),
                  "mid_up": (v.get("call") or {}).get("mid_up") if v.get("available") else None}
        row = {**{k: args[k] for k in DECISION_KEYS}, "ts": now, "wake_id": ctx.wake_id, "source": "agent", **at,
               "note": None, "wake_status": None}
        self.sessions.journal("decision", row, now, session_id=ctx.sid)
        return {"ok": True, "recorded": {k: row[k] for k in ("action", "market", "window_end", "mid_up")}}

    async def end_session(self, ctx: Ctx, args: dict, now: int) -> dict:
        ev = self._events_fn(ctx.sid)
        if ev is not None:
            ev.end_session()
        r = self.sessions.stop(ctx.sid, now, "agent", f"The agent ended the session: {args['reason']}")
        if not r["ok"]:
            return r
        return {"ok": True, "state": "ended", "note": "Open positions stay open with their take-profit and stops; "
                                                      "they are now yours to manage."}


# -- the tools, defined once -------------------------------------------------------------------------------------------
_ENTRY_PROPS = {"market": MARKET, "side": SIDE, "stake": STAKE, "order_type": ORDER_TYPE}
_PERPS = _obj({}, [])

TOOLS: tuple[Tool, ...] = (
    Tool("observe", "read",
         "Snapshot of every market this session may trade, from the same server code as the Play screen: window and "
         "seconds left, market prices (dollars per contract, 0-1) and freshness, the call and how many of the six "
         "lights agree, the six lights, the BTC gap to the strike ($), the cone, BTC series (about 30 points) and "
         "levels, per side the entry terms for `stake` (contracts, if_wins / if_loses in $, the market buy's worst "
         "case in $ and whether it fits), positions with advice and owner (agent = yours to manage, you = the "
         "user's), mode and limits; plus balances ($) and the session: state, PAPER/LIVE per market, budget used "
         "(realized + at_risk) and left ($), per-trade cap, seconds left, API spend ($), wakes, alarms.",
         _obj({"stake": {**STAKE, "description": "Dollars to size the entry terms at (default: the session's "
                                                 "per-trade cap)."}}, []), AgentTools.observe),
    Tool("get_signals", "read",
         "The call and the six lights now for one market (1m/5m/15m pressure, market mid, our model, crowd). Flip "
         "history is not recorded yet (flips: null).",
         _obj({"market": MARKET}, ["market"]), AgentTools.get_signals),
    Tool("get_odds", "read",
         "This window's recorded odds history for one market (Up/Down bid and ask over time, dollars per contract), "
         "where the app records it; otherwise not available.",
         _obj({"market": MARKET}, ["market"]), AgentTools.get_odds),
    Tool("get_btc_series", "read",
         "The window's full BTC index series ([ms, $] pairs, up to 300), its high / low / open levels, the BTC gap to "
         "the strike and the cone to the close.",
         _obj({"market": MARKET}, ["market"]), AgentTools.get_btc_series),
    Tool("get_scoreboard", "read",
         "Results on one market (P&L in $, record, recent trades), all trades or only this session's.",
         _obj({"market": MARKET, "this_session_only": {"type": "boolean"}}, ["market"]), AgentTools.get_scoreboard),
    Tool("get_perps_record", "read", "Not available yet: perps for agents come in a later step. Takes no arguments.",
         _PERPS, AgentTools.perps_later),
    Tool("get_journal", "read", "This session's journal: past wakes, tool calls with their notes, notes and state "
                                "changes, oldest first.",
         _obj({"after_id": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1,
                                                                        "maximum": 200}}, []), AgentTools.get_journal),
    Tool("quote_entry", "read",
         "The full ticket an entry would get now, re-priced on the venue: contracts, sized_at (the ask), limit_price "
         "(the most paid per contract, dollars 0-1), cost_at_sized and max_cost (worst case incl. fee, $), take "
         "profit; would_send says whether the order gate would pass now and why not. Prepares nothing to approve.",
         _obj({**_ENTRY_PROPS, "max_price": {**PRICE, "description": "Limit orders only: the limit price."}},
              ["market", "side", "stake", "order_type"]), AgentTools.quote_entry),
    Tool("place_entry", "write",
         "Buy one side now. Re-priced at send time and refused, with the new numbers and nothing sent, if the "
         "ticket's limit_price is above max_price or its worst case (max_cost) above max_worst_case; otherwise sent "
         "at once through the app's order gate (session budget, per-trade cap, user limits). Take profit: the "
         "screen's (99¢, ride to settlement). For a market order max_price bounds the ask plus market protection.",
         _obj({**_ENTRY_PROPS, "max_price": {**PRICE, "description": "Highest price per contract you accept "
                                                                     "(dollars 0-1); a limit order's limit."},
               "max_worst_case": {"type": "number", "exclusiveMinimum": 0,
                                  "description": "Most this order may cost in $, fee included."},
               "btc_stop": {"type": ["number", "null"], "exclusiveMinimum": 0,
                            "description": "Optional BTC stop ($) set with the entry in one step: below BTC for UP, "
                                           "above for DOWN; null or absent = none."},
               "note": NOTE, "request_id": REQUEST_ID},
              ["market", "side", "stake", "order_type", "max_price", "max_worst_case", "note", "request_id"]),
         AgentTools.place_entry),
    Tool("place_exit", "write", "Sell all of one of your positions now at the bid. Only positions this session "
                                "opened and the user has not taken over.",
         _obj({"bracket_id": BRACKET_ID, "note": NOTE, "request_id": REQUEST_ID},
              ["bracket_id", "note", "request_id"]), AgentTools.place_exit),
    Tool("set_take_profit", "write", "Move one of your positions' resting take profit (dollars per contract, above "
                                     "your entry, at most 0.99).",
         _obj({"bracket_id": BRACKET_ID, "price": PRICE, "note": NOTE, "request_id": REQUEST_ID},
              ["bracket_id", "price", "note", "request_id"]), AgentTools.set_take_profit),
    Tool("set_btc_stop", "write", "Set, move or remove (null) one of your positions' BTC stop: a BTC price ($) at "
                                  "which the app sells at the bid (below BTC for UP, above for DOWN).",
         _obj({"bracket_id": BRACKET_ID, "level": {"type": ["number", "null"], "exclusiveMinimum": 0},
               "note": NOTE, "request_id": REQUEST_ID}, ["bracket_id", "level", "note", "request_id"]),
         AgentTools.set_btc_stop),
    Tool("quote_perp_entry", "read", "Not available yet: perps for agents come in a later step. Takes no arguments.",
         _PERPS, AgentTools.perps_later),
    Tool("place_perp_entry", "write", "Not available yet: perps for agents come in a later step. Takes no arguments.",
         _PERPS, AgentTools.perps_later),
    Tool("place_perp_exit", "write", "Not available yet: perps for agents come in a later step. Takes no arguments.",
         _PERPS, AgentTools.perps_later),
    Tool("place_perp_trigger", "write", "Not available yet: perps for agents come in a later step. Takes no "
                                        "arguments.", _PERPS, AgentTools.perps_later),
    Tool("set_alarms", "session",
         "Replace your alarms (at most 10); each fires once and wakes you. btc_above / btc_below: BTC $; mid_above / "
         "mid_below: a market's UP mid (0-1); seconds_left_below: a market's window; at_time: ms since epoch.",
         _obj({"alarms": {"type": "array", "maxItems": 10, "items": _obj(
             {"kind": {"type": "string", "enum": list(ALARM_KINDS)}, "value": {"type": "number"},
              "market": {**MARKET, "description": "Required for mid_* and seconds_left_below only."}},
             ["kind", "value"])}}, ["alarms"]), AgentTools.set_alarms),
    Tool("snooze", "session", "Mute ordinary wakes until a time (until_ms, ms since epoch) or until an event type "
                              "arrives (until_event); urgent events still wake you. Give exactly one.",
         _obj({"until_ms": {"type": "integer", "minimum": 0},
               "until_event": {"type": "string", "enum": sorted(EVENT_CLASS)}}, []), AgentTools.snooze),
    Tool("update_notes", "session", "Replace your working notes (at most 6000 characters); they are shown to you "
                                    "at every wake.",
         _obj({"text": {"type": "string", "minLength": 1, "maxLength": NOTES_MAX}}, ["text"]),
         AgentTools.update_notes),
    Tool("end_session", "session", "End this session now. Open positions stay open with their take profit and "
                                   "stops and pass to the user; nothing is sold.",
         _obj({"reason": {"type": "string", "minLength": 1, "maxLength": 500}}, ["reason"]), AgentTools.end_session),
    Tool("record_decision", "session",
         "Required: call exactly once at the end of every wake, after any orders. Records your decision for the "
         "user's screen and scorecard: action, the side, your probability that UP settles with its range and source, "
         "the market price you compared against, conservative net edge per contract in cents after fees, the pass "
         "reason (required for NO_TRADE, allowed for HOLD), thesis and invalidation. Every field is required; use "
         "null where you do not know, never an invented number. The app adds the time, the market's window and its "
         "UP mid now.",
         _obj(DECISION_PROPS, list(DECISION_KEYS)), AgentTools.record_decision),
)

_names = [t.name for t in TOOLS]
if len(set(_names)) != len(_names) or any(t.kind not in ("read", "write", "session") for t in TOOLS):
    raise ValueError("agent tool registry: duplicate name or unknown kind")
