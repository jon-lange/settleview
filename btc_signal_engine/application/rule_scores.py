"""The rule scorekeeper and code-enforced retirement (2026-10-30; the HyperGrok / 9-bot desk lessons: a scorekeeper in
code, retirement rules in code so a retired rule cannot be armed, process-vs-outcome review).

Tally (pure, `tally`): per (rule, market) across ALL agent sessions, from the journal's "rule" rows that opened an
entry (ok, with a bracket id; a repeat names the same bracket and counts once) joined to those trading_brackets rows.
Only closed, filled brackets are trades; a settling one (window closed, official result pending) and an open one are
counted apart as `pending` / `open`, never as a win or a loss; an unfilled one is no trade. Per trade: net = proceeds
(incl. a settlement's $1 a contract) - entry_cost (incl. the entry fee) - tp_fees - exit_fees; a win is net > 0.
`avg_price` = sum entry_cost / sum filled (the price paid per contract incl. fees) = the hold-to-settlement break-even
win rate (`break_even_pct`); net_c = net / contracts in cents; max_drawdown_usd = the deepest fall of the cumulative
net from its running peak (from $0), in close order; last_trade_ts = the latest entry (any state).

Backtest reference (`BACKTEST`, from docs/research/LEARNINGS.md rows, Kalshi 15m): the break-even % and the holdout
net c/contract of the variant each rule runs, shown beside the live numbers.

Status (`RuleBook`): per rule id, persisted in strategy_settings (strategy "agent", key "rule_status"): {status:
active | retired, reason, at, by: user | auto, since (the last reactivation, else None), history}. A rule with no
stored entry takes its seed (`SEEDED`: HPANIC retired by the user on 2026-10-30, "0/3 live, killed in backtest"),
else active. Launch refuses a retired rule (code rule_retired); a running session's runner skips it from its next
tick (no new entries; open positions keep their code exits). Auto-retire (`enforce`): an active rule whose trial
tally (all its markets; trades closed after its last reactivation, else all) has >= AUTO_TRADES closed trades with
net <= $0, or net <= AUTO_LOSS, is retired by "auto" when `enforce` first sees it (the runner calls it on each close
of one of its brackets, at attach and every minute; launch calls it first). Reactivation (the user, from the PC, with
the typed phrase "REACTIVATE <rule>") starts a fresh trial: earlier trades stay in the lifetime tally but not in the
30-trade bar or the auto checks.
"""

from __future__ import annotations

from collections.abc import Iterable

from btc_signal_engine.application.agent_rules import RULES
from btc_signal_engine.application.ports import Repository
from btc_signal_engine.application.settings_store import get_setting, put_setting
from btc_signal_engine.domain import brackets
from btc_signal_engine.domain.fmt import usd, usd_short
from btc_signal_engine.domain.markets import market as market_spec

BRACKETS = "trading_brackets"
JOURNAL = "agent_journal"
PROPOSAL_KIND = "proposal"  # an assist proposal's journal row (agent_proposals), its data updated as it resolves
OPEN_STATES = ("sending", "unknown", "open", "stopping")
SETTLING = "settling"  # window closed holding, official result pending (application/trading.py "Settlement")
SETTING = {"strategy": "agent", "key": "rule_status"}
STATUSES = ("active", "retired")
BY = ("user", "auto")
AUTO_TRADES = 30
AUTO_NET = 0.0  # retire at AUTO_TRADES closed trades when net <= this
AUTO_LOSS = -5.0  # retire at once when net <= this
HISTORY_KEPT = 20
REACTIVATE = "REACTIVATE"  # the typed phrase is "REACTIVATE <rule id>"
HPANIC_RETIRED_TS = 1_793_409_415_000  # 2026-10-30 20:16:55 CDT, the LEARNINGS commit that records it
SEEDED: dict[str, dict] = {
    "HPANIC": {"status": "retired", "reason": "0/3 live, killed in backtest", "at": HPANIC_RETIRED_TS, "by": "user",
               "since": None},
}

