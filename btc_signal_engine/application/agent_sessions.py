"""Agent sessions (Agent mode, docs/plans/2026-10-30-1402-feat-agent-mode-mcp-plan.md, U3; R1-R6, R10-R12, R19).

Multi-agent (docs/plans/2026-10-31-0010-feat-multi-agent-plan.md, U1/U2; KD1-KD3, KD7): this is a REGISTRY of
sessions keyed by id. Any number run at once; every call names its session (there is no "the live session"; only the
deprecated single-session routes ask for `latest()`, the most recently launched live one). One agent = ONE market.
An agent then trades inside its session through the ordinary order path. Nothing here sends an order: the trading
services ask `check` for every order an agent sends (the order gate's step 2, in place of "someone is at the
screen"); the session is the one the acting `agent:<id>` context names (actor.agent_session), and a refusal names its
reason in plain English.

Rules:
- Launch validates every field strictly; an unknown venue, a missing end time or a budget of 0 is an error, never a
  default. `market` is ONE market key (MARKETS); a list, or the deprecated `venues` list with more than one market,
  is refused (code one_market): launch one agent per market. The deprecated `venues` field (the Agent tab and the
  presets saved before 2026-10-31) is accepted only as a list of exactly one market; giving both is refused. The
  stored limits keep `venues: [market]` next to `market` so readers of the old shape keep working. The market's
  trading mode is pinned at launch (R6); production is refused until the user has reviewed a paper session
  (`allow_production`, set by the app's wiring). Another live session on the same market is allowed (KD2): the
  launch answer and the views name it (`also_traded_by`).
- Agents (L0): an agent is a lasting identity (table `agents`: id, name, market, created_ts, archived); each session
  is one run of one agent (`agent_id` on the session and on every journal row), so an agent's journal spans all its
  runs. Launch picks the agent: `agent_id` (an existing, unarchived agent; unknown: unknown_agent; archived:
  agent_archived; already running: agent_busy; one run at a time; a `market` that differs: market_mismatch; a `name`
  with it: invalid, rename instead) -> a new run of it; else `name` (1-40 characters after trimming, unique among
  unarchived agents, case-insensitive, else name_taken) -> a new agent; neither -> the market's default agent: the
  idle unarchived agent named "<short market label> agent" ("Kalshi 15m agent"), else " 2", " 3" ... (the lowest
  numbered idle one, else a new one with the lowest number free). `rename` renames the agent.
- Positions are owned (KD2): an agent may see, exit or modify only its own positions (`check`'s ownership step, and
  observe hides other sessions' positions). Two sessions holding opposite sides of the same market window is allowed
  and shown: `overlaps()` lists it and `check_overlaps` journals a warning row (kind "overlap") into both sessions
  once and tells `overlap_fn`; nothing is blocked.
- Global limits (KD3, `global_limits` / `set_global_limits`, off by default, set on the PC): `max_loss_day` (the
  most every agent session together may lose in the local day: realized net loss plus the worst case of every agent
  position opened that day) and `api_day` (estimated API dollars of every session started that day, or still live).
  When set, the gate refuses an agent entry that would pass max_loss_day (code over_global_limit); once either is
  reached, `review` pauses every running agent with code global_limit and resume waits until it is no longer reached.
- The kill switch (any controlled service killed) pauses every running agent (code kill_switch).
- Budget (R2): the most the session may lose. Realized net loss plus the worst case (max_cost) of every position the
  session opened that is not closed, plus the new order's worst case, never exceeds it. Winnings lower the realized
  loss, so they fund further trades. Positions the user took over still count (R11): ownership decides who may manage
  a position, never whether it counts. The figures come from the trading rows stamped with the session id. A
  position whose window closed and whose official result is not in yet (`settling`) counts at its worst case: what
  it cost less what it already brought back (its held contracts paying $0), never as a win or a booked loss.
- Per-trade cap: an entry's max_cost may not exceed it.
- Rules (`limits.rules`, agent_rules.RULES ids, default none; in-app sessions only, each rule with at least one of
  its markets among the venues; it runs on those): the runner opens their entries; while any is armed the agent's
  own opening tools are refused. `limits.one_bet_per_window` (bool, default true): at most one rule entry per
  settlement instant across venues (off: the same rule may fire on Kalshi 15m and Polymarket 15m in one window).
  A retired rule (rule_scores.RuleBook: by the user, or automatically at its scorekeeper's bars) is refused
  (code rule_retired); launch first applies any auto-retirement the tally has crossed since the last session.
- An agent may exit or adjust only positions its session opened and the user has not taken over.
- States: running -> paused (kill switch, a venue's mode changed, model unavailable) -> running (user resumes) ;
  running|paused -> ended (Stop, end time, API-spend cap, the agent's own end_session, invalid key, app restart).
  A session found running or paused at start-up is ended "restart", never resumed.
- An external agent (MCP) proves its session with a token shown once at launch; only its sha256 is stored.
- Autonomy level (`limits.level`, required at launch, LEVELS; anything else is refused, code invalid_level):
  "auto" = the runner opens an armed rule's match at once (as before); "assist" = the runner only PROPOSES it
  (agent_proposals): the user approves or skips, and only the user's approval can send it. Assist needs an in-app
  session with at least one rule; `limits.proposal_ttl_s` (assist only, PROPOSAL_TTL_S bounds, default 20) is how
  long a proposal waits. In assist the gate refuses every entry the agent sends itself (assist_needs_you); exits of
  its own positions stay allowed (they reduce risk). The assist steps (`assist_step`) are the only way an assist
  entry passes: "propose" (the proposal's re-priced ticket, previewed as the agent: nothing approvable) and
  "approve" (the user's tap: never the agent actor, and a person must be at the screen, as for the slide).
"""

from __future__ import annotations

from datetime import datetime, tzinfo

import hashlib
import logging
import secrets
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol

from btc_signal_engine.application.actor import (AGENT_PREFIX, actor, agent_session, current_actor,
                                                 current_agent_session)
from btc_signal_engine.application.agent_rules import RULES
from btc_signal_engine.application.ports import Repository
from btc_signal_engine.application.rule_scores import BRACKETS, JOURNAL, OPEN_STATES, SETTLING, RuleBook
from btc_signal_engine.application.trading import GateResult
from btc_signal_engine.domain.markets import market as market_spec
from btc_signal_engine.domain.markets import phone_markets

log = logging.getLogger(__name__)

SESSIONS = "agent_sessions"
WAKE_INPUTS = "agent_wake_inputs"  # each wake's exact model input (migration 0021)

# Agent venues = the Play tabs (market key -> the tab's trading strategy). Perps join when their gate is wired.
VENUES: dict[str, str] = {m.key: m.phone_strategy for m in phone_markets()}
VENUE_LABELS: dict[str, str] = {m.key: m.label for m in phone_markets()}
# KD7's default agent names use the short market label: "Kalshi 15m agent", "Polymarket 5m agent"
SHORT_LABELS: dict[str, str] = {k: v.replace(" min", "m").replace(" hour", "h") for k, v in VENUE_LABELS.items()}
KINDS = ("in_app", "external")
MAX_SESSION_MS = 8 * 3_600_000
MAX_MINUTES = MAX_SESSION_MS // 60_000
# Why a session paused, as a machine code next to the text (pause_code): the panel switches on the code.
PAUSE_CODES = ("kill_switch", "mode_changed", "model_unavailable", "runtime_misconfigured", "bad_request",
               "global_limit", "other")
MARKETS = tuple(VENUES)  # the market keys an agent may own (one per session)
NAME_MAX = 40
GLOBAL_LIMITS = {"strategy": "agent", "key": "global_limits"}  # KD3, strategy_settings
GLOBAL_KEYS = ("max_loss_day", "api_day")
AGENTS = "agents"
# Every journal kind written (the journal routes' `kinds` filter is refused on anything else)
JOURNAL_KINDS = ("wake", "tool", "note", "notes", "state", "decision", "rule", "rule_status", "proposal",
                 "proposal_outcome", "proposal_scored", "overlap")
