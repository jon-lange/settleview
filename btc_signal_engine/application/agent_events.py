"""Agent event engine: turns market and account changes into a small number of meaningful agent wakes (R13, R14).

Pure and clock-injected (every method takes `now` in ms); the runner (U6) owns the 1 s loop and the I/O.

Multi-agent (KD6): the call and light flip detection (`MarketSignals`) runs once per market for every session arming
it, through one shared `SignalBoard` (the fleet's); each session's engine turns those flips into its own news (lights
only while it holds there). What depends on the session (its positions, its last wake's price references, its
decision points, alarms and snooze) stays per engine and is a cheap comparison of the shared market state.

1. Diff. `observe(now, Snapshot)` compares a compact state per allowed market (built from `PlayService.now_view`; the
   snapshot holds the session's allowed markets only, and a non-urgent event for any other market is ignored) and the
   session's own positions with the previous one. What it emits depends on whether the session holds a position in
   that market (2026-10-30 live run: wakes at second 0 of a window had nothing to decide on, and routine moves while
   flat cost wakes for nothing).
   - A new window resets that market's baselines (its price reference, call side and once-per-window marks) and emits
     nothing by itself.
   - Decision points (normal; flat in that market, entry open, and the window "ready": a fresh quote, a strike and a
     BTC gap), each once per window: `window_ready` (the first time the window is ready) and `entry_check` (entry
     closes within `entry_check_lead_ms`; Kalshi 15m closes entries 2 min before the end, so 5 min before the end).
     A window_ready inside the entry-check lead counts as that window's entry check too.
   - Both states: `call_changed` when Play's call (its verdict at Play's up_at / down_at: ENTER UP / ENTER DOWN /
     WAIT, read only with a fresh quote)
     changes and holds `signal_hold_ms` (an agree-count change alone is not one; the first call of a window is its
     baseline); `price_move` when the mid moved (`flat_mid_move` flat, `mid_move` holding) since the last wake, or BTC
     moved (`flat_btc_move_usd` with no position anywhere, `btc_move_usd` holding); and agent_alarm.
   - Holding only (Play's own prompts to a person, from now_view and domain/strategy/play): `advice_changed` (Play's
     advice became CONSIDER EXIT; urgent), `advice_cleared` (back to HOLD), `near_stop` (BTC came within Play's
     `exit.near_stop_usd` of the position's stop, or of Play's ride rule `index_stop_level(strike, exit.stop_gap)`
     when none is set; urgent; re-armed once BTC is twice that far again), `near_take_profit` (the held side's bid
     within `take_profit_near` of its resting take profit; once per position), `btc_near_strike` (BTC crossed the
     strike or came within `near_strike_usd`), `signal_changed` (a light's lean flipped and held 5 s; flips that
     revert sooner emit nothing) and `window_ending` (`window_ending_ms` left; urgent).
     Rule positions (`Snapshot.from_views(rule_ids=...)`: entries an armed rule opened, which ride to settlement with
     no take-profit order): never `near_take_profit`, and while one is held in a market a `signal_changed` whose
     lights all went to None (no lean, e.g. SKIP) emits nothing; near_stop, window_ending, advice and account events
     stay.
   Account events the diff cannot see (fill, position_closed, settled, order_refused, perps_risk, limit_hit,
   session_ending, human_note, rule_entry: an armed rule opened an entry) are pushed in with `push`; limit_hit counts once per session. The first sight of a
   market only sets its baseline (a window already ready then is not announced). Unknown event types are errors.
   `schedule(now)` gives each market's clock (seconds to entry cutoff and to close) and its next decision point.
2. Coalesce. Pending events are keyed by (type, venue, span) keeping the first and latest values and times. A wake
   is due when an urgent event waits and `urgent_gap_ms` has passed since the last wake ended, or normal events have
   settled (3 s with no new one, or 10 s since the first) and `min_gap_ms` has passed. Low events never wake on their
   own, except `idle_check`, added when a position has been open with no wake for `idle_check_ms`. Never two wakes
   at once (`begin_wake` / `end_wake`).
3. Bounds and drop rules (checked when a non-urgent wake would be due, so each batch is logged once in `skipped`):
   at `max_wakes_per_hour` normal events are skipped; with no position and entry closed on every market, a batch of
   only price or light events is skipped; with the session budget used up and no position, everything but
   window_ready is skipped. Urgent events always wake (cap, snooze and budget included).
4. Alarms (at most 10) fire once and are removed; snooze (until a time or an event type) mutes normal and low events
   only. Both clear at session end. An alarm the agent set is urgent: it asked to be woken for exactly that.
5. During a running wake every new event is pending for the next wake, except that `take_urgent_during_wake` hands
   the urgent ones to the runner (as the current wake's `new_events`) and removes them, so none is delivered twice.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from btc_signal_engine.domain.strategy import play as P

URGENT, NORMAL, LOW = "urgent", "normal", "low"
EVENT_CLASS = {
    "window_ready": NORMAL, "entry_check": NORMAL, "call_changed": NORMAL, "signal_changed": NORMAL,
    "price_move": NORMAL, "btc_near_strike": NORMAL, "advice_cleared": NORMAL, "near_take_profit": NORMAL,
    "window_ending": URGENT, "advice_changed": URGENT, "near_stop": URGENT, "fill": URGENT,
    "position_closed": URGENT, "settled": URGENT,
    "order_refused": URGENT, "perps_risk": URGENT, "limit_hit": URGENT, "session_ending": URGENT,
    "human_note": URGENT, "agent_alarm": URGENT, "rule_entry": URGENT,
    "idle_check": LOW,
}
ONCE_PER_SESSION = frozenset({"limit_hit"})
PRICE_OR_LIGHT = frozenset({"price_move", "btc_near_strike", "signal_changed", "call_changed"})
DECISION_POINTS = ("window_ready", "entry_check")  # the scheduled per-window wakes, while flat in that market
POSITION_EVENTS = frozenset({"rule_entry", "fill", "position_closed", "settled", "btc_near_strike", "advice_changed",
                             "advice_cleared", "near_stop", "near_take_profit", "window_ending", "idle_check"})
ALARM_KINDS = ("btc_above", "btc_below", "mid_above", "mid_below", "at_time", "seconds_left_below")
MARKET_ALARMS = frozenset({"mid_above", "mid_below", "seconds_left_below"})
CONSIDER_EXIT = "CONSIDER EXIT"
HOUR_MS = 3_600_000
SKIPPED_KEEP = 500

Key = tuple[str, str | None, str | None]


@dataclass(frozen=True)
class EventsConfig:
    urgent_gap_ms: int = 5_000
    min_gap_ms: int = 30_000
    quiet_ms: int = 3_000
    max_settle_ms: int = 10_000
    max_wakes_per_hour: int = 60
    idle_check_ms: int = 600_000
    mid_move: float = 0.05  # holding a position in that market
    btc_move_usd: float = 100.0  # holding a position anywhere
    flat_mid_move: float = 0.20  # flat in that market: only a large move wakes (replay: 0.10 -> ~29 wakes/h, 0.20 + 60 s hold -> ~17)
    flat_btc_move_usd: float = 150.0  # no position anywhere
    near_strike_usd: float = 25.0  # holding only
    signal_hold_ms: int = 60_000  # a light's or the call's new side must hold this long (a flip that reverts within a minute is noise)
    entry_check_lead_ms: int = 180_000  # entry check this long before the entry cutoff (Kalshi 15m: 5 min left; the user prompt allows no entries with under 3 min left)
    window_ending_ms: int = 90_000
    take_profit_near: float = 0.05  # holding: the held side's bid this close to the resting take profit
    max_alarms: int = 10


@dataclass
class MarketState:
    venue: str
    span: str
    start: int
    end: int
    entry_cutoff_ms: int
    entry_open: bool
    mid_up: float | None
    strike: float | None
    btc: float | None
    gap: float | None
    action: str | None
    agree: int | None
    leans: dict[str, str | None]
    fresh: bool = False  # the quote is live (now_view's market.fresh)
    stop_gap: float | None = None  # Play's exit rule (now_view's exit): the stop is this far past the strike
    near_stop_usd: float | None = None  # ... and CONSIDER EXIT this close to the stop

    @classmethod
    def from_view(cls, v: dict) -> MarketState:
        w, m, b, c = v["window"], v.get("market") or {}, v.get("btc") or {}, v.get("call") or {}
        return cls(venue=v["venue"], span=v["span"], start=int(w["start"]), end=int(w["end"]),
                   entry_cutoff_ms=int(w["entry_cutoff_ms"]), entry_open=bool(w["entry_open"]),
                   mid_up=m.get("mid_up"), strike=m.get("strike", b.get("strike")), btc=b.get("btc"),
                   gap=b.get("gap"), action=c.get("action"), agree=(c.get("agree") or {}).get("n"),
                   leans={s["key"]: s.get("lean") for s in v.get("signals") or []}, fresh=bool(m.get("fresh")),
                   stop_gap=(v.get("exit") or {}).get("stop_gap"),
                   near_stop_usd=(v.get("exit") or {}).get("near_stop_usd"))

    @property
    def ready(self) -> bool:
        """Something to decide on: a fresh quote, a strike and BTC's gap to it."""
        return self.fresh and self.mid_up is not None and self.strike is not None and self.gap is not None

    @property
    def call_side(self) -> str | None:
        """The app's call, read only with a live quote (a stale one reads WAIT, which is no call)."""
        return self.action if self.fresh and self.mid_up is not None else None

    def to_cutoff(self, now: int) -> int:
        return self.end - self.entry_cutoff_ms - now


