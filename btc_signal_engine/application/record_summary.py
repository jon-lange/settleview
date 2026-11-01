"""Your record over a span of local days: the numbers behind the Telegram daily summary (application/notifier.py) and
the web app's Record tab (GET /api/record/summary). One computation, used by both.

Trades are the closed, filled brackets of every Play strategy (yours on the slide and every agent session's), counted
on the day they closed: proceeds - entry cost - take-profit and exit fees, to the $0.0001 (a win is a net above $0).
A rule's trades are the brackets a rule entry opened (a "rule" journal row that opened it, or an ENTRY decision naming
it: an approved assist proposal). "Agent" = brackets with a session id, "you" = without. API = the agent sessions
started in the span (their usage.api_usd and wakes): every session, however many ran at once. "By agent" splits the
agent part per agent (a lasting identity, every run of it): its trades closed in the span, the API of its runs started
in the span, and the net after that API. Perps are not here (their own record, from Kalshi's fills, is on
the Perps screen), exactly as in the daily summary.

Days are local: `tz` is the user's day zone (domain/days.py; the app resolves it once at startup and every "today"
uses it), and the summary names it (`day_tz`). Ranges: "today" (since local midnight), "week" (today and the six
local days before it), "all" (everything recorded).

"All" reads its rule links from a `RuleLinks` (the Record route keeps one): the journal's "rule" and "decision" rows
read once, then only the new ones each time (ix_agent_journal_kind), instead of the whole journal on every request."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from datetime import datetime, tzinfo

from btc_signal_engine.application.agent_sessions import AGENTS, SESSIONS, VENUE_LABELS, VENUES, AgentSessions
from btc_signal_engine.application.ports import Repository
from btc_signal_engine.application.rule_scores import BRACKETS, JOURNAL
from btc_signal_engine.domain import brackets, days

DAY_MS = 86_400_000
RANGES = ("today", "week", "all")
WEEK_DAYS = 7
UTC_ZONE = days.zone(None)  # the stated default zone; the app passes the one it resolved at startup (bootstrap)
PLAY_STRATEGIES = tuple(dict.fromkeys(VENUES.values()))  # every Play strategy an agent or you trade (you, you_poly...)


net4 = brackets.net  # a bracket's net after every fee, to the $0.0001 (domain/brackets.py: the one rule)


def closed_at(b: dict) -> int:
    return int(b.get("closed_ts") or b.get("exit_ts") or b.get("updated_ts") or 0)


def local(ms: int, tz: tzinfo) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz)


def local_day_start(now: int, tz: tzinfo, days_back: int = 0) -> int:
    """Local midnight of today in `tz`, or of `days_back` local days before today (domain/days.py: by date, so a
    daylight-saving change never moves it an hour)."""
    return days.day_start(now, tz, days_back)


def range_start(now: int, range_: str, tz: tzinfo) -> int:
    if range_ not in RANGES:
        raise ValueError(f"unknown range {range_!r} (known: {', '.join(RANGES)})")
    if range_ == "today":
        return local_day_start(now, tz)
    if range_ == "week":
        return local_day_start(now, tz, WEEK_DAYS - 1)
    return 0


def rule_links(repo: Repository, *, session_id: str | None = None, since: int | None = None) -> dict[str, dict]:
    """bracket id -> {rule, market, inputs} for rule entries (the "rule" rows that opened one, and the ENTRY
    decisions that name their bracket: an approved assist proposal)."""
    where: dict = {"session_id": session_id} if session_id else {}
    filters: list = [("kind", "in", ("rule", "decision"))]
    if since is not None:
        filters.append(("ts", ">=", since))
    out: dict[str, dict] = {}
    for r in repo.select(JOURNAL, where, filters=filters, order_by=["id"]):
        _fold(out, r)
    return out


class RuleLinks:
    """`rule_links` over the whole journal, read incrementally: the rule and decision rows past the last id read
    (re-reading the last OVERLAP ids, so a row whose insert committed after a higher id was read is still seen),
    folded in id order exactly as `rule_links` folds them. Safe from several worker threads."""

    OVERLAP = 200

    def __init__(self, repo: Repository) -> None:
        self.repo = repo
        self._links: dict[str, dict] = {}
        self._after = 0
        self._lock = threading.Lock()

    def all(self) -> dict[str, dict]:
        with self._lock:
            rows = self.repo.select(JOURNAL, filters=[("kind", "in", ("rule", "decision")),
                                                      ("id", ">", max(0, self._after - self.OVERLAP))],
                                    order_by=["id"])
            for r in rows:
                self._after = max(self._after, int(r["id"]))
                _fold(self._links, r)
            return dict(self._links)


def _fold(out: dict[str, dict], r: dict) -> None:
    """One journal row into the links (`rule_links`' rule: a rule entry wins, an ENTRY decision only fills in)."""
    d = r.get("data") or {}
    bid = d.get("bracket_id")
    if not bid or not d.get("rule"):
        return
    if r["kind"] == "rule" and d.get("ok"):
        out[bid] = {"rule": d["rule"], "market": d.get("market"), "inputs": d.get("inputs") or {}}
    elif r["kind"] == "decision" and d.get("action") == "ENTRY":
        out.setdefault(bid, {"rule": d["rule"], "market": d.get("market"), "inputs": {}})


def closed_stats(rows: Iterable[dict], links: dict[str, dict]) -> dict:
    """Closed, filled trades among `rows`: count, wins (net > 0), net, and per rule {rule: (n, wins, net)}."""
    closed = [b for b in rows if b.get("state") == "closed" and float(b.get("filled") or 0) > 0]
    by_rule: dict[str, list] = {}
    for b in closed:
        r = (links.get(b["id"]) or {}).get("rule")
        if r:
            e = by_rule.setdefault(r, [0, 0, 0.0])
            e[0] += 1
            e[1] += 1 if net4(b) > 0 else 0
            e[2] += net4(b)
    return {"trades": len(closed), "wins": sum(1 for b in closed if net4(b) > 0),
            "net": round(sum(net4(b) for b in closed), 2), "closed": closed,
            "by_rule": {r: (n, w, round(x, 2)) for r, (n, w, x) in sorted(by_rule.items())}}


def closed_between(repo: Repository, start: int, now: int, strategies: Iterable[str] = PLAY_STRATEGIES, *,
                   all_links: RuleLinks | None = None) -> dict:
    """The trades closed in [start, now] over every agent session and your own Play trades, with their rule links
    (journal rows from a day before `start`, so an entry opened before midnight still names its rule; from start 0,
    `all_links` when given, else every row)."""
    rows = [b for b in repo.select(BRACKETS, filters=[("strategy", "in", list(strategies)),
                                                      ("updated_ts", ">=", start)])
            if start <= closed_at(b) <= now]
    if start <= 0 and all_links is not None:
        links = all_links.all()
    else:
        links = rule_links(repo, since=start - DAY_MS if start > 0 else None)
    return {**closed_stats(rows, links), "links": links, "start": start}


def sessions_between(repo: Repository, start: int, now: int) -> list[dict]:
    return repo.select(SESSIONS, filters=[("started_ts", ">=", start), ("started_ts", "<=", now)])


def _part(closed: list[dict]) -> dict:
    return {"trades": len(closed), "wins": sum(1 for b in closed if net4(b) > 0),
            "net": round(sum(net4(b) for b in closed), 2)}


def by_agent(repo: Repository, closed: list[dict], sessions: list[dict]) -> list[dict]:
    """Per agent: {agent_id, name, market, market_label, archived, runs, trades, wins, net, api_usd, net_after_api},
    best net after API first. `closed`: the span's closed trades (an agent's = those of its sessions); `sessions`: the
    sessions started in the span (their API). A session without an agent (none since migration 0016) is its own
    line, named as the session was."""
    sids = {b["session_id"] for b in closed if b.get("session_id")} | {s["id"] for s in sessions}
    rows = {s["id"]: s for s in repo.select(SESSIONS, filters=[("id", "in", list(sids))])} if sids else {}
    agents = {a["id"]: a for a in repo.select(AGENTS)} if rows else {}
    out: dict[str, dict] = {}

    def line(sid: str) -> dict:
        s = rows.get(sid) or {"id": sid, "limits": {}}
        aid = s.get("agent_id") or f"session:{sid}"
        if aid not in out:
            a = agents.get(s.get("agent_id") or "")
            market = a["market"] if a else AgentSessions.market_key(s)
            out[aid] = {"agent_id": s.get("agent_id"), "name": a["name"] if a else AgentSessions.name_of(s),
                        "market": market, "market_label": VENUE_LABELS.get(market or "", market),
                        "archived": bool(a["archived"]) if a else False, "runs": set(), "trades": 0, "wins": 0,
                        "net": 0.0, "api_usd": 0.0}
        out[aid]["runs"].add(sid)
        return out[aid]

    for b in closed:
        if b.get("session_id"):
            e = line(b["session_id"])
            e["trades"] += 1
            e["wins"] += 1 if net4(b) > 0 else 0
            e["net"] += net4(b)
    for s in sessions:
        line(s["id"])["api_usd"] += float((s.get("usage") or {}).get("api_usd") or 0.0)
    res = [{**e, "runs": len(e["runs"]), "net": round(e["net"], 2), "api_usd": round(e["api_usd"], 4),
            "net_after_api": round(e["net"] - e["api_usd"], 2)} for e in out.values()]
    return sorted(res, key=lambda x: (-x["net_after_api"], x["name"]))


def summary(repo: Repository, now: int, range_: str, *, tz: tzinfo = UTC_ZONE,
            strategies: Iterable[str] = PLAY_STRATEGIES, all_links: RuleLinks | None = None) -> dict:
    """The Record tab's card: net, trades and wins over the range; the split you / agent; the API cost of the agent
    sessions started in it; per agent (`by_agent`); and per rule (every rule that traded, plus each rule a session in the range ran with and
    that did not trade, as 0 trades)."""
    start = range_start(now, range_, tz)
    day = closed_between(repo, start, now, strategies, all_links=all_links)
    sessions = sessions_between(repo, start, now)
    closed = day["closed"]
    rules = {r: {"rule": r, "trades": n, "wins": w, "net": x} for r, (n, w, x) in day["by_rule"].items()}
    for s in sessions:
        for r in ((s.get("limits") or {}).get("rules") or []):
            rules.setdefault(r, {"rule": r, "trades": 0, "wins": 0, "net": 0.0})
    usage = [s.get("usage") or {} for s in sessions]
    return {"ok": True, "range": range_, "start_ms": start, "now_ms": now, "day_tz": days.zone_name(tz),
            "net": day["net"], "trades": day["trades"], "wins": day["wins"],
            "you": _part([b for b in closed if not b.get("session_id")]),
            "agent": _part([b for b in closed if b.get("session_id")]),
            "api": {"usd": round(sum(float(u.get("api_usd") or 0.0) for u in usage), 4),
                    "wakes": sum(int(u.get("wakes") or 0) for u in usage), "sessions": len(sessions)},
            "by_rule": sorted(rules.values(), key=lambda x: (-x["trades"], x["rule"])),
            "by_agent": by_agent(repo, closed, sessions)}