DAY_MS = 86_400_000
OVERLAP = "overlap"  # journal kind: opposite sides of one market window held by two sessions (KD2 warning)
LIVE = ("running", "paused")
OK = GateResult(True)
LEVELS = ("auto", "assist")
PROPOSAL_TTL_S = (10, 60)  # an assist proposal waits this long at least / at most (seconds)
DEFAULT_PROPOSAL_TTL_S = 20
ASSIST_STEPS = ("propose", "approve")

_assist_step: ContextVar[str | None] = ContextVar("assist_step", default=None)


@contextmanager
def assist_step(session_id: str, step: str) -> Iterator[None]:
    """Run the enclosed order calls as an assist step of `session_id` (agent_proposals only). "propose": as the
    session's agent (the proposal's ticket, previewed); "approve": the session's order under the actor already set
    (the user's request), never as the agent."""
    if step not in ASSIST_STEPS:
        raise ValueError(f"unknown assist step {step!r}; steps are {', '.join(ASSIST_STEPS)}")
    a = current_actor.set(AGENT_PREFIX + session_id[:8]) if step == "propose" else None
    sid = current_agent_session.set(session_id)
    st = _assist_step.set(step)
    try:
        yield
    finally:
        _assist_step.reset(st)
        current_agent_session.reset(sid)
        if a is not None:
            current_actor.reset(a)


def current_assist_step() -> str | None:
    return _assist_step.get()


# U11 (Telegram): a person answering on the paired remote chat. Set only by application/notifier.py around ONE
# proposal approval, after it checked the paired user, chat and the tap's one-time nonce; it stands in for "a person
# at the screen" in the assist "approve" step only (never for the slide, an agent order or anything else).
_remote_human: ContextVar[str | None] = ContextVar("remote_human", default=None)


@contextmanager
def remote_human(label: str) -> Iterator[None]:
    """Run the enclosed proposal approval as the remote person `label` (e.g. "telegram:<user id>"): the actor is that
    label and the approve step's presence check accepts it."""
    if not label or label.startswith(AGENT_PREFIX):
        raise ValueError(f"not a person's label: {label!r}")
    a = current_actor.set(label)
    r = _remote_human.set(label)
    try:
        yield
    finally:
        _remote_human.reset(r)
        current_actor.reset(a)


POSITION_KEYS = ("id", "strategy", "ticker", "side", "state", "filled", "entry_avg", "entry_cost", "max_cost",
                 "take_profit", "exit_reason", "proceeds", "tp_fees", "exit_fees", "stop_index_below",
                 "stop_index_above", "window_end", "created_ts", "closed_ts")


def position_view(b: Mapping[str, Any]) -> dict:
    """One session position for the Agent panel, with its exact figures: `entry_cost` (contracts x price + the entry
    fee), `entry_fees`, `fees` (entry + take-profit + exit fees, once filled) and `realized` (closed and filled:
    proceeds - entry_cost - tp_fees - exit_fees, the scorekeeper's net; else None). No take-profit order (it rides to
    settlement): take_profit None."""
    out = {k: b.get(k) for k in POSITION_KEYS}
    if b.get("no_tp"):
        out["take_profit"] = None
    filled = float(b.get("filled") or 0.0)
    cost = b.get("entry_cost")
    entry_fees = b.get("entry_fees")
    if entry_fees is None and cost is not None and b.get("entry_avg") is not None and filled > 0:
        entry_fees = round(float(cost) - filled * float(b["entry_avg"]), 4)
    out["entry_fees"] = entry_fees
    out["fees"] = (round(float(entry_fees or 0.0) + float(b.get("tp_fees") or 0.0) + float(b.get("exit_fees") or 0.0), 4)
                   if filled > 0 else None)
    out["realized"] = (round(float(b.get("proceeds") or 0.0) - float(cost or 0.0) - float(b.get("tp_fees") or 0.0)
                             - float(b.get("exit_fees") or 0.0), 4)
                       if b.get("state") == "closed" and filled > 0 else None)
    return out


class Controlled(Protocol):
    """What a session needs from each trading service it may use."""

    mode: str

    def killed(self) -> bool: ...

    def present(self, now: int) -> bool: ...


