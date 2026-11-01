"""Agent runner: the in-app agent's wake loop on the user's Anthropic key (Agent mode U6; R4, R13-R17; KTD6-KTD8).

One runner per in-app session. In the app the fleet (agent_fleet.AgentFleet, multi-agent KD4/KD5) drives every
session's runner from one tick loop: `step` each tick (everything below but the wake; the rules never wait for the
model), and `wake_due` -> the process-wide wake queue, which runs `wake` as its own task. `run(session_id)` is a
session's loop on its own (the tests): every `tick_s` it:
1. lets the session review itself (end time, kill switch, mode change) and returns once the session is no longer live;
   while paused it does nothing else;
2. diffs the session's own brackets (trading_brackets rows stamped with its id) and pushes fill / position_closed /
   settled / order_refused events; pushes session_ending once at `session_ending_ms` before the end;
3. feeds the event engine a snapshot of the session's markets (`PlayService.now_view`, its own positions only);
4. runs the session's armed entry rules (agent_rules; "rules open, code exits, the agent supervises") on those views,
   each rule on each of its markets that is among the session's venues: a match opens one entry through
   `AgentTools.call("place_entry", by_rule=...)` as the agent (request_id "rule:<id>:<market>:<window start>", an IoC
   limit at `agent_rules.entry_cap`: the matched ask + APPROVE_SLACK (2c), never above the rule's ask cap, the one
   cap rule an Assist proposal and a setup take too; a match without a usable ask sends nothing), stake and max_worst_case = the per-trade cap, the rule's invalidation BTC
   stop, if it has one, in the same ticket), journals a "rule" row (rule, market, inputs and outcome; a refusal's
   reason) and, on
   success, a "decision" row (ENTRY, p_source experimental, source "rule", its rule and market) and pushes an urgent
   `rule_entry` event so the agent wakes to supervise. Each (rule, market, window) is tried once (a failure is not
   retried in that window); at most one rule entry per market window; with the session's one_bet_per_window on (the
   default) at most one per settlement instant (window end) across venues: the first entry takes it and a later
   match elsewhere is journaled once as skipped (code one_bet_per_window); none while the session holds that market,
   with under the rule's min_left_ms left (3 minutes; HPANIC: the market's entry cutoff, checked by the rule), or
   while the session is paused or stopping, and none for a rule retired (rule_scores.RuleBook) since launch: it is
   skipped from the next tick (its open positions keep their code exits). At the session's "assist" level a match is
   never placed: it becomes a proposal (agent_proposals.AgentProposals.propose) that only the user can approve; a
   refused proposal is journaled as the rule's refused attempt, a shown one takes the market window's (and, with
   one_bet_per_window, the settlement instant's) one rule slot whatever the user answers; open proposals expire on
   every tick; an approved one comes back through `proposal_approved` (decision row, rule_entry wake) like an auto
   entry. While a wake runs, a side task keeps evaluating the rules every
   tick (the main loop waits on the wake); a flag keeps two evaluations from overlapping. In-app sessions only
   (launch refuses rules for an external session);
5. lets the scorekeeper retire a rule whose live tally crossed its bars (rule_scores.RuleBook.enforce: on each close
   of one of the session's positions, at attach and every `enforce_every_ms`): each change is journaled ("rule_status")
   and told to `notify_fn` ({kind: "rule_status"}: the panel's CUSTOM event);
6. wakes when the engine says so.

One wake (KTD6: stateless, a flat cost per wake), run by the `AgentHarness` port (the Claude Agent SDK adapter):
- system prompt = [system prompt, user prompt if any, the tool note] as one text; the tools (stable order) are the
  only ones offered; one user message: the memory block (the session envelope and facts as data, the last 5
  decisions, this session's open / settling trades and its last `trades_kept` closed ones with a summary of all,
  alarms, the agent's notes), the coalesced events, and an inline `observe` result;
- then up to `max_tool_turns` model turns inside the harness. Before the first turn its worst case (every estimated
  input token at the cache write rate plus `max_tokens` output) is reserved against the spend cap (KTD8); if it does
  not fit the session ends "spend_cap". Each finished turn is settled from its `usage` with our own price table (the
  cap's authority; the runtime's own estimate is only journalled). After each tool call the harness asks `_gate`
  before the next model request: the next turn's worst case is reserved, or the wake stops ("spend_cap"; or
  "session_stopped" when the session is no longer running). Tool calls run through `AgentTools.call` as the session;
  writes and session tools are shielded, so a Stop never cancels an order in flight. Urgent events that arrived during
  a tool call ride on its result as a `new_events` block;
- it stops on end_turn, refusal, max_tokens, the turn cap, or the gate; one "wake" journal row records the trigger,
  decision, thinking summary, tool calls and results, the text and tool calls in the order written (`segments`:
  {kind: "text", text} | {kind: "tool", index into tools}), refusal, tokens, estimated cost, the runtime's own report,
  duration. The agent must call record_decision once per wake; a wake that ends without a successful one gets an
  UNRECORDED "decision" row (written just before the wake row; no re-prompt). The memory block's last decisions are
  these rows.
- the wake's exact input is journaled beside its row (`agent_wake_inputs`, keyed by the wake row's journal id): the
  user message verbatim after `scrub_fn` (the app's secret scrubber), its sha256, and a `fingerprint` of everything
  else the model saw (system prompt and tool specs sha256, user layer versions, model, effort, max_tokens, max_turns,
  SDK / CLI version and git sha from `build_fn`, "unknown" when not known); the wake row carries the fingerprint and
  `input` {sha256, chars, scrubbed, stored}. `sensitive` names the parts an eval export must redact (the balances are
  kept: they are part of the input). A failure to store it is logged and never fails the wake.
Errors: model unavailable -> the session pauses; invalid key -> it ends "invalid_key"; a rejected request -> it pauses
with the message; an unsafe runtime (wrong credential source or tools) -> it pauses "agent runtime misconfigured"; any
other error -> the wake row's status is "error", the traceback is logged and the session pauses (code "other"). A
cancelled or failed turn keeps its reservation as spent (the panel labels spend "estimated").

`stop()` first marks the session stopping (the order gate and the runner refuse every new write from then on), then
cancels the loop (and so the current model call), waits for every order in flight to finish and be recorded (the
agent's write, and a rule entry wherever it runs: the fleet's step, the side task; each is its own tracked task, so a
cancelled caller never loses its "rule" row; at most `stop_drain_s`), re-reads the positions, then ends the session.
A wake re-checks the session when it starts (a queued wake that starts after a pause or an end does nothing and keeps
its events) and again before the model call; a write dispatched while paused is refused. The account diff after a
tool call or a rule entry re-baselines only the bracket it touched: every other position's news is still pushed. A
turn's spend reservation is held in the sessions' fleet-wide ledger (`AgentSessions.hold_api`), so concurrent wakes
check the global daily API limit against each other's reservations.
`start_fn(row)` is told as each wake starts (its trigger); `notify_fn(row)` about each wake row


written; `live_fn(session_id, wake_id, kind, text)` gets the model's thinking / text / tool_use deltas as they stream.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from btc_signal_engine.application.agent_rules import RULES, entry_cap
from btc_signal_engine.application.agent_rules import evaluate as rule_match
from btc_signal_engine.application.agent_rules import note as rule_note
from btc_signal_engine.application.agent_proposals import AgentProposals
from btc_signal_engine.application.agent_events import (DECISION_POINTS, POSITION_EVENTS, AgentEvents, EventsConfig,
                                                        SignalBoard, Snapshot)
from btc_signal_engine.application.agent_sessions import (BRACKETS, OPEN_STATES, SETTLING, VENUES, AgentSessions,
                                                          market_of_strategy)
from btc_signal_engine.application.agent_tools import DECISION_KEYS, AgentTools, unrecorded_decision
from btc_signal_engine.application.play import PlayService
from btc_signal_engine.application.ports import (AgentHarness, Clock, LlmBadRequest, LlmInvalidKey,
                                                 LlmMisconfigured, LlmUnavailable, TurnUsage, WakeResult)
from btc_signal_engine.domain import brackets
from btc_signal_engine.domain.markets import market as market_spec

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Price:
    """Dollars per million tokens."""

    input: float
    output: float
    cache_read: float
    cache_write: float  # 5-minute TTL: 1.25x input


# Checked against the claude-api reference (shared/models.md, shared/prompt-caching.md) on 2026-10-30.
PRICES: dict[str, Price] = {
    "claude-opus-5-5": Price(4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": Price(2.00, 10.00, 0.20, 2.50),
    "claude-haiku-5-5": Price(0.10, 0.50, 0.01, 0.125),
}
# Haiku 5.5 bills a prompt over 100K tokens at a higher rate for the whole request.
LONG_PROMPT_TOKENS = 100_000
LONG_PRICES: dict[str, Price] = {"claude-haiku-5-5": Price(0.50, 2.50, 0.05, 0.625)}
CHARS_PER_TOKEN = 2.5  # conservative: JSON-heavy prompts and Haiku 5.5's denser tokenizer

FACTS = (
    "Kalshi's 15-minute BTC prices are well calibrated: the market price is already a good probability, and the app's "
    "own model adds no information beyond it.",
    "Most simple entry and exit rules (momentum, stop-losses, take-profits, forced entries) lose about the cost of "
    "trading.",
    "A win rate means nothing without the price paid: compare it with the break-even rate the entry price implies.",
)
TOOL_NOTE = (
    "How this app talks to you. Each wake you get one message with <memory> (your session's limits and usage, your "
    "last decisions, your trades, alarms and notes, as data), <events> (what woke you) and <observe> (the observe "
    "tool's result now). Prices are dollars per contract (0-1); amounts are dollars. Every write tool needs a short "
    "`note` (your reason) and a `request_id` unique to that action; repeating a request_id repeats nothing. A tool "
    "result may be followed by <new_events>: urgent things that happened while you were working. You are not woken on "
    "every tick. While you hold nothing in a market you are woken at its decision points (window_ready: the new "
    "window has a live quote and a strike; entry_check: about 3 minutes before entries close), when the app's call "
    "changes (WAIT / ENTER UP / ENTER DOWN), on a large move, by your own alarms, and for urgent news; while you hold "
    "a position there, by its fills and closes, Play's exit advice changing, BTC near the strike or the stop, the "
    "take profit near, its last 90 seconds and price moves. <memory>.wake says which of these woke you and "
    "<memory>.markets gives each market's seconds to entry cutoff and to close and its next decision point, so you "
    "need not snooze to reach the next window. Use set_alarms or snooze to steer your wakes and update_notes to "
    "remember across wakes. You must call record_decision exactly once at the end of every wake, after any orders and "
    "update_notes: it is what the user sees and what your record is scored on; use null for anything you do not know. "
    "Then end the wake with a short plain-text summary. "
    "Entry rules: <memory>.session.rules lists the rules armed for this session (empty = none). When any is armed, "
    "entries are opened only by the app when a rule matches, never by you: place_entry is refused (rules_own_entries) "
    "(a rule with status 'retired' opens nothing more; its open positions keep their code exits). "
    "At <memory>.session.level 'assist' the app does not place a rule's match: it proposes the ticket to the user, "
    "who approves or skips it; only the user can approve (you never can) and an entry you send yourself is refused "
    "(assist_needs_you). At either level the rule's entry, once sent, "
    "arrives as a rule_entry event, already protected by code: a BTC stop at the rule's "
    "invalidation level when the rule has one (rules described 'no stop', e.g. HPANIC and LATERESID, have none) "
    "and NO take-profit order: it rides to settlement and is paid by the market's official result "
    "($1 a contract if it wins, $0 if it loses). Your job on rule entries is to supervise and journal: default hold "
    "to settlement; the code stop can only be tightened (loosening or removing it, or adding a take profit, is "
    "refused: rule_stop_locked). A soft exit (place_exit) needs a stated "
    "thesis-invalidation reason that the code stop does not already cover, in its note and in record_decision "
    "(action EXIT with that reason, or HOLD with why you pass). "
    "Settlement: a position still holding when its window closes leaves <observe> and shows in <memory>.trades with "
    "state 'settling': the window is closed and the venue's official result is pending; it cannot be sold or changed, "
    "and until the result is booked it counts at its worst case (the held contracts paying $0) in "
    "budget_used.pending_settlement. A 'settled' event then reports it with held, proceeds and pnl."
)
DECISION_MEMORY_TEXT = 200  # chars of thesis / invalidation / next_check kept per decision in the memory block
DECISION_MEMORY_KEYS = ("ts", "wake_id", "market", "window_end", "mid_up", "action", "side", "p_estimate", "p_low",
                        "p_high", "p_source", "market_p", "edge_cents", "pass_reason", "thesis", "invalidation",
                        "next_check", "note")


def price(model: str, prompt_tokens: int = 0) -> Price:
    """The model's rates; an unknown model is an error."""
    if model not in PRICES:
        raise ValueError(f"unknown model {model!r}; known: {', '.join(PRICES)}")
    if prompt_tokens > LONG_PROMPT_TOKENS and model in LONG_PRICES:
        return LONG_PRICES[model]
    return PRICES[model]