# docs/research/LEARNINGS.md (Kalshi 15m backtests; minute resolution, 1c slippage, holdout = the last 10 days).
BACKTEST: dict[str, dict] = {
    "H4": {"row": "H4", "n": 10, "won_pct": 100.0, "break_even_pct": 94.6, "net_c": 5.4,
           "note": "holdout n=10 (fails n>=30); Oct 29-30 forward check 11/12 vs 91.2% break-even, +0.45c"},
    "CUSHION100": {"row": "EXP-CUSHION100", "n": None, "won_pct": None, "break_even_pct": 78.0, "net_c": None,
                   "note": "no backtest of this rule: the user's real trades 9/9 vs ~78% break-even (anecdote); the "
                           "$100-cushion PNW backtest (TP/stop exits) n=89 -4.7c"},
    "HPANIC": {"row": "H-PANIC", "n": 285, "won_pct": 16.1, "break_even_pct": 20.5, "net_c": -4.09,
               "note": "variant 35c / <= 4 min, holdout t -1.91: KILLED"},
    "LATERESID": {"row": "LATE_RESIDUAL", "n": 215, "won_pct": 25.1, "break_even_pct": 24.9, "net_c": 0.14,
                  "note": "variant 4 min / 5c / 65c, holdout t +0.05 (3 min: n=229, -0.29c): KILLED"},
}


# -- the tally (pure) -------------------------------------------------------------------------------------------------
def trade_of(link: dict, b: dict) -> dict:
    """One rule entry's bracket as a trade: {rule, market, bracket_id, state (closed | settling | open), filled, cost,
    net (closed only), created_ts, closed_ts}."""
    filled = float(b.get("filled") or 0.0)
    st = b.get("state")
    state = "open" if st in OPEN_STATES else "settling" if st == SETTLING else "closed"
    cost = float(b.get("entry_cost") or 0.0)
    net = brackets.net(b) if state == "closed" and filled > 0 else None
    return {"rule": link["rule"], "market": link["market"], "bracket_id": b.get("id"), "state": state,
            "filled": filled, "cost": cost, "net": net, "created_ts": b.get("created_ts"),
            "closed_ts": b.get("closed_ts") or b.get("exit_ts") or b.get("updated_ts")}


