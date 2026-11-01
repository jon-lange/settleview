"""Ask Settleview, Manual level (2026-10-30; wireframe v2 "Phone" steps 1-5 and 8): one quiet line under Play's call,
the setup lights, the pre-trade check while sliding, "Fill in ticket" and the hold note. Every word comes from the
code templates here; no model is called. It never places, approves or cancels anything: "Fill in ticket" only hands
the screen the rule's side, max price and BTC stop, and the user's slide still sends through the normal path.

Modes (the user's Play setting, kept on the device like Play's other settings): "off" (nothing is computed: the screen
does not ask, and `view` refuses it), "quiet" (warnings only) and "full" (adds the lights, the rule lines, the fill-in
ticket, the coin-flip context and the hold note). Warning keys: WARNINGS. An unknown mode or warning key is an error.

The line (`pick_line`), by priority: a rule ready (green) > a rule setting up (amber; the one furthest along) > a
warning (only for a side the user picked) > context ("Coin flip. Passing fits your record.") > nothing.

Warnings (`warning`), for one side at this moment, each only when its toggle is on AND the user's own record in that
bucket (your_record) has at least one trade and is losing (net < $0): nothing to say otherwise.
- against_btc: buying the side BTC is against (UP with BTC below the price to beat, DOWN above it).
- late: under 3 minutes left (agent_rules.MIN_LEFT_MS, the floor the rules keep for the same reason).
- band_50_75: the side's ask is 50-75c.
Several fold into one sentence ("This is against BTC, and late."); the details give each bucket's numbers and say
"only N trades" below MIN_N.

Context (full, no side picked): the call is a coin flip (WAIT with a live price), no rule is amber or green, and the
user's 50-75c record has at least MIN_N trades and loses: "Coin flip. Passing fits your record."

Hold note (full, an open position): the entry rule when the order was filled in from a rule (its copilot_shown
record) or opened by an agent rule (rule_scores' journal link): "The rule says hold. Your stop is set." / "No stop is
set."; otherwise, only when the user's early sells (your_record early_exits, at least MIN_N) cost money against
holding: "Holding has beaten your early sells."

What was shown with an order (`record_shown`): one trading_audit row (event SHOWN, the bracket's id, env and strategy)
whose detail is what the screen showed when the slide sent it (mode, lights, line, warning, filled-in rule, BTC vs the
price to beat), so a later scorecard can score followed vs ignored. One row per bracket; a repeat is a no-op.
"""

from __future__ import annotations

from collections.abc import Iterable

from btc_signal_engine.application.actor import actor
from btc_signal_engine.application.agent_rules import MIN_LEFT_MS, RULES
from btc_signal_engine.application.play import PlayService
from btc_signal_engine.application.rule_scores import BACKTEST, RuleBook, tally
from btc_signal_engine.application.setups import GREEN, STATES, lights as setup_lights
from btc_signal_engine.application.your_record import BAND, BRACKETS, SHOWN, YourRecord, against
from btc_signal_engine.domain.markets import MARKETS, UnknownMarket, by_venue_span, venue as venue_spec

MODES = ("off", "quiet", "full")
WARNINGS = {"against_btc": "Against BTC", "late": "Under 3 minutes left", "band_50_75": "50–75¢ favourites"}
PHRASES = {"against_btc": "against BTC", "late": "late", "band_50_75": "at 50–75¢"}
MIN_N = 5  # below this many trades a record is "only N trades", never a strong claim
CACHE_MS = 30_000  # the rules' live records and your record are re-read at most this often
EPS = 1e-9


def check_mode(mode: str) -> str:
    if mode not in MODES:
        raise ValueError(f"unknown Ask Settleview mode {mode!r}; modes are {', '.join(MODES)}")
    return mode


def check_warnings(keys: Iterable[str]) -> frozenset[str]:
    out = frozenset(keys)
    bad = sorted(out - set(WARNINGS))
    if bad:
        raise ValueError(f"unknown warning {', '.join(map(repr, bad))}; warnings are {', '.join(WARNINGS)}")
    return out


# -- words ------------------------------------------------------------------------------------------------------------
def signed_usd(x: float) -> str:
    return ("−" if x < -EPS else "+" if x > EPS else "") + f"${abs(x):.2f}"


def signed_c(x: float | None) -> str:
    return "—" if x is None else ("−" if x < -EPS else "+" if x > EPS else "") + f"{abs(x):.0f}¢"


def trades_words(n: int) -> str:
    word = "trade" if n == 1 else "trades"
    return f"only {n} {word}" if n < MIN_N else f"{n} {word}"


def fold(phrases: list[str]) -> str:
    """"This is against BTC." / "This is against BTC, and late." / "This is against BTC, late, and at 50–75¢."."""
    if len(phrases) == 1:
        return f"This is {phrases[0]}."
    return "This is " + ", ".join(phrases[:-1]) + ", and " + phrases[-1] + "."


