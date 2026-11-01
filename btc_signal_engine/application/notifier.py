"""The Telegram remote (U11; docs/plans/2026-10-30-1402-feat-agent-mode-mcp-plan.md), application side: pairing, the
alert preferences, the alerts, the commands and the proposal buttons. One bot, one user. The transport is the port
`Messenger` (adapters/telegram/ implements it with python-telegram-bot, long polling); nothing here imports it.

Who: only the paired user in the paired private chat. Everything else (another user, a group, a wrong chat) is
ignored and logged, never answered. Pairing: the PC asks for a one-time code (`new_code`: 8 characters, 10 minutes, a
new one replaces the old, 5 wrong tries kill it); the user sends `/start <code>` in a private chat with the bot, which
stores that user id and chat id. Re-pairing (a new code) and unpairing are PC-only (the routes).

Stale: a message sent more than STALE_MS (60 s) before it is handled is dropped (a backlog after an outage or a
restart never runs late commands). Taps carry their own expiry instead.

Alerts (per kind on/off, ALERT_KINDS; defaults: proposals, trades and a session ending with positions open on, the
rest off):
- proposals: each new Assist proposal with Approve / Skip buttons (callback data "a|s:<proposal id>:<nonce>", one
  nonce per proposal message, single use). Approve calls AgentProposals.approve, the card's path (prepare -> OrderGate
  -> approve, the session's limits, the price re-check, the TTL), inside `remote_human("telegram:<user id>")`: the
  Telegram user is the human actor and stands in for "a person at the screen" for that one approval only. The message
  is edited in place with the outcome; a tap after expiry, on an answered proposal, or a second tap answers "Expired
  or already handled." and sends nothing.
- trades: the agent's fills, exits and settlements (trading brackets stamped with a session id, diffed every pump;
  the first pump only takes a baseline).
- activity (2026-10-30, default on): a running commentary of an agent session, so a fully autonomous session can be
  followed from Telegram alone. It replaces the trades alert's lines with its own (one message per event, never
  two, whichever of the two are on):
  * session started: level, markets, max loss, per trade, end time, rules armed;
  * entry: "🔵 Bought Up $1 at 86¢ · CUSHION100", details "BTC +$118 vs strike · 6:40 left · stop BTC $76,560"
    (a rule's entry: the "rule" journal row's inputs; the agent's own: its place_entry note). A fill whose rule /
    tool row is not journaled yet waits up to FILL_WAIT_MS for it;
  * material agent moves only: an EXIT / REDUCE / HEDGE decision (its thesis), a BTC stop set / tightened / moved /
    removed, a take profit moved (the tool row's note), a rule match that was not sent (why). Never HOLD, NO_TRADE,
    snoozes, alarms or wakes;
  * heads-ups, once per position each: BTC has covered 75% of the way from where it was at entry to the stop
    (NEAR_STOP_SHARE of that distance left), and the last LAST_MS of the window with the position winning or
    losing at BTC now (`view_fn`);
  * a close: "🟢 +$0.14 · Settled Up · CUSHION100", details "Session +$0.31 · 3 trades · 2 won" (stopped out,
    take profit, sold alike);
  * a pause; the hourly digest (from the session's start, every hour; skipped when nothing above happened in that
    hour); the session end summary (net, trades / wins, per rule, API, positions handed back; it stands in for the
    session_end and pauses alerts' end line).
  Rate limit: activity lines wait in an outbox sent at most once every ACTIVITY_GAP_MS as ONE message (a burst is
  coalesced). A close and the end summary go at once, carrying whatever waits in the outbox; proposals and command
  replies never wait.
- pauses: a session paused or ended (not when the bot's own /pause or /stop did it: that command already answered;
  only the sessions that command acts on are skipped, another agent's pause or end meanwhile is still told).

- session_end: a session ended with positions still open.
- settle_warnings: a position settling long after its close (the trading services' settle warnings), once each.
- setups (opt-in): a setup light turning green while no agent session is live (Manual), once per rule, market and
  window ("CUSHION100 ready on Kalshi 15 min").
- data (default on; docs/plans/2026-10-31-0830-ops-portable-runtime-plan.md P4): a research recorder went silent
  (application/recorder_health.py: quotes 2 minutes, settlement 10 minutes) and, once it writes again, that it is
  back; a missing research database is said once per app run. Checked every DATA_EVERY_MS. The daily summary adds
  yesterday's coverage line (5m windows recorded of 288, longest gap, settled); with data alerts on, a day without
  trades still gets that one line ("🌙 No trades today").
- daily (default on): once a day at `daily_at` (local "HH:MM", default 22:00, stored with the alert choices), the
  day's totals over every agent session and your own Play trades (closed today, local day): net, trades / wins, by
  rule (and agent's own / yours), API spend, best and worst trade, sessions run. Nothing when nothing closed that
  day. The day it was sent is stored, so a restart never sends it twice.

Multi-agent (docs/plans/2026-10-31-0010-feat-multi-agent-plan.md KD8; supersedes the single-session wording below):
one bot for every agent. Every alert names its agent (its session name). /status lists the agents (numbered, one
line each, the total first); /stop with one agent stops it at once, with several asks which (a button per agent and
All, each with the Yes, stop / Cancel second tap); `/stop <n>`, `/stop <name>` and `/stop all` act at once;
/pause and /note take `<n>` (or all for /pause) when more than one runs (with one agent, /note's leading number is
its text; `/note #<n> text` names an agent always). The pinned status is the fleet line ("📌 3 agents · +$3.10 · 2
winning · 1 holding · 0 need you", one line per agent below, at most PIN_AGENTS), pinned while
any agent runs. The hourly digest is fleet-level (one line per agent inside the quote), skipped for a quiet hour. The
daily summary adds a by-agent line. Mute per agent (`/mute <n>`, `/unmute <n>`, `set_muted`; stored with the alert
choices): a muted agent's entries, closes, heads-ups and activity lines are not sent; its pauses, its end and its
proposals still are. Two agents holding opposite sides of one window send one warning (KD2).

Commands: /status (a running session: one block; none: today so far, the last session and the presets), /stop
(always, at once), /pause, /note <text>, /lights (each lit rule's line, by market), /launch (saved presets only: tap
one, then a Launch button valid 60 s; the launch takes the launch route's own path with its confirm supplied here,
and the preset's venues must still be in their saved modes). Refused with a plain sentence: resume, raising limits,
mode changes, prompt edits, reactivating a retired rule. BOT_COMMANDS is the menu the transport registers at start.

Message format (2026-10-30, Telegram best practice): every message is HTML (parse mode HTML, never MarkdownV2) and
every dynamic string (theses, notes, rule reasons, labels, preset names) goes through `esc` (html.escape). The first
line alone carries the news (it is the lock-screen preview): one status emoji, used sparingly (🔵 entry, 🟢 / 🔴 a
positive / negative close, ⚠️ heads-up / refused rule / needs you, ⏸ pause, ⏹ stop or session end, 📊 hourly digest,
🌙 daily summary, 🟣 Assist proposal, ▶️ session started, 📌 the pinned live status), the key numbers in <b>, the rest
plain. Details sit behind a tap in <blockquote expandable> (`Note` -> `compose`). No monospace tables. Telegram's
4096-character limit: `compose` drops detail lines (marked "…") before heads, and coalesced heads last.
Silent (disable_notification): the session started message, digests, the daily summary, heads-ups, stop / take-profit
moves, unfilled orders and one-bet-per-window skips. Entries, closes, pauses, the session end, refused rule matches,
agent exits, settle warnings, setups and proposals notify. A coalesced message is silent only when all of it is.
Pinned live status: a session's start sends a silent one-line status ("📌 Live · Auto · Kalshi 15 min · +$0.10 (1) ·
ends 06:35 · no position"), pins it silently and edits it in place at most once every PIN_EDIT_MS and only when its
text changes (at once after an entry or a close); the session's end edits it to the result and unpins it. Its id is
stored (LIVE_PIN), so a stale pin (a restart, a missed end) is closed and unpinned by the next pump or session. A
failed pin / unpin / edit is logged once and never affects the rest of the bot.
Menu buttons under the digest and the pinned status: Status / Lights (answer like the commands) and Stop (callback
data "v:..."; two taps: the first turns the row into Yes, stop / Cancel for STOP_CONFIRM_MS, Yes takes /stop's path;
a stale or expired tap answers EXPIRED and does nothing). Nothing is sent for a wake where nothing happened.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import secrets
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any, Protocol

from btc_signal_engine.application.actor import current_actor
from btc_signal_engine.application.agent_fleet_view import FleetView
from btc_signal_engine.application.agent_sessions import SESSIONS, VENUE_LABELS, VENUES, remote_human
from btc_signal_engine.application.ports import Repository
from btc_signal_engine.application import record_summary as rec
from btc_signal_engine.application.recorder_health import RecorderWatch, change_text, coverage_text
from btc_signal_engine.application.rule_scores import BRACKETS, JOURNAL, OPEN_STATES, SETTLING

log = logging.getLogger(__name__)

ALERT_KINDS = ("proposals", "trades", "activity", "pauses", "session_end", "settle_warnings", "setups", "data",
               "daily")
DEFAULT_ALERTS = {"proposals": True, "trades": True, "activity": True, "pauses": False, "session_end": True,
                  "settle_warnings": False, "setups": False, "data": True, "daily": True}
DAILY_AT = "daily_at"  # stored with the alert choices: the daily summary's local time "HH:MM"
DEFAULT_DAILY_AT = "22:00"
HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
PAIRING = {"strategy": "telegram", "key": "pairing"}
ALERTS = {"strategy": "telegram", "key": "alerts"}
DAILY_SENT = {"strategy": "telegram", "key": "daily_sent"}
LIVE_PIN = {"strategy": "telegram", "key": "live_pin"}  # the pinned fleet status {fleet: true, chat_id, message_id}
PIN_AGENTS = 5  # the pinned status lists this many agents, then "+ N more" (KD8)
MUTED = "muted"  # stored with the alert choices: session ids whose trade / activity alerts are muted
PIN_EDIT_MS = 60_000  # the pinned status is edited at most this often (at once after an entry or a close)
STOP_CONFIRM_MS = 10_000  # the menu's Stop: the Yes, stop button works this long
MAX_CHARS = 4096  # Telegram's message limit
REASON_CHARS = 300  # a reason / pause text in a message is cut to this
MINUS = "−"  # U+2212: negative amounts
ACTIVITY_GAP_MS = 20_000  # activity lines: at most one message this often (a burst is one message)
DIGEST_MS = 3_600_000
HEADS_EVERY_MS = 5_000
NEAR_STOP_SHARE = 0.25  # heads-up when BTC is this share of its entry distance from the stop
LAST_MS = 90_000
FILL_WAIT_MS = 5_000  # a fill waits this long for its rule / tool journal row
MATERIAL = {"EXIT": "exit", "REDUCE": "reduce", "HEDGE": "hedge"}  # the agent decisions worth a message
THESIS_CHARS = 120
DAY_MS = 86_400_000
BOT_COMMANDS = (("status", "Your agents and the total"), ("lights", "Setup lights now"),
                ("stop", "Stop an agent (or all)"), ("pause", "Pause an agent (or all)"),
                ("note", "Send an agent a note"), ("launch", "Start a saved preset"),
                ("mute", "Mute an agent's trade alerts"), ("unmute", "Unmute an agent"))
END_WORDS = {"time": "end time", "stopped": "stopped by you", "agent": "the agent ended it",
             "spend_cap": "API cap reached", "invalid_key": "invalid API key", "restart": "the app restarted"}
ENV_WORDS = {"prod": "real money", "shadow": "paper", "demo": "demo"}
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LEN = 8
CODE_TTL_MS = 10 * 60_000
CODE_TRIES = 5
STALE_MS = 60_000
CONFIRM_MS = 60_000  # a /launch confirm button
PICK_MS = 10 * 60_000  # a /launch preset list
SETTLE_EVERY_MS = 60_000
SETUPS_EVERY_MS = 5_000
DATA_EVERY_MS = 15_000  # the recorders' freshness is checked this often
WATCH_MS = 6 * 3_600_000  # agent brackets updated this recently are watched for trade alerts
NOTE_MAX = 1000
LAUNCH_CONFIRM = "START"  # the launch route's phrase; the confirm button stands for it
STRATEGY_LABEL = {strategy: VENUE_LABELS[k] for k, strategy in VENUES.items()}

HELP = "Commands: /status /stop /pause /note /lights /launch /mute /unmute."
EXPIRED = "Expired or already handled."
ON_PC = {
    "/resume": "Resuming stays on the PC.",
    "/limits": "Limits are changed on the PC.",
    "/raise": "Limits are changed on the PC.",
    "/budget": "Limits are changed on the PC.",
    "/mode": "Trading modes are changed on the PC.",
    "/prod": "Trading modes are changed on the PC.",
    "/prompt": "Prompts are edited on the PC.",
    "/prompts": "Prompts are edited on the PC.",
    "/reactivate": "Retired rules are reactivated on the PC.",
    "/extend": "Sessions are extended on the PC.",
}

Keyboard = Sequence[Sequence[tuple[str, str]]]  # rows of (label, callback data)


class Messenger(Protocol):
    """The chat transport (adapters/telegram/bot.py). Every text is HTML (parse mode HTML)."""

    async def send(self, chat_id: int, text: str, buttons: Keyboard | None = None, *,
                   silent: bool = False) -> int | None:
        """Send a message (`silent`: no notification sound); its id, or None when it could not be sent (logged by
        the transport)."""

    async def edit(self, chat_id: int, message_id: int, text: str, buttons: Keyboard | None = None) -> bool:
        """Edit a message's text and buttons (None removes them); False when it could not be edited."""

    async def edit_buttons(self, chat_id: int, message_id: int, buttons: Keyboard | None) -> bool:
        """Replace a message's buttons only (its text unchanged); False when it could not be done."""

    async def answer(self, callback_id: str, text: str | None = None) -> None:
        """Answer a button tap (the small notice on the phone; plain text)."""

    async def pin(self, chat_id: int, message_id: int) -> bool:
        """Pin a message silently; False when it could not be pinned."""

    async def unpin(self, chat_id: int, message_id: int) -> bool:
        """Unpin a message; False when it could not be unpinned."""


