"""The fleet's read model (multi-agent U5, the approved wireframe): one row per agent session and the fleet summary.

Row ({id, agent_id, name, market, market_label, kind, level, state, chip, line, net, open, trades, wins, budget, api, ends_ts,
seconds_left, started_ts, ended_ts, end_reason, pause_code, pause_reason, rules, needs_you, also_traded_by, wakes,
spark, holding}):
- `chip` is the state word the list shows: "Proposal" (an assist proposal waits for you), "Paused", "Holding",
  "Watching", "Ended"; `line` the sentence after it ("Holding Up $1.00 · LATERESID · 5h left", "Watching · CUSHION100,
  H4 · 5h left", "Watching · model judgment (no rules) · 2h left", "Paused · <reason>").
- `net` = realized net of the session's closed trades (gains positive), `open` = {positions, at_risk} its open or
  settling positions and their worst case; `trades` / `wins` its closed, filled trades; `spark` its cumulative net
  after each closed trade (oldest first, the last SPARK_POINTS).
- `budget` {total, used, left, pct}; `api` {used (estimated), cap, pct} (cap None for an external session).
- `needs_you`: [{code, text}] with codes proposal (one waits), paused, budget_near (>= NEAR of the budget used),
  api_near (>= NEAR of the API cap), overlap (holds the opposite side of a window another agent holds). Rows that
  need you sort first.
- `holding`: [{bracket_id, side ("UP" / "DOWN"), cost, rule (the rule entry that opened it, else None)}] its open
  or settling filled positions, oldest first (Play's "2 agents here · one holds Up $1 (CUSHION100)").
- `wakes`: the wake queue's view of this session (agent_fleet.WakeScheduler.metrics), None without the fleet.
Summary ({count, running, paused, net, winning, losing, open, budget, api, today, needs_you, by_market, best, worst,
queue, global_limits}): totals over the live sessions; `today` = every agent session's closed trades this local day
(net, trades, wins); `needs_you` [{id, name, reasons}] the rows that need you; `by_market` [{market, market_label,
count, net}] in market order; `best` / `worst` {id, name, net} (None without trades).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from btc_signal_engine.application.agent_sessions import (LIVE, SESSIONS, VENUE_LABELS, VENUES, AgentSessions,
                                                          _parts)
from btc_signal_engine.application.rule_scores import BRACKETS, JOURNAL, OPEN_STATES, SETTLING

NEAR = 0.9
SPARK_POINTS = 30
CHIPS = ("Proposal", "Paused", "Holding", "Watching", "Ended")


def _net(b: dict) -> float:
    return round(float(b.get("proceeds") or 0.0) - float(b.get("entry_cost") or 0.0) - float(b.get("tp_fees") or 0.0)
                 - float(b.get("exit_fees") or 0.0), 4)


def _closed(b: dict) -> bool:
    return b.get("state") == "closed" and float(b.get("filled") or 0) > 0


def _held(b: dict) -> bool:
    """An open or settling position that bought something."""
    return b.get("state") in (*OPEN_STATES, SETTLING) and float(b.get("filled") or 0) > 0


def _left(seconds: int) -> str:
    mins = max(0, int(seconds)) // 60
    return f"{mins // 60}h left" if mins >= 60 else f"{mins}m left"


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _pct(used: float, total: float | None) -> float | None:
    return round(100.0 * used / total, 1) if total else None


class FleetView:
    def __init__(self, sessions: AgentSessions, *, proposals: Any | None = None, fleet: Any | None = None) -> None:
        self.sessions, self.proposals, self.fleet = sessions, proposals, fleet

    def _brackets(self, ids: Iterable[str]) -> dict[str, list[dict]]:
        ids = list(ids)
        out: dict[str, list[dict]] = {i: [] for i in ids}
        if not ids:
            return out
        for b in self.sessions.repo.select(BRACKETS, filters=[("session_id", "in", ids)], order_by=["created_ts"]):
            out.setdefault(b["session_id"], []).append(b)
        return out

    def rows(self, now: int, *, include_ended: int = 0) -> list[dict]:
        """Every live session's row ("needs you" first, then by launch), plus the `include_ended` most recently
        ended ones (last)."""
        live = self.sessions.live()
        ended = []
        if include_ended > 0:
            ended = self.sessions.repo.select(SESSIONS, filters=[("state", "=", "ended")],
                                              order_by=["-started_ts"], limit=include_ended)
        rows = [*live, *ended]
        brackets = self._brackets(s["id"] for s in rows)
        overlap_ids = {x["session_id"] for w in self.sessions.overlaps() for side in ("UP", "DOWN")
                       for x in w["sides"][side]}
        held = [sid for sid, bs in brackets.items() if any(_held(b) for b in bs)]
        rules = self._rules_of(held)  # one journal read for every session that holds something
        out = [self.row(s, now, brackets.get(s["id"], []), overlap=s["id"] in overlap_ids,
                        rules_of=rules.get(s["id"], {})) for s in rows]
        live_rows = [r for r in out if r["state"] in LIVE]
        live_rows.sort(key=lambda r: (not r["needs_you"], r["started_ts"], r["id"]))
        return live_rows + [r for r in out if r["state"] not in LIVE]

    def _rules_of(self, session_ids: list[str]) -> dict[str, dict[str, str]]:
        """session id -> {bracket id: the rule entry that opened it} (its "rule" journal row, or an ENTRY decision),
        for these sessions in one query (ix_agent_journal_session)."""
        out: dict[str, dict[str, str]] = {sid: {} for sid in session_ids}
        if not session_ids:
            return out
        for j in self.sessions.repo.select(JOURNAL, filters=[("session_id", "in", list(session_ids)),
                                                             ("kind", "in", ("rule", "decision"))]):
            d = j.get("data") or {}
            if d.get("bracket_id") and d.get("rule") and (d.get("ok") or d.get("action") == "ENTRY"):
                out.setdefault(j["session_id"], {}).setdefault(d["bracket_id"], d["rule"])
        return out

    def row(self, s: dict, now: int, brackets: list[dict] | None = None, *, overlap: bool = False,
            rules_of: dict[str, str] | None = None) -> dict:
        sid, lim = s["id"], s["limits"]
        if brackets is None:
            brackets = self.sessions.repo.select(BRACKETS, {"session_id": sid}, order_by=["created_ts"])
        market = self.sessions.market_key(s)
        closed = [b for b in brackets if _closed(b)]
        held = [b for b in brackets if _held(b)]
        parts = _parts(brackets)
        used = round(parts["realized"] + parts["at_risk"], 4)
        budget = float(lim["budget"])
        usage = s.get("usage") or {}
        api_used = round(float(usage.get("api_usd") or 0.0), 4)
        cap = lim.get("spend_cap")
        live = s["state"] in LIVE
        seconds_left = max(0, (s["ends_ts"] - now) // 1000) if live else 0
        pending = self.proposals.pending(sid) if (self.proposals is not None and live) else []
        needs = []
        if pending:
            needs.append({"code": "proposal", "text": f"Proposal: {pending[0].get('line') or 'an entry'} waits for "
                                                      "you."})
        if s["state"] == "paused":
            needs.append({"code": "paused", "text": f"Paused: {s.get('pause_reason') or 'no reason given'}"})
        if live and budget and used >= NEAR * budget:
            needs.append({"code": "budget_near", "text": f"{round(100 * used / budget)}% of its budget used."})
        if live and cap and api_used >= NEAR * float(cap):
            needs.append({"code": "api_near", "text": f"{round(100 * api_used / float(cap))}% of its API cap used."})
        if live and overlap:
            needs.append({"code": "overlap", "text": "Holds the opposite side of a window another agent holds."})
        rules = list(lim.get("rules") or [])
        what = ", ".join(rules) if rules else "model judgment (no rules)"
        if not live:
            chip, line = "Ended", f"Ended · {s.get('end_reason') or 'ended'}"
        elif pending:
            chip, line = "Proposal", f"Proposal · {pending[0].get('line') or 'an entry'} · {_left(seconds_left)}"
        elif s["state"] == "paused":
            chip, line = "Paused", f"Paused · {s.get('pause_reason') or 'no reason given'}"
        elif held:
            b = held[-1]
            side = "Up" if b.get("side") == "UP" else "Down"
            chip = "Holding"
            line = f"Holding {side} {_money(float(b.get('entry_cost') or 0.0))} · {what} · {_left(seconds_left)}"
        else:
            chip, line = "Watching", f"Watching · {what} · {_left(seconds_left)}"
        cum, spark = 0.0, []
        for b in sorted(closed, key=lambda b: (b.get("closed_ts") or b.get("updated_ts") or 0)):
            cum += _net(b)
            spark.append(round(cum, 4))
        net = round(sum(_net(b) for b in closed), 4)
        if rules_of is None:  # which rule entry opened each held position (`rows` reads them for every session)
            rules_of = self._rules_of([sid]).get(sid, {}) if held else {}
        holding = [{"bracket_id": b["id"], "side": b.get("side"), "cost": round(float(b.get("entry_cost") or 0.0), 4),
                    "rule": rules_of.get(b["id"])} for b in held]
        wakes = None
        if self.fleet is not None and s["kind"] == "in_app" and live:
            wakes = self.fleet.metrics(sid, now)
        return {"id": sid, "agent_id": s.get("agent_id"), "name": self.sessions.name_of(s), "market": market,
                "market_label": VENUE_LABELS.get(market or "", market), "kind": s["kind"],
                "level": lim.get("level"), "state": s["state"], "chip": chip, "line": line, "net": net,
                "open": {"positions": len(held), "at_risk": parts["at_risk"]},
                "trades": len(closed), "wins": sum(1 for b in closed if _net(b) > 0),
                "budget": {"total": budget, "used": used, "left": round(budget - used, 4), "pct": _pct(used, budget)},
                "api": {"used": api_used, "cap": cap, "pct": _pct(api_used, float(cap) if cap else None)},
                "ends_ts": s["ends_ts"], "seconds_left": seconds_left, "started_ts": s["started_ts"],
                "ended_ts": s.get("ended_ts"), "end_reason": s.get("end_reason"), "pause_code": s.get("pause_code"),
                "pause_reason": s.get("pause_reason"), "rules": rules, "needs_you": needs,
                "also_traded_by": self.sessions.also_traded_by(market, exclude=sid) if live else [],
                "wakes": wakes, "spark": spark[-SPARK_POINTS:], "holding": holding}

    def summary(self, now: int, rows: list[dict] | None = None) -> dict:
        rows = [r for r in (rows if rows is not None else self.rows(now)) if r["state"] in LIVE]
        by_market = []
        for key in VENUES:
            mine = [r for r in rows if r["market"] == key]
            if mine:
                by_market.append({"market": key, "market_label": VENUE_LABELS[key], "count": len(mine),
                                  "net": round(sum(r["net"] for r in mine), 4)})
        traded = [r for r in rows if r["trades"]]
        best = max(traded, key=lambda r: r["net"], default=None)
        worst = min(traded, key=lambda r: r["net"], default=None)
        caps = [r["api"]["cap"] for r in rows if r["api"]["cap"]]
        return {"count": len(rows), "running": sum(1 for r in rows if r["state"] == "running"),
                "paused": sum(1 for r in rows if r["state"] == "paused"),
                "net": round(sum(r["net"] for r in rows), 4),
                "winning": sum(1 for r in rows if r["net"] > 0), "losing": sum(1 for r in rows if r["net"] < 0),
                "open": {"positions": sum(r["open"]["positions"] for r in rows),
                         "at_risk": round(sum(r["open"]["at_risk"] for r in rows), 4)},
                "budget": {"used": round(sum(r["budget"]["used"] for r in rows), 4),
                           "total": round(sum(r["budget"]["total"] for r in rows), 2)},
                "api": {"used": round(sum(r["api"]["used"] for r in rows), 4),
                        "cap": round(sum(float(c) for c in caps), 2) if caps else None},
                "today": self.today(now),
                "needs_you": [{"id": r["id"], "name": r["name"], "reasons": r["needs_you"]} for r in rows
                              if r["needs_you"]],
                "by_market": by_market,
                "best": {"id": best["id"], "name": best["name"], "net": best["net"]} if best else None,
                "worst": {"id": worst["id"], "name": worst["name"], "net": worst["net"]} if worst else None,
                "queue": self.fleet.summary(now) if self.fleet is not None else None,
                "global_limits": {"limits": self.sessions.global_limits(), "used": self.sessions.global_usage(now)}}

    def today(self, now: int) -> dict:
        """Every agent session's trades closed today (local day, as Record): {net, trades, wins}."""
        start = self.sessions.day_start(now)
        rows = [b for b in self.sessions.repo.select(BRACKETS, filters=[("session_id", "is_null", False),
                                                                         ("updated_ts", ">=", start)])
                if _closed(b) and (b.get("closed_ts") or b.get("updated_ts") or 0) >= start]
        return {"net": round(sum(_net(b) for b in rows), 4), "trades": len(rows),
                "wins": sum(1 for b in rows if _net(b) > 0)}