def bucket_words(key: str, b: dict) -> str:
    n = b["n"]
    if key == "against_btc":
        return f"Against BTC: you've won {b['wins']} of {n}, {signed_usd(b['net_usd'])}" + (
            f" ({trades_words(n)})." if n < MIN_N else ".")
    if key == "late":
        return f"Under 3 minutes left: your average is {signed_c(b['net_c'])} a contract over {trades_words(n)}."
    return f"Your trades at 50–75¢: {signed_c(b['net_c'])} per contract over {trades_words(n)}."


# -- the pure pieces ------------------------------------------------------------------------------------------------------
def conditions(side: str, view: dict, now: int) -> list[str]:
    """The warning keys that apply to buying `side` now (before the toggles and the record)."""
    out = []
    if against(side, (view.get("btc") or {}).get("gap")):
        out.append("against_btc")
    if int(view["window"]["end"]) - now < MIN_LEFT_MS:
        out.append("late")
    ask = ((view.get("entry") or {}).get(side) or {}).get("ask")
    if ask is not None and BAND[0] - EPS <= ask <= BAND[1] + EPS:
        out.append("band_50_75")
    return out


def warning(side: str, view: dict, now: int, record: dict, enabled: frozenset[str]) -> dict | None:
    """The one folded warning for buying `side` now, or None (see the module doc)."""
    keys = [k for k in conditions(side, view, now)
            if k in enabled and record[k]["n"] >= 1 and record[k]["net_usd"] < -EPS]
    if not keys:
        return None
    return {"kind": "warning", "tone": "warn", "side": side, "keys": keys, "text": fold([PHRASES[k] for k in keys]),
            "details": [bucket_words(k, record[k]) for k in keys]}


def context(view: dict, lights: list[dict], record: dict) -> dict | None:
    call = view.get("call") or {}
    if call.get("action") != "WAIT" or call.get("mid_up") is None:
        return None
    if any(x["state"] != "grey" for x in lights):
        return None
    b = record["band_50_75"]
    if b["n"] < MIN_N or b["net_usd"] >= -EPS:
        return None
    return {"kind": "context", "tone": "agent", "text": "Coin flip. Passing fits your record.",
            "details": ["No tested rule is setting up in this window.", bucket_words("band_50_75", b)]}


def rule_line(x: dict) -> dict:
    return {"kind": "ready" if x["state"] == GREEN else "setup", "tone": "agent", "rule": x["rule"],
            "text": x["line"], "details": list(x["details"]), "progress": x["progress"], "side": x["side"]}


def pick_line(mode: str, lights: list[dict], warn: dict | None, ctx: dict | None) -> dict | None:
    """The one line: ready > setting up > warning > context > nothing; "quiet" shows warnings only."""
    check_mode(mode)
    if mode == "off":
        return None
    if mode == "quiet":
        return warn
    green = [x for x in lights if x["state"] == GREEN]
    if green:
        return rule_line(green[0])
    amber = [x for x in lights if x["state"] == "amber"]
    if amber:
        return rule_line(max(amber, key=lambda x: x["progress"]))
    return warn or ctx


def hold_note(pos: dict, rule: str | None, record: dict) -> dict | None:
    """The one line on an open position (see the module doc), or None."""
    stop = pos.get("stop_level")
    early = record["early_exits"]
    cost = early["vs_hold_usd"]
    early_words = None
    if early["n"] >= 1:
        early_words = (f"Your early sells have cost about ${cost:.2f} against holding ({trades_words(early['n'])})."
                       if cost > EPS else
                       f"Your early sells have made ${-cost:.2f} more than holding ({trades_words(early['n'])}).")
    if rule is not None:
        details = [f"{rule} holds to settlement." + (f" Your BTC stop is at ${stop:,.0f}." if stop is not None
                                                     else " No BTC stop is set on this position.")]
        if early_words:
            details.append(early_words)
        return {"kind": "hold", "tone": "agent", "rule": rule,
                "text": "The rule says hold. " + ("Your stop is set." if stop is not None else "No stop is set."),
                "details": details}
    if early["n"] >= MIN_N and cost > EPS:
        return {"kind": "hold", "tone": "agent", "rule": None, "text": "Holding has beaten your early sells.",
                "details": [early_words, "Play's own advice above is unchanged."]}
    return None