@dataclass
class PositionState:
    id: Any
    venue: str
    span: str
    held: float
    advice: str | None
    side: str | None = None
    stop_level: float | None = None  # the stop set on it (None = none set)
    take_profit: float | None = None
    bid: float | None = None  # the held side's bid (Play's advice reads it)
    rule: bool = False  # opened by an armed rule: rides to settlement, no take-profit order


@dataclass
class Snapshot:
    markets: dict[tuple[str, str], MarketState]
    positions: list[PositionState]
    btc: float | None

    @classmethod
    def from_views(cls, views: list[dict], position_ids: set | None = None, *,
                   rule_ids: set | None = None) -> Snapshot:
        """One `now_view` per allowed market; `position_ids` keeps only the session's own brackets (None = all);
        `rule_ids` marks the brackets an armed rule opened."""
        markets = {(v["venue"], v["span"]): MarketState.from_view(v) for v in views}
        positions = [PositionState(id=p.get("id"), venue=v["venue"], span=v["span"], held=float(p.get("held") or 0),
                                   advice=(p.get("advice") or {}).get("action"), side=p.get("side"),
                                   stop_level=p.get("stop_level"), take_profit=p.get("take_profit"),
                                   bid=(p.get("advice") or {}).get("bid"), rule=p.get("id") in (rule_ids or ()))
                     for v in views for p in v.get("positions") or []
                     if position_ids is None or p.get("id") in position_ids]
        btc = next((m.btc for m in markets.values() if m.btc is not None), None)
        return cls(markets=markets, positions=positions, btc=btc)

    def holds(self, venue: str | None = None, span: str | None = None, *, rule: bool = False) -> bool:
        """Any position held (in that market); `rule`: only positions an armed rule opened."""
        return any(p.held > 0 and (venue is None or (p.venue, p.span) == (venue, span)) and (p.rule or not rule)
                   for p in self.positions)