class AgentControls(Protocol):
    """The launch route's own paths (adapters/http/agent_routes.AgentActions)."""

    async def launch(self, spec: dict, *, from_pc: bool) -> dict: ...

    async def stop(self, session_id: str) -> dict: ...

    async def stop_all(self) -> dict: ...

    def pause(self, session_id: str, why: str) -> dict: ...

    def note(self, session_id: str, text: str) -> dict: ...

    def answered(self, out: dict) -> dict: ...


@dataclass(frozen=True)
class Inbound:
    """A text message: who sent it, in which chat, when (ms, Telegram's own time)."""
    user_id: int
    chat_id: int
    private: bool
    user_label: str | None
    text: str
    sent_ts: int


@dataclass(frozen=True)
class Tap:
    """A button tap."""
    user_id: int
    chat_id: int
    message_id: int | None
    callback_id: str
    data: str


@contextmanager
def as_person(label: str) -> Iterator[None]:
    """The enclosed calls' actor is the paired Telegram user (a note's "by", a launch's audit). Unlike
    `remote_human` it does NOT stand in for a person at the screen (that is for one proposal approval only)."""
    a = current_actor.set(label)
    try:
        yield
    finally:
        current_actor.reset(a)


def esc(x: Any) -> str:
    """A dynamic string made safe for an HTML message."""
    return html.escape(str(x), quote=False)


def bold(s: str) -> str:
    """`s` (already HTML) in bold."""
    return f"<b>{s}</b>"


def clip(text: Any, n: int = REASON_CHARS) -> str:
    """A free text on one line, cut to n characters (escape after)."""
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"


@dataclass
class Note:
    """One message's worth of news: a head line (HTML, the lock-screen preview) and detail lines (HTML) shown in an
    expandable quote. `menu`: the session id whose Status / Lights / Stop row goes under it."""
    head: str
    details: list[str] = field(default_factory=list)
    silent: bool = False
    menu: str | None = None


def _block(head: str, details: Sequence[str]) -> str:
    return head + ("\n<blockquote expandable>" + "\n".join(details) + "</blockquote>" if details else "")


CUT = "…"


def compose(notes: Sequence[Note], limit: int = MAX_CHARS) -> str:
    """The notes as one HTML message under Telegram's limit: detail lines go first (from the longest note, marked
    "…"), then the later notes' heads (the first note is the news), counted in an "… and N more" line."""
    units = [(n.head, list(n.details)) for n in notes]

    def text(extra: str = "") -> str:
        return "\n\n".join(_block(h, d) for h, d in units) + extra
    t = text()
    while len(t) > limit and any(d and d != [CUT] for _, d in units):
        i = max((k for k, (_, d) in enumerate(units) if d and d != [CUT]), key=lambda k: len(_block(*units[k])))
        d = units[i][1]
        if d[-1] == CUT:
            d.pop()
        d.pop()
        d.append(CUT)
        t = text()
    dropped = 0
    while len(t) > limit and len(units) > 1:
        units.pop()
        dropped += 1
        t = text(f"\n\n… and {dropped} more")
    return t if len(t) <= limit else cut_html(t, limit)  # one head over the limit: never ours (heads are short)


_TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)[^<>]*>")
_ENTITY = re.compile(r"&(#\d+|#x[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]*);")


def cut_html(t: str, limit: int) -> str:
    """`t` (Telegram HTML) cut to at most `limit` characters on a text boundary: never inside a tag or an entity,
    with "…" and every tag still open closed again, so the message still parses."""
    if len(t) <= limit:
        return t
    out: list[str] = []
    stack: list[str] = []
    size, i = 0, 0

    def closing() -> int:
        return sum(len(n) + 3 for n in stack)
    while i < len(t):
        m = _TAG.match(t, i) if t[i] == "<" else _ENTITY.match(t, i) if t[i] == "&" else None
        piece = m.group(0) if m else t[i]
        is_tag = bool(m) and t[i] == "<"
        if is_tag and m.group(1):  # a closing tag: always fits (it was counted when its tag opened)
            if stack and stack[-1] == m.group(2).lower():
                stack.pop()
            out.append(piece)
            size += len(piece)
            i += len(piece)
            continue
        extra = len(m.group(2)) + 3 if is_tag else 0  # an opening tag also reserves its closing tag
        if size + len(piece) + extra + closing() + len(CUT) > limit:
            break
        out.append(piece)
        size += len(piece)
        if is_tag:
            stack.append(m.group(2).lower())
        i += len(piece)
    return "".join(out).rstrip() + CUT + "".join(f"</{n}>" for n in reversed(stack))


def usd(x: float) -> str:
    return (MINUS + "$" if x < 0 else "$") + f"{abs(x):,.2f}"


def signed(x: float) -> str:
    r = round(float(x), 2)
    return ("+" if r >= 0 else MINUS) + f"${abs(r):,.2f}"


def cents(p: float | None) -> str:
    return "?" if p is None else f"{round(p * 100):.0f}¢"


def side_word(side: str | None) -> str:
    return "Up" if side == "UP" else "Down" if side == "DOWN" else str(side)


_net4 = rec.net4  # a bracket's net after every fee (application/record_summary.py, shared with the Record tab)


def _net(b: dict) -> float:
    return round(_net4(b), 2)


def money(x: float) -> str:
    """Dollars, whole ones without cents: "$1", "$0.86", "-$0.40"."""
    r = round(abs(float(x)), 2)
    s = f"${r:,.0f}" if abs(r - round(r)) < 1e-9 else f"${r:,.2f}"
    return (MINUS if x < -0.004 else "") + s


def btc_usd(x: float) -> str:
    return f"${float(x):,.0f}"


def gap_usd(g: float) -> str:
    return ("+" if round(float(g)) >= 0 else MINUS) + f"${abs(float(g)):,.0f}"


def result_emoji(net: float) -> str:
    """🟢 a positive close, 🔴 a negative one, nothing for exactly $0.00."""
    r = round(float(net), 2)
    return "🟢 " if r > 0 else "🔴 " if r < 0 else ""


def mmss(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60}:{s % 60:02d}"


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def short(text: str | None, n: int = THESIS_CHARS) -> str:
    """A thesis or note as one short line: its first sentence, cut to n characters."""
    t = " ".join(str(text or "").split())
    m = re.match(r"(.+?[.!?])(\s|$)", t)
    t = m.group(1) if m else t
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"


def _plain_reason(reason: str | None) -> str:
    t = (reason or "refused").strip()
    t = re.sub(r"[;.]?\s*nothing was (sent|done)\.?$", "", t, flags=re.I).rstrip(" .;")
    return (t[:1].lower() + t[1:]) if t[:2] != t[:2].upper() else t


def _stop_of(b: dict) -> float | None:
    v = b.get("stop_index_below") if b.get("side") == "UP" else b.get("stop_index_above")
    return float(v) if v is not None else None


_closed_at = rec.closed_at


def _cost(b: dict) -> float:
    if b.get("entry_cost") is not None:
        return float(b["entry_cost"])
    return float(b.get("filled") or 0.0) * float(b.get("entry_avg") or 0.0)


def _market_of(strategy: str | None) -> str | None:
    return next((k for k, s in VENUES.items() if s == strategy), None)


def _record(n: int, wins: int, net: float) -> str:
    return f"{wins}/{n} {signed(net)}"


EXIT_WORDS = {"settled": "Settled", "tp": "Take profit", "stop": "Stopped out", "exit": "Sold", "manual": "Sold"}