class LaunchError(ValueError):
    """A launch refused for a stated reason (`code`)."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code, self.reason = code, reason


def _money(spec: Mapping[str, Any], key: str, *, required: bool = True) -> float | None:
    v = spec.get(key)
    if v is None and not required:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not v > 0:
        raise LaunchError("invalid", f"{key} must be a dollar amount above $0 (got {v!r}).")
    return round(float(v), 2)




def market_of(spec: Mapping[str, Any], default: str | None = None) -> str:
    """The launch's ONE market key: `market`, or the deprecated `venues` given as a list of exactly one market (the
    Agent tab and presets saved before 2026-10-31). A list or a second market is refused (one_market)."""
    has_market, has_venues = spec.get("market") is not None, spec.get("venues") is not None
    if has_market and has_venues:
        raise LaunchError("invalid", "Give the agent's market as `market` only (venues is the old field).")
    if has_venues:
        venues = spec.get("venues")
        if not isinstance(venues, (list, tuple)) or not venues:
            raise LaunchError("invalid", "Pick the agent's market.")
        if len(venues) > 1:
            raise LaunchError("one_market", "One agent trades one market: launch one agent per market.")
        key = venues[0]
    elif has_market:
        key = spec.get("market")
        if isinstance(key, (list, tuple)):
            raise LaunchError("one_market", "One agent trades one market: give one market key, then launch "
                                            "another agent for the next market.")
    elif default is not None:  # an existing agent's own market
        key = default
    else:
        raise LaunchError("invalid", "Pick the agent's market.")
    if not isinstance(key, str) or key not in VENUES:
        raise LaunchError("unknown_venue", f"Unknown market {key!r}; markets are {', '.join(VENUES)}.")
    return key


def clean_name(name: Any) -> str | None:
    """A session name trimmed to single spaces, 1-NAME_MAX characters, else None."""
    if not isinstance(name, str):
        return None
    n = " ".join(name.split())
    return n if 1 <= len(n) <= NAME_MAX else None


def _limit_value(key: str, v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not v > 0:
        raise LaunchError("invalid", f"{key} must be a dollar amount above $0, or null for off (got {v!r}).")
    return round(float(v), 2)


def _parts(brackets: list[dict]) -> dict:
    """Budget figures of these brackets: realized net loss (gains negative), at-risk worst case, pending settlement."""
    realized = at_risk = pending = 0.0
    for b in brackets:
        net_cost = ((b.get("entry_cost") or 0.0) + (b.get("tp_fees") or 0.0) + (b.get("exit_fees") or 0.0)
                    - (b.get("proceeds") or 0.0))
        if b["state"] in OPEN_STATES:
            at_risk += float(b.get("max_cost") or 0.0)
        elif b["state"] == SETTLING:  # worst case: the held contracts pay $0
            at_risk += net_cost
            pending += net_cost
        elif (b.get("filled") or 0) > 0:  # closed: what it cost minus what it brought back
            realized += net_cost
    return {"realized": round(realized, 4), "at_risk": round(at_risk, 4), "pending_settlement": round(pending, 4)}


class AgentSessions:
    def __init__(self, repo: Repository, services: Mapping[str, Controlled], *, allow_production: bool = False,
                 id_fn: Callable[[], str] = lambda: uuid.uuid4().hex,
                 token_fn: Callable[[], str] = lambda: secrets.token_urlsafe(24),
                 notify_fn: Callable[[dict], None] | None = None, rule_book: RuleBook | None = None,
                 overlap_fn: Callable[[dict], None] | None = None,
                 agent_id_fn: Callable[[], str] = lambda: uuid.uuid4().hex) -> None:
        """`services` maps each trading strategy ("you", "you_poly", "you_poly5") to its service. `notify_fn(session
        row)` is told after every state change of any session (the live panels' streams, Telegram). `overlap_fn(w)`
        is told once about each new opposite-side overlap warning (KD2)."""
        self.repo = repo
        self.services = dict(services)
        self.allow_production = allow_production
        self._id, self._token, self._agent_id = id_fn, token_fn, agent_id_fn
        self._notify = notify_fn
        self.overlap_fn = overlap_fn
        self.rule_book = rule_book if rule_book is not None else RuleBook(repo)  # rule status (rule_scores)
        self._live: dict[str, dict] = {}  # session id -> its row while running or paused
        self._stopping: set[str] = set()  # a Stop was asked for: no new order of that session passes the gate
        self._overlaps_seen: set[tuple] = set()  # (strategy, ticker, frozenset of session ids) already warned
        # KD3 fleet-wide API ledger: session id -> the dollars its model turn in flight reserved (released when the
        # turn is settled). Every session's wake checks the global daily API limit against usage + these, so
        # concurrent wakes cannot each reserve the same last dollars.
        self._api_held: dict[str, float] = {}
        self._global: dict | None = None  # the stored global limits, cached (this object writes them)

    # -- start-up -------------------------------------------------------------------------------------------
    def restore(self, now: int) -> None:
        """At start-up: every session left running or paused is ended "restart" (never resumed); positions stay."""
        for row in self.repo.select(SESSIONS, filters=[("state", "in", LIVE)]):
            self._live[row["id"]] = row
            self._end(row["id"], "restart", now, "The app restarted; agent sessions never resume on their own.")

    # -- reads ------------------------------------------------------------------------------------------------
    def get(self, session_id: str | None) -> dict | None:
        """The live (running or paused) session row with this id, or None."""
        return self._live.get(session_id) if session_id else None

    def live(self) -> list[dict]:
        """Every live session row, oldest launch first."""
        return sorted(self._live.values(), key=lambda s: (s["started_ts"], s["id"]))

    def latest(self) -> dict | None:
        """DEPRECATED (the single-session routes and Telegram's legacy fallbacks only, until the fleet UI lands): the
        most recently launched live session, or None. Nothing else may treat one session as "the" session."""
        rows = self.live()
        return rows[-1] if rows else None

    def latest_view(self, now: int) -> dict | None:
        """DEPRECATED (single-session routes only): the most recently launched live session's view, else the most
        recently started session's (ended) view, else None."""
        s = self.latest()
        if s is None:
            rows = self.repo.select(SESSIONS, order_by=["-started_ts"], limit=1)
            s = rows[0] if rows else None
        return self._view_of(s, now) if s is not None else None

    def row(self, session_id: str) -> dict | None:
        """Any session's row (live from memory, else stored), or None."""
        s = self._live.get(session_id)
        if s is not None:
            return s
        rows = self.repo.select(SESSIONS, {"id": session_id}, limit=1)
        return rows[0] if rows else None

    def view(self, session_id: str, now: int) -> dict | None:
        s = self.row(session_id)
        return self._view_of(s, now) if s is not None else None

    def _view_of(self, s: dict, now: int) -> dict:
        parts = self.budget_parts(s["id"])
        used = round(parts["realized"] + parts["at_risk"], 4)
        lim, market = s["limits"], self.market_key(s)
        live = s["state"] in LIVE
        return {"id": s["id"], "agent_id": s.get("agent_id"), "name": self.name_of(s), "market": market,
                "kind": s["kind"],
                "state": s["state"], "limits": lim, "pinned": s["pinned"],
                "model": s["model"], "prompt_versions": s["prompt_versions"], "usage": s["usage"] or {},
                "pause_reason": s["pause_reason"], "pause_code": s.get("pause_code"), "started_ts": s["started_ts"],
                "ends_ts": s["ends_ts"], "ended_ts": s["ended_ts"], "end_reason": s["end_reason"],
                "updated_ts": s.get("updated_ts"), "budget_used": used, "budget_parts": parts,
                "budget_left": round(lim["budget"] - used, 4), "taken_over": s["taken_over"] or [],
                "also_traded_by": self.also_traded_by(market, exclude=s["id"]) if live else [],
                "seconds_left": max(0, (s["ends_ts"] - now) // 1000) if live else 0}

    @staticmethod
    def market_key(s: Mapping[str, Any]) -> str | None:
        """A session's one market (a session stored before multi-agent: its first venue)."""
        lim = s.get("limits") or {}
        if lim.get("market"):
            return lim["market"]
        venues = lim.get("venues") or []
        return venues[0] if venues else None

    @staticmethod
    def name_of(s: Mapping[str, Any]) -> str:
        """A session's name (a session stored before names: "<market label> agent")."""
        lim = s.get("limits") or {}
        if lim.get("name"):
            return lim["name"]
        key = AgentSessions.market_key(s)
        return f"{SHORT_LABELS.get(key or '', key or 'Agent')} agent"

    def also_traded_by(self, market: str | None, exclude: str | None = None) -> list[dict]:
        """The other live sessions on this market (KD2's "Also traded by"): [{id, name}]."""
        return [{"id": s["id"], "name": self.name_of(s)} for s in self.live()
                if s["id"] != exclude and self.market_key(s) == market]

    def default_name(self, market: str) -> str:
        """"<market label> agent", then " 2", " 3" ...: the lowest one no unarchived agent uses."""
        base = f"{SHORT_LABELS[market]} agent"
        taken = {a["name"].casefold() for a in self.agents()}
        if base.casefold() not in taken:
            return base
        n = 2
        while f"{base} {n}".casefold() in taken:
            n += 1
        return f"{base} {n}"

    def _name_taken(self, name: str, exclude: str | None = None) -> bool:
        """Another unarchived agent has this name (case-insensitive); `exclude`: an agent id."""
        return any(a["name"].casefold() == name.casefold() for a in self.agents() if a["id"] != exclude)

    # -- agents (L0) --------------------------------------------------------------------------------------------
    def agents(self, *, archived: bool = False) -> list[dict]:
        """Every agent (unarchived ones; `archived`: also the archived), oldest first (ties: by name)."""
        rows = self.repo.select(AGENTS, order_by=["created_ts", "name", "id"])
        return [dict(a) for a in rows if archived or not a["archived"]]

    def agent(self, agent_id: str | None) -> dict | None:
        rows = self.repo.select(AGENTS, {"id": agent_id}, limit=1) if agent_id else []
        return dict(rows[0]) if rows else None

    def running_agent(self, agent_id: str) -> dict | None:
        """The live session (run) of this agent, or None."""
        return next((s for s in self._live.values() if s.get("agent_id") == agent_id), None)

    def _default_agent(self, market: str) -> dict | None:
        """The market's idle default-named agent with the lowest number ("Kalshi 15m agent", then " 2" ...)."""
        base = f"{SHORT_LABELS[market]} agent".casefold()
        best = None
        for a in self.agents():
            name = a["name"].casefold()
            if a["market"] != market or self.running_agent(a["id"]) is not None:
                continue
            if name == base:
                n = 1
            elif name.startswith(base + " ") and name[len(base) + 1:].isdigit():
                n = int(name[len(base) + 1:])
            else:
                continue
            if best is None or n < best[0]:
                best = (n, a)
        return best[1] if best else None

    def _pick_agent(self, spec: Mapping[str, Any], now: int, *, dry_run: bool) -> tuple[dict, str, bool]:
        """(agent row, market, new) for this launch (see the module docstring); a new agent is not stored here."""
        aid = spec.get("agent_id")
        if aid is not None:
            a = self.agent(aid) if isinstance(aid, str) else None
            if a is None:
                raise LaunchError("unknown_agent", f"There is no agent {aid!r}.")
            if a["archived"]:
                raise LaunchError("agent_archived", f"{a['name']} is archived; it keeps its runs but cannot run again.")
            if spec.get("name") is not None:
                raise LaunchError("invalid", "An existing agent keeps its name; rename it instead of naming it here.")
            market = market_of(spec, default=a["market"])
            if market != a["market"]:
                raise LaunchError("market_mismatch", f"{a['name']} trades {VENUE_LABELS.get(a['market'], a['market'])}"
                                                     "; an agent trades one market. Launch another agent for "
                                                     f"{VENUE_LABELS[market]}.")
            if self.running_agent(a["id"]) is not None:
                raise LaunchError("agent_busy", f"{a['name']} is already running; stop that run first, or launch "
                                                "another agent.")
            return a, market, False
        market = market_of(spec)
        raw = spec.get("name")
        if raw is None:
            a = self._default_agent(market)
            if a is not None:
                return a, market, False
            name = self.default_name(market)
        else:
            name = clean_name(raw)
            if name is None:
                raise LaunchError("invalid_name", f"An agent's name is 1 to {NAME_MAX} characters.")
            if not dry_run and self._name_taken(name):
                raise LaunchError("name_taken", f"An agent called {name!r} already exists; run it again with its "
                                                "agent_id, or pick another name.")
        return {"id": self._agent_id(), "name": name, "market": market, "created_ts": now, "archived": False,
                "updated_ts": now}, market, True

    def preset_target(self, template: str | None, market: str) -> dict:
        """What a preset's name template launches: {"agent_id"} of the idle unarchived agent with that name on that
        market; else {"name"}: the template, or (taken) "<template> 2", " 3" ...; no template: {} (the default
        agent)."""
        if not template:
            return {}
        same = next((a for a in self.agents() if a["name"].casefold() == template.casefold()), None)
        if same is not None and same["market"] == market and self.running_agent(same["id"]) is None:
            return {"agent_id": same["id"]}
        if same is None:
            return {"name": template}
        n = 2
        while self._name_taken(f"{template} {n}"):
            n += 1
        name = clean_name(f"{template} {n}")
        return {"name": name} if name else {}

    def next_agent(self, market: str) -> dict:
        """What a launch on `market` with neither agent_id nor name runs: {agent_id, name} of the market's idle default
        agent, else {agent_id: None, name: the default name a new one gets} (the launch sheet's prefilled name)."""
        a = self._default_agent(market)
        return {"agent_id": a["id"], "name": a["name"]} if a else {"agent_id": None, "name": self.default_name(market)}

    def archive_agent(self, agent_id: str, now: int) -> dict:
        """Retire an agent (Record's "Retire"): it keeps its runs and journal and cannot run again. Refused while it
        runs (agent_busy) or when it is archived already (agent_archived)."""
        a = self.agent(agent_id)
        if a is None:
            return {"ok": False, "code": "not_found", "reason": f"There is no agent {agent_id!r}."}
        if a["archived"]:
            return {"ok": False, "code": "agent_archived", "reason": f"{a['name']} is retired already."}
        if self.running_agent(agent_id) is not None:
            return {"ok": False, "code": "agent_busy", "reason": f"{a['name']} is running; stop it first."}
        self.repo.update(AGENTS, {"id": agent_id}, {"archived": True, "updated_ts": now})
        return {"ok": True, "agent": {**self.agent(agent_id), "archived": True}}

    def rename_agent(self, agent_id: str, name: Any, now: int) -> dict:
        a = self.agent(agent_id)
        if a is None:
            return {"ok": False, "code": "not_found", "reason": f"There is no agent {agent_id!r}."}
        n = clean_name(name)
        if n is None:
            return {"ok": False, "code": "invalid_name", "reason": f"An agent's name is 1 to {NAME_MAX} characters."}
        if self._name_taken(n, exclude=agent_id):
            return {"ok": False, "code": "name_taken", "reason": f"Another agent is called {n!r}."}
        self.repo.update(AGENTS, {"id": agent_id}, {"name": n, "updated_ts": now})
        s = self.running_agent(agent_id)
        if s is not None:
            s["limits"] = {**s["limits"], "name": n}
            s["updated_ts"] = now
            self._save(s, ("limits", "updated_ts"))
            self.journal("state", {"renamed": n, "reason": f"Renamed {n!r} by you."}, now, session_id=s["id"])
            self._changed(s)
        return {"ok": True, "agent": self.agent(agent_id)}

    def budget_used(self, session_id: str) -> float:
        """Realized net loss (gains count negative) plus the worst case of every position not yet closed (R2)."""
        parts = self.budget_parts(session_id)
        return round(parts["realized"] + parts["at_risk"], 4)

    @staticmethod
    def budget_used_of(rows: list[dict]) -> float:
        """`budget_used` over rows already read (the fleet's one read per tick)."""
        parts = _parts(rows)
        return round(parts["realized"] + parts["at_risk"], 4)

    def brackets_by_session(self, session_ids: list[str]) -> dict[str, list[dict]]:
        """Every bracket of these sessions in one query (ix_trading_brackets_session), grouped by session id (each
        id present, [] when it has none)."""
        out: dict[str, list[dict]] = {sid: [] for sid in session_ids}
        if session_ids:
            for b in self.repo.select(BRACKETS, filters=[("session_id", "in", list(session_ids))]):
                out.setdefault(b["session_id"], []).append(b)
        return out

    def budget_parts(self, session_id: str) -> dict:
        """The budget used, split: `realized` net loss of closed positions (gains negative) and `at_risk` = the worst
        case of every position not yet closed: max_cost while live; for a settling one (result pending) what it cost
        less what it already brought back, also given alone as `pending_settlement`."""
        return _parts(self.repo.select(BRACKETS, {"session_id": session_id}))

    def session_for_token(self, token: str) -> str | None:
        """The live external session this token proves, or None."""
        if not token:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        for s in self._live.values():
            if s["kind"] == "external" and s.get("token_hash") and secrets.compare_digest(digest, s["token_hash"]):
                return s["id"]
        return None

    # -- launch / rename / stop / pause / resume ----------------------------------------------------------------
    def launch(self, spec: Mapping[str, Any], now: int, *, prompt_versions: dict | None = None) -> dict:
        """Start a session from the user's launch form. Returns {ok, id, session, also_traded_by, token?} or {ok:
        False, code, reason}."""
        try:
            row, token = self._validate(spec, now)
        except LaunchError as e:
            return {"ok": False, "code": e.code, "reason": e.reason}
        row["prompt_versions"] = prompt_versions
        agent = row.pop("_agent")
        if agent.pop("_new"):
            self.repo.insert(AGENTS, [agent], ignore_conflicts=False)
        self.repo.insert(SESSIONS, [row], ignore_conflicts=False)
        self._live[row["id"]] = row
        self.journal("state", {"state": "running", "reason": "Launched by you."}, now, session_id=row["id"])
        self._changed(row)
        view = self.view(row["id"], now)
        out = {"ok": True, "id": row["id"], "agent_id": row["agent_id"], "session": view,
               "also_traded_by": view["also_traded_by"]}
        if token is not None:
            out["token"] = token  # shown once; only its hash is kept
        return out

    def validate(self, spec: Mapping[str, Any], now: int) -> dict | None:
        """A dry run of launch's checks (a saved preset, U11): the refusal {ok False, code, reason} launch would give
        now, or None. A preset's name is a template, so a name an agent already has is no refusal here (the launch
        runs that agent again or numbers the name, `preset_target`). Nothing is stored."""
        try:
            self._validate(spec, now, dry_run=True)
        except LaunchError as e:
            return {"ok": False, "code": e.code, "reason": e.reason}
        return None

    def _validate(self, spec: Mapping[str, Any], now: int, *, dry_run: bool = False) -> tuple[dict, str | None]:
        kind = spec.get("kind")
        if kind not in KINDS:
            raise LaunchError("invalid", f"kind must be one of {', '.join(KINDS)} (got {kind!r}).")
        agent, market, new = self._pick_agent(spec, now, dry_run=dry_run)
        name = agent["name"]
        budget = _money(spec, "budget")
        per_trade = _money(spec, "per_trade")
        if per_trade > budget:
            raise LaunchError("invalid", "The per-trade cap cannot be larger than the budget.")
        ends, minutes = spec.get("ends_ts"), spec.get("minutes")
        if (ends is None) == (minutes is None):
            raise LaunchError("invalid", "Give the session's length in minutes or its end time (ends_ts): exactly "
                                         "one of the two.")
        if minutes is not None:
            if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not 1 <= minutes <= MAX_MINUTES:
                raise LaunchError("invalid", f"minutes must be a number from 1 to {MAX_MINUTES} (got {minutes!r}).")
            ends = now + round(minutes * 60_000)  # the server's clock, not the device's
        if isinstance(ends, bool) or not isinstance(ends, int) or not now < ends <= now + MAX_SESSION_MS:
            raise LaunchError("invalid", "The end time must be in the future and at most 8 hours away.")
        spend_cap = _money(spec, "spend_cap", required=kind == "in_app")
        model = spec.get("model")
        if kind == "in_app" and (not isinstance(model, str) or not model):
            raise LaunchError("invalid", "Pick a model for the in-app agent.")
        sell_on_stop = spec.get("sell_on_stop", False)
        if not isinstance(sell_on_stop, bool):
            raise LaunchError("invalid", "sell_on_stop must be true or false.")
        rules = spec.get("rules", [])
        if not isinstance(rules, (list, tuple)) or not all(isinstance(r, str) for r in rules):
            raise LaunchError("invalid", f"rules must be a list of rule ids (got {rules!r}).")
        bad = [r for r in rules if r not in RULES]
        if bad:
            raise LaunchError("invalid", f"Unknown rule {bad[0]!r}; rules are {', '.join(RULES)}.")
        if len(set(rules)) != len(rules):
            raise LaunchError("invalid", "A rule is listed twice.")
        if rules:
            self.rule_book.enforce(now)  # a crossing found since the last session (a late settlement) retires first
        for r in rules:
            st = self.rule_book.status(r)
            if st["status"] == "retired":
                by = "automatically" if st["by"] == "auto" else "by you"
                raise LaunchError("rule_retired", f"Rule {r} is retired ({by}): "
                                                  f"{(st['reason'] or 'no reason').rstrip('.')}. Leave it off, or "
                                                  "reactivate it on the PC (Agent tab, Scorecard).")
        if rules and kind != "in_app":
            raise LaunchError("invalid", "Rules run only in in-app agent sessions (the app's own loop opens their "
                                         "entries); launch without rules for an external agent.")
        for r in rules:
            if market not in RULES[r].markets:
                names = " or ".join(market_spec(k).label for k in RULES[r].markets)
                raise LaunchError("invalid", f"Rule {r} trades {names}, not {VENUE_LABELS[market]}; leave the rule "
                                             "off or launch it as an agent on its own market.")
        one_bet = spec.get("one_bet_per_window", True)
        if not isinstance(one_bet, bool):
            raise LaunchError("invalid", f"one_bet_per_window must be true or false (got {one_bet!r}).")
        level = spec.get("level")
        if level not in LEVELS:
            raise LaunchError("invalid_level", f"level must be one of {', '.join(LEVELS)} (got {level!r}).")
        ttl = spec.get("proposal_ttl_s")
        if level == "assist":
            if kind != "in_app" or not rules:
                raise LaunchError("invalid_level", "Assist proposes the armed rules' entries for you to approve: "
                                                   "launch an in-app session with at least one rule.")
            ttl = DEFAULT_PROPOSAL_TTL_S if ttl is None else ttl
            lo, hi = PROPOSAL_TTL_S
            if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not lo <= ttl <= hi:
                raise LaunchError("invalid", f"proposal_ttl_s must be {lo} to {hi} seconds (got {ttl!r}).")
        elif ttl is not None:
            raise LaunchError("invalid", "proposal_ttl_s is only for an assist session.")
        strategy = VENUES[market]
        svc = self.services.get(strategy)
        if svc is None:
            raise LaunchError("unavailable", f"{VENUE_LABELS[market]} is not available in this app.")
        if svc.mode == "prod" and not self.allow_production:
            raise LaunchError("production_locked", f"{VENUE_LABELS[market]} is in production. Agent sessions run in "
                                                   "paper mode until you have reviewed a paper session; switch it "
                                                   "to shadow first.")
        token = self._token() if kind == "external" and not dry_run else None
        sid = self._id()
        if not dry_run and sid in self._live:
            raise LaunchError("invalid", "A session with this id is already live.")
        row = {"id": sid, "agent_id": agent["id"], "_agent": {**agent, "_new": new}, "kind": kind,
               "state": "running",
               "limits": {"name": name, "market": market, "venues": [market], "budget": budget,
                          "per_trade": per_trade, "spend_cap": spend_cap, "sell_on_stop": sell_on_stop,
                          "rules": list(rules), "one_bet_per_window": one_bet, "level": level,
                          "proposal_ttl_s": ttl},
               "pinned": {strategy: svc.mode}, "model": model if kind == "in_app" else None, "prompt_versions": None,
               "token_hash": hashlib.sha256(token.encode()).hexdigest() if token else None, "taken_over": [],
               "usage": {}, "pause_reason": None, "pause_code": None, "started_ts": now, "ends_ts": ends,
               "ended_ts": None, "end_reason": None, "updated_ts": now}
        return row, token

    def rename(self, session_id: str, name: Any, now: int) -> dict:
        """Rename the agent this live session is a run of."""
        s = self.get(session_id)
        if s is None:
            return {"ok": False, "code": "no_session", "reason": "That agent is not running."}
        out = self.rename_agent(s["agent_id"], name, now)
        if not out["ok"]:
            return out
        return {"ok": True, "session": self.view(session_id, now), "agent": out["agent"]}

    def begin_stop(self, session_id: str) -> None:
        """A Stop was asked for: from now on the gate refuses every new order of that session (an order already past
        the gate still finishes). Its end clears it."""
        if session_id in self._live:
            self._stopping.add(session_id)

    def stopping(self, session_id: str) -> bool:
        return session_id in self._stopping

    def stop(self, session_id: str, now: int, reason: str = "stopped", text: str = "Stopped by you.") -> dict:
        if session_id not in self._live:
            return {"ok": False, "code": "no_session", "reason": "There is no such running agent session to stop."}
        self._end(session_id, reason, now, text)
        return {"ok": True, "session": self.view(session_id, now)}

    def pause(self, session_id: str, now: int, why: str, code: str = "other") -> None:
        """Pause a running session: `why` is shown to the user, `code` (PAUSE_CODES) is what the panel acts on."""
        if code not in PAUSE_CODES:
            raise ValueError(f"unknown pause code {code!r}; known: {', '.join(PAUSE_CODES)}")
        s = self._live.get(session_id)
        if s is None or s["state"] != "running":
            return
        s.update(state="paused", pause_reason=why, pause_code=code, updated_ts=now)
        self._save(s, ("state", "pause_reason", "pause_code", "updated_ts"))
        self.journal("state", {"state": "paused", "reason": why, "code": code}, now, session_id=session_id)
        self._changed(s)

    def pause_all(self, now: int, why: str, code: str) -> list[str]:
        """Pause every running session (the kill switch, a global limit). Returns the ids paused."""
        ids = [s["id"] for s in self.live() if s["state"] == "running"]
        for sid in ids:
            self.pause(sid, now, why, code)
        return ids

    def resume(self, session_id: str, now: int) -> dict:
        s = self._live.get(session_id)
        if s is None or s["state"] != "paused":
            return {"ok": False, "code": "not_paused", "reason": "That agent session is not paused."}
        blocked = self._blocked_reason(s, self._killed_now()) or self._global_reached(now)
        if blocked is not None:
            return {"ok": False, "code": "cannot_resume", "reason": blocked[1]}
        s.update(state="running", pause_reason=None, pause_code=None, updated_ts=now)
        self._save(s, ("state", "pause_reason", "pause_code", "updated_ts"))
        self.journal("state", {"state": "running", "reason": "Resumed by you."}, now, session_id=session_id)
        self._changed(s)
        return {"ok": True, "session": self.view(session_id, now)}

    def review(self, now: int) -> None:
        """Called often (the fleet's tick and the review loop): each session ends at its end time; a mode change
        pauses that session; the kill switch or a reached global limit pauses every running one."""
        if not self._live:
            return
        for s in self.live():
            if now >= s["ends_ts"]:
                self._end(s["id"], "time", now, "The session reached its end time.")
        killed = self._killed_now()
        for s in self.live():
            if s["state"] == "running":
                blocked = self._blocked_reason(s, killed)
                if blocked is not None:
                    self.pause(s["id"], now, blocked[1], blocked[0])
        if any(s["state"] == "running" for s in self._live.values()):
            reached = self._global_reached(now)
            if reached is not None:
                self.pause_all(now, reached[1], reached[0])

    def _killed_now(self) -> bool:
        """The kill switch, read once per distinct service in use (one switch reaches every phone service)."""
        strategies = {k for s in self._live.values() for k in s["pinned"]}
        return any(self.services[k].killed() for k in strategies if k in self.services)

    def _blocked_reason(self, s: dict, killed: bool) -> tuple[str, str] | None:
        """(pause code, text) when this session may not run now, else None."""
        if killed:
            return "kill_switch", "The kill switch is on. Turn it off first, then resume."
        for strategy, pinned in s["pinned"].items():
            svc = self.services[strategy]
            if svc.mode != pinned:
                return "mode_changed", (f"{self._label(strategy)} switched from {pinned} to {svc.mode}; this session "
                                        "cannot continue on a different mode. End it and launch a new one.")
        return None

    def note_usage(self, session_id: str, now: int, **add: float) -> dict:
        """Add to the session's usage counters (wakes, api_usd, tokens...); returns the totals."""
        s = self._live.get(session_id)
        if s is None:
            return {}
        usage = dict(s["usage"] or {})
        for k, v in add.items():
            usage[k] = round(usage.get(k, 0) + v, 6)
        s.update(usage=usage, updated_ts=now)
        self._save(s, ("usage", "updated_ts"))
        return usage

    def _end(self, session_id: str, reason: str, now: int, text: str) -> None:
        s = self._live.pop(session_id)
        s.update(state="ended", ended_ts=now, end_reason=reason, pause_reason=None, pause_code=None, updated_ts=now)
        self._save(s, ("state", "ended_ts", "end_reason", "pause_reason", "pause_code", "updated_ts"))
        self.journal("state", {"state": "ended", "reason": text, "end_reason": reason}, now, session_id=session_id)
        self._stopping.discard(session_id)
        self._api_held.pop(session_id, None)
        self._changed(s)

    # -- KD3: optional global limits --------------------------------------------------------------------------
    def global_limits(self) -> dict:
        """{max_loss_day, api_day}: dollars, None = off (the default). Read once, then cached: only
        `set_global_limits` writes them (the review asks every tick)."""
        if self._global is None:
            rows = self.repo.select("strategy_settings", GLOBAL_LIMITS, limit=1)
            v = rows[0]["value"] if rows else {}
            v = v if isinstance(v, dict) else {}
            self._global = {k: v.get(k) for k in GLOBAL_KEYS}
        return dict(self._global)

    def set_global_limits(self, changes: Mapping[str, Any], now: int) -> dict:
        """Set either limit (a dollar amount above $0, or None = off). An unknown key is refused, never dropped."""
        bad = [k for k in changes if k not in GLOBAL_KEYS]
        if bad:
            return {"ok": False, "code": "invalid", "reason": f"Unknown global limit {bad[0]!r}; limits are "
                                                              f"{', '.join(GLOBAL_KEYS)}."}
        try:
            new = {**self.global_limits(), **{k: _limit_value(k, v) for k, v in changes.items()}}
        except LaunchError as e:
            return {"ok": False, "code": e.code, "reason": e.reason}
        if not self.repo.update("strategy_settings", GLOBAL_LIMITS, {"value": new, "updated_ts": now}):
            self.repo.insert("strategy_settings", [{**GLOBAL_LIMITS, "value": new, "updated_ts": now}],
                             ignore_conflicts=False)
        self._global = {k: new.get(k) for k in GLOBAL_KEYS}
        return {"ok": True, "limits": new, "used": self.global_usage(now)}

    day_tz: tzinfo | None = None  # the local zone for "today" (set by bootstrap to the daily summary's zone)

    def day_start(self, now: int) -> int:
        """Local midnight of today (`day_tz`, else this machine's zone): the day the global limits and the fleet's
        "today" count, the same day Record and the Telegram daily summary use."""
        t = datetime.fromtimestamp(now / 1000, self.day_tz) if self.day_tz is not None             else datetime.fromtimestamp(now / 1000).astimezone()
        return int(t.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)

    def global_usage(self, now: int) -> dict:
        """Today's (local day, as Record and the daily summary) use across every agent session: `loss_day` = realized net loss plus the worst case of
        every agent position opened today; `api_day` = estimated API dollars of every session started today or
        still live."""
        start = self.day_start(now)
        rows = self.repo.select(BRACKETS, filters=[("session_id", "is_null", False), ("created_ts", ">=", start)])
        parts = _parts(rows)
        sessions = {s["id"]: s for s in self.repo.select(SESSIONS, filters=[("started_ts", ">=", start)])}
        sessions.update(self._live)
        api = sum(float((s.get("usage") or {}).get("api_usd") or 0.0) for s in sessions.values())
        return {"loss_day": round(parts["realized"] + parts["at_risk"], 4), "api_day": round(api, 6)}

    def global_api_left(self, now: int, *, exclude: str | None = None) -> float | None:
        """Dollars of the global daily API limit left (None: no global API limit): the limit less today's estimated
        spend and every live session's turn reserved now (`hold_api`), but `exclude`'s own reservation (the session
        asking, about to replace it)."""
        cap = self.global_limits()["api_day"]
        if cap is None:
            return None
        held = sum(v for k, v in self._api_held.items() if k != exclude)
        return round(cap - self.global_usage(now)["api_day"] - held, 6)

    def hold_api(self, session_id: str, usd: float) -> None:
        """Set (or, with 0, release) the session's reservation for its model turn in flight. Synchronous: a caller
        that checks `global_api_left` and then holds, with no await between, reserves atomically for the fleet."""
        if usd > 0 and session_id in self._live:
            self._api_held[session_id] = float(usd)
        else:
            self._api_held.pop(session_id, None)

    def api_held(self) -> float:
        """Dollars reserved now by every session's model turn in flight."""
        return round(sum(self._api_held.values()), 6)


    def _global_reached(self, now: int) -> tuple[str, str] | None:
        lim = self.global_limits()
        if lim["max_loss_day"] is None and lim["api_day"] is None:
            return None
        used = self.global_usage(now)
        tail = "Every agent is paused; raise or turn off the limit on the PC, or wait for the next day (midnight local time)."
        if lim["max_loss_day"] is not None and used["loss_day"] >= lim["max_loss_day"] - 1e-9:
            return "global_limit", (f"The global daily max loss across agents is reached (${used['loss_day']:.2f} of "
                                    f"${lim['max_loss_day']:.2f}). {tail}")
        if lim["api_day"] is not None and used["api_day"] >= lim["api_day"] - 1e-9:
            return "global_limit", (f"The global daily API limit across agents is reached (${used['api_day']:.2f} of "
                                    f"${lim['api_day']:.2f}). {tail}")
        return None

    def _global_check(self, cost: float, now: int) -> GateResult:
        """KD3 before an agent entry: reached -> every agent pauses; this order passing it -> refused."""
        lim = self.global_limits()["max_loss_day"]
        if lim is None:
            return OK
        used = self.global_usage(now)["loss_day"]
        if used >= lim - 1e-9:
            reached = self._global_reached(now)
            self.pause_all(now, reached[1] if reached else "The global daily max loss is reached.", "global_limit")
            return GateResult(False, "global_limit", f"The global daily max loss across agents (${lim:.2f}) is "
                                                     "reached; every agent is paused. Nothing was sent.")
        if used + cost > lim + 1e-9:
            return GateResult(False, "over_global_limit", f"Over the global daily max loss across agents: this order "
                                                          f"could cost ${cost:.2f} and ${max(0.0, lim - used):.2f} of "
                                                          f"the ${lim:.2f} is left today; nothing was sent.")
        return OK

    # -- the order gate's step 2 for agent orders ----------------------------------------------------------
    def check(self, strategy: str, ticket: dict, now: int, exit_order: bool) -> GateResult:
        sid = agent_session()
        s = self._live.get(sid) if sid else None
        if s is None:
            return GateResult(False, "no_agent_session", "This agent session is not running; nothing was sent.")
        if sid in self._stopping:
            return GateResult(False, "session_stopped", "The agent session is stopping; nothing was sent.")
        if now >= s["ends_ts"]:
            self._end(sid, "time", now, "The session reached its end time.")
            return GateResult(False, "session_ended", "The agent session has ended; nothing was sent.")
        # An exit of a position on the session's pinned environment still goes after the venue's mode changed (a
        # credential blip that drops a service to its safe mode must not trap a production position; review fix plan
        # 2026-10-31 P0 #3): the mode change pauses the session for everything else, never for that exit.
        svc = self.services.get(strategy)
        pinned_exit = (exit_order and svc is not None and strategy in s["pinned"]
                       and ticket.get("env") == (getattr(svc, "modes", None) or {}).get(s["pinned"][strategy]))
        if s["state"] != "running" and not (pinned_exit and s.get("pause_code") == "mode_changed"):
            return GateResult(False, "session_paused", f"The agent session is paused: {s['pause_reason']} "
                                                       "Nothing was sent.")
        if strategy not in s["pinned"]:
            return GateResult(False, "venue_not_allowed", f"{self._label(strategy)} is not this agent's market; "
                                                          "nothing was sent.")
        svc = self.services[strategy]
        if svc.mode != s["pinned"][strategy] and not pinned_exit:
            why = (f"{self._label(strategy)} switched from {s['pinned'][strategy]} to {svc.mode}; this session cannot "
                   "continue on a different mode. End it and launch a new one.")
            self.pause(sid, now, why, "mode_changed")
            return GateResult(False, "mode_changed", why)
        limits = s["limits"]
        step = current_assist_step()
        if step is not None:
            if limits.get("level") != "assist" or exit_order:
                return GateResult(False, "not_assist", "Only an assist session's entry proposals take this path; "
                                                       "nothing was sent.")
            if step == "approve":
                if actor().startswith(AGENT_PREFIX):
                    return GateResult(False, "agent_cannot_approve", "Only you can approve a proposal; nothing was "
                                                                     "sent.")
                if not svc.present(now) and _remote_human.get() is None:
                    return GateResult(False, "not_present", "Nobody is at the screen: approving needs the app open, "
                                                            "visible and used in the last few minutes. Nothing was "
                                                            "sent.")
        elif limits.get("level") == "assist" and not exit_order:
            return GateResult(False, "assist_needs_you", "This session is at the Assist level: entries are proposed "
                                                         "to you, and only your Approve sends one. Nothing was sent.")
        if exit_order:
            return self._owns(ticket.get("bracket_id"), s)
        cost = float(ticket.get("max_cost") or 0.0)
        if cost > limits["per_trade"] + 1e-9:
            return GateResult(False, "over_per_trade", f"This order could cost ${cost:.2f}, over the session's "
                                                       f"${limits['per_trade']:.2f} per-trade cap; nothing was sent.")
        used = self.budget_used(sid)
        if used + cost > limits["budget"] + 1e-9:
            return GateResult(False, "over_budget", f"Over the session budget: this order could cost ${cost:.2f} and "
                                                    f"${max(0.0, limits['budget'] - used):.2f} of the "
                                                    f"${limits['budget']:.2f} budget is left; nothing was sent.")
        return self._global_check(cost, now)

    def _owns(self, bracket_id: str | None, s: dict) -> GateResult:
        rows = self.repo.select(BRACKETS, {"id": bracket_id}, limit=1) if bracket_id else []
        if not rows or rows[0].get("session_id") != s["id"] or bracket_id in (s["taken_over"] or []):
            return GateResult(False, "not_yours", "This position is not one this agent session manages; nothing was "
                                                  "sent.")
        return OK

    def takeover(self, strategy: str, bracket: dict, now: int) -> None:
        """A person changed or sold a position an agent session opened: it is theirs from now on (R11)."""
        s = self._live.get(bracket.get("session_id") or "")
        if s is None or bracket["id"] in (s["taken_over"] or []):
            return
        s["taken_over"] = [*(s["taken_over"] or []), bracket["id"]]
        s["updated_ts"] = now
        self._save(s, ("taken_over", "updated_ts"))
        self.journal("state", {"takeover": bracket["id"], "strategy": strategy,
                               "reason": "You took this position over; the agent no longer manages it."}, now,
                     session_id=s["id"])
        self._changed(s)

    # -- KD2: overlap warnings ----------------------------------------------------------------------------------
    def overlaps(self) -> list[dict]:
        """Opposite sides of one market window held (open) by two different live sessions: [{strategy, market,
        ticker, window_end, sides: {UP: [{session_id, name, bracket_id}], DOWN: [...]}}]. Shown, never blocked."""
        if len(self._live) < 2:
            return []
        rows = self.repo.select(BRACKETS, filters=[("session_id", "in", list(self._live)),
                                                   ("state", "in", tuple(OPEN_STATES))])
        groups: dict[tuple, dict] = {}
        for b in rows:
            if b.get("side") not in ("UP", "DOWN") or b["session_id"] not in self._live:
                continue
            g = groups.setdefault((b.get("strategy") or "", b.get("ticker") or ""),
                                  {"UP": [], "DOWN": [], "window_end": b.get("window_end")})
            g[b["side"]].append({"session_id": b["session_id"], "bracket_id": b["id"],
                                 "name": self.name_of(self._live[b["session_id"]])})
        out = []
        for (strategy, ticker), g in sorted(groups.items()):
            up_s = {x["session_id"] for x in g["UP"]}
            down_s = {x["session_id"] for x in g["DOWN"]}
            if up_s and down_s and len(up_s | down_s) > 1:
                market = next((k for k, st in VENUES.items() if st == strategy), None)
                out.append({"strategy": strategy, "market": market, "ticker": ticker, "window_end": g["window_end"],
                            "sides": {"UP": g["UP"], "DOWN": g["DOWN"]}})
        return out

    def check_overlaps(self, now: int) -> list[dict]:
        """Journal each NEW overlap once into every session in it (kind "overlap", with a sentence) and tell
        `overlap_fn`; returns the new ones."""
        new = []
        for w in self.overlaps():
            members = frozenset(x["session_id"] for side in ("UP", "DOWN") for x in w["sides"][side])
            mark = (w["strategy"], w["ticker"], members)
            if mark in self._overlaps_seen:
                continue
            self._overlaps_seen.add(mark)
            label = VENUE_LABELS.get(w["market"] or "", w["strategy"])
            who = " vs ".join(f"{', '.join(sorted({x['name'] for x in w['sides'][side]}))} {side.title()}"
                              for side in ("UP", "DOWN"))
            w = {**w, "names": sorted({x["name"] for side in ("UP", "DOWN") for x in w["sides"][side]}),
                 "text": f"Opposite sides of one {label} window: {who}. Both stay open; this is a warning, not a "
                         "block."}
            for sid in sorted(members):
                self.journal(OVERLAP, w, now, session_id=sid)
            new.append(w)
            if self.overlap_fn is not None:
                try:
                    self.overlap_fn(w)
                except Exception:
                    log.warning("agent overlap notify failed", exc_info=True)
        return new

    # -- journal and persistence ------------------------------------------------------------------------------
    def journal(self, kind: str, data: dict, now: int, *, session_id: str) -> int:
        """Write one journal row of this session (stamped with its agent); returns its id."""
        if not session_id:
            raise ValueError("a journal row needs its session id")
        row = self.row(session_id)
        return int(self.repo.insert(JOURNAL, [{"session_id": session_id, "agent_id": (row or {}).get("agent_id"),
                                               "ts": now, "kind": kind, "data": data}])[0])

    def wake_input(self, journal_id: int, session_id: str, wake_id: int, ts: int, message: str, sha256: str,
                   fingerprint: dict, sensitive: dict) -> None:
        """One wake's exact model input beside its journal row (agent_wake_inputs; the evals' replay set)."""
        self.repo.insert(WAKE_INPUTS, [{"journal_id": journal_id, "session_id": session_id, "wake_id": wake_id,
                                        "ts": ts, "message": message, "message_sha256": sha256,
                                        "fingerprint": fingerprint, "sensitive": sensitive}])

    def wake_inputs(self, session_id: str) -> list[dict]:
        """A session's stored wake inputs, oldest first."""
        return self.repo.select(WAKE_INPUTS, {"session_id": session_id}, order_by=["wake_id"])

    # -- the journal across runs (L0, U6b) ----------------------------------------------------------------------
    @staticmethod
    def _kinds(kinds: Any) -> list[str] | None:
        if kinds is None:
            return None
        bad = [k for k in kinds if k not in JOURNAL_KINDS]
        if bad:
            raise ValueError(f"unknown journal kind {bad[0]!r}; kinds are {', '.join(JOURNAL_KINDS)}")
        return list(kinds)

    def journal_page(self, *, agent_id: str | None = None, before_id: int | None = None, limit: int = 100,
                     kinds: Any = None) -> dict:
        """Journal rows newest first (one agent's across all its runs, or every agent's), `before_id` pages back,
        `kinds` filters (an unknown kind is a ValueError, never ignored): {rows, next_before_id (None at the end),
        runs (a summary of each run the page's rows belong to)}."""
        kinds = self._kinds(kinds)
        where = {"agent_id": agent_id} if agent_id is not None else {}
        filters: list = []
        if before_id is not None:
            filters.append(("id", "<", before_id))
        if kinds is not None:
            filters.append(("kind", "in", kinds))
        rows = self.repo.select(JOURNAL, where, filters=filters, order_by=["-id"], limit=limit + 1)
        more = len(rows) > limit
        rows = rows[:limit]
        sids = list(dict.fromkeys(r["session_id"] for r in rows))
        return {"rows": rows, "next_before_id": rows[-1]["id"] if more and rows else None,
                "runs": self.runs(session_ids=sids)}

    def runs(self, *, agent_id: str | None = None, session_ids: list[str] | None = None,
             agent_ids: list[str] | None = None) -> list[dict]:
        """Run summaries (an agent's, several agents' (`agent_ids`), or these sessions'), newest first: {session_id,
        agent_id, name, run (1 = the agent's first), state, started_ts, ended_ts, end_reason, net (closed trades),
        trades}. At most three queries however many agents or runs (the agent list used to ask per agent)."""
        if agent_id is not None:
            agent_ids = [agent_id]
        if agent_ids is not None:
            if not agent_ids:
                return []
            sessions = self.repo.select(SESSIONS, filters=[("agent_id", "in", list(agent_ids))])
            every = sessions  # every run of these agents: the run numbers need nothing more
        elif session_ids:
            sessions = self.repo.select(SESSIONS, filters=[("id", "in", session_ids)])
            aids = sorted({s["agent_id"] for s in sessions if s.get("agent_id")})
            every = self.repo.select(SESSIONS, filters=[("agent_id", "in", aids)]) if aids else []
        else:
            return []
        sessions = [self._live.get(s["id"], s) for s in sessions]
        by_agent: dict[str, list[dict]] = {}
        for s in every:
            if s.get("agent_id"):
                by_agent.setdefault(s["agent_id"], []).append(s)
        order: dict[str, int] = {}
        for runs in by_agent.values():  # the run number counts every run of that agent, oldest first
            for i, s in enumerate(sorted(runs, key=lambda x: (x["started_ts"], x["id"])), 1):
                order[s["id"]] = i
        net: dict[str, list] = {s["id"]: [0.0, 0] for s in sessions}
        for b in self.repo.select(BRACKETS, filters=[("session_id", "in", list(net))]) if net else []:
            if b.get("state") == "closed" and float(b.get("filled") or 0) > 0:
                e = net[b["session_id"]]
                e[0] += (float(b.get("proceeds") or 0.0) - float(b.get("entry_cost") or 0.0)
                         - float(b.get("tp_fees") or 0.0) - float(b.get("exit_fees") or 0.0))
                e[1] += 1
        out = [{"session_id": s["id"], "agent_id": s.get("agent_id"), "name": self.name_of(s),
                "run": order.get(s["id"]), "state": s["state"], "started_ts": s["started_ts"],
                "ended_ts": s.get("ended_ts"), "end_reason": s.get("end_reason"),
                "net": round(net[s["id"]][0], 4), "trades": net[s["id"]][1]} for s in sessions]
        return sorted(out, key=lambda r: (-r["started_ts"], r["session_id"]))

    def agent_list(self, now: int, *, archived: bool = False) -> list[dict]:
        """Every agent with its runs at a glance: {id, name, market, market_label, created_ts, archived,
        running_session_id, runs, last_run, net, trades}. Four queries for the whole list (agents, their sessions,
        those sessions' brackets), not three per agent."""
        agents = self.agents(archived=archived)
        runs_of: dict[str, list[dict]] = {}
        for r in self.runs(agent_ids=[a["id"] for a in agents]):  # newest first, kept per agent
            runs_of.setdefault(r["agent_id"], []).append(r)
        out = []
        for a in agents:
            runs = runs_of.get(a["id"], [])
            live = self.running_agent(a["id"])
            out.append({**a, "archived": bool(a["archived"]), "market_label": VENUE_LABELS.get(a["market"],
                                                                                              a["market"]),
                        "running_session_id": live["id"] if live else None, "runs": len(runs),
                        "last_run": runs[0] if runs else None, "net": round(sum(r["net"] for r in runs), 4),
                        "trades": sum(r["trades"] for r in runs)})
        return out

    def journal_rows(self, session_id: str, *, after_id: int = 0, limit: int = 200) -> list[dict]:
        rows = self.repo.select(JOURNAL, {"session_id": session_id}, filters=[("id", ">", after_id)],
                                order_by=["-id"], limit=limit)
        return list(reversed(rows))

    def _save(self, s: dict, keys: tuple[str, ...]) -> None:
        self.repo.update(SESSIONS, {"id": s["id"]}, {k: s[k] for k in keys})

    def _changed(self, s: dict) -> None:
        if self._notify is None:
            return
        try:
            self._notify(s)
        except Exception:
            log.warning("agent session notify failed", exc_info=True)

    @staticmethod
    def _label(strategy: str) -> str:
        for key, strat in VENUES.items():
            if strat == strategy:
                return VENUE_LABELS[key]
        return strategy