def tally(trades: Iterable[dict], since: int | None = None) -> dict:
    """The score of these trades (only those closed after `since` when given; pending / open ones always counted)."""
    closed, pending, opened, last = [], 0, 0, None
    for t in trades:
        if t["filled"] <= 0:
            continue
        if t["created_ts"] is not None:
            last = t["created_ts"] if last is None else max(last, t["created_ts"])
        if t["state"] == "settling":
            pending += 1
        elif t["state"] == "open":
            opened += 1
        elif since is None or (t["closed_ts"] or 0) > since:
            closed.append(t)
    n = len(closed)
    contracts = round(sum(t["filled"] for t in closed), 4)
    cost = sum(t["cost"] for t in closed)
    net = round(sum(t["net"] for t in closed), 4)
    wins = sum(1 for t in closed if t["net"] > 0)
    peak = cum = dd = 0.0
    for t in sorted(closed, key=lambda t: (t["closed_ts"] or 0, t["created_ts"] or 0)):
        cum += t["net"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    avg = round(cost / contracts, 4) if contracts > 0 else None
    return {"trades": n, "wins": wins, "win_pct": round(wins / n * 100, 2) if n else None, "contracts": contracts,
            "avg_price": avg, "break_even_pct": round(cost / contracts * 100, 2) if contracts > 0 else None,
            "net_usd": net, "net_c": round(net / contracts * 100, 2) if contracts > 0 else None,
            "max_drawdown_usd": round(dd, 4), "last_trade_ts": last, "pending": pending, "open": opened}


def auto_reason(t: dict) -> str | None:
    """Why this trial tally retires its rule, or None."""
    if t["net_usd"] <= AUTO_LOSS + 1e-9:
        return (f"Retired automatically: net {usd(t['net_usd'])} reached the {usd_short(AUTO_LOSS)} limit "
                f"({t['trades']} closed trades).")
    if t["trades"] >= AUTO_TRADES and t["net_usd"] <= AUTO_NET + 1e-9:
        return (f"Retired automatically: {t['trades']} closed trades (the {AUTO_TRADES}-trade bar) with net "
                f"{usd(t['net_usd'])} <= $0.")
    return None


# -- the repository read --------------------------------------------------------------------------------------------
class RuleTrades:
    """The rule entries' brackets, read incrementally: journal rule rows are scanned once (by id), brackets re-read."""

    def __init__(self, repo: Repository) -> None:
        self.repo = repo
        self._links: dict[str, dict] = {}  # bracket id -> {rule, market}
        self._after = 0

    def all(self) -> list[dict]:
        for row in self.repo.select(JOURNAL, {"kind": "rule"}, filters=[("id", ">", self._after)],
                                    order_by=["id"]):
            self._after = max(self._after, int(row["id"]))
            d = row.get("data") or {}
            if d.get("ok") and d.get("bracket_id") and d.get("rule") in RULES:
                self._links.setdefault(d["bracket_id"], {"rule": d["rule"], "market": d.get("market")})
        if not self._links:
            return []
        rows = self.repo.select(BRACKETS, filters=[("id", "in", list(self._links))])
        return [trade_of(self._links[b["id"]], b) for b in rows]


# -- the status ---------------------------------------------------------------------------------------------------------
class RuleBook:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo
        self.trades = RuleTrades(repo)
        self._value: dict | None = None  # the stored value, cached (this object writes it)
        self._checked_at: int | None = None  # the last `enforce` tally (one book for the whole fleet)

    def _stored(self) -> dict:
        if self._value is None:
            v = get_setting(self.repo, SETTING, {})
            self._value = dict(v) if isinstance(v, dict) else {}
        return self._value

    def status(self, rule_id: str) -> dict:
        if rule_id not in RULES:
            raise ValueError(f"unknown rule {rule_id!r}; rules are {', '.join(RULES)}")
        e = self._stored().get(rule_id) or SEEDED.get(rule_id)
        if not isinstance(e, dict) or e.get("status") not in STATUSES:
            e = {"status": "active", "reason": None, "at": None, "by": None, "since": None}
        return {k: e.get(k) for k in ("status", "reason", "at", "by", "since")}

    def statuses(self) -> dict[str, dict]:
        return {r: self.status(r) for r in RULES}

    def retired(self, rule_id: str) -> bool:
        return self.status(rule_id)["status"] == "retired"

    def seed(self, now: int) -> None:
        """Persist the seeds of rules with no stored entry (start-up)."""
        v = self._stored()
        missing = {r: {**e, "history": [{k: e[k] for k in ("status", "reason", "at", "by")}]}
                   for r, e in SEEDED.items() if r not in v}
        if missing:
            self._write({**v, **missing}, now)

    def set(self, rule_id: str, status: str, now: int, *, by: str, reason: str) -> dict:
        """Store a rule's new status; returns it. Reactivation stamps `since` (a fresh trial)."""
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)} (got {status!r})")
        if by not in BY:
            raise ValueError(f"by must be one of {', '.join(BY)} (got {by!r})")
        cur = self.status(rule_id)
        old = self._stored().get(rule_id) or {}
        since = now if status == "active" else cur["since"]
        entry = {"status": status, "reason": reason, "at": now, "by": by, "since": since,
                 "history": [*(old.get("history") or []),
                             {"status": status, "reason": reason, "at": now, "by": by}][-HISTORY_KEPT:]}
        self._write({**self._stored(), rule_id: entry}, now)
        return self.status(rule_id)

    def _write(self, value: dict, now: int) -> None:
        put_setting(self.repo, SETTING, value, now)
        self._value = value

    def enforce(self, now: int, *, closed: bool = False, every_ms: int | None = None) -> list[dict] | None:
        """Retire (by auto) each active rule whose trial tally crossed a bar. Returns the changes: [{rule, status,
        reason, at, by, since, tally}]. With `every_ms` (the runners: every session of the fleet asks every tick it
        saw a close, and every minute) the tally runs at most once per tick (`now`) for a close and once per
        `every_ms` otherwise, whoever asks first; a skipped call returns None (the caller asks again later).
        Without it (launch, the PC) it always runs."""
        if every_ms is not None and self._checked_at is not None:
            fresh = now - self._checked_at < every_ms
            if (closed and now == self._checked_at) or (not closed and fresh):
                return None
        self._checked_at = now
        trades = self.trades.all()

        out = []
        for rid in RULES:
            st = self.status(rid)
            if st["status"] != "active":
                continue
            t = tally([x for x in trades if x["rule"] == rid], since=st["since"])
            why = auto_reason(t)
            if why is not None:
                out.append({"rule": rid, **self.set(rid, "retired", now, by="auto", reason=why), "tally": t})
        return out