class RemoteControl:
    def __init__(self, repo: Repository, *, now_fn: Callable[[], int], sessions: Any | None = None,
                 proposals: Any | None = None, presets: Any | None = None, actions: AgentControls | None = None,
                 lights_fn: Callable[[int], list[dict]] | None = None,
                 settle_fn: Callable[[int], list[dict]] | None = None, configured: bool = False,
                 code_fn: Callable[[], str] | None = None, nonce_fn: Callable[[], str] | None = None,
                 view_fn: Callable[[str, int], dict | None] | None = None, tz: tzinfo | None = None,
                 recorders: RecorderWatch | None = None, bot_username: str | None = None) -> None:
        """`sessions` AgentSessions, `proposals` AgentProposals, `presets` AgentPresets, `actions` the launch route's
        paths: None when agent mode is off (the commands say so). `lights_fn(now)` -> [{market, market_label, lights:
        [{rule, state, line, side, ask}]}] (Ask Settleview's lights per Play market). `settle_fn(now)` -> the settle
        warnings [{id, ticker, minutes, venue_label?}]. `view_fn(market key, now)` -> {btc, strike, window_end} of
        that market now, or None (the heads-ups and /status). `tz`: the zone of "local" times (HH:MM, the day);
        None = this PC's own zone. `configured`: a bot token is set. `recorders`: the research recorders' freshness
        (the data alert and the daily coverage line); None = not watched. `bot_username`: the bot's @username without
        the @ (BTCFLOW_TELEGRAM_BOT_USERNAME); None = not known yet: the transport fills it from getMe when it starts."""
        self.repo, self.now_fn = repo, now_fn
        self.sessions, self.proposals, self.presets, self.actions = sessions, proposals, presets, actions
        self.lights_fn, self.settle_fn, self.view_fn, self.tz = lights_fn, settle_fn, view_fn, tz
        self.recorders = recorders
        self._data_next = 0
        self.configured = configured
        self.bot_username = bot_username
        self.running = False  # set by the transport while it polls
        self.messenger: Messenger | None = None
        self._code_fn = code_fn or (lambda: "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN)))
        self._nonce_fn = nonce_fn or (lambda: secrets.token_urlsafe(8))
        self._code: dict | None = None  # {code, expires_ts, tries}
        self._events: deque[tuple[str, Any]] = deque(maxlen=500)  # alerts waiting for the next pump
        # the sessions the bot's own /stop or /pause is acting on now: their change is not alerted again (another
        # agent's pause or end during it still is)
        self._own: set[str] = set()
        self._cache: dict | None = None  # per-pump reads (session stats, mutes, fleet rows); None outside a pump
        self._props: dict[str, dict] = {}  # proposal id -> {nonce, message_id, line, done}
        self._taps: dict[str, dict] = {}  # nonce -> a /launch pick or confirm {kind, name, expires_ts, message_id}
        self._brackets: dict[str, tuple[bool, bool]] | None = None  # bracket id -> (filled, closed); None: no baseline
        self._settle_seen: set[str] | None = None
        self._settle_next = 0
        self._greens: set[tuple] | None = None
        self._setups_next = 0
        # activity
        self._jid: int | None = None  # the journal tail's last row id; None: no baseline yet
        self._outbox: list[Note] = []
        self._act_ts: int | None = None  # when the last activity message went
        self._announced: set[str] = set()  # sessions whose start was told
        self._hour: dict[str, int] = {}  # session id -> things that happened since its last digest
        self._digest_next: int | None = None  # when the next fleet digest is due (None: no agent running yet)
        self._heads: set[tuple[str, str]] = set()  # (bracket id, "stop" | "last") told
        self._dist0: dict[str, float] = {}  # bracket id -> BTC's distance to its stop at entry
        self._stops: dict[str, float | None] = {}  # bracket id -> its BTC stop at the last pump
        self._heads_next = 0
        self._daily_day: str | None = None
        # the pinned live status and the menu's two-step Stop
        self._pin: dict | None = None  # {session_id, chat_id, message_id} (LIVE_PIN)
        self._pin_loaded = False
        self._pin_text: str | None = None  # its text as last edited
        self._pin_ts: int | None = None  # when it was last edited
        self._pin_due = False  # an entry or a close: edit at once
        self._pin_warned: set[str] = set()  # pin / unpin / edit failures already logged
        self._confirms: dict[str, dict] = {}  # nonce -> a Stop confirm {session_id, chat_id, message_id, expires_ts}

    # -- pairing and preferences (the PC's routes) --------------------------------------------------------------------
    def _setting(self, key: dict) -> dict | None:
        rows = self.repo.select("strategy_settings", key, limit=1)
        v = rows[0]["value"] if rows else None
        return dict(v) if isinstance(v, dict) else None

    def _put(self, key: dict, value: dict, now: int) -> None:
        if not self.repo.update("strategy_settings", key, {"value": value, "updated_ts": now}):
            self.repo.insert("strategy_settings", [{**key, "value": value, "updated_ts": now}], ignore_conflicts=False)

    def paired(self) -> dict | None:
        p = self._setting(PAIRING)
        return p if p and p.get("user_id") is not None and p.get("chat_id") is not None else None

    def alerts(self) -> dict[str, bool]:
        stored = self._setting(ALERTS) or {}
        return {k: bool(stored[k]) if isinstance(stored.get(k), bool) else DEFAULT_ALERTS[k] for k in ALERT_KINDS}

    def daily_at(self) -> str:
        """The daily summary's local time "HH:MM" (DEFAULT_DAILY_AT until set)."""
        v = (self._setting(ALERTS) or {}).get(DAILY_AT)
        return v if isinstance(v, str) and HHMM.match(v) else DEFAULT_DAILY_AT

    def set_alerts(self, changes: Mapping[str, Any], now: int) -> dict:
        """Turn alerts on or off ({kind: bool}) and / or set the daily summary's time ({"daily_at": "HH:MM"})."""
        bad = [k for k in changes if k not in ALERT_KINDS and k != DAILY_AT]
        if bad:
            return {"ok": False, "code": "unknown_alert", "reason": f"Unknown alert {bad[0]!r}; alerts are "
                                                                     f"{', '.join(ALERT_KINDS)}."}
        wrong = [k for k, v in changes.items() if k != DAILY_AT and not isinstance(v, bool)]
        if wrong:
            return {"ok": False, "code": "invalid", "reason": f"{wrong[0]} must be true or false."}
        if DAILY_AT in changes and not (isinstance(changes[DAILY_AT], str) and HHMM.match(changes[DAILY_AT])):
            return {"ok": False, "code": "invalid", "reason": f"daily_at must be a time HH:MM, 00:00 to 23:59 "
                                                              f"(got {changes[DAILY_AT]!r})."}
        if not changes:
            return {"ok": False, "code": "invalid", "reason": "Name at least one alert to turn on or off."}
        flags = {k: v for k, v in changes.items() if k != DAILY_AT}
        at = changes.get(DAILY_AT, self.daily_at())
        value = {**self.alerts(), **flags}
        self._put(ALERTS, {**value, DAILY_AT: at}, now)
        if DAILY_AT in changes:
            self._daily_day = None  # a new time may still fall later today
        return {"ok": True, "alerts": value, DAILY_AT: at}

    def new_code(self, now: int) -> dict:
        """A one-time pairing code (replaces any pending one)."""
        self._code = {"code": self._code_fn(), "expires_ts": now + CODE_TTL_MS, "tries": 0}
        return {"ok": True, "code": self._code["code"], "expires_ts": self._code["expires_ts"]}

    def unpair(self, now: int) -> dict:
        was = self.paired()
        self._put(PAIRING, {}, now)
        self._code = None
        self._props.clear()
        self._taps.clear()
        self._confirms.clear()
        return {"ok": True, "was_paired": was is not None}

    def status(self) -> dict:
        p = self.paired()
        return {"ok": True, "configured": self.configured, "running": self.running, "paired": p is not None,
                "bot_username": self.bot_username if self.configured else None,
                "user_label": p.get("user_label") if p else None, "alerts": self.alerts(), DAILY_AT: self.daily_at(),
                MUTED: sorted(self.muted())}

    def _try_pair(self, m: Inbound, code: str, now: int) -> bool:
        c = self._code
        if c is None:
            return False
        if now >= c["expires_ts"]:
            self._code = None
            return False
        if not secrets.compare_digest(code.strip().upper().encode(), c["code"].encode()):
            c["tries"] += 1
            if c["tries"] >= CODE_TRIES:
                log.warning("telegram: pairing code withdrawn after %d wrong tries", CODE_TRIES)
                self._code = None
            return False
        self._code = None  # one time
        self._put(PAIRING, {"user_id": m.user_id, "chat_id": m.chat_id, "user_label": m.user_label,
                            "paired_ts": now}, now)
        self._props.clear()
        self._taps.clear()
        self._confirms.clear()
        return True

    def _authorized(self, user_id: int, chat_id: int) -> dict | None:
        p = self.paired()
        if p is None or p["user_id"] != user_id or p["chat_id"] != chat_id:
            return None
        return p

    async def _say(self, chat_id: int, text: str, buttons: Keyboard | None = None, *,
                   silent: bool = False) -> int | None:
        """Send an HTML message (cut to the limit)."""
        if self.messenger is None:
            return None
        if len(text) > MAX_CHARS:
            text = compose([Note(text)])
        try:
            return await self.messenger.send(chat_id, text, buttons, silent=silent)
        except Exception:
            log.warning("telegram: a message could not be sent", exc_info=True)
            return None

    async def _send_notes(self, chat_id: int, notes: Sequence[Note]) -> int | None:
        """Notes as one message: the ones that notify first (the preview line carries the news), silent only when all
        are; the first menu found goes under it."""
        notes = sorted(notes, key=lambda n: n.silent)  # stable: in order within each group
        menu = next((n.menu for n in notes if n.menu), None)
        return await self._say(chat_id, compose(notes), self.menu_buttons(menu) if menu else None,
                               silent=all(n.silent for n in notes))

    async def _edit(self, chat_id: int, message_id: int | None, text: str) -> None:
        if self.messenger is None or message_id is None:
            return
        try:
            ok = await self.messenger.edit(chat_id, message_id, text, None)
        except Exception:
            log.warning("telegram: a message could not be edited", exc_info=True)
            return
        if not ok:
            log.warning("telegram: a message could not be edited")

    async def _answer(self, callback_id: str, text: str | None = None) -> None:
        if self.messenger is None:
            return
        try:
            await self.messenger.answer(callback_id, text)
        except Exception:
            log.warning("telegram: a tap could not be answered", exc_info=True)

    # -- inbound: messages ------------------------------------------------------------------------------------------
    async def handle_message(self, m: Inbound) -> None:
        now = self.now_fn()
        if now - m.sent_ts > STALE_MS:
            log.info("telegram: dropped a message sent %d s ago (older than %d s)", (now - m.sent_ts) // 1000,
                     STALE_MS // 1000)
            return
        word, _, rest = (m.text or "").strip().partition(" ")
        cmd = word.split("@", 1)[0].lower()
        rest = rest.strip()
        if cmd == "/start" and rest and self._code is not None:
            if not m.private:
                log.info("telegram: ignored a pairing attempt outside a private chat (chat %s)", m.chat_id)
                return
            if self._try_pair(m, rest, now):
                log.info("telegram: paired with user %s", m.user_id)
                await self._say(m.chat_id, "Paired. Proposals and trades will arrive here.")
            else:
                log.info("telegram: ignored a wrong or expired pairing code from user %s", m.user_id)
            return
        if self._authorized(m.user_id, m.chat_id) is None:
            log.info("telegram: ignored a message from user %s in chat %s (not the paired user and chat)",
                     m.user_id, m.chat_id)
            return
        reply = await self._command(cmd, rest, m)
        if isinstance(reply, tuple):
            await self._say(m.chat_id, *reply)
        elif reply is not None:
            await self._say(m.chat_id, reply)

    async def _command(self, cmd: str, rest: str, m: Inbound) -> str | tuple | None:
        if cmd in ON_PC:
            return ON_PC[cmd]
        if cmd == "/stop":
            return await self._stop(rest)
        if cmd == "/status":
            return self.status_line(self.now_fn())
        if cmd == "/lights":
            return self.lights_line(self.now_fn())
        if self.actions is None and cmd in ("/pause", "/note", "/launch", "/mute", "/unmute"):
            return "Agent mode is off on this PC."
        if cmd == "/pause":
            targets, why = self._targets(rest, "pause")
            if targets is None:
                return why
            ids = {s["id"] for s in targets} - self._own
            self._own |= ids
            try:
                outs = [self.actions.pause(s["id"], "Paused by you from Telegram. Resume on the PC.")
                        for s in targets]
            finally:
                self._own -= ids
            bad = [o for o in outs if not o.get("ok")]
            if bad and len(outs) == 1:
                return esc(bad[0].get("reason") or "Not done.")
            names = ", ".join(esc(self.sessions.name_of(s)) for s, o in zip(targets, outs, strict=True) if o.get("ok"))
            return f"Paused {names}. Resume on the PC." if names else "Nothing was running to pause."
        if cmd == "/note":
            if not rest:
                return "Write the note after /note."
            first, _, text = rest.partition(" ")
            live = self._agents()
            # an agent number only with several agents ("/note 2 text") or written "#2"; with one agent a leading
            # number is the note's own text ("/note 5 minutes left: hold")
            explicit = first.startswith("#") and first[1:].isdigit()
            if text.strip() and (explicit or (first.isdigit() and len(live) > 1)):
                targets, why = self._targets(first.lstrip("#"), "note")
            elif len(live) == 1:
                targets, why, text = live, None, rest
            else:
                targets, why = None, self._which("note", "/note 1 your text")
            if targets is None:
                return why
            with as_person(self._label(m.user_id)):
                out = self.actions.note(targets[0]["id"], text.strip()[:NOTE_MAX])
            return (f"Noted for {esc(self.sessions.name_of(targets[0]))}." if out.get("ok")
                    else esc(out.get("reason") or "Not done."))
        if cmd in ("/mute", "/unmute"):
            targets, why = self._targets(rest, cmd[1:])
            if targets is None:
                return why
            for s in targets:
                self.set_muted(s["id"], cmd == "/mute", self.now_fn())
            names = ", ".join(esc(self.sessions.name_of(s)) for s in targets)
            return (f"Muted {names}: no trade alerts from it (pauses, its end and proposals still come)."
                    if cmd == "/mute" else f"Unmuted {names}.")
        if cmd == "/launch":
            return self._launch_menu(m)
        return HELP

    def _agents(self) -> list[dict]:
        """Every live session, in /status order (oldest launch first)."""
        return self.sessions.live() if self.sessions is not None else []

    def _which(self, verb: str, example: str) -> str:
        lines = [f"{i}. {esc(self.sessions.name_of(s))}" for i, s in enumerate(self._agents(), 1)]
        return compose([Note(f"Which agent should I {verb}? Send {esc(example)} or all.", lines)])

    def _targets(self, rest: str, verb: str) -> tuple[list[dict] | None, str | None]:
        """The agents a command names: none given and one running -> it; "all" -> every one; a number (as /status
        lists them) or a name -> that one. Else (None, the question or the refusal)."""
        live = self._agents()
        if not live:
            return None, "No agent is running."
        arg = rest.strip()
        if not arg:
            if len(live) == 1:
                return live, None
            return None, self._which(verb, f"/{verb} 1")
        if arg.lower() == "all":
            return live, None
        if arg.isdigit():
            i = int(arg)
            if 1 <= i <= len(live):
                return [live[i - 1]], None
            return None, f"There is no agent {i}; /status lists them."
        hit = [s for s in live if self.sessions.name_of(s).casefold() == arg.casefold()]
        if len(hit) == 1:
            return hit, None
        return None, f"No running agent is called {esc(arg)}; /status lists them."

    def _cached(self, key: Any, fn: Callable[[], Any]) -> Any:
        """`fn()` once per pump (outside a pump: every time)."""
        c = self._cache
        if c is None:
            return fn()
        if key not in c:
            c[key] = fn()
        return c[key]

    def muted(self) -> set[str]:
        return set(self._cached("muted", self._muted))

    def _muted(self) -> frozenset[str]:
        v = (self._setting(ALERTS) or {}).get(MUTED)
        return frozenset(x for x in v if isinstance(x, str)) if isinstance(v, list) else frozenset()

    def set_muted(self, session_id: str, on: bool, now: int) -> dict:
        """Mute (or unmute) one agent's trade and activity alerts. Pauses, its end and proposals still come."""
        if not isinstance(session_id, str) or not session_id:
            return {"ok": False, "code": "invalid", "reason": "Name the agent session to mute."}
        if not isinstance(on, bool):
            return {"ok": False, "code": "invalid", "reason": "muted must be true or false."}
        if self.sessions is not None and self.sessions.row(session_id) is None:
            return {"ok": False, "code": "not_found", "reason": f"There is no agent session {session_id!r}."}
        cur = self.muted()
        cur = cur | {session_id} if on else cur - {session_id}
        stored = self._setting(ALERTS) or {}
        self._put(ALERTS, {**stored, MUTED: sorted(cur)}, now)
        if self._cache is not None:
            self._cache.pop("muted", None)
        return {"ok": True, "muted": sorted(cur)}

    def _is_muted(self, session_id: str | None) -> bool:
        return bool(session_id) and session_id in self.muted()

    def _name(self, session_id: str | None) -> str:
        """The agent's name (escaped) for a message."""
        if not session_id or self.sessions is None:
            return "Agent"
        row = self.sessions.row(session_id)
        return esc(self.sessions.name_of(row)) if row is not None else "Agent"

    @staticmethod
    def _label(user_id: int) -> str:
        return f"telegram:{user_id}"

    async def _stop(self, rest: str = "") -> str | tuple:
        """/stop: one agent running (or /stop <n> / <name> / all) -> stopped at once; several and none named -> a
        button per agent and All, each with the Yes, stop / Cancel second tap."""
        if self.actions is None:
            return "Agent mode is off on this PC."
        live = self._agents()
        if not live:
            return "No session to stop."
        arg = rest.strip()
        if not arg and len(live) > 1:
            return self.stop_picker()
        targets, why = self._targets(arg, "stop")
        if targets is None:
            return why
        return await self._stop_targets([s["id"] for s in targets], all_=arg.lower() == "all")

    def stop_picker(self) -> tuple[str, Keyboard]:
        rows = [[(clip(self.sessions.name_of(s), 40), f"v:x:{s['id']}")] for s in self._agents()]
        return "Stop which agent?", [*rows, [("All agents", "v:x:all")]]

    async def _stop_targets(self, ids: list[str], *, all_: bool = False) -> str:
        names = {sid: self._name(sid) for sid in ids}
        own = (set(ids) | ({s["id"] for s in self._agents()} if all_ else set())) - self._own
        self._own |= own
        try:
            if all_:
                out = await self.actions.stop_all()
                done = [x["id"] for x in out.get("stopped") or [] if x.get("ok")]
            else:
                done = []
                for sid in ids:
                    o = await self.actions.stop(sid)
                    if o.get("ok"):
                        done.append(sid)
                    elif len(ids) == 1:
                        return ("No session to stop." if o.get("code") == "no_session"
                                else esc(o.get("reason") or "Not done."))
        finally:
            self._own -= own
        if not done:
            return "No session to stop."
        n = sum(self._open_positions(sid) for sid in done)
        who = ", ".join(names.get(sid) or self._name(sid) for sid in done)
        head = f"Stopped {who}." if len(done) < 3 else f"Stopped {len(done)} agents."
        return head if n == 0 else f"{head} {n} open position{'s' if n != 1 else ''} keep their stops."

    # -- reads shared by the commands and the alerts ------------------------------------------------------------------
    def _open_positions(self, session_id: str) -> int:
        rows = self.repo.select(BRACKETS, {"session_id": session_id},
                                filters=[("state", "in", (*OPEN_STATES, SETTLING))])
        return len(rows)

    def _local(self, ms: int) -> datetime:
        return rec.local(ms, self.tz)

    def hm(self, ms: int) -> str:
        return self._local(ms).strftime("%H:%M")

    def _day_start(self, now: int) -> int:
        return rec.local_day_start(now, self.tz)

    def _links(self, *, session_id: str | None = None, since: int | None = None) -> dict[str, dict]:
        """bracket id -> {rule, market, inputs} for rule entries (record_summary.rule_links)."""
        return rec.rule_links(self.repo, session_id=session_id, since=since)

    def _own_entry(self, session_id: str, bracket_id: str, created_ts: int) -> dict | None:
        """The agent's own place_entry tool row that opened this bracket, or None."""
        for r in self.repo.select(JOURNAL, {"session_id": session_id, "kind": "tool"},
                                  filters=[("ts", ">=", int(created_ts) - 60_000)], order_by=["id"]):
            d = r.get("data") or {}
            res = d.get("result") if isinstance(d.get("result"), dict) else {}
            br = res.get("bracket") if isinstance(res.get("bracket"), dict) else {}
            if d.get("name") == "place_entry" and br.get("id") == bracket_id:
                return d
        return None

    def _session_row(self, session_id: str) -> dict | None:
        rows = self.repo.select(SESSIONS, {"id": session_id}, limit=1)
        return rows[0] if rows else None

    @staticmethod
    def _stats(rows: list[dict], links: dict[str, dict]) -> dict:
        """Closed, filled trades among `rows`: count, wins (net > 0), net, and per rule {rule: (n, wins, net)}."""
        return rec.closed_stats(rows, links)

    def _session_stats(self, session_id: str) -> dict:
        """One session's trades and their tally; read once per pump (every close and line of it shares it)."""
        return self._cached(("stats", session_id), lambda: self._read_stats(session_id))

    def _read_stats(self, session_id: str) -> dict:
        rows = self.repo.select(BRACKETS, {"session_id": session_id}, order_by=["created_ts"])
        links = self._links(session_id=session_id)
        return {**self._stats(rows, links), "rows": rows, "links": links}

    @staticmethod
    def _rule_lines(st: dict) -> list[str]:
        """One detail line per rule: "CUSHION100 1/1 +$0.16"."""
        return [f"{esc(r)} {_record(n, w, x)}" for r, (n, w, x) in st["by_rule"].items()]

    @staticmethod
    def _trades_words(st: dict) -> str:
        return f"{plural(st['trades'], 'trade')} · {st['wins']} won"

    @staticmethod
    def _left_words(seconds: int) -> str:
        mins = max(0, int(seconds)) // 60
        return f"{mins // 60}h {mins % 60:02d}m" if mins >= 60 else f"{mins}m"

    @staticmethod
    def _level_word(kind: str | None, limits: dict) -> str:
        if kind == "external":
            return "External"
        return "Assist" if limits.get("level") == "assist" else "Auto"

    @staticmethod
    def _markets_words(limits: dict) -> str:
        return esc(", ".join(VENUE_LABELS.get(k, k) for k in limits.get("venues") or []) or "no markets")

    @staticmethod
    def _money_line(v: dict) -> str:
        """"Budget $0.10 / $20 · API $0.02 / $3" (no API for an external session)."""
        lim = v["limits"]
        line = f"Budget {money(max(0.0, v['budget_used']))} / {money(lim['budget'])}"  # wins: $0 used
        if v.get("kind") != "external":
            api = float((v.get("usage") or {}).get("api_usd") or 0.0)
            cap = lim.get("spend_cap")
            line += f" · API {money(api)}" + (f" / {money(float(cap))}" if cap else "")
        return line

    def _view(self, market: str | None, now: int) -> dict | None:
        if self.view_fn is None or market is None:
            return None
        try:
            return self.view_fn(market, now)
        except Exception:
            log.warning("telegram: the market view failed", exc_info=True)
            return None

    def _gap_now(self, b: dict, now: int) -> float | None:
        """BTC minus the strike now, for this position's window (None: unknown)."""
        v = self._view(_market_of(b.get("strategy")), now)
        if (v and v.get("btc") is not None and v.get("strike") is not None
                and v.get("window_end") == b.get("window_end")):
            return float(v["btc"]) - float(v["strike"])
        return None

    def _position_line(self, b: dict, link: dict | None, now: int) -> str:
        market = _market_of(b.get("strategy"))
        bits = [bold(f"{side_word(b.get('side'))} {money(_cost(b))} at {cents(b.get('entry_avg'))}"),
                esc((link or {}).get("rule") or "agent"), esc(VENUE_LABELS.get(market or "", b.get("strategy") or "?"))]
        gap = self._gap_now(b, now)
        if gap is not None:
            bits.append(f"BTC {gap_usd(gap)} vs strike")
        stop = _stop_of(b)
        bits.append(f"stop BTC {btc_usd(stop)}" if stop is not None else "no stop")
        if b.get("state") == SETTLING:
            bits.append("settling")
        return " · ".join(bits)

    @staticmethod
    def _held(st: dict) -> list[dict]:
        return [b for b in st["rows"] if b.get("state") in (*OPEN_STATES, SETTLING) and float(b.get("filled") or 0) > 0]

    # -- /status and /lights ------------------------------------------------------------------------------------------
    def status_line(self, now: int) -> str:
        if self.sessions is None:
            return "Agent mode is off on this PC."
        rows = self._rows(now)
        if not rows:
            return self._today_status(now)
        total = round(sum(r["net"] for r in rows), 2)
        trades = sum(r["trades"] for r in rows)
        head = (f"{bold(plural(len(rows), 'agent'))} · total {bold(signed(total))} · {plural(trades, 'trade')}"
                + (f" · ⚠️ {len([r for r in rows if r['needs_you']])} need you" if any(r["needs_you"] for r in rows)
                   else ""))
        details, muted = [], self.muted()
        order = {s["id"]: i for i, s in enumerate(self._agents(), 1)}
        for r in sorted(rows, key=lambda r: order.get(r["id"], 0)):
            line = (f"{order.get(r['id'], '?')}. {bold(esc(r['name']))} · {esc(r['chip'])} · {signed(r['net'])} · "
                    f"{plural(r['trades'], 'trade')} · ends {self.hm(r['ends_ts'])}"
                    + (" · muted" if r["id"] in muted else ""))
            details.append(line)
            if r["state"] == "paused":
                details.append(f"   Paused: {esc(clip(r.get('pause_reason') or 'no reason given'))}")
            st = self._session_stats(r["id"])
            details += ["   " + self._position_line(b, st["links"].get(b["id"]), now) for b in self._held(st)]
        return compose([Note(head, details)])

    def _rows(self, now: int) -> list[dict]:
        """The fleet's rows for every live session (agent_fleet_view.FleetView)."""
        if self.sessions is None:
            return []
        return self._cached(("rows", now), lambda: FleetView(self.sessions, proposals=self.proposals).rows(now))

    def _today_status(self, now: int) -> str:
        """No session running: today so far, the last session, the presets."""
        day = self._day(now)
        head = bold("No agent running") + (f" · today {bold(signed(day['net']))} · {self._trades_words(day)}"
                                           if day["trades"] else " · no trades today")
        details = []
        last = self.sessions.latest_view(now)
        if last is not None and last.get("state") == "ended" and last.get("ended_ts"):
            st = self._session_stats(last["id"])
            details.append(f"Last agent ({esc(last['name'])}) ended {self.hm(last['ended_ts'])}: {signed(st['net'])} · "
                           f"{plural(st['trades'], 'trade')}")
        if self.presets is not None:
            ps = self.presets.all()
            details.append("Presets: " + ", ".join(
                f"{esc(p['name'])} ({esc(', '.join(p['launch'].get('rules') or []) or 'no rules')})" for p in ps)
                if ps else "No presets saved.")
        return compose([Note(head, details)])

    def lights_line(self, now: int) -> str:
        if self.lights_fn is None:
            return "Setup lights are not available."
        try:
            markets = self.lights_fn(now)
        except Exception:
            log.warning("telegram: setup lights failed", exc_info=True)
            return "The setup lights could not be read."
        details: list[str] = []
        ready = setting = 0
        for mk in markets:
            lines = []
            for x in mk.get("lights") or []:
                st = x.get("state")
                if st == "green":
                    ready += 1
                    what = f"{side_word(x.get('side'))} {cents(x.get('ask'))}"
                    lines.append(f"{esc(x['rule'])} is ready: {bold(what)}")
                elif st == "amber":
                    setting += 1
                    lines.append(esc(x.get("line") or f"{x['rule']} is setting up."))
                elif st != "grey":
                    raise ValueError(f"unknown light state {st!r}")
            if lines:
                details += [bold(esc(mk["market_label"])), *lines]
        if not details:
            return f"{bold('All grey')} · nothing is setting up."
        return compose([Note(f"{bold('Lights')} · {ready} ready · {setting} setting up", details)])

    # -- /launch --------------------------------------------------------------------------------------------------
    def _launch_menu(self, m: Inbound) -> str | tuple:
        if self.presets is None:
            return "Presets are not available."
        presets = self.presets.all()
        if not presets:
            return "No presets yet. Save one from the PC's launch form."
        now = self.now_fn()
        rows = []
        for p in presets:
            nonce = self._nonce_fn()
            self._taps[nonce] = {"kind": "pick", "name": p["name"], "expires_ts": now + PICK_MS}
            rows.append([(p["name"], f"p:{nonce}")])
        return "Pick a preset.", rows

    @staticmethod
    def preset_line(p: dict) -> str:
        x = p["launch"]
        rules = ", ".join(x.get("rules") or []) or "no rules"
        venues = ", ".join(VENUE_LABELS.get(v, v) for v in x.get("venues") or [])
        return (f"{p['name']}: {x.get('level')} · {rules} · {venues} · {usd(float(x['budget']))} budget, "
                f"{usd(float(x['per_trade']))} a trade · {x.get('minutes')} min")

    # -- inbound: taps -----------------------------------------------------------------------------------------------
    async def handle_tap(self, t: Tap) -> None:
        if self._authorized(t.user_id, t.chat_id) is None:
            log.info("telegram: ignored a tap from user %s in chat %s (not the paired user and chat)",
                     t.user_id, t.chat_id)
            return
        kind, _, rest = (t.data or "").partition(":")
        if kind in ("a", "s"):
            await self._proposal_tap(t, kind, rest)
        elif kind in ("p", "c"):
            await self._launch_tap(t, kind, rest)
        elif kind == "v":
            await self._menu_tap(t, rest)
        else:
            await self._answer(t.callback_id, EXPIRED)

    async def _proposal_tap(self, t: Tap, kind: str, rest: str) -> None:
        pid, _, nonce = rest.partition(":")
        entry = self._props.get(pid)
        now = self.now_fn()
        p = self.proposals.get(pid) if self.proposals is not None else None
        if (entry is None or entry["done"] or not secrets.compare_digest(nonce.encode(), entry["nonce"].encode())
                or p is None or p.get("state") != "pending" or now >= int(p["expires_ts"])):
            await self._answer(t.callback_id, EXPIRED)
            return
        if entry.get("busy"):  # a second tap while the first is being sent: nothing more is sent
            await self._answer(t.callback_id, "Already sending…")
            return
        entry["busy"] = True
        try:
            with remote_human(self._label(t.user_id)):
                out = await (self.proposals.approve(pid, now) if kind == "a" else self.proposals.skip(pid, now))
        except Exception:
            # nothing answered the proposal (it stays pending until its TTL): the buttons work again
            log.warning("telegram: a proposal %s failed", "approve" if kind == "a" else "skip", exc_info=True)
            entry["busy"] = False
            await self._answer(t.callback_id, "That failed; nothing was sent. Tap again to retry.")
            return
        entry["busy"], entry["done"] = False, True  # answered: single use from here on

        if self.actions is not None:
            try:
                self.actions.answered(out)
            except Exception:
                log.warning("telegram: the agent panel refresh failed", exc_info=True)
        view = out.get("proposal") or p
        text, toast = self._outcome(view, out)
        await self._edit(t.chat_id, entry.get("message_id") or t.message_id, text)
        entry["edited"] = True
        await self._answer(t.callback_id, toast)

    @staticmethod
    def _outcome(view: dict, out: dict | None = None) -> tuple[str, str]:
        """A resolved proposal: the message's new HTML text, and the tap's notice (plain text)."""
        line = esc(view.get("line") or "")
        st = view.get("state")
        words = {"approved": "Approved", "skipped": "Skipped", "expired": "Expired"}
        if st in words:
            return f"{bold(words[st])} · {line}", words[st]
        why = (view.get("outcome") or {}).get("reason") or (out or {}).get("reason") or "refused"
        reason = clip(why).rstrip(".")
        text = compose([Note(f"⚠️ {bold('Not sent')} · {line}", [esc(reason[:1].upper() + reason[1:]) + "."])])
        return text, clip(f"Not sent: {reason}", 190)

    @classmethod
    def _outcome_text(cls, view: dict, out: dict | None = None) -> str:
        return cls._outcome(view, out)[0]

    async def _launch_tap(self, t: Tap, kind: str, nonce: str) -> None:
        now = self.now_fn()
        tap = self._taps.pop(nonce, None)
        if tap is None or tap["kind"] != ("pick" if kind == "p" else "confirm"):
            await self._answer(t.callback_id, EXPIRED)
            return
        if now >= tap["expires_ts"]:
            await self._answer(t.callback_id, EXPIRED)
            if kind == "c":
                await self._edit(t.chat_id, t.message_id, f"Expired · {esc(tap['name'])}: nothing launched.")
            return
        if self.presets is None or self.actions is None:
            await self._answer(t.callback_id, "Agent mode is off on this PC.")
            return
        p = self.presets.get(tap["name"])
        if p is None:
            await self._answer(t.callback_id, "That preset is gone.")
            return
        if kind == "p":
            confirm = self._nonce_fn()
            self._taps[confirm] = {"kind": "confirm", "name": p["name"], "expires_ts": now + CONFIRM_MS}
            await self._answer(t.callback_id)
            await self._say(t.chat_id, f"Launch {esc(self.preset_line(p))}? The button works for "
                                       f"{CONFIRM_MS // 1000} s.", [[("Launch", f"c:{confirm}")]])
            return
        await self._answer(t.callback_id, "Launching…")
        ok = self.presets.launchable(p["name"])
        if not ok.get("ok"):
            await self._edit(t.chat_id, t.message_id, f"Not launched: {esc(ok['reason'])}")
            return
        with as_person(self._label(t.user_id)):
            out = await self.actions.launch(ok["launch"], from_pc=False)
        if out.get("ok"):
            s = out["session"]
            text = (f"Launched {bold(esc(p['name']))}" + (f" as {esc(s['name'])}" if s.get("name") else "")
                    + f" · {esc(s['limits'].get('level'))} · {s['seconds_left'] // 60} min.")
        else:
            text = f"Not launched: {esc(out.get('reason'))}"
        await self._edit(t.chat_id, t.message_id, text)

    # -- the menu row: Status, Lights, a two-step Stop -----------------------------------------------------------------
    @staticmethod
    def menu_buttons(_session_id: str | None = None) -> Keyboard:
        """The row under the digest and the pinned status (callback data "v:...", apart from the proposals' "a:" /
        "s:" and /launch's "p:" / "c:"): Status, Lights and Stop… (which agent, or all)."""
        return [[("Status", "v:st"), ("Lights", "v:li"), ("Stop…", "v:sx")]]

    async def _menu_tap(self, t: Tap, rest: str) -> None:
        what, _, arg = rest.partition(":")
        now = self.now_fn()
        if what in ("st", "li"):
            await self._answer(t.callback_id)
            await self._say(t.chat_id, self.status_line(now) if what == "st" else self.lights_line(now))
        elif what == "sx":  # Stop…: one agent -> its confirm here; several -> pick which (or all) first
            live = self._agents()
            if self.actions is None or not live or t.message_id is None:
                await self._answer(t.callback_id, "No agent is running.")
                return
            if len(live) == 1:
                await self._confirm(t, live[0]["id"], self.menu_buttons(), now)
                return
            await self._answer(t.callback_id)
            text, buttons = self.stop_picker()
            await self._say(t.chat_id, text, buttons)
        elif what == "x":  # the first tap: the row becomes Yes, stop / Cancel for STOP_CONFIRM_MS
            live = {s["id"] for s in self._agents()}
            ok = arg == "all" and live or arg in live
            if self.actions is None or not arg or not ok or t.message_id is None:
                await self._answer(t.callback_id, EXPIRED)
                return
            await self._confirm(t, arg, None, now)
        elif what in ("y", "n"):
            c = self._confirms.pop(arg, None)  # single use
            if c is None:
                await self._answer(t.callback_id, EXPIRED)
                return
            await self._buttons(c["chat_id"], c["message_id"], c["restore"])
            live = {s["id"] for s in self._agents()}
            target = c["session_id"]
            if now >= c["expires_ts"] or not (target == "all" and live or target in live):
                await self._answer(t.callback_id, EXPIRED)
                return
            if what == "n":
                await self._answer(t.callback_id, "Cancelled.")
                return
            await self._answer(t.callback_id, "Stopping…")
            if target == "all":
                await self._say(t.chat_id, await self._stop_targets(sorted(live), all_=True))
            else:
                await self._say(t.chat_id, await self._stop_targets([target]))  # /stop's own path
        else:
            await self._answer(t.callback_id, EXPIRED)

    async def _confirm(self, t: Tap, target: str, restore: Keyboard | None, now: int) -> None:
        """Turn the tapped message's buttons into Yes, stop / Cancel for `target` (a session id or "all")."""
        nonce = self._nonce_fn()
        if restore is None:  # a picker message: its own buttons come back after the answer
            restore = self.stop_picker()[1]
        self._confirms[nonce] = {"session_id": target, "chat_id": t.chat_id, "message_id": t.message_id,
                                 "expires_ts": now + STOP_CONFIRM_MS, "restore": restore}
        who = "every agent" if target == "all" else self._name(target)
        await self._buttons(t.chat_id, t.message_id, [[("Yes, stop", f"v:y:{nonce}"), ("Cancel", f"v:n:{nonce}")]])
        await self._answer(t.callback_id, clip(f"Stop {who}? Tap Yes, stop within {STOP_CONFIRM_MS // 1000} s.",
                                               190))

    async def _buttons(self, chat_id: int, message_id: int, buttons: Keyboard | None) -> None:
        if self.messenger is None:
            return
        try:
            ok = await self.messenger.edit_buttons(chat_id, message_id, buttons)
        except Exception:
            log.warning("telegram: a message's buttons could not be changed", exc_info=True)
            return
        if not ok:
            log.info("telegram: a message's buttons could not be changed")

    async def _confirms_expiry(self, chat: int, alerts: dict, now: int) -> None:
        """A Stop confirm left untapped: its row goes back to what it was."""
        for nonce, c in list(self._confirms.items()):
            if now >= c["expires_ts"]:
                del self._confirms[nonce]
                await self._buttons(c["chat_id"], c["message_id"], c["restore"])

    def on_overlap(self, w: dict) -> None:
        """AgentSessions' overlap warning (KD2): two agents hold opposite sides of one window."""
        self._events.append(("overlap", dict(w)))

    # -- outbound: the hooks (sync, from the app) ---------------------------------------------------------------------
    def on_proposal(self, name: str, view: dict) -> None:
        """AgentProposals' notify: "proposal_new" / "proposal_resolved"."""
        self._events.append((name, dict(view)))

    def on_session(self, row: dict | None) -> None:
        """AgentSessions' notify. Only named fields are copied (the row may hold the token hash: never read). A
        launch (state running, updated when it started) is told once as "session_start"."""
        row = row or {}
        sid, state = row.get("id"), row.get("state")
        if not sid:
            return
        if state in ("paused", "ended"):
            self._events.append(("session", {"id": sid, "state": state, "kind": row.get("kind"),
                                             "pause_reason": row.get("pause_reason"),
                                             "end_reason": row.get("end_reason"),
                                             "usage": dict(row.get("usage") or {}), "own": sid in self._own}))
        elif (state == "running" and sid not in self._announced and row.get("started_ts") is not None
              and row.get("started_ts") == row.get("updated_ts")):
            self._announced.add(sid)
            self._events.append(("session_start", {"id": sid, "kind": row.get("kind"),
                                                   "limits": dict(row.get("limits") or {}),
                                                   "started_ts": row.get("started_ts"), "ends_ts": row.get("ends_ts")}))

    # -- outbound: the pump (the transport's loop calls it about every second) ------------------------------------
    async def pump(self) -> None:
        """One pass of the alerts. Reads shared by its steps (each session's stats, the mutes, the fleet rows) are
        made once per pump (`_cached`); the taps, proposal entries and bracket baseline are pruned."""
        self._cache = {}
        try:
            await self._pump()
        finally:
            self._cache = None

    async def _pump(self) -> None:
        now = self.now_fn()
        self._prune(now)
        p = self.paired()
        events = list(self._events)
        self._events.clear()
        if p is None or self.messenger is None:
            self._baseline(now)
            return
        alerts = self.alerts()
        chat = p["chat_id"]
        for name, value in events:
            try:
                await self._alert(chat, alerts, name, value, now)
            except Exception:
                log.warning("telegram: an alert failed", exc_info=True)
        for step in (self._journal, self._trades, self._heads_up, self._digest, self._flush, self._live,
                     self._confirms_expiry, self._daily, self._settle, self._setups, self._data):
            try:
                await step(chat, alerts, now)
            except Exception:
                log.warning("telegram: %s failed", step.__name__, exc_info=True)

    PROPS_KEPT = 200  # proposal entries remembered (answered and edited ones go first)

    def _prune(self, now: int) -> None:
        """Bounded memory however long the bot runs: expired launch taps go; answered proposals whose message was
        edited go (a late tap on one answers "Expired or already handled."), the oldest beyond PROPS_KEPT too."""
        for nonce in [n for n, tap in self._taps.items() if now >= tap["expires_ts"]]:
            del self._taps[nonce]
        for pid in [k for k, e in self._props.items() if e.get("done") and e.get("edited")]:
            del self._props[pid]
        while len(self._props) > self.PROPS_KEPT:
            del self._props[next(iter(self._props))]

    def _baseline(self, now: int) -> None:
        """Unpaired or not sending: keep the diffs' baselines current, so pairing never replays old trades."""
        try:
            self._brackets = self._bracket_states(now)
        except Exception:
            log.warning("telegram: agent brackets could not be read", exc_info=True)
        try:
            self._jid = self._journal_max()
        except Exception:
            log.warning("telegram: the agent journal could not be read", exc_info=True)
        self._outbox.clear()
        self._settle_seen = None
        self._greens = None

    # -- the activity outbox (rate limit and coalescing) ---------------------------------------------------------------
    def _queue(self, note: Note, session_id: str | None = None, *, always: bool = False) -> None:
        """An activity note into the outbox; a muted agent's trade and activity notes are dropped (`always`: a pause
        or its end, which need you, still go)."""
        if not always and self._is_muted(session_id):
            return
        self._outbox.append(note)
        if session_id:
            self._hour[session_id] = self._hour.get(session_id, 0) + 1

    async def _flush(self, chat: int, alerts: dict, now: int) -> None:
        """Send the waiting activity notes as one message, at most once every ACTIVITY_GAP_MS."""
        if not self._outbox or (self._act_ts is not None and now - self._act_ts < ACTIVITY_GAP_MS):
            return
        notes, self._outbox = self._outbox, []
        self._act_ts = now
        await self._send_notes(chat, notes)

    async def _send_now(self, chat: int, note: Note, now: int) -> None:
        """A message that never waits (a close, the end summary): its own news first (the preview line), then
        whatever waits in the outbox."""
        notes, self._outbox = [note, *self._outbox], []
        self._act_ts = now
        await self._send_notes(chat, notes)

    # -- events --------------------------------------------------------------------------------------------------------
    async def _alert(self, chat: int, alerts: dict, name: str, v: dict, now: int) -> None:
        if name == "proposal_new":
            if not alerts["proposals"] or v.get("state") != "pending":
                return
            nonce = self._nonce_fn()
            entry = {"nonce": nonce, "message_id": None, "done": False, "edited": False}
            self._props[v["id"]] = entry
            entry["message_id"] = await self._say(chat, self.proposal_text(v), [
                [("Approve", f"a:{v['id']}:{nonce}"), ("Skip", f"s:{v['id']}:{nonce}")]])
        elif name == "proposal_resolved":
            entry = self._props.get(v.get("id") or "")
            if entry is None or entry.get("edited"):
                return
            entry["done"] = entry["edited"] = True
            await self._edit(chat, entry["message_id"], self._outcome_text(v))
        elif name == "overlap":
            if alerts["activity"] or alerts["trades"]:
                label = esc(VENUE_LABELS.get(v.get("market") or "", v.get("strategy") or "?"))
                sides = " vs ".join(f"{esc(', '.join(sorted({x['name'] for x in v['sides'][s]})))} {s.title()}"
                                    for s in ("UP", "DOWN"))
                self._queue(Note(f"⚠️ {bold('Opposite sides')} · {label}",
                                 [f"{sides}. Both stay open; a warning, not a block."]), always=True)
        elif name == "session_start":
            if alerts["activity"]:
                self._hour[v["id"]] = 0
                if self._digest_next is None:
                    self._digest_next = int(v["started_ts"]) + DIGEST_MS
                self._queue(self.start_note(v), always=True)  # its own start is no hourly news
                await self._pin_start(chat, v["id"], now)
        elif name == "session":
            who = self._name(v["id"])
            if v["state"] == "paused":
                self._pin_due = True
                if v["own"]:
                    return
                note = Note(f"⏸ {bold('Paused')} · {who} · {esc(clip(v.get('pause_reason') or 'no reason given'))}")
                if alerts["activity"]:
                    self._queue(note, v["id"], always=True)
                elif alerts["pauses"]:
                    await self._send_notes(chat, [note])
                return
            self._forget_heads(v["id"])
            self._pin_due = True
            if alerts["activity"]:  # the summary names the positions handed back: no separate end line
                self._hour.pop(v["id"], None)
                await self._send_now(chat, self.end_note(v), now)
                return
            if v["own"]:
                return
            n = self._open_positions(v["id"])
            why = esc(END_WORDS.get(v.get("end_reason") or "", v.get("end_reason") or "no reason given"))
            if n and alerts["session_end"]:
                await self._say(chat, f"⏹ {bold(who + ' ended')} · {why} · {plural(n, 'open position')}")
            elif alerts["pauses"]:
                await self._say(chat, f"⏹ {bold(who + ' ended')} · {why}")
        else:
            raise ValueError(f"unknown remote event {name!r}")

    def _forget_heads(self, session_id: str) -> None:
        """A session ended: its positions' heads-up marks go (other agents keep theirs)."""
        ids = {b["id"] for b in self.repo.select(BRACKETS, {"session_id": session_id})}
        self._heads = {x for x in self._heads if x[0] not in ids}
        for bid in ids:
            self._dist0.pop(bid, None)

    @staticmethod
    def proposal_text(v: dict) -> str:
        """"🟣 Buy Up $1.00 · max 60¢ · Kalshi 15 min · H4 · Kalshi 15m agent" and how long it waits."""
        ttl = v.get("ttl_s")
        head = (f"🟣 {bold(esc(v.get('line') or 'Proposal'))} · {esc(v.get('market_label') or '?')} · "
                f"{esc(v.get('rule') or 'agent')}" + (f" · {esc(v['agent_name'])}" if v.get("agent_name") else ""))
        return compose([Note(head, [f"Answer within {ttl:g} s."] if ttl else [])])

    def start_note(self, v: dict) -> Note:
        lim = v.get("limits") or {}
        rules = lim.get("rules") or []
        head = (f"▶️ {bold(self._name(v['id']) + ' started')} · {self._level_word(v.get('kind'), lim)} · "
                f"{self._markets_words(lim)}")
        return Note(head, [f"Max loss {bold(money(float(lim['budget'])))} · {money(float(lim['per_trade']))} a trade · "
                           f"ends {self.hm(int(v['ends_ts']))}",
                           f"Rules: {esc(', '.join(rules))}" if rules else "No rules armed"], silent=True)

    def start_text(self, v: dict) -> str:
        return compose([self.start_note(v)])

    def end_note(self, v: dict) -> Note:
        st = self._session_stats(v["id"])
        why = END_WORDS.get(v.get("end_reason") or "", v.get("end_reason") or "no reason given")
        n = self._open_positions(v["id"])
        first = self._trades_words(st)
        if v.get("kind") != "external":
            first += f" · API {usd(float((v.get('usage') or {}).get('api_usd') or 0.0))}"
        details = [first, *self._rule_lines(st),
                   f"{plural(n, 'position')} handed back to you" if n else "No positions left open"]
        return Note(f"⏹ {bold(signed(st['net']))} · {self._name(v['id'])} ended · {esc(why)}", details)

    def end_text(self, v: dict) -> str:
        return compose([self.end_note(v)])

    # -- the pinned live status ----------------------------------------------------------------------------------------
    def _pin_state(self) -> dict | None:
        if not self._pin_loaded:
            v = self._setting(LIVE_PIN) or {}
            self._pin = v if v.get("message_id") is not None and (v.get("fleet") or v.get("session_id")) else None
            self._pin_loaded = True
        return self._pin

    def _pin_failed(self, what: str) -> None:
        """A pin / unpin / edit of the live status failed: logged once each, never raised."""
        if what in self._pin_warned:
            log.debug("telegram: the live status %s failed again", what)
            return
        self._pin_warned.add(what)
        log.warning("telegram: the live status %s failed (rights, or the message is too old); carrying on", what)

    async def _pin_call(self, what: str, chat_id: int, message_id: int, *args: Any) -> bool:
        try:
            if what == "edit":
                ok = await self.messenger.edit(chat_id, message_id, *args)
            else:
                ok = await getattr(self.messenger, what)(chat_id, message_id)
        except Exception:
            ok = False
        if not ok:
            self._pin_failed(what)
        return bool(ok)

    def live_text(self, now: int) -> str:
        """"📌 3 agents · +$3.10 · 2 winning · 1 holding · 0 need you", then one line per agent (the first
        PIN_AGENTS; the rest as "+ N more")."""
        rows = self._rows(now)
        total = round(sum(r["net"] for r in rows), 2)
        winning = sum(1 for r in rows if r["net"] > 0)
        holding = sum(1 for r in rows if r["open"]["positions"])
        needs = sum(1 for r in rows if r["needs_you"])
        head = (f"📌 {bold(plural(len(rows), 'agent'))} · {bold(signed(total))} · {winning} winning · {holding} "
                f"holding · {needs} need you")
        lines = []
        for r in rows[:PIN_AGENTS]:
            lines.append(f"{esc(r['name'])} · {esc(r['chip'])} · {signed(r['net'])} ({r['trades']})"
                         + (" ⚠️" if r["needs_you"] else ""))
        if len(rows) > PIN_AGENTS:
            lines.append(f"+ {len(rows) - PIN_AGENTS} more")
        return "\n".join([head, *lines])

    def final_text(self, now: int) -> str:
        """The pinned status once no agent runs: "⏹ No agents running · today +$0.16 (1)"."""
        day = self._agent_day(now)
        return f"⏹ {bold('No agents running')} · today {bold(signed(day['net']))} ({day['trades']})"

    async def _pin_start(self, chat: int, session_id: str, now: int) -> None:
        """A session started: the fleet's pin is edited at once; with none (or a stale single-session one), send,
        pin and store it (all silent)."""
        old = self._pin_state()
        if old is not None and old.get("fleet"):
            self._pin_due = True
            return
        if old is not None:
            await self._pin_close(old, now)
        if self.sessions is None or self.sessions.get(session_id) is None:
            return
        text = self.live_text(now)
        mid = await self._say(chat, text, self.menu_buttons(), silent=True)
        if mid is None:
            return
        await self._pin_call("pin", chat, mid)
        self._pin = {"fleet": True, "chat_id": chat, "message_id": mid}
        self._put(LIVE_PIN, self._pin, now)
        self._pin_text, self._pin_ts, self._pin_due = text, now, False

    async def _pin_close(self, pin: dict, now: int) -> None:
        """Edit the pinned status to the day's result, unpin it and forget it."""
        self._pin, self._pin_text, self._pin_ts = None, None, None
        self._put(LIVE_PIN, {}, now)
        await self._pin_call("edit", pin["chat_id"], pin["message_id"], self.final_text(now), None)
        await self._pin_call("unpin", pin["chat_id"], pin["message_id"])

    async def _live(self, chat: int, alerts: dict, now: int) -> None:
        """Keep the pinned fleet status current: an edit only when its text changed, at most every PIN_EDIT_MS (at
        once after an entry, a close, a start, a pause or an end); with no agent left (or a stale single-session
        pin) it is closed."""
        pin = self._pin_state()
        if pin is None:
            return
        if not pin.get("fleet") or not self._agents():
            await self._pin_close(pin, now)
            return
        if not self._pin_due and self._pin_ts is not None and now - self._pin_ts < PIN_EDIT_MS:
            return  # throttled: the fleet rows are not even built
        text = self.live_text(now)
        if text == self._pin_text:
            self._pin_due = False
            return
        self._pin_due, self._pin_ts = False, now
        if await self._pin_call("edit", pin["chat_id"], pin["message_id"], text, self.menu_buttons()):
            self._pin_text = text

    # -- the journal tail: material agent moves -------------------------------------------------------------------------
    def _journal_max(self) -> int:
        rows = self.repo.select(JOURNAL, order_by=["-id"], limit=1)
        return int(rows[0]["id"]) if rows else 0

    async def _journal(self, chat: int, alerts: dict, now: int) -> None:
        if self._jid is None:
            self._jid = self._journal_max()
            return
        rows = self.repo.select(JOURNAL, filters=[("id", ">", self._jid)], order_by=["id"], limit=500)
        if rows:
            self._jid = max(int(r["id"]) for r in rows)
        if not alerts["activity"]:
            return
        for r in rows:
            note = self.journal_note(r)
            if note:
                self._queue(note, r.get("session_id"))

    def journal_note(self, r: dict) -> Note | None:
        """One journal row as an activity note (naming its agent), or None when it is not worth a message."""
        kind, d = r.get("kind"), r.get("data") or {}
        who = self._name(r.get("session_id"))
        if kind == "decision":
            act = MATERIAL.get(d.get("action") or "")
            if act is None or d.get("source") != "agent":
                return None
            head = bold(f"Agent: {act}" + (f" {side_word(d['side'])}" if d.get("side") else "")) + f" · {who}"
            return Note(head, [esc(short(d.get("thesis")))] if d.get("thesis") else [])
        if kind == "rule":
            if d.get("ok") or d.get("repeat") or d.get("code") == "proposed":
                return None
            rule = esc(d.get("rule") or "?")
            if d.get("code") == "one_bet_per_window":
                by = (d.get("taken_by") or {}).get("rule")
                return Note(f"{bold(rule + ' skipped')} · {who}",
                            ["One bet per window" + (f" ({esc(by)} took it)." if by else ".")], silent=True)
            reason = clip(_plain_reason(d.get("reason")))
            return Note(f"⚠️ {bold(rule + ' not sent')} · {who}", [esc(reason[:1].upper() + reason[1:]) + "."])
        if kind == "tool":
            if not d.get("ok") or d.get("repeat"):
                return None
            args = d.get("args") or {}
            details = [esc(short(args["note"]))] if args.get("note") else []
            if d.get("name") == "set_btc_stop":
                bid, level = args.get("bracket_id"), args.get("level")
                old = self._stops.get(bid)
                if level is None:
                    return Note(bold("Stop removed") + f" · {who}", details, silent=True)
                if old is None:
                    return Note(bold(f"Stop set at BTC {btc_usd(level)}") + f" · {who}", details, silent=True)
                side = self._side(bid)
                tighter = level > old if side == "UP" else level < old
                return Note(bold(f"Stop {'tightened' if tighter else 'moved'} to BTC {btc_usd(level)}") + f" · {who}",
                            details, silent=True)
            if d.get("name") == "set_take_profit":
                return Note(bold(f"Take profit moved to {cents(args.get('price'))}") + f" · {who}", details,
                            silent=True)
        return None

    def journal_line(self, r: dict) -> str | None:
        """journal_note as the message it would be."""
        n = self.journal_note(r)
        return compose([n]) if n else None

    def _side(self, bracket_id: str | None) -> str | None:
        rows = self.repo.select(BRACKETS, {"id": bracket_id}, limit=1) if bracket_id else []
        return rows[0].get("side") if rows else None

    # -- the bracket diff: entries and closes ------------------------------------------------------------------------
    def _agent_brackets(self, now: int) -> list[dict]:
        return self.repo.select(BRACKETS, filters=[("session_id", "is_null", False),
                                                   ("updated_ts", ">=", now - WATCH_MS)])

    @staticmethod
    def _states(rows: list[dict]) -> dict[str, tuple[bool, bool]]:
        return {b["id"]: (float(b.get("filled") or 0) > 0, b.get("state") == "closed") for b in rows}

    def _bracket_states(self, now: int) -> dict[str, tuple[bool, bool]]:
        rows = self._agent_brackets(now)
        self._stops = {b["id"]: _stop_of(b) for b in rows}
        return self._states(rows)

    async def _trades(self, chat: int, alerts: dict, now: int) -> None:
        rows = self._agent_brackets(now)
        seen = self._brackets
        states = self._states(rows)
        # the baseline keeps every bracket still watched and every one not yet closed; a closed one that left the
        # watch window never changes again, so it goes (the map stays bounded)
        self._brackets = {**{k: v for k, v in (seen or {}).items() if not v[1] or k in states}, **states}

        self._stops = {b["id"]: _stop_of(b) for b in rows}  # the watched brackets only (bounded)
        act = alerts["activity"]
        if seen is None:
            return
        for b in rows:
            filled, closed = states[b["id"]]
            was_filled, was_closed = seen.get(b["id"], (False, False))
            if (filled and not was_filled) or (closed and not was_closed):
                self._pin_due = True  # the pinned status shows it at once
        if not (alerts["trades"] or act):
            return
        muted = self.muted()
        for b in rows:
            filled, closed = states[b["id"]]
            was_filled, was_closed = seen.get(b["id"], (False, False))
            where = esc(STRATEGY_LABEL.get(b.get("strategy"), b.get("strategy")))
            side = side_word(b.get("side"))
            sid = b.get("session_id")
            if sid in muted:  # a muted agent's entries and closes are not told (its pauses and end still are)
                continue
            if filled and not was_filled:
                if act:
                    note = self.entry_note(b, now)
                    if note is None:  # its rule / tool row is not journaled yet: look again next pump
                        self._brackets[b["id"]] = (False, False)
                        continue
                    self._queue(note, sid)
                else:
                    what = f"Filled {side} {float(b['filled']):g} at {cents(b.get('entry_avg'))}"
                    await self._say(chat, f"🔵 {bold(what)} · {where}")
            if closed and not was_closed:
                if act:
                    if filled:
                        self._hour[sid] = self._hour.get(sid, 0) + 1
                        await self._send_now(chat, self.close_note(b), now)
                    else:
                        self._queue(Note(f"{bold(side + ' order not filled')} · {where}", silent=True), sid)
                elif filled:
                    word = EXIT_WORDS.get(b.get("exit_reason") or "", "Closed")
                    await self._say(chat, f"{result_emoji(_net(b))}{bold(signed(_net(b)))} · {word} {side} · {where}")
                else:
                    await self._say(chat, f"{bold('Not filled')} · {side} · {where}", silent=True)

    def _rule_record(self, st: dict, rule: str) -> str | None:
        rec = st["by_rule"].get(rule)
        return f"{esc(rule)} this session: {_record(*rec)}" if rec else None

    def entry_note(self, b: dict, now: int) -> Note | None:
        """"🔵 Bought Up $1 at 86¢ · Kalshi 15m agent" over "CUSHION100 · BTC +$118 vs strike · 6:40 left · stop BTC
        $76,560" (and the rule's record this session); None while the entry's rule / tool row may still be on its way
        (FILL_WAIT_MS)."""
        sid, bid = b["session_id"], b["id"]
        link = self._links(session_id=sid).get(bid)
        tool = None if link else self._own_entry(sid, bid, int(b.get("created_ts") or now))
        if link is None and tool is None and now - int(b.get("created_ts") or 0) < FILL_WAIT_MS:
            return None
        bought = f"Bought {side_word(b.get('side'))} {money(_cost(b))} at {cents(b.get('entry_avg'))}"
        head = f"🔵 {bold(bought)} · {self._name(sid)}"
        stop = _stop_of(b)
        stop_words = f"stop BTC {btc_usd(stop)}" if stop is not None else "no stop"
        details: list[str] = []
        if link:
            inp = link.get("inputs") or {}
            bits = [esc(link["rule"])]
            if inp.get("gap") is not None and inp.get("left_s") is not None:
                bits += [f"BTC {gap_usd(inp['gap'])} vs strike", f"{mmss(inp['left_s'])} left"]
            details.append(" · ".join([*bits, stop_words]))
            rec = self._rule_record(self._session_stats(sid), link["rule"])
            if rec:
                details.append(rec)
            if stop is not None and inp.get("btc") is not None:
                d0 = float(inp["btc"]) - stop if b.get("side") == "UP" else stop - float(inp["btc"])
                if d0 > 0:
                    self._dist0[bid] = d0
        else:
            note = ((tool or {}).get("args") or {}).get("note")
            if note:
                details.append(esc(short(note)))
            details.append(stop_words[:1].upper() + stop_words[1:])
        return Note(head, details)

    def entry_text(self, b: dict, now: int) -> str | None:
        n = self.entry_note(b, now)
        return compose([n]) if n else None

    def close_note(self, b: dict) -> Note:
        """"🔴 −$0.10 · Settled Down · Kalshi 15m agent" over "LATERESID · Agent −$0.10 · 1 trade · 0 won · fleet
        +$3.10"."""
        word = EXIT_WORDS.get(b.get("exit_reason") or "", "Closed")
        st = self._session_stats(b["session_id"])
        rule = (st["links"].get(b["id"]) or {}).get("rule") or "agent"
        net = _net(b)
        head = f"{result_emoji(net)}{bold(signed(net))} · {word} {side_word(b.get('side'))} · {self._name(b['session_id'])}"
        fleet = self._fleet_net(b["session_id"], st["net"])
        return Note(head, [f"{esc(rule)} · Agent {bold(signed(st['net']))} · {self._trades_words(st)}"
                           + (f" · fleet {signed(fleet)}" if fleet is not None else "")])

    def _fleet_net(self, session_id: str, own: float) -> float | None:
        """The live fleet's total net (this agent's own included even if it just ended); None with one agent."""
        if self.sessions is None:
            return None
        others = [s["id"] for s in self._agents() if s["id"] != session_id]
        if not others:
            return None
        return round(own + sum(self._session_stats(sid)["net"] for sid in others), 2)

    def close_text(self, b: dict) -> str:
        return compose([self.close_note(b)])

    # -- heads-ups: near the stop, the last 90 s ----------------------------------------------------------------------
    async def _heads_up(self, chat: int, alerts: dict, now: int) -> None:
        if not alerts["activity"] or self.view_fn is None or self.sessions is None or now < self._heads_next:
            return
        self._heads_next = now + HEADS_EVERY_MS
        live = [s["id"] for s in self._agents() if not self._is_muted(s["id"])]
        if not live:
            return
        for b in self.repo.select(BRACKETS, {"state": "open"}, filters=[("session_id", "in", live)]):
            if float(b.get("filled") or 0) <= 0:
                continue
            sid = b["session_id"]
            v = self._view(_market_of(b.get("strategy")), now)
            if not v or v.get("btc") is None or v.get("window_end") != b.get("window_end"):
                continue
            bid, btc, up = b["id"], float(v["btc"]), b.get("side") == "UP"
            what = f"{side_word(b.get('side'))} {money(_cost(b))} at {cents(b.get('entry_avg'))} · {self._name(sid)}"
            stop = _stop_of(b)
            if stop is not None and (bid, "stop") not in self._heads:
                dist = btc - stop if up else stop - btc
                d0 = self._dist0.setdefault(bid, dist)
                if d0 > 0 and 0 < dist <= NEAR_STOP_SHARE * d0:
                    self._heads.add((bid, "stop"))
                    self._queue(Note(f"⚠️ {bold(f'${dist:,.0f} from the stop')} · {what}",
                                     [f"BTC {btc_usd(btc)} · stop BTC {btc_usd(stop)}"], silent=True), sid)
            left = int(b["window_end"]) - now
            if 0 < left <= LAST_MS and (bid, "last") not in self._heads and v.get("strike") is not None:
                self._heads.add((bid, "last"))
                strike = float(v["strike"])
                win = btc >= strike if up else btc < strike
                self._queue(Note(f"⚠️ {bold(f'{mmss(left / 1000)} left · ' + ('winning' if win else 'losing'))} · "
                                 f"{what}", [f"BTC {btc_usd(btc)} · {gap_usd(btc - strike)} vs strike"],
                                 silent=True), sid)

    # -- the hourly digest ---------------------------------------------------------------------------------------------
    async def _digest(self, chat: int, alerts: dict, now: int) -> None:
        """The hourly fleet digest while any agent runs (one line per agent inside); skipped for an hour where
        nothing happened."""
        if not alerts["activity"] or self.sessions is None:
            return
        live = self._agents()
        if not live:
            self._digest_next = None
            return
        if self._digest_next is None:  # the bot started mid-session: from the next full hour of the oldest agent
            start = int(live[0]["started_ts"])
            self._digest_next = start + ((now - start) // DIGEST_MS + 1) * DIGEST_MS
            for s in live:
                self._hour.setdefault(s["id"], 0)
        if now < self._digest_next:
            return
        span_start = self._digest_next - DIGEST_MS
        nxt = self._digest_next
        while nxt <= now:
            nxt += DIGEST_MS
        self._digest_next = nxt
        happened = sum(self._hour.get(s["id"], 0) for s in live)
        for s in live:
            self._hour[s["id"]] = 0
        if happened == 0:  # only quiet wakes this hour: nothing to say
            return
        self._outbox.append(self.digest_note(now, span_start, span_start + DIGEST_MS))

    def digest_note(self, now: int, span_start: int, span_end: int) -> Note:
        """"📊 Fleet +$3.10 · 00:00–01:00 · 9 trades" over one line per agent ("Kalshi 15m agent +$2.52 · 6 trades ·
        5h left"), the fleet's budget and API, and what needs you; with the Status / Lights / Stop… row."""
        rows = self._rows(now)
        total = round(sum(r["net"] for r in rows), 2)
        trades = sum(r["trades"] for r in rows)
        head = f"📊 Fleet {bold(signed(total))} · {self.hm(span_start)}–{self.hm(span_end)} · {plural(trades, 'trade')}"
        details = [f"{esc(r['name'])} {signed(r['net'])} · {plural(r['trades'], 'trade')} · "
                   f"{self._left_words(r['seconds_left'])} left" for r in rows]
        api = round(sum(r["api"]["used"] for r in rows), 4)
        used = round(sum(r["budget"]["used"] for r in rows), 2)
        budget = round(sum(r["budget"]["total"] for r in rows), 2)
        details.append(f"Budget {money(max(0.0, used))} / {money(budget)} · API {usd(api)}")
        needs = [f"{esc(r['name'])}: {esc(clip(r['needs_you'][0]['text'])).rstrip('.')}" for r in rows if r["needs_you"]]
        if needs:
            details.append("⚠️ Needs you: " + "; ".join(needs) + ".")
        return Note(head, details, silent=True, menu="fleet")

    def digest_text(self, now: int, span_start: int, span_end: int) -> str:
        return compose([self.digest_note(now, span_start, span_end)])

    def _agent_day(self, now: int) -> dict:
        """Every agent session's trades closed today (local day): {net, trades}."""
        day = self._day(now)
        mine = [b for b in day["closed"] if b.get("session_id")]
        return {"net": round(sum(_net4(b) for b in mine), 2), "trades": len(mine)}

    # -- the daily summary ----------------------------------------------------------------------------------------------
    def _day(self, now: int) -> dict:
        """Today's closed trades (local day) over every agent session and your own Play trades."""
        return rec.closed_between(self.repo, self._day_start(now), now, STRATEGY_LABEL)

    def _coverage_line(self, now: int) -> str | None:
        cov = self.recorders.coverage(now) if self.recorders is not None else None
        return esc(coverage_text(cov)) if cov else None

    def daily_text(self, now: int, *, data: bool = False, coverage: str | None = None) -> str | None:
        """"🌙 −$0.45 · Today · 2 trades · 1 won" over sessions, API, the money kind, each rule, each agent and best /
        worst; with `data`, yesterday's recorder coverage too (alone on a day without trades). `coverage`: that line
        already read (the pump reads it off the event loop)."""
        day = self._day(now)
        cov = coverage if coverage is not None else (self._coverage_line(now) if data else None)
        if not day["trades"]:
            return compose([Note(f"🌙 {bold('No trades today')}", [cov])]) if cov else None
        sessions = rec.sessions_between(self.repo, day["start"], now)
        api = sum(float((s.get("usage") or {}).get("api_usd") or 0.0) for s in sessions)

        def who(b: dict) -> str:
            r = (day["links"].get(b["id"]) or {}).get("rule")
            return r or ("agent's own" if b.get("session_id") else "yours")
        groups: dict[str, list] = {}
        for b in day["closed"]:
            e = groups.setdefault(who(b), [0, 0, 0.0])
            e[0] += 1
            e[1] += 1 if _net4(b) > 0 else 0
            e[2] += _net4(b)
        order = sorted(groups, key=lambda k: (k in ("agent's own", "yours"), k == "yours", k))
        envs = sorted({ENV_WORDS.get(b.get("env"), str(b.get("env"))) for b in day["closed"]})
        details = [f"{plural(len(sessions), 'session')} · API {usd(api)} · {esc(', '.join(envs))}",
                   *(f"{esc(k)} {_record(n, w, round(x, 2))}" for k in order for n, w, x in [groups[k]])]
        by_agent: dict[str, list] = {}
        for b in day["closed"]:
            if not b.get("session_id"):
                continue
            row = self.sessions.row(b["session_id"]) if self.sessions is not None else None
            name = self.sessions.name_of(row) if row is not None else "agent"
            e = by_agent.setdefault(name, [0, 0, 0.0])
            e[0] += 1
            e[1] += 1 if _net4(b) > 0 else 0
            e[2] += _net4(b)
        if by_agent:
            details.append("By agent: " + " · ".join(f"{esc(k)} {_record(n, w, round(x, 2))}"
                                                     for k, (n, w, x) in sorted(by_agent.items())))
        if day["trades"] > 1:
            best = max(day["closed"], key=_net4)
            worst = min(day["closed"], key=_net4)
            details.append(f"Best {signed(_net(best))} ({esc(who(best))} {side_word(best.get('side'))}) · worst "
                           f"{signed(_net(worst))} ({esc(who(worst))} {side_word(worst.get('side'))})")
        if cov:
            details.append(cov)
        head = f"🌙 {bold(signed(day['net']))} · Today · {self._trades_words(day)}"
        return compose([Note(head, details)])

    async def _daily(self, chat: int, alerts: dict, now: int) -> None:
        if not alerts["daily"]:
            return
        local = self._local(now)
        hh, mm = (int(x) for x in self.daily_at().split(":"))
        if (local.hour, local.minute) < (hh, mm):
            return
        day = local.date().isoformat()
        if self._daily_day == day:
            return
        self._daily_day = day
        if (self._setting(DAILY_SENT) or {}).get("day") == day:
            return
        self._put(DAILY_SENT, {"day": day}, now)
        # The research database is read in a worker thread: a recorder's write lock must never stall the app's loop.
        cov = await asyncio.to_thread(self._coverage_line, now) if alerts["data"] else None
        text = self.daily_text(now, coverage=cov)
        if text:
            await self._say(chat, text, silent=True)

    # -- settle warnings and setups -------------------------------------------------------------------------------------
    async def _settle(self, chat: int, alerts: dict, now: int) -> None:
        if self.settle_fn is None or now < self._settle_next:
            return
        self._settle_next = now + SETTLE_EVERY_MS
        warns = self.settle_fn(now)
        ids = {w["id"] for w in warns}
        seen = self._settle_seen
        self._settle_seen = ids
        if seen is None or not alerts["settle_warnings"]:
            return
        for w in warns:
            if w["id"] not in seen:
                where = esc(w.get("venue_label") or "the venue")
                if w.get("kind") == "unresolved_order":  # a lost order answer still unprovable (order_resolution)
                    await self._say(chat, f"⚠️ {bold('Check ' + where)} · {esc(w['ticker'])}: could not confirm "
                                          "whether your order filled; it counts at its maximum until it is known")
                    continue
                await self._say(chat, f"⚠️ {bold('Check ' + where)} · {esc(w['ticker'])} still settling after "
                                      f"{esc(w.get('minutes'))} min")

    async def _setups(self, chat: int, alerts: dict, now: int) -> None:
        if not alerts["setups"] or self.lights_fn is None:
            self._greens = None
            return
        if now < self._setups_next:
            return
        self._setups_next = now + SETUPS_EVERY_MS
        manual = self.sessions is None or not self.sessions.live()
        greens = set()
        labels = {}
        for mk in self.lights_fn(now):
            for x in mk.get("lights") or []:
                if x.get("state") == "green":
                    key = (x["rule"], mk["market"], mk.get("window_end"))
                    greens.add(key)
                    labels[key] = mk["market_label"]
        seen = self._greens
        self._greens = greens
        if seen is None or not manual:
            return
        for key in sorted(greens - seen, key=str):
            await self._say(chat, f"{bold(esc(key[0]) + ' ready')} · {esc(labels[key])}")

    # -- the research recorders' freshness (data) -----------------------------------------------------------------------
    async def _data(self, chat: int, alerts: dict, now: int) -> None:
        if self.recorders is None or now < self._data_next:
            return
        self._data_next = now + DATA_EVERY_MS
        changes = await asyncio.to_thread(self.recorders.changes, now)  # off the loop, as the daily line
        if not alerts["data"]:
            return
        for c in changes:
            head, detail = change_text(c, self.recorders.db_label)
            emoji = "🟢" if c.kind == "back" else "⚠️"
            await self._say(chat, f"{emoji} {bold(esc(head))} · {esc(detail)}", silent=c.kind == "back")