# -- the service ----------------------------------------------------------------------------------------------------------
class Copilot:
    def __init__(self, play: PlayService, book: RuleBook, record: YourRecord) -> None:
        self.play, self.book, self.record = play, book, record
        self.repo = book.repo
        self._rules: tuple[int, dict, dict] | None = None  # (read at, {rule: live tally}, {bracket id: rule})
        self._yours: dict[str, tuple[int, dict]] = {}  # mode -> (read at, your_record stats)

    def _rule_records(self, now: int) -> tuple[dict, dict]:
        if self._rules is None or now - self._rules[0] >= CACHE_MS:
            trades = self.book.trades.all()
            recs = {rid: tally([t for t in trades if t["rule"] == rid]) for rid in RULES}
            self._rules = (now, recs, {t["bracket_id"]: t["rule"] for t in trades})
        return self._rules[1], self._rules[2]

    def your_record(self, mode: str, now: int) -> dict:
        hit = self._yours.get(mode)
        if hit is None or now - hit[0] >= CACHE_MS:
            hit = self._yours[mode] = (now, self.record.stats(mode))
        return hit[1]

    def _entry_rule(self, bracket_id: str, links: dict) -> str | None:
        rows = self.repo.select("trading_audit", {"event": SHOWN, "bracket_id": bracket_id}, limit=1)
        filled = ((rows[0].get("detail") or {}).get("filled") or {}) if rows else {}
        return filled.get("rule") if filled.get("rule") in RULES else links.get(bracket_id)

    def view(self, now: int, venue: str, span: str, *, mode: str, warnings: Iterable[str],
             stake: float | None = None) -> dict:
        """What Ask Settleview shows on this Play tab now (see the module doc)."""
        check_mode(mode)
        if mode == "off":
            raise ValueError("Ask Settleview is off: nothing is computed")
        enabled = check_warnings(warnings)
        venue_spec(venue)  # an unknown venue or span is an error, never a default
        if span not in {m.span for m in MARKETS}:
            raise UnknownMarket(f"unknown span {span!r}")
        try:
            market: str | None = by_venue_span(venue, span).key
        except UnknownMarket:  # a real pair with no market (Kalshi 5m / 1h): unavailable, as Play answers it
            market = None
        v = self.play.now_view(now, venue, span=span, stake=stake)
        available = market is not None and bool(v.get("available"))
        out = {"mode": mode, "warnings_on": sorted(enabled), "market": market, "available": available,
               "reason": None if available else (v.get("reason") or f"{venue} has no {span} market here."),
               "window_end": (v.get("window") or {}).get("end"), "ticker": (v.get("window") or {}).get("ticker"),
               "lights": [], "line": {"none": None, "UP": None, "DOWN": None}, "warnings": {"UP": None, "DOWN": None},
               "fill": None, "hold": None, "btc": None}
        if not available:
            return out
        b = v.get("btc") or {}
        out["btc"] = {"btc": b.get("btc"), "strike": b.get("strike"), "gap": b.get("gap"),
                      "left_ms": int(v["window"]["end"]) - now}
        yours = self.your_record((v.get("mode") or {})["mode"], now)
        warns = {s: warning(s, v, now, yours, enabled) for s in ("UP", "DOWN")}
        out["warnings"] = warns
        lights: list[dict] = []
        links: dict = {}
        if mode == "full":
            recs, links = self._rule_records(now)
            lights = setup_lights(market, v, self.book.statuses(), recs, BACKTEST)
            green = next((x for x in lights if x["state"] == GREEN), None)
            if green is not None:
                out["fill"] = {"rule": green["rule"], **green["match"]}
        out["lights"] = lights
        ctx = context(v, lights, yours) if mode == "full" else None
        out["line"] = {"none": pick_line(mode, lights, None, ctx),
                       "UP": pick_line(mode, lights, warns["UP"], ctx),
                       "DOWN": pick_line(mode, lights, warns["DOWN"], ctx)}
        positions = v.get("positions") or []
        if mode == "full" and positions:
            p = positions[0]
            out["hold"] = hold_note(p, self._entry_rule(p["id"], links), yours)
        return out

    # -- what was shown with an order -----------------------------------------------------------------------------
    def record_shown(self, strategy: str, bracket_id: str, shown: dict, now: int) -> dict:
        check_mode(shown["mode"])
        if shown["mode"] == "off":
            raise ValueError("Ask Settleview was off: nothing was shown")
        check_warnings((shown.get("warning") or {}).get("keys") or [])
        for x in shown.get("lights") or []:
            if x["rule"] not in RULES or x["state"] not in STATES:
                raise ValueError(f"unknown light {x!r}")
        filled = shown.get("filled")
        if filled is not None and filled.get("rule") not in RULES:
            raise ValueError(f"unknown filled-in rule {filled.get('rule')!r}")
        rows = self.repo.select(BRACKETS, {"id": bracket_id, "strategy": strategy}, limit=1)
        if not rows:
            return {"ok": False, "code": "unknown_bracket",
                    "reason": f"No {strategy!r} order {bracket_id!r} to attach what was shown to."}
        if self.repo.select("trading_audit", {"event": SHOWN, "bracket_id": bracket_id}, limit=1):
            return {"ok": True, "repeat": True}
        line = shown.get("line") or {}
        self.repo.insert("trading_audit", [{
            "ts": now, "event": SHOWN, "env": rows[0]["env"], "bracket_id": bracket_id, "strategy": strategy,
            "code": line.get("kind"), "reason": (line.get("text") or "")[:500] or None, "detail": shown,
            "actor": actor()}], ignore_conflicts=False)
        return {"ok": True, "repeat": False}