def proposal_tally(props: Iterable[dict]) -> dict:
    """Assist proposals (agent_proposals rows' data): how many were approved / skipped / expired / refused / still
    open, and for the missed ones (skipped or expired) what they would have made or lost at settlement: `missed_pnl_usd`
    sums the scored ones only (`missed_scored`, `missed_won`), `missed_unscored` = results not in (or not readable)."""
    out = {"proposed": 0, "approved": 0, "skipped": 0, "expired": 0, "refused": 0, "open": 0, "missed_scored": 0,
           "missed_won": 0, "missed_pnl_usd": 0.0, "missed_unscored": 0}
    for p in props:
        out["proposed"] += 1
        st = p.get("state")
        if st in ("approved", "skipped", "expired", "refused"):
            out[st] += 1
        else:
            out["open"] += 1
        if st in ("skipped", "expired"):
            w = p.get("would") or {}
            if w.get("state") == "scored":
                out["missed_scored"] += 1
                out["missed_won"] += 1 if w.get("won") else 0
                out["missed_pnl_usd"] += float(w.get("pnl") or 0.0)
            else:
                out["missed_unscored"] += 1
    out["missed_pnl_usd"] = round(out["missed_pnl_usd"], 4)
    return out


def scorecard(repo: Repository, book: RuleBook) -> dict:
    """The panel's Scorecard: per rule {rule, status, live (all markets), trial (since reactivation), bar, backtest,
    proposals (assist: `proposal_tally`)} and per (rule, market) {rule, market, market_label, status, live, backtest,
    proposals}."""
    trades = book.trades.all() if book.repo is repo else RuleTrades(repo).all()
    props = [r.get("data") or {} for r in repo.select(JOURNAL, {"kind": PROPOSAL_KIND})]
    rules, rows = [], []
    for rid, rule in RULES.items():
        st = book.status(rid)
        mine = [t for t in trades if t["rule"] == rid]
        trial = tally(mine, since=st["since"])
        my_props = [p for p in props if p.get("rule") == rid]
        rules.append({"rule": rid, "label": rule.label, "status": st, "live": tally(mine), "trial": trial,
                      "bar": {"trades": min(trial["trades"], AUTO_TRADES), "of": AUTO_TRADES},
                      "backtest": BACKTEST.get(rid), "proposals": proposal_tally(my_props)})
        for m in rule.markets:
            rows.append({"rule": rid, "market": m, "market_label": market_spec(m).label, "status": st,
                         "live": tally([t for t in mine if t["market"] == m]), "backtest": BACKTEST.get(rid),
                         "proposals": proposal_tally([p for p in my_props if p.get("market") == m])})
    return {"rules": rules, "rows": rows, "auto": {"trades": AUTO_TRADES, "net_at_or_below": AUTO_NET,
                                                   "loss": AUTO_LOSS}}