@dataclass
class Event:
    type: str
    cls: str
    venue: str | None
    span: str | None
    first: Any
    latest: Any
    first_ms: int
    latest_ms: int
    count: int = 1

    def as_dict(self) -> dict:
        return {"type": self.type, "class": self.cls, "venue": self.venue, "span": self.span, "first": self.first,
                "latest": self.latest, "first_ms": self.first_ms, "latest_ms": self.latest_ms, "count": self.count}


def _merge(old: Any, new: Any) -> Any:
    """Coalesced latest value: dicts merge (lists inside concatenate, e.g. two alarms); anything else is replaced."""
    if not (isinstance(old, dict) and isinstance(new, dict)):
        return new
    out = dict(old)
    for k, x in new.items():
        out[k] = out[k] + x if isinstance(out.get(k), list) and isinstance(x, list) else x
    return out


def _alarm(a: dict) -> dict:
    """A validated alarm in its stored form; anything malformed is an error, never coerced."""
    if not isinstance(a, dict) or set(a) - {"kind", "value", "venue", "span"}:
        raise ValueError(f"alarm must have only kind, value, venue, span: {a!r}")
    kind, value = a.get("kind"), a.get("value")
    if kind not in ALARM_KINDS:
        raise ValueError(f"unknown alarm kind {kind!r}; one of {', '.join(ALARM_KINDS)}")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"alarm value must be a number: {value!r}")
    venue, span = a.get("venue"), a.get("span")
    if kind in MARKET_ALARMS and not (venue and span):
        raise ValueError(f"a {kind} alarm needs its venue and span")
    if kind not in MARKET_ALARMS and (venue or span):
        raise ValueError(f"a {kind} alarm takes no venue or span")
    return {"kind": kind, "value": value, "venue": venue, "span": span}