def cost_usd(model: str, usage: dict) -> float:
    """Estimated dollars for one turn's usage."""
    inp, cw, cr = (int(usage.get(k) or 0) for k in
                   ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    p = price(model, inp + cw + cr)
    return (inp * p.input + cw * p.cache_write + cr * p.cache_read + int(usage.get("output_tokens") or 0) * p.output) / 1e6


def worst_case_usd(model: str, input_tokens: int, max_tokens: int) -> float:
    """A turn's most possible cost: every input token written to cache, and `max_tokens` of output."""
    p = price(model, input_tokens)
    return (input_tokens * p.cache_write + max_tokens * p.output) / 1e6


def estimate_tokens(*parts: Any) -> int:
    return math.ceil(sum(len(json.dumps(x, separators=(",", ":"), default=str)) for x in parts) / CHARS_PER_TOKEN)


@dataclass(frozen=True)
class RunnerConfig:
    tick_s: float = 1.0
    max_tool_turns: int = 6
    max_tokens: int = 16_000
    effort: str = "medium"
    session_ending_ms: int = 300_000
    decisions_kept: int = 5
    result_chars: int = 1_000  # tool results in the journal row, compacted
    enforce_every_ms: int = 60_000  # the scorekeeper's auto-retire check, besides each close
    trades_kept: int = 10  # closed trades in the memory block (every open / settling one is always there)
    stop_drain_s: float = 30.0  # how long a Stop waits for the orders in flight before it ends the session anyway


def _dumps(x: Any) -> str:
    return json.dumps(x, separators=(",", ":"), default=str)


def _compact(d: dict) -> dict:
    """One decision row for the memory block: the known fields only, long text cut."""
    out = {}
    for k in DECISION_MEMORY_KEYS:
        v = d.get(k)
        if v is None:
            continue
        out[k] = v[:DECISION_MEMORY_TEXT] + "…" if isinstance(v, str) and len(v) > DECISION_MEMORY_TEXT else v
    return out


UNKNOWN = "unknown"
# What an eval export must redact from a stored wake input (docs/research/2026-10-31-eval-data-inventory.md, "Sensitive
# data"): account balances are part of the model's input, so they are stored, and marked here.
WAKE_INPUT_SENSITIVE = {"drop": ["observe.balances"],
                        "redact_numbers_after_words": ["cash", "balance", "balances", "equity"],
                        "free_text": ["memory.notes", "memory.recent_decisions", "events"],
                        "hash_ids": ["memory.trades[].id", "observe.markets.*.positions"]}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _specs_sha256(specs: list[dict]) -> str:
    return _sha256(json.dumps(specs, sort_keys=True, separators=(",", ":"), default=str))


def _woke_for(events: list[dict]) -> dict:
    """What woke the agent, by kind: scheduled decision points, its positions, or anything else (notes, alarms,
    flips, moves, session news)."""
    seen: dict[str, list[str]] = {"decision_points": [], "position_events": [], "other": []}
    for e in events:
        kind = ("decision_points" if e["type"] in DECISION_POINTS else
                "position_events" if e["type"] in POSITION_EVENTS else "other")
        if e["type"] not in seen[kind]:
            seen[kind].append(e["type"])
    return seen


class AgentRunner:
    def __init__(self, sessions: AgentSessions, tools: AgentTools, play: PlayService, harness: AgentHarness, *,
                 prompts_fn: Callable[[], tuple[str, str, dict]], clock: Clock,
                 events_config: EventsConfig | None = None, config: RunnerConfig | None = None,
                 notify_fn: Callable[[dict], None] | None = None,
                 live_fn: Callable[[str, int, str, str], None] | None = None,
                 start_fn: Callable[[dict], None] | None = None,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                 proposals: AgentProposals | None = None, signal_board: SignalBoard | None = None,
                 scrub_fn: Callable[[str], str] | None = None, build_fn: Callable[[], dict] | None = None) -> None:
        """`prompts_fn()` -> (system prompt, user prompt or "", their versions). `notify_fn(row)` is told about each
        wake journal row; `live_fn(session_id, wake_id, kind, text)` about each streamed delta; `start_fn(row)` about
        each wake as it starts ({session_id, ts, kind: "wake_start", data: {wake_id, reason, trigger}}).
        `scrub_fn(text)` removes secrets from the stored wake input (None: stored as sent); `build_fn()` ->
        {git_sha, sdk, cli_version} for the wake's fingerprint (a missing part is "unknown")."""
        self.sessions, self.tools, self.play, self.harness = sessions, tools, play, harness
        self._prompts = prompts_fn
        self.clock = clock
        self.events_config = events_config or EventsConfig()
        self.config = config or RunnerConfig()
        self._notify = notify_fn
        self._live = live_fn
        self._start = start_fn
        self._sleep = sleep
        self._scrub = scrub_fn
        self._build = build_fn
        self._wake_input: dict | None = None  # the running wake's stored message, written by `_finish`
        self.events: AgentEvents | None = None  # the attached session's event engine (AgentTools' events_fn)
        self.session_id: str | None = None
        self._task: asyncio.Task | None = None
        self._wake_task: asyncio.Task | None = None  # the wake running now (the fleet starts it as its own task)
        self._seen: dict[str, tuple] = {}  # bracket id -> (state, filled, exit_reason)
        self._rule_brackets: set[str] = set()  # brackets this session's rules opened (no take profit; code stop)
        self._ending_sent = False
        self._inflight: asyncio.Task | None = None
        self._inflight_entry: dict | None = None  # the wake row's entry for the write in flight
        self._orders: set[asyncio.Task] = set()  # every order task in flight (agent writes, rule entries)
        self._held = 0.0  # the current turn's spend reservation (see `_reserved`)
        self._text_open = False  # the wake row's last segment is text still streaming
        self._turn_text = False  # this turn streamed text (else its text is taken from the turn's report)
        self._rule_tried: set[tuple[str, str, int]] = set()  # (rule id, market, window start) already tried
        self._rule_entered: set[tuple[str, int]] = set()  # (market, window start) with a rule entry
        self._rule_settle: dict[int, dict] = {}  # settlement instant (window end) -> {rule, market} that took it
        self._rules_busy = False
        self._waking = False
        self._closed_seen = False  # a session position closed since the last auto-retire check
        self._enforced_at: int | None = None
        # The fleet's one read of every live session's brackets this tick (`prime`): (tick ms, this session's rows),
        # used by this tick's scan, holds check and wake budget instead of a query each; None = read it here.
        self._primed: tuple[int, list[dict]] | None = None
        self.proposals = proposals  # assist sessions' proposals (None: assist cannot run in this app)
        self.signal_board = signal_board  # KD6: the fleet's shared per-market call / light detection (None: own)
        if proposals is not None:
            proposals.on_approved(self.proposal_approved)

    def now(self) -> int:
        return self.clock.now_ms()

    @property
    def _reserved(self) -> float:
        """The current turn's spend reservation, until its usage is settled. It is also held in the sessions'
        fleet-wide API ledger (AgentSessions.hold_api), so other sessions' wakes count it against the global daily
        API limit before they reserve their own."""
        return self._held

    @_reserved.setter
    def _reserved(self, usd: float) -> None:
        self._held = usd
        if self.session_id is not None:
            self.sessions.hold_api(self.session_id, usd)

    def _track(self, aw: Awaitable[Any]) -> asyncio.Task:
        """An order task in flight: a Stop waits for it (`stop`), never cancels it."""
        task = asyncio.ensure_future(aw)
        self._orders.add(task)
        task.add_done_callback(self._orders.discard)
        return task

    def _running(self) -> bool:
        """The session is live, running (not paused) and no Stop was asked for."""
        s = self._live_session()
        return s is not None and s["state"] == "running" and not self.sessions.stopping(self.session_id)


    # -- the loop -----------------------------------------------------------------------------------------------------
    def attach(self, session_id: str) -> bool:
        """Bind to the running in-app session `session_id` (idempotent); False when it is not the live session."""
        s = self.sessions.get(session_id)
        if s is None:
            return False
        if self.session_id != session_id:
            price(s["model"])  # an unknown model is an error before any call
            if s["limits"]["level"] == "assist" and self.proposals is None:
                raise ValueError("an assist session needs the runner's proposals service")
            allowed = {(market_spec(k).venue, market_spec(k).span) for k in s["limits"]["venues"]}
            self.session_id, self.events = session_id, AgentEvents(self.events_config, markets=allowed,
                                                                 board=self.signal_board)
            self._seen, self._ending_sent = {}, False
            self._rule_tried, self._rule_entered, self._rule_brackets = set(), set(), set()
            self._rule_settle = {}
            self._scan(self.now(), push=False)
            self._enforce(self.now())
        return True

    async def run(self, session_id: str) -> None:
        """The session's loop; returns when the session is no longer live."""
        if not self.attach(session_id):
            return
        self._task = asyncio.current_task()
        side = asyncio.ensure_future(self._rules_during_wakes())
        try:
            while await self.tick():
                await self._sleep(self.config.tick_s)
        finally:
            side.cancel()
            self._task = None

    async def _rules_during_wakes(self) -> None:
        """The main loop waits on a wake; meanwhile the armed rules still run every tick."""
        while True:
            await self._sleep(self.config.tick_s)
            if self._waking:
                try:
                    await self.rules_tick(self.now())
                except Exception:
                    log.exception("agent rules failed during a wake")

    def _live_session(self) -> dict | None:
        return self.sessions.get(self.session_id)

    def prime(self, now: int, rows: list[dict]) -> None:
        """The fleet's one read of every live session's brackets at tick `now` (KD6 for the account: one query per
        tick for the whole fleet, not three or more per session). Valid for that tick until this session sends an
        order (`_unprime`)."""
        self._primed = (now, rows)

    def _unprime(self) -> None:
        self._primed = None

    def _brackets(self, now: int | None = None) -> list[dict]:
        """This session's brackets: the fleet's primed rows of tick `now`, else a read."""
        p = self._primed
        if now is not None and p is not None and p[0] == now:
            return p[1]
        return self.sessions.repo.select(BRACKETS, {"session_id": self.session_id})

    async def tick(self) -> bool:
        """One pass of a session run on its own (`run`, the tests): review, `step`, then the wake when one is due,
        awaited here. False once the session is over. The fleet (agent_fleet.AgentFleet) instead calls `step` for
        every session each tick and queues the wakes (KD4/KD5)."""
        now = self.now()
        self.sessions.review(now)
        if not await self.step(now):
            return False
        if self.wake_due(now) is not None:
            await self.wake(now)
        return self._live_session() is not None

    async def step(self, now: int, views: dict[str, dict] | None = None, *, expire: bool = True) -> bool:
        """Everything of one tick but the model wake (KD4: rules never wait for the model). `views` (market key ->
        `PlayService.now_view`) are the fleet's shared reads of this tick (KD6: one read per market for every
        session); missing ones are read here. `expire`: let open proposals expire (the fleet does it once per tick
        for every session). While a wake runs only the rules path runs (the wake owns the account diff, as before).
        False once the session is over."""
        if expire and self.proposals is not None:
            self.proposals.expire(now)
        s = self._live_session()
        if s is None:
            if self.events is not None:
                self.events.end_session()
            return False
        if s["state"] != "running":
            return True
        views = dict(views or {})
        if self._waking:
            try:
                await self.rules_tick(now, views)
            except Exception:
                log.exception("agent rules failed during a wake")
            return True
        ev = self.events
        ids = self._scan(now, push=True, primed=True)
        for key in s["limits"]["venues"]:
            if key not in views:
                m = market_spec(key)
                views[key] = self.play.now_view(now, m.venue, span=m.span)
        ev.observe(now, Snapshot.from_views([views[k] for k in s["limits"]["venues"]], ids,
                                            rule_ids=self._rule_brackets))
        if self._closed_seen or self._enforced_at is None or now - self._enforced_at >= self.config.enforce_every_ms:
            self._enforce(now)
        try:
            await self.rules_tick(now, views)
        except Exception:  # a rule failure never stops the session's loop
            log.exception("agent rules failed")
        left = s["ends_ts"] - now
        if not self._ending_sent and left <= self.config.session_ending_ms:
            self._ending_sent = True
            ev.push(now, "session_ending", value={"seconds_left": max(0, left // 1000)})
        return self._live_session() is not None

    @property
    def waking(self) -> bool:
        return self._waking

    def wake_due(self, now: int) -> str | None:
        """"urgent" / "normal" when the event engine says a wake is due now (its drop rules applied), else None."""
        s = self._live_session()
        if s is None or s["state"] != "running" or self._waking or self.events is None:
            return None
        p = self._primed
        used = (self.sessions.budget_used_of(p[1]) if p is not None and p[0] == now
                else self.sessions.budget_used(self.session_id))
        budget_left = float(s["limits"]["budget"]) - used
        if not self.events.should_wake(now, budget_exhausted=budget_left <= 0):
            return None
        return "urgent" if self.events.has_urgent() else "normal"

    def has_urgent(self) -> bool:
        """An urgent event waits (the fleet moves a queued normal wake ahead)."""
        return self.events is not None and self.events.has_urgent()

    def detach(self) -> None:
        """The session is over: unhook from the proposals service (many sessions over a day stay bounded)."""
        if self.proposals is not None:
            self.proposals.off_approved(self.proposal_approved)

    def _scan(self, now: int, *, push: bool, quiet: set | frozenset = frozenset(), primed: bool = False) -> set:
        """Diff the session's brackets; push account events when `push`, except for the brackets in `quiet` (the
        one an action just touched: its own change is not news to the agent; it is re-baselined, every other
        position's fill, stop-out or settlement is still pushed). Returns the ids the agent manages. `primed`: the
        tick's step may use the fleet's rows of this tick; every other scan (after an action, in a wake) reads."""
        taken = set((self._live_session() or {}).get("taken_over") or [])
        mine = set()
        for b in self._brackets(now if primed else None):
            bid, filled = b["id"], float(b.get("filled") or 0)
            state = (b["state"], filled, b.get("exit_reason"))
            prev = self._seen.get(bid)
            self._seen[bid] = state
            if bid not in taken:
                mine.add(bid)
            if not push or prev == state or bid in quiet:
                continue
            key = market_of_strategy(b.get("strategy"))
            where = {"venue": market_spec(key).venue, "span": market_spec(key).span} if key else {}
            info = {"id": bid, "side": b.get("side"), "filled": filled, "state": b["state"],
                    "exit_reason": b.get("exit_reason")}
            if b["state"] not in OPEN_STATES and b["state"] != SETTLING and filled > 0:  # closed: its result
                info.update(held=b.get("held"), proceeds=b.get("proceeds"),
                            pnl=brackets.net(b))
            if filled > (prev[1] if prev else 0.0):
                self.events.push(now, "fill", **where, value={"brackets": [info]})
            if b["state"] == SETTLING:  # window closed, result pending: the "settled" event comes with the result
                continue
            if b["state"] not in OPEN_STATES and (prev is None or prev[0] in OPEN_STATES or prev[0] == SETTLING):
                why = b.get("exit_reason")
                kind = "settled" if why == "settled" else "order_refused" if why == "rejected" else "position_closed"
                self._closed_seen = True
                self.events.push(now, kind, **where, value={"brackets": [info]})
        return mine

    # -- entry rules --------------------------------------------------------------------------------------------------
    def _enforce(self, now: int) -> list[dict]:
        """The scorekeeper's auto-retirement: each rule retired now is journaled and told to the panel. A failure is
        logged, never stops the loop. The rule book is shared by the fleet and runs the tally at most once per tick
        (`now`) for a close and once per `enforce_every_ms` otherwise, whichever session asks first; a close it
        skipped is asked again next tick."""
        closed = self._closed_seen
        self._enforced_at = now
        try:
            changes = self.sessions.rule_book.enforce(now, closed=closed, every_ms=self.config.enforce_every_ms)
        except Exception:
            log.exception("rule auto-retire check failed")
            self._closed_seen = False
            return []
        if changes is None:  # another session ran it this tick (or this minute): a close waits for the next tick
            return []
        self._closed_seen = False

        for ch in changes:
            self.sessions.journal("rule_status", ch, now, session_id=self.session_id)
            if self._notify is not None:
                try:
                    self._notify({"session_id": self.session_id, "ts": now, "kind": "rule_status", "data": ch})
                except Exception:
                    log.warning("agent rule status notify failed", exc_info=True)
        return changes

    async def rules_tick(self, now: int, views: dict[str, dict] | None = None) -> list[dict]:
        """Evaluate the session's armed rules on its markets' views (`views` by market key; read when missing) and
        open each match. Returns the "rule" rows written this pass."""
        s = self._live_session()
        if s is None or s["state"] != "running" or self.sessions.stopping(self.session_id) or self._rules_busy:
            return []
        lim = s.get("limits") or {}
        armed = lim.get("rules") or []
        if not armed:
            return []
        one_bet = lim.get("one_bet_per_window", True)  # a session stored before the option had it on
        venues = lim.get("venues") or []
        self._rules_busy = True
        out = []
        views = dict(views or {})
        try:
            book = self.sessions.rule_book
            for rid in armed:
                if book.retired(rid):  # retired since launch: no new entries from this tick on
                    continue
                rule = RULES[rid]
                for market in (k for k in rule.markets if k in venues):
                    v = views.get(market)
                    if v is None:
                        m = market_spec(market)
                        v = views[market] = self.play.now_view(now, m.venue, span=m.span)
                    w = v.get("window") or {}
                    start, end = w.get("start"), w.get("end")
                    if start is None or end is None or (rid, market, start) in self._rule_tried:
                        continue
                    if (market, start) in self._rule_entered:
                        continue
                    if rule.min_left_ms is not None and end - now < rule.min_left_ms:
                        continue
                    if self._holds(market, now):
                        continue
                    match = rule_match(rid, market, v)
                    if match is None:
                        continue
                    self._rule_tried.add((rid, market, start))
                    self._unprime()  # an entry or proposal may follow: later reads this tick see its bracket
                    taken = self._rule_settle.get(int(end)) if one_bet else None
                    if taken is not None:
                        out.append(self._rule_skip(now, rid, market, match, taken))
                        continue
                    if lim["level"] == "assist":
                        out.append(await self._rule_propose(now, s, rid, market, match))
                    else:
                        out.append(await self._rule_entry(now, s, rid, market, match))
        finally:
            self._rules_busy = False
        return out

    @staticmethod
    def _rule_inputs(m: dict) -> dict:
        return {k: m[k] for k in ("market", "window_start", "window_end", "ticker", "elapsed_s", "left_s", "btc",
                                  "strike", "gap", "gap_pct", "side", "ask", "ask_cap", "stop_index_below",
                                  "stop_index_above", "fair_up", "fee", "residual", "edge") if k in m}

    def _rule_skip(self, now: int, rid: str, market: str, m: dict, taken: dict) -> dict:
        """A match on a settlement instant another rule entry already took (one bet per window across venues)."""
        row = {"rule": rid, "market": market, "ok": False, "code": "one_bet_per_window",
               "reason": "skipped: one bet per window across venues", "repeat": False, "bracket_id": None,
               "inputs": self._rule_inputs(m), "request_id": None, "note": rule_note(m), "taken_by": dict(taken)}
        self.sessions.journal("rule", row, now, session_id=self.session_id)
        self._notify_rule(now, row)
        return row

    def _notify_rule(self, now: int, row: dict) -> None:
        if self._notify is not None:
            try:
                self._notify({"session_id": self.session_id, "ts": now, "kind": "rule", "data": row})
            except Exception:
                log.warning("agent rule notify failed", exc_info=True)

    def _holds(self, market: str, now: int | None = None) -> bool:
        """The session has a position (open or still sending) in this market (the tick's primed rows when fresh)."""
        strategy = VENUES[market]
        rows = self._brackets(None if self._waking else now)  # a wake's own orders land mid-tick: read then
        return any(b.get("strategy") == strategy and b["state"] in OPEN_STATES for b in rows)

    async def _rule_entry(self, now: int, s: dict, rid: str, market: str, m: dict) -> dict:
        """One rule entry, start to journal, as its own tracked task: the caller (the fleet's step, the side task
        during a wake, the loop) being cancelled by a Stop never cancels the order nor loses its "rule" row; the
        Stop waits for it (`stop`)."""
        return await asyncio.shield(self._track(self._rule_entry_run(now, s, rid, market, m)))

    async def _rule_entry_run(self, now: int, s: dict, rid: str, market: str, m: dict) -> dict:
        sid, rule = self.session_id, RULES[rid]
        per_trade = float(s["limits"]["per_trade"])
        stop = brackets.stop_of(m)
        text = rule_note(m)
        # the one cap rule (agent_rules.entry_cap): the matched ask + 2c, never above the rule's cap, as Assist
        cap = entry_cap(rule, m.get("ask"))
        args = {"market": market, "side": m["side"], "stake": per_trade, "order_type": "limit",
                "max_price": cap, "max_worst_case": per_trade,
                "btc_stop": stop, "note": text[:500], "request_id": f"rule:{rid}:{market}:{m['window_start']}"}
        if cap is None:  # no usable ask: nothing is sent (never the rule's own cap as a fallback)
            res = {"ok": False, "code": "no_price", "reason": "The rule matched without a usable ask; nothing was "
                                                              "sent."}
        else:
            try:
                res = await self.tools.call("place_entry", args, now, session_id=sid, by_rule=rid)
            except Exception as e:  # recorded as the rule's refusal; not retried this window
                log.exception("rule entry failed")
                res = {"ok": False, "code": "error", "reason": f"{type(e).__name__}: {e}"}
        bracket = res.get("bracket") if isinstance(res.get("bracket"), dict) else {}
        inputs = self._rule_inputs(m)
        row = {"rule": rid, "market": market, "ok": bool(res.get("ok")), "code": res.get("code"),
               "reason": res.get("reason"), "repeat": bool(res.get("repeat")), "bracket_id": bracket.get("id"),
               "inputs": inputs, "request_id": args["request_id"], "note": text}
        self.sessions.journal("rule", row, now, session_id=sid)
        if res.get("ok"):
            self._take_slot(rid, market, m)
            self._entered(now, rid, market, m, bracket.get("id"), text, inputs, source="rule")
        self._notify_rule(now, row)
        return row

    def _take_slot(self, rid: str, market: str, m: dict) -> None:
        """This rule entry (or assist proposal) takes its market window, and its settlement instant."""
        self._rule_entered.add((market, m["window_start"]))
        self._rule_settle.setdefault(int(m["window_end"]), {"rule": rid, "market": market})

    def _entered(self, now: int, rid: str, market: str, m: dict, bracket_id: str | None, text: str, inputs: dict,
                 *, source: str) -> None:
        """A rule's entry was sent: its decision row and the urgent rule_entry event (the agent supervises)."""
        if bracket_id:
            self._rule_brackets.add(bracket_id)  # rides to settlement: the event engine knows it is a rule's
        # the rule's own fill is told by rule_entry, not as a fill; every other position's news still is
        self._scan(self.now(), push=True, quiet={bracket_id} if bracket_id else frozenset())

        stop = brackets.stop_of(m)
        spec = market_spec(market)
        if stop is None:
            invalidation = "None: the rule has no stop (no stop); it rides to settlement."
        else:
            stop_word = f"BTC {'falls to' if m['side'] == 'UP' else 'rises to'} ${stop:,.2f}"
            invalidation = f"{stop_word} (the code stop sells at the bid); else hold to settlement."
        decision = {**{k: None for k in DECISION_KEYS}, "market": market, "action": "ENTRY",
                    "side": m["side"], "p_source": "experimental", "thesis": text[:600],
                    "invalidation": invalidation,
                    "ts": now, "wake_id": None, "source": source, "rule": rid,
                    "window_start": m["window_start"], "window_end": m["window_end"],
                    "mid_up": None, "note": None, "wake_status": None, "bracket_id": bracket_id}
        self.sessions.journal("decision", decision, now, session_id=self.session_id)
        self.events.push(now, "rule_entry", venue=spec.venue, span=spec.span,
                         value={"rule": rid, "market": market, "bracket_id": bracket_id, "side": m["side"],
                                "ask": m.get("ask"), "btc_stop": stop, "inputs": inputs})

    async def _rule_propose(self, now: int, s: dict, rid: str, market: str, m: dict) -> dict:
        """Assist: the match becomes a proposal for the user; nothing is sent here. A refusal is the rule's refused
        attempt (journaled like an auto one); a shown proposal takes the window's rule slot."""
        try:
            res = await self.proposals.propose(now, s, rid, market, m)
        except Exception as e:
            log.exception("rule proposal failed")
            res = {"ok": False, "code": "error", "reason": f"{type(e).__name__}: {e}"}
        p = res.get("proposal") or {}
        row = {"rule": rid, "market": market, "ok": False, "code": res.get("code"), "reason": res.get("reason"),
               "repeat": bool(res.get("repeat")), "bracket_id": None, "inputs": self._rule_inputs(m),
               "request_id": f"rule:{rid}:{market}:{m['window_start']}", "note": rule_note(m), "source": "assist",
               "proposal_id": p.get("id")}
        if res.get("ok"):
            self._take_slot(rid, market, m)
            row.update(code="proposed", reason="Proposed to you; nothing is sent unless you approve.")
        self.sessions.journal("rule", row, now, session_id=self.session_id)
        self._notify_rule(now, row)
        return row

    def proposal_approved(self, p: dict, bracket: dict, now: int) -> None:
        """AgentProposals' hook: the user approved this session's proposal and its entry was sent."""
        if p.get("session_id") != self.session_id or self.events is None:
            return
        self._unprime()
        m = {**p["inputs"], "side": p["side"], "window_start": p["window_start"], "window_end": p["window_end"]}
        self._entered(now, p["rule"], p["market"], m, bracket.get("id"), p.get("note") or "", p["inputs"],
                      source="assist")

    # -- one wake -----------------------------------------------------------------------------------------------------
    async def wake(self, now: int) -> dict:
        """Run one wake now (the events engine said so). Returns the journal row's data. A wake queued while the
        session was running may start after it was paused, stopped or ended: it then does nothing ({status:
        "not_running"}, no journal row) and its events stay pending for the next wake."""
        ev = self.events
        if ev is None or not self._running():
            return {"status": "not_running", "wake_id": None}
        woke = ev.begin_wake(now)
        self._waking = True
        self._wake_task = asyncio.current_task()
        try:
            return await self._wake(now, woke)
        finally:
            self._waking = False
            self._wake_task = None
            ev.end_wake(self.now())  # always: a failed wake must never leave the engine "running" (no more wakes)

    async def _wake(self, now: int, woke: dict) -> dict:
        ev, cfg = self.events, self.config
        sid = self.session_id
        s = self._live_session()
        model = s["model"]
        wake_id = int(self.sessions.note_usage(sid, now, wakes=1).get("wakes", 0))
        rec: dict = {"wake_id": wake_id, "status": "done", "reason": woke["reason"], "trigger": woke["events"],
                     "model": model, "decision": None, "thinking": [], "tools": [], "refusal": None,
                     "stop_reason": None, "turns": 0, "message_ids": [], "segments": [],
                     "tokens": {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}, "est_usd": 0.0,
                     "reserved_lost_usd": 0.0, "duration_ms": 0}
        self._reserved = 0.0
        self._text_open = self._turn_text = False
        self._wake_input = None
        if self._start is not None:
            try:
                self._start({"session_id": sid, "ts": now, "kind": "wake_start",
                             "data": {"wake_id": wake_id, "reason": woke["reason"], "trigger": woke["events"]}})
            except Exception:
                log.warning("agent wake start hook failed", exc_info=True)
        try:
            system_text, user_text, versions = self._prompts()
            rec["prompt_versions"] = versions
            system = "\n\n".join(x for x in (system_text, user_text, TOOL_NOTE) if x)
            specs = [{**t, "kind": self.tools.kind(t["name"])} for t in self.tools.tools()]
            message = await self._wake_message(now, woke["events"])
            self._note_input(rec, system, versions, specs, model, message)
            need = worst_case_usd(model, estimate_tokens(system, specs, message), cfg.max_tokens)
            spent = self._spent()
            cap = s["limits"]["spend_cap"]
            # KD3: None = no global daily API limit; what is left after every other session's turn reserved now
            global_left = self.sessions.global_api_left(now, exclude=sid)
            if not self._running():  # paused, stopped or ended while the wake message was read: no model call
                rec["status"] = "not_running"
            elif spent + need > cap + 1e-9:
                rec["status"] = "spend_cap"
                rec["spend"] = {"spent": round(spent, 6), "next_turn_worst_case": round(need, 6), "cap": cap}
            elif global_left is not None and need > global_left + 1e-9:
                rec["status"] = "global_limit"
                rec["spend"] = {"global_left": global_left, "next_turn_worst_case": round(need, 6)}
            else:
                self._reserved = need  # checked and held with no await in between: atomic across the fleet
                out = await self.harness.run_wake(
                    system, message, specs, lambda name, args: self._execute(rec, name, args), session_id=sid,
                    model=model, effort=cfg.effort, max_turns=cfg.max_tool_turns, max_tokens=cfg.max_tokens,
                    max_budget_usd=round(cap - spent, 6), on_delta=self._delta(rec, sid, wake_id),
                    on_turn=lambda t: self._settle(rec, model, t),
                    gate=lambda ctx, chars: self._gate(rec, model, cap, ctx, chars))
                # A completed wake reports every turn it ran (the harness reconciles with the runtime's own total),
                # so a reservation still held is for a turn that never started: released, not spent.
                self._reserved = 0.0
                self._apply(rec, out)
        except asyncio.CancelledError:
            rec["status"] = "cancelled"
            await self._drain(rec)
            self._lose(rec, self._reserved)
            self._finish(rec, now)
            raise
        except LlmInvalidKey as e:
            rec.update(status="invalid_key", error=str(e))
            self._lose(rec, self._reserved)
            self._finish(rec, now)
            self.sessions.stop(sid, self.now(), "invalid_key", f"The Anthropic API key is invalid: {e}")
            return rec
        except LlmMisconfigured as e:
            rec.update(status="misconfigured", error=str(e))
            self._lose(rec, self._reserved)
            self._finish(rec, now)
            self.sessions.pause(sid, self.now(), f"agent runtime misconfigured: {e}", "runtime_misconfigured")
            return rec
        except LlmUnavailable as e:
            rec.update(status="unavailable", error=str(e))
            self._lose(rec, self._reserved)
            self._finish(rec, now)
            self.sessions.pause(sid, self.now(), f"The model is unavailable: {e}", "model_unavailable")
            return rec
        except LlmBadRequest as e:
            rec.update(status="bad_request", error=str(e))
            self._lose(rec, self._reserved)
            self._finish(rec, now)
            self.sessions.pause(sid, self.now(), f"The model rejected the app's request: {e}", "bad_request")
            return rec
        except Exception as e:  # anything unmapped: journalled and paused, never a silently dead session
            log.exception("agent wake failed")
            why = f"{type(e).__name__}: {e}"
            rec.update(status="error", error=why)
            try:
                self._lose(rec, self._reserved)
                self._finish(rec, now)
            except Exception:
                log.exception("agent wake journal failed")
            self.sessions.pause(sid, self.now(), f"The agent wake failed: {why}", "other")
            return rec
        self._finish(rec, now)
        if rec["status"] == "spend_cap":
            self.sessions.stop(sid, self.now(), "spend_cap", "The session reached its API-spend cap.")
            ev.end_session()
        elif rec["status"] == "global_limit":
            self.sessions.pause_all(self.now(), "The global daily API limit across agents is reached: the next "
                                                "wake would not fit. Every agent is paused; raise or turn off the "
                                                "limit on the PC.", "global_limit")
        return rec

    def _note_input(self, rec: dict, system: str, versions: dict, specs: list[dict], model: str,
                    message: str) -> None:
        """The wake's exact input (stored by `_finish` beside the wake row) and its fingerprint (on the row)."""
        cfg = self.config
        rec["fingerprint"] = {"system_prompt_sha256": _sha256(system), "user_layer_versions": versions,
                              "tool_specs_sha256": _specs_sha256(specs), "model": model, "effort": cfg.effort,
                              "max_tokens": cfg.max_tokens, "max_turns": cfg.max_tool_turns,
                              "sdk": UNKNOWN, "cli_version": UNKNOWN, "git_sha": UNKNOWN}
        stored = message
        if self._scrub is not None:
            try:
                stored = self._scrub(message)
            except Exception:  # never store what could not be scrubbed
                log.warning("agent wake input scrub failed; the input is not stored", exc_info=True)
                rec["input"] = {"sha256": _sha256(message), "chars": len(message), "scrubbed": False,
                                "stored": False}
                return
        rec["input"] = {"sha256": _sha256(stored), "chars": len(stored), "scrubbed": stored != message,
                        "stored": True}
        self._wake_input = {"message": stored}

    def _fingerprint_build(self, rec: dict) -> None:
        """The SDK / CLI version and git sha, once the wake ran (the CLI reports its version on its first message)."""
        fp = rec.get("fingerprint")
        if fp is None:
            return
        info: dict = {}
        if self._build is not None:
            try:
                info = dict(self._build() or {})
            except Exception:
                log.warning("agent build info failed", exc_info=True)
        rt = rec.get("runtime") or {}
        fp["sdk"] = str(rt.get("runtime") or info.get("sdk") or UNKNOWN)
        fp["cli_version"] = str(rt.get("cli_version") or info.get("cli_version") or UNKNOWN)
        fp["git_sha"] = str(info.get("git_sha") or UNKNOWN)

    def _store_input(self, rec: dict, journal_id: int, ts: int) -> None:
        wi, self._wake_input = self._wake_input, None
        if wi is None or not (rec.get("input") or {}).get("stored"):
            return
        try:
            self.sessions.wake_input(journal_id, self.session_id, rec["wake_id"], ts, wi["message"],
                                     rec["input"]["sha256"], rec["fingerprint"], WAKE_INPUT_SENSITIVE)
        except Exception:
            log.warning("agent wake input journal failed", exc_info=True)

    def _spent(self) -> float:
        return float(((self._live_session() or {}).get("usage") or {}).get("api_usd") or 0.0)

    def _gate(self, rec: dict, model: str, cap: float, context_tokens: int, new_chars: int) -> str | None:
        """Between turns (after a tool call, before the next model request): stop when the session is no longer
        running (or a Stop was asked for), or when the next turn's worst case does not fit the spend cap (KTD8); else
        reserve it."""
        live = self._live_session()
        if live is None or live["state"] != "running" or self.sessions.stopping(self.session_id):
            return "session_stopped"
        need = worst_case_usd(model, context_tokens + math.ceil(new_chars / CHARS_PER_TOKEN), self.config.max_tokens)
        spent = self._spent()
        if spent + need > cap + 1e-9:
            rec["spend"] = {"spent": round(spent, 6), "next_turn_worst_case": round(need, 6), "cap": cap}
            return "spend_cap"
        global_left = self.sessions.global_api_left(self.now(), exclude=self.session_id)
        if global_left is not None and need > global_left + 1e-9:
            rec["spend"] = {"global_left": global_left, "next_turn_worst_case": round(need, 6)}
            return "global_limit"
        self._reserved = need
        return None

    def _apply(self, rec: dict, out: WakeResult) -> None:
        """The harness's result into the wake row (its turns were settled as they finished)."""
        rec["status"] = "refusal" if rec["stop_reason"] == "refusal" else out.status
        if rec["status"] == "refusal":
            rec["refusal"] = {"text": out.text or None}
        # a call the model made that never reached the registry (the runtime refused it, or the wake stopped first)
        pending = list(rec["tools"])
        for c in out.tool_calls:
            args = c.get("input") or {}
            hit = next((e for e in pending if e["name"] == c.get("name") and e["args"] == args), None)
            if hit is not None:
                pending.remove(hit)
                continue
            rec["tools"].append({"name": c.get("name"), "args": args, "ok": False, "code": "not_run",
                                 "result": "The runtime did not run this call (its input was refused, or the wake "
                                           "stopped first)."})
        rec["runtime"] = {**(out.detail or {}), "cost_usd": out.cost_usd, "usage": out.usage, "turns": out.turns}

    async def _execute(self, rec: dict, name: str, args: dict) -> tuple[str, bool]:
        """One model tool call through the registry, as the session. Writes and session tools are shielded (a Stop
        never cancels an order in flight). Urgent events that arrived meanwhile ride on the result as <new_events>."""
        sid = self.session_id
        self._unprime()  # the call may send an order: the rules' holds check reads again this tick
        self._scan(self.now(), push=True)  # account changes during the model's turn are news
        entry = {"name": name, "args": args, "ok": None, "code": None, "result": None}
        rec["tools"].append(entry)
        self._text_open = False
        self._tool_segments(rec)
        kind = self.tools.kind(name)
        writes = kind in ("write", "session")
        refused = None
        if writes and self.sessions.stopping(sid):
            # The runtime can still dispatch a call while a Stop unwinds the wake: nothing new starts after a Stop
            # (the order gate refuses it too); only a write already in flight finishes.
            refused = {"ok": False, "code": "session_stopped",
                       "reason": "The agent session is stopping; nothing was sent."}
        elif kind == "write" and not self._running():
            # paused (or ended) during the wake: no order, stop or take-profit change starts (the gate agrees)
            live = self._live_session()
            refused = ({"ok": False, "code": "session_paused",
                        "reason": f"The agent session is paused: {live.get('pause_reason') or ''} Nothing was sent."}
                       if live is not None else
                       {"ok": False, "code": "no_agent_session",
                        "reason": "The agent session has ended; nothing was sent."})
        if refused is not None:
            self._record(entry, refused)
            return _dumps(refused), True
        call = self.tools.call(name, args, self.now(), session_id=sid, wake_id=rec["wake_id"])
        try:
            if writes:
                self._inflight, self._inflight_entry = self._track(call), entry
                out = await asyncio.shield(self._inflight)
            else:
                out = await call
        except asyncio.CancelledError:
            raise  # the wake is being cancelled (a Stop): `_drain` records the write in flight
        except Exception as e:  # the tool's own failure: recorded and told to the model, never a dead wake
            log.exception("agent tool %s failed", name)
            out = {"ok": False, "code": "error", "reason": f"{type(e).__name__}: {e}"}
        finally:
            # cleared once its task is done (even when it raised); a cancelled wake leaves it for `_drain`
            if self._inflight_entry is entry and (self._inflight is None or self._inflight.done()):
                self._inflight = self._inflight_entry = None
        self._record(entry, out)
        # the agent's own action is not news to it (the bracket it touched is re-baselined); everything else is
        self._scan(self.now(), push=True, quiet=self._touched(args, out))
        text = _dumps(out)
        urgent = self.events.take_urgent_during_wake()
        if urgent:
            text += "\n<new_events>" + _dumps(urgent) + "</new_events>"
            rec.setdefault("new_events", []).extend(urgent)
        return text, out.get("ok") is False

    @staticmethod
    def _touched(args: dict, out: dict) -> frozenset:
        """The bracket a tool call acted on (its bracket_id argument, or the one it opened)."""
        b = out.get("bracket") if isinstance(out, dict) and isinstance(out.get("bracket"), dict) else {}
        return frozenset(x for x in ((args or {}).get("bracket_id"), b.get("id")) if x)

    def _delta(self, rec: dict, sid: str, wake_id: int) -> Callable[[str, str], None]:

        """Each streamed delta: text and tool calls go into the wake row's `segments` in the order the model wrote
        them (so a run rebuilt from the journal reads as it did live), then to `live_fn`."""
        live = self._live

        def on_delta(kind: str, text: str) -> None:
            segs = rec["segments"]
            if kind == "text":
                self._turn_text = True
                if self._text_open:
                    segs[-1]["text"] += text
                else:
                    segs.append({"kind": "text", "text": text})
                    self._text_open = True
            elif kind == "tool_use":
                self._text_open = False
                segs.append({"kind": "tool", "index": sum(1 for x in segs if x["kind"] == "tool")})
            if live is not None:
                try:
                    live(sid, wake_id, kind, text)
                except Exception:
                    log.warning("agent live hook failed", exc_info=True)
        return on_delta

    @staticmethod
    def _tool_segments(rec: dict) -> None:
        """Every tool call in the row has its segment (one the stream never announced goes at the end)."""
        have = {x["index"] for x in rec["segments"] if x["kind"] == "tool"}
        rec["segments"] += [{"kind": "tool", "index": i} for i in range(len(rec["tools"])) if i not in have]

    async def _wake_message(self, now: int, events: list[dict]) -> str:
        sid = self.session_id
        obs = await self.tools.call("observe", {}, now, session_id=sid)
        return "\n\n".join(("<memory>" + _dumps(self._memory(now, events)) + "</memory>",
                             "<events>" + _dumps(events) + "</events>",
                             "<observe>" + _dumps(obs) + "</observe>"))

    def _memory(self, now: int, events: list[dict]) -> dict:
        sid, repo = self.session_id, self.sessions.repo
        v = self.sessions.view(sid, now)
        parts = self.sessions.budget_parts(sid)
        lim, usage = v["limits"], v["usage"] or {}
        decisions = self.tools.decisions(sid, limit=self.config.decisions_kept)
        trades, trades_summary = self._trades(repo.select(BRACKETS, {"session_id": sid}, order_by=["created_ts"]))
        return {"session": {"venues": lim["venues"], "budget": lim["budget"], "per_trade": lim["per_trade"],
                            "budget_used": {**parts, "used": v["budget_used"], "left": v["budget_left"]},
                            "seconds_left": v["seconds_left"], "spend_cap_usd": lim.get("spend_cap"),
                            "api_spend_usd_estimated": usage.get("api_usd", 0), "wakes": usage.get("wakes", 0),
                            "taken_over_by_user": v["taken_over"],
                            "rules": [{"id": r, "markets": [k for k in RULES[r].markets if k in lim["venues"]],
                                       "label": RULES[r].label, "description": RULES[r].description,
                                       "status": self.sessions.rule_book.status(r)["status"]}
                                      for r in lim.get("rules") or []],
                            "one_bet_per_window": lim.get("one_bet_per_window", True),
                            "level": lim["level"]},
                "wake": _woke_for(events),
                "markets": self._markets(now, lim["venues"]),
                "facts": list(FACTS),
                "recent_decisions": [_compact(d) for d in decisions],
                "trades": trades,
                "trades_summary": trades_summary,
                "alarms": self.events.alarms(), "snoozed": self.events.snoozed(),
                "notes": self.tools.notes(sid)}

    TRADE_KEYS = ("id", "strategy", "ticker", "side", "state", "filled", "held", "entry_avg", "max_cost",
                  "take_profit", "no_tp", "stop_index_below", "stop_index_above", "exit_reason", "exit_avg",
                  "proceeds", "created_ts", "window_end")

    def _trades(self, rows: list[dict]) -> tuple[list[dict], dict]:
        """The memory block's trades (oldest first): every open or settling position and the last `trades_kept`
        closed ones; and one summary line over every closed one of the session (the block stays bounded however
        long the session runs)."""
        closed = [b for b in rows if b["state"] not in OPEN_STATES and b["state"] != SETTLING]
        keep = {id(b) for b in closed[-self.config.trades_kept:]} if self.config.trades_kept > 0 else set()
        shown = [{k: b.get(k) for k in self.TRADE_KEYS} for b in rows
                 if b["state"] in OPEN_STATES or b["state"] == SETTLING or id(b) in keep]
        filled = [b for b in closed if float(b.get("filled") or 0) > 0]
        nets = [brackets.net(b) for b in filled]
        return shown, {"closed": len(closed), "closed_shown": len(keep), "filled": len(filled),
                       "won": sum(1 for x in nets if x > 0), "lost": sum(1 for x in nets if x <= 0),
                       "net": round(sum(nets), 4)}

    def _markets(self, now: int, keys: list[str]) -> list[dict]:
        """Each allowed market's clock and next decision point, from the engine's last snapshot."""
        sched = self.events.schedule(now)
        out = []
        for key in keys:
            m = market_spec(key)
            s = sched.get((m.venue, m.span))
            if s is not None:
                out.append({"market": key, **s})
        return out

    def _record(self, entry: dict, out: dict) -> None:
        text = _dumps(out)
        entry.update(ok=bool(out.get("ok")), code=out.get("code"),
                     result=out if len(text) <= self.config.result_chars else text[:self.config.result_chars] + "…")

    async def _drain(self, rec: dict) -> None:
        """A Stop arrived: let a write in flight finish and record it (never cancelled)."""
        task, entry = self._inflight, self._inflight_entry
        self._inflight = self._inflight_entry = None
        if task is None:
            return
        try:
            out = await asyncio.shield(task)
        except asyncio.CancelledError:
            out = await task if task.done() else {"ok": None, "code": "still_running"}
        except Exception as e:  # the tool's own failure, recorded as such
            out = {"ok": False, "code": "error", "reason": str(e)}
        if entry is not None and entry["result"] is None:
            self._record(entry, out)

    def _settle(self, rec: dict, model: str, t: TurnUsage) -> None:
        """A finished turn: its real cost (our price table, the cap's authority) replaces its reservation."""
        u = t.usage
        usd = cost_usd(model, u)
        tok = rec["tokens"]
        tok["input"] += int(u.get("input_tokens") or 0)
        tok["output"] += int(u.get("output_tokens") or 0)
        tok["cache_write"] += int(u.get("cache_creation_input_tokens") or 0)
        tok["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
        rec["est_usd"] = round(rec["est_usd"] + usd, 6)
        if not t.unstreamed:
            self._reserved = 0.0
            rec["turns"] += 1
            rec["stop_reason"] = t.stop_reason
            if t.message_id:
                rec["message_ids"].append(t.message_id)
            if t.thinking.strip():
                rec["thinking"].append(t.thinking.strip())
            if t.text.strip():
                rec["decision"] = t.text.strip()
                if not self._turn_text:  # the turn's text never streamed: kept from its report
                    rec["segments"].append({"kind": "text", "text": t.text.strip()})
            self._text_open = self._turn_text = False
        self.sessions.note_usage(self.session_id, self.now(), api_usd=usd, turns=0 if t.unstreamed else 1,
                                 input_tokens=int(u.get("input_tokens") or 0),
                                 output_tokens=int(u.get("output_tokens") or 0),
                                 cache_write_tokens=int(u.get("cache_creation_input_tokens") or 0),
                                 cache_read_tokens=int(u.get("cache_read_input_tokens") or 0))

    def _lose(self, rec: dict, reserved: float) -> None:
        """A cancelled or failed turn keeps its reservation as spent (KTD8)."""
        self._reserved = 0.0
        if reserved > 0:
            rec["reserved_lost_usd"] = round(rec["reserved_lost_usd"] + reserved, 6)
            rec["est_usd"] = round(rec["est_usd"] + reserved, 6)
            self.sessions.note_usage(self.session_id, self.now(), api_usd=reserved)

    def _finish(self, rec: dict, started: int) -> None:
        end = self.now()
        rec["duration_ms"] = end - started
        self._tool_segments(rec)
        sid = self.session_id
        if rec["status"] != "not_running" and not any(t["name"] == "record_decision" and t["ok"]
                                                      for t in rec["tools"]):
            # the smallest guard: no re-prompt, but the UI never shows a silent gap
            self.sessions.journal("decision", unrecorded_decision(end, rec["wake_id"], rec["status"]), end,
                                  session_id=sid)
        self._fingerprint_build(rec)
        jid = self.sessions.journal("wake", rec, end, session_id=sid)
        self._store_input(rec, jid, end)
        self.events.end_wake(end)
        if self._notify is not None:
            try:
                self._notify({"session_id": sid, "ts": end, "kind": "wake", "data": rec})
            except Exception:
                log.warning("agent journal notify failed", exc_info=True)

    # -- stop ---------------------------------------------------------------------------------------------------------
    async def stop(self, now: int, reason: str = "stopped", text: str = "Stopped by you.") -> dict:
        """Refuse every new agent write from now on, cancel the loop (and its model call), let every order in flight
        finish and be recorded (the agent's write and any rule entry, whoever runs it: the fleet's step, the side
        task during a wake; waited at most `stop_drain_s`), re-read the positions, then end the session."""
        self.sessions.begin_stop(self.session_id)
        me = asyncio.current_task()
        tasks = list(dict.fromkeys(t for t in (self._wake_task, self._task)
                                   if t is not None and not t.done() and t is not me))
        for task in tasks:  # the loop (a session run alone) and the wake (queued by the fleet), each once
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.warning("agent loop ended with an error while stopping", exc_info=True)
        await self._drain_orders()
        if self.session_id is not None:
            try:
                self._scan(self.now(), push=False)  # the positions the session ends with, fills included
            except Exception:
                log.warning("agent bracket scan failed while stopping", exc_info=True)
        if self.events is not None:
            self.events.end_session()
        if self._live_session() is not None:
            return self.sessions.stop(self.session_id, now, reason, text)
        return {"ok": False, "code": "no_session", "reason": "There is no agent session to stop."}

    async def _drain_orders(self) -> None:
        """Wait for every order task in flight (each journals its own result), at most `stop_drain_s` in all; one
        still running then is logged and keeps running (it is never cancelled; its rows land when it finishes)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.stop_drain_s
        while self._orders:
            left = deadline - loop.time()
            if left <= 0:
                log.warning("agent session %s: %d order(s) still in flight after %.0f s; ending the session anyway",
                            (self.session_id or "")[:8], len(self._orders), self.config.stop_drain_s)
                return
            await asyncio.wait(set(self._orders), timeout=left)