class MarketSignals:
    """One market's call and light flip detection (KD6: run once per market, shared by every session arming it).

    `update(now, m)` advances the hold state machines (a new side must hold `signal_hold_ms`) and returns this tick's
    flips: (call flip {action, from, agree} or None, light flips {key: new lean}). The same state seen again at the
    same `now` (the next session reading the shared market this tick) returns the same answer without advancing, so
    N sessions cost one detection. A new window resets the call's baseline (its first call is not a flip)."""

    def __init__(self, hold_ms: int) -> None:
        self.hold_ms = hold_ms
        self._start: int | None = None
        self._call: list = [None, None, None]  # [stable, candidate, since]
        self._signals: dict[str, list] = {}  # light key -> [stable, candidate, since]
        self._last: tuple | None = None  # (state key, result) of the last update

    @staticmethod
    def _key(now: int, m: MarketState) -> tuple:
        return (now, m.start, m.action, m.fresh, m.mid_up, m.agree, tuple(sorted(m.leans.items(), key=str)))

    def update(self, now: int, m: MarketState) -> tuple[dict | None, dict[str, str | None]]:
        key = self._key(now, m)
        if self._last is not None and self._last[0] == key:
            return self._last[1]
        if self._start != m.start:
            self._start = m.start
            self._call = [None, None, None]
        out = (self._diff_call(now, m), self._diff_signals(now, m))
        self._last = (key, out)
        return out

    def _diff_call(self, now: int, m: MarketState) -> dict | None:
        side = m.call_side
        st = self._call
        if side is None:
            st[1] = st[2] = None
        elif st[0] is None:
            st[0] = side
        elif side == st[0]:
            st[1] = st[2] = None
        elif side != st[1]:
            st[1], st[2] = side, now
        elif now - st[2] >= self.hold_ms:
            flip = {"action": side, "from": st[0], "agree": m.agree}
            st[0], st[1], st[2] = side, None, None
            return flip
        return None

    def _diff_signals(self, now: int, m: MarketState) -> dict[str, str | None]:
        flipped = {}
        for key, lean in m.leans.items():
            st = self._signals.setdefault(key, [lean, None, None])
            if lean == st[0]:
                st[1] = st[2] = None
            elif st[2] is None or lean != st[1]:  # a new candidate (None is a lean, so `since` marks one)
                st[1], st[2] = lean, now
            elif now - st[2] >= self.hold_ms:
                st[0], st[1], st[2] = lean, None, None
                flipped[key] = lean
        return flipped


class SignalBoard:
    """The MarketSignals of every market, shared by every session's engine (the fleet holds one; an engine built
    alone has its own)."""

    def __init__(self, hold_ms: int) -> None:
        self.hold_ms = hold_ms
        self._markets: dict[tuple[str, str], MarketSignals] = {}

    def market(self, venue: str, span: str) -> MarketSignals:
        k = (venue, span)
        ms = self._markets.get(k)
        if ms is None:
            ms = self._markets[k] = MarketSignals(self.hold_ms)
        return ms


class AgentEvents:
    """One session's event engine. See the module docstring for the rules."""

    def __init__(self, config: EventsConfig | None = None, *, markets: set[tuple[str, str]] | None = None,
                 board: SignalBoard | None = None) -> None:
        """`markets`: the session's allowed (venue, span) pairs; None = whatever the snapshots hold. `board`: the
        shared per-market call / light detection (KD6; the fleet's), else this engine's own."""
        self.config = config or EventsConfig()
        if board is not None and board.hold_ms != self.config.signal_hold_ms:
            raise ValueError("the shared signal board's hold time differs from this engine's signal_hold_ms")
        self._board = board if board is not None else SignalBoard(self.config.signal_hold_ms)
        self._markets = None if markets is None else frozenset(markets)
        self._pending: dict[Key, Event] = {}
        self._snap: Snapshot | None = None
        self._mid_ref: dict[tuple[str, str], float | None] = {}
        self._btc_ref: float | None = None
        self._advice: dict[Any, str | None] = {}
        self._stop_armed: dict[Any, bool] = {}  # position id -> near_stop may fire (re-armed once well clear)
        self._once: set[tuple] = set()  # (type, venue, span, window end) already emitted
        self._session_once: set[str] = set()
        self._alarms: list[dict] = []
        self._snooze: dict | None = None
        self._running = False
        self._last_end: int | None = None
        self._wake_starts: deque[int] = deque()
        self._skipped: deque[dict] = deque(maxlen=SKIPPED_KEEP)

    # -- inputs ------------------------------------------------------------------------------------------------------
    def push(self, now: int, type: str, *, venue: str | None = None, span: str | None = None,
             value: Any = None) -> None:
        """Add one event (account events, notes, or anything the runner sees directly)."""
        if type not in EVENT_CLASS:
            raise ValueError(f"unknown event type {type!r}")
        if type in ONCE_PER_SESSION:
            if type in self._session_once:
                return
            self._session_once.add(type)
        cls = EVENT_CLASS[type]
        allowed = self._markets if self._markets is not None else self._snap.markets if self._snap else None
        if cls != URGENT and venue is not None and allowed is not None and (venue, span) not in allowed:
            return  # not one of the session's markets: nothing to wake for
        if self._snooze is not None and (self._snooze["until_event"] == type
                                         or (self._snooze["until_ms"] is not None
                                             and now >= self._snooze["until_ms"])):
            self._snooze = None
        if self._snooze is not None and cls != URGENT:
            return
        key = (type, venue, span)
        e = self._pending.get(key)
        if e is None:
            self._pending[key] = Event(type, cls, venue, span, value, value, now, now)
        else:
            e.latest, e.latest_ms, e.count = _merge(e.latest, value), now, e.count + 1

    def observe(self, now: int, snap: Snapshot) -> None:
        """Diff the new snapshot against the last one and emit what changed."""
        prev = self._snap
        if prev is None or self._btc_ref is None:
            self._btc_ref = snap.btc
        for k, m in snap.markets.items():
            old = prev.markets.get(k) if prev is not None else None
            if old is None or old.start != m.start:  # a new window (internal: baselines reset, no wake by itself)
                self._mid_ref[k] = m.mid_up  # a new window's price is not a move
                self._once = {x for x in self._once if x[1:3] != k}
            if old is None:
                if m.ready:  # first sight: a window already ready is not announced
                    self._once.add(("window_ready", m.venue, m.span, m.end))
                self._board.market(m.venue, m.span).update(now, m)  # baselines; nothing is news on first sight
            else:
                self._diff_market(now, old, m, snap)
        self._diff_btc(now, prev, snap)
        self._diff_positions(now, snap)
        self._snap = snap
        self._check_alarms(now, snap)

    def _diff_market(self, now: int, old: MarketState, m: MarketState, snap: Snapshot) -> None:
        c, where = self.config, {"venue": m.venue, "span": m.span}
        holds = snap.holds(m.venue, m.span)
        if not holds:
            self._decision_points(now, m)
        if holds and 0 < m.end - now <= c.window_ending_ms:
            self._once_per_window(now, "window_ending", m, {"seconds_left": (m.end - now) // 1000})
        call_flip, light_flips = self._board.market(m.venue, m.span).update(now, m)
        if call_flip is not None:
            self.push(now, "call_changed", venue=m.venue, span=m.span, value=call_flip)
        self._signals_news(now, m, light_flips, emit=holds, quiet_none=snap.holds(m.venue, m.span, rule=True))
        ref = self._mid_ref.get((m.venue, m.span))
        move = c.mid_move if holds else c.flat_mid_move
        if (m.mid_up is not None and m.mid_up != old.mid_up and ref is not None
                and abs(m.mid_up - ref) >= move - 1e-9):
            self.push(now, "price_move", **where, value={"mid_up": m.mid_up, "from": ref})
        if holds and old.start == m.start and old.gap is not None and m.gap is not None and old.gap != m.gap:
            crossed = (old.gap >= 0) != (m.gap >= 0)
            near = abs(m.gap) <= c.near_strike_usd < abs(old.gap)
            if crossed or near:
                self.push(now, "btc_near_strike", **where,
                          value={"btc": m.btc, "strike": m.strike, "gap": m.gap, "crossed": crossed})

    def _decision_points(self, now: int, m: MarketState) -> None:
        """window_ready / entry_check, once per window each, while flat there with entry open and the window ready."""
        to_cut = m.to_cutoff(now)
        if not (m.entry_open and m.ready and to_cut > 0):
            return
        in_lead = to_cut <= self.config.entry_check_lead_ms
        value = {"start": m.start, "end": m.end, "seconds_to_close": (m.end - now) // 1000,
                 "seconds_to_entry_cutoff": to_cut // 1000, "mid_up": m.mid_up, "strike": m.strike, "btc": m.btc,
                 "gap": m.gap, "call": m.call_side}
        if ("window_ready", m.venue, m.span, m.end) not in self._once:
            if in_lead:  # ready this late: it is the entry check too
                self._once.add(("entry_check", m.venue, m.span, m.end))
            self._once_per_window(now, "window_ready", m, value)
        elif in_lead:
            self._once_per_window(now, "entry_check", m, value)

    def _once_per_window(self, now: int, type: str, m: MarketState, value: dict) -> None:
        mark = (type, m.venue, m.span, m.end)
        if mark not in self._once:
            self._once.add(mark)
            self.push(now, type, venue=m.venue, span=m.span, value=value)

    def _signals_news(self, now: int, m: MarketState, flipped: dict, *, emit: bool, quiet_none: bool) -> None:
        """The shared light flips as this session's news: only while it holds there (lights are routine while
        flat); `quiet_none` (a rule position held here): flips that only take lights to None are not news."""
        if quiet_none and flipped and all(v in (None, "SKIP") for v in flipped.values()):
            return
        if flipped and emit:
            self.push(now, "signal_changed", venue=m.venue, span=m.span, value=dict(flipped))

    def _diff_btc(self, now: int, prev: Snapshot | None, snap: Snapshot) -> None:
        if prev is None or snap.btc is None or snap.btc == prev.btc or self._btc_ref is None:
            return
        move = self.config.btc_move_usd if snap.holds() else self.config.flat_btc_move_usd
        if abs(snap.btc - self._btc_ref) >= move:
            self.push(now, "price_move", value={"btc": snap.btc, "from": self._btc_ref})

    def _diff_positions(self, now: int, snap: Snapshot) -> None:
        """Play's own prompts for a held position: its advice changing, BTC nearing its stop, the take profit near."""
        advice, armed = {}, {}
        for p in snap.positions:
            advice[p.id] = p.advice
            seen = p.id in self._advice
            where = {"venue": p.venue, "span": p.span}
            if seen and self._advice[p.id] != p.advice:
                if p.advice == CONSIDER_EXIT:
                    self.push(now, "advice_changed", **where, value={"ids": [p.id], "advice": p.advice})
                elif self._advice[p.id] == CONSIDER_EXIT and p.advice == "HOLD":
                    self.push(now, "advice_cleared", **where, value={"ids": [p.id], "advice": p.advice})
            armed[p.id] = self._stop_armed.get(p.id, True)
            m = snap.markets.get((p.venue, p.span))
            if p.held <= 0 or m is None or p.side not in ("UP", "DOWN"):
                continue
            level = p.stop_level
            if level is None and m.strike is not None and m.stop_gap is not None:
                level = P.index_stop_level(p.side, m.strike, m.stop_gap)  # Play's ride rule: strike -/+ $10
            dist = P.stop_distance(p.side, m.btc, level)
            if dist is not None and m.near_stop_usd is not None:
                inside = dist <= m.near_stop_usd + P.EPS
                if inside and armed[p.id]:
                    armed[p.id] = False
                    if seen:  # a position first seen inside the band was just filled: the fill wakes for it
                        self.push(now, "near_stop", **where,
                                  value={"ids": [p.id], "side": p.side, "btc": m.btc, "stop_level": level,
                                         "stop_set": p.stop_level is not None, "distance": dist})
                elif not inside and dist > 2 * m.near_stop_usd:
                    armed[p.id] = True
            if (not p.rule and p.take_profit is not None and p.bid is not None
                    and p.bid >= p.take_profit - self.config.take_profit_near - P.EPS):
                mark = ("near_take_profit", p.id)
                if mark not in self._once:
                    self._once.add(mark)
                    self.push(now, "near_take_profit", **where,
                              value={"ids": [p.id], "side": p.side, "bid": p.bid, "take_profit": p.take_profit})
        self._advice, self._stop_armed = advice, armed

    # -- alarms and snooze -------------------------------------------------------------------------------------------
    def set_alarms(self, alarms: list[dict]) -> list[dict]:
        """Replace the session's alarms (the `set_alarms` tool); at most `max_alarms`."""
        if len(alarms) > self.config.max_alarms:
            raise ValueError(f"at most {self.config.max_alarms} alarms, got {len(alarms)}")
        self._alarms = [_alarm(a) for a in alarms]
        return self.alarms()

    def alarms(self) -> list[dict]:
        return [dict(a) for a in self._alarms]

    def _check_alarms(self, now: int, snap: Snapshot) -> None:
        keep = []
        for a in self._alarms:
            observed = self._alarm_reading(now, a, snap)
            kind, v = a["kind"], a["value"]
            fired = observed is not None and (
                observed >= v if kind in ("btc_above", "mid_above", "at_time") else
                observed < v if kind == "seconds_left_below" else observed <= v)
            if not fired:
                keep.append(a)
                continue
            self.push(now, "agent_alarm", venue=a["venue"], span=a["span"],
                      value={"fired": [{"alarm": dict(a), "observed": observed, "at_ms": now}]})
        self._alarms = keep

    @staticmethod
    def _alarm_reading(now: int, a: dict, snap: Snapshot) -> float | None:
        kind = a["kind"]
        if kind == "at_time":
            return now
        if kind in ("btc_above", "btc_below"):
            return snap.btc
        m = snap.markets.get((a["venue"], a["span"]))
        if m is None:
            return None
        return m.mid_up if kind in ("mid_above", "mid_below") else (m.end - now) / 1000

    def snooze(self, *, until_ms: int | None = None, until_event: str | None = None) -> dict:
        """Mute normal and low events until a time or until an event of that type arrives (the `snooze` tool)."""
        if (until_ms is None) == (until_event is None):
            raise ValueError("snooze needs exactly one of until_ms or until_event")
        if until_event is not None and until_event not in EVENT_CLASS:
            raise ValueError(f"unknown event type {until_event!r}")
        self._snooze = {"until_ms": until_ms, "until_event": until_event}
        return dict(self._snooze)

    def snoozed(self) -> dict | None:
        return None if self._snooze is None else dict(self._snooze)

    def schedule(self, now: int) -> dict[tuple[str, str], dict]:
        """Each session market's clock and its next decision point (None while holding: position events wake then)."""
        snap, lead = self._snap, self.config.entry_check_lead_ms
        out: dict[tuple[str, str], dict] = {}
        for k, m in (snap.markets.items() if snap is not None else ()):
            to_cut, holds = m.to_cutoff(now), snap.holds(*k)
            to_close = max(0, (m.end - now) // 1000)
            if holds:
                nxt = None
            elif not m.entry_open or to_cut <= 0:
                nxt = {"type": "window_ready", "in_s": to_close}  # the next window
            elif ("window_ready", *k, m.end) not in self._once:
                nxt = {"type": "window_ready", "in_s": 0}  # waiting for a fresh quote and the strike
            elif ("entry_check", *k, m.end) not in self._once:
                nxt = {"type": "entry_check", "in_s": max(0, (to_cut - lead) // 1000)}
            else:
                nxt = {"type": "window_ready", "in_s": to_close}
            out[k] = {"window_start": m.start, "window_end": m.end, "seconds_to_close": to_close,
                      "seconds_to_entry_cutoff": max(0, to_cut // 1000), "entry_open": m.entry_open,
                      "quote_ready": m.ready, "holding": holds, "next_decision_point": nxt}
        return out

    def end_session(self) -> None:
        """Alarms, snooze and pending events end with the session."""
        self._alarms, self._snooze = [], None
        self._pending.clear()

    # -- wakes -------------------------------------------------------------------------------------------------------
    def should_wake(self, now: int, *, budget_exhausted: bool = False) -> bool:
        """Whether the runner should start a wake now. Applies the drop rules, logging what it skips."""
        if self._running:
            return False
        c = self.config
        if self._snooze is not None and self._snooze["until_ms"] is not None and now >= self._snooze["until_ms"]:
            self._snooze = None
        holds = self._snap is not None and self._snap.holds()
        if any(e.cls == URGENT for e in self._pending.values()):
            return self._since_end(now) >= c.urgent_gap_ms
        if budget_exhausted and not holds:
            self._skip(now, "session budget used up", lambda e: e.type != "window_ready")
        normal = [e for e in self._pending.values() if e.cls == NORMAL]
        if normal:
            settled = (now - max(e.latest_ms for e in normal) >= c.quiet_ms
                       or now - min(e.first_ms for e in normal) >= c.max_settle_ms)
            if not settled or self._since_end(now) < c.min_gap_ms:
                return False
            entry_closed = self._snap is not None and not any(m.entry_open for m in self._snap.markets.values())
            if not holds and entry_closed and all(e.type in PRICE_OR_LIGHT for e in self._pending.values()):
                self._skip(now, "no position and entry closed", lambda e: True)
                return False
            if self._wakes_last_hour(now) >= c.max_wakes_per_hour:
                self._skip(now, "hourly wake cap", lambda e: True)
                return False
            return True
        if holds and self._snooze is None and self._since_end(now) >= c.idle_check_ms:
            self.push(now, "idle_check", value={"idle_s": self._since_end(now) // 1000})
            return True
        return False

    def has_urgent(self) -> bool:
        """An urgent event is pending (pure: no drop rules run)."""
        return any(e.cls == URGENT for e in self._pending.values())

    def begin_wake(self, now: int) -> dict:
        """Start a wake: hand back every pending event (coalesced, oldest first) and reset the price references."""
        self._running = True
        self._wake_starts.append(now)
        events = self._take(lambda e: True)
        if self._snap is not None:
            self._btc_ref = self._snap.btc
            self._mid_ref.update({k: m.mid_up for k, m in self._snap.markets.items()})
        reason = (URGENT if any(e["class"] == URGENT for e in events) else
                  "idle_check" if events and all(e["type"] == "idle_check" for e in events) else NORMAL)
        return {"started_ms": now, "reason": reason, "events": events}

    def take_urgent_during_wake(self) -> list[dict]:
        """Urgent events that arrived during the running wake, for its next tool result; removed from pending."""
        return self._take(lambda e: e.cls == URGENT) if self._running else []

    def end_wake(self, now: int) -> None:
        self._running = False
        self._last_end = now

    def pending(self) -> list[dict]:
        return [e.as_dict() for e in self._ordered(self._pending.values())]

    @property
    def skipped(self) -> list[dict]:
        """Batches not woken for, newest last: {"at_ms", "reason", "events"}."""
        return list(self._skipped)

    # -- helpers -----------------------------------------------------------------------------------------------------
    def _since_end(self, now: int) -> float:
        return float("inf") if self._last_end is None else now - self._last_end

    def _wakes_last_hour(self, now: int) -> int:
        while self._wake_starts and self._wake_starts[0] <= now - HOUR_MS:
            self._wake_starts.popleft()
        return len(self._wake_starts)

    @staticmethod
    def _ordered(events: Any) -> list[Event]:
        return sorted(events, key=lambda e: (e.first_ms, e.type, e.venue or "", e.span or ""))

    def _take(self, which: Any) -> list[dict]:
        keys = [k for k, e in self._pending.items() if which(e)]
        return [e.as_dict() for e in self._ordered(self._pending.pop(k) for k in keys)]

    def _skip(self, now: int, reason: str, which: Any) -> None:
        events = self._take(lambda e: e.cls != URGENT and which(e))
        if events:
            self._skipped.append({"at_ms": now, "reason": reason, "events": events})
