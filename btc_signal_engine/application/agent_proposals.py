"""Assist proposals (Agent mode's "Assist" autonomy level, 2026-10-30; the copilot wireframe's Assist column).

In an assist session an armed rule's match is never placed by the app. The runner asks `propose`, which builds the
whole bracket ticket the rule would send (side, stake = the per-trade cap, an IoC limit at the rule's ask cap, its BTC
stop, ride to settlement: no take-profit order) through the trading service's own prepare path in preview (re-priced
on the venue, the order gate's checks run as the session's agent, assist step "propose"; NO approval token exists, so
nothing can be sent from it), and shows it to the user with the rule, its inputs and its live record, and what it
could make / lose at the ask now (could make = contracts x $1 - cost at the ask incl. the venue's fee; could lose =
that cost; the worst case at the max price is `max_cost`).

Rules:
- One proposal per (session, rule, market, window): its request_id is the auto executor's
  "rule:<id>:<market>:<window start>"; asking again returns the first answer marked `repeat`, nothing re-prepared.
- It waits `proposal_ttl_s` (the session's option, 10-60 s, default 20) and never past the rule's own entry floor
  (its min_left_ms, else the market's entry cutoff): `expires_ts` is the earlier of the two.
- Approve (`approve`, the user's route only): refused for the agent actor or any agent session context
  (agent_cannot_approve: no agent tool reaches it), for an expired proposal (it is resolved "expired", nothing sent),
  for a session that is not running, or with less than the rule's floor left. Otherwise the ticket is prepared again
  NOW through `TradingService.prepare` and approved through `TradingService.approve` (the slide's path: token, the
  order gate with the session's limits re-checked at send time, a person present, the ask at or under the limit),
  inside assist step "approve" (the bracket is the session's; the audit actor is the user). The fresh ticket must keep
  the proposal's bounds (limit <= max price, worst case <= the per-trade cap, the same side, ticker and BTC stop),
  else its token is declined and nothing is sent. One Approve sends the entry with its BTC stop in the same bracket.
  Any refusal resolves the proposal "refused" with the code and reason: a proposal is answered once, never retried.
- Skip (`skip`, the user only) and expiry (`expire`, on every runner tick and the app's review loop; a session that
  ended or a restart expires what is open) send nothing.
- Every outcome is one journal row of kind "proposal_outcome" {proposal_id, rule, market, outcome, code, reason, by,
  bracket_id}, and the proposal's own row (kind "proposal", rule_scores.PROPOSAL_KIND) carries its state. An approved
  proposal also writes the "rule" row (ok, its bracket id, source "assist") so the scorekeeper and the agent's code
  exit locks treat it as the rule's entry, and tells `approved_fn` (the runner: decision row, rule_entry wake).
- Skipped and expired proposals are scored once the window's official result is known (`settle_due`: the same venue
  result read the trading service's settlement uses, `market_result(ticker)` on the proposal's gateway, every
  `settle_poll_ms`): would = {state: scored, result, won, pnl = contracts x $1 if won - cost at the ask}; a gateway
  that cannot read results leaves it `unsupported` (said, never guessed). One "proposal_scored" journal row each.
- `notify_fn(name, view)`: CUSTOM "proposal_new" (a new proposal) and "proposal_resolved" (its outcome, and again when
  a missed one is scored) for the agent feed.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from typing import Any

from btc_signal_engine.application.actor import AGENT_PREFIX, actor, agent_session
from btc_signal_engine.application.agent_rules import APPROVE_SLACK, DEFAULT_CUTOFF_MS, RULES, entry_cap, order_fee
from btc_signal_engine.application.agent_rules import note as rule_note
from btc_signal_engine.application.agent_sessions import VENUES, AgentSessions, assist_step
from btc_signal_engine.application.agent_tools import AgentTools
from btc_signal_engine.application.play import PlayService
from btc_signal_engine.application.rule_scores import JOURNAL, PROPOSAL_KIND, tally
from btc_signal_engine.domain.brackets import stop_key, stop_of
from btc_signal_engine.domain.fmt import cents, usd
from btc_signal_engine.domain.markets import market as market_spec

log = logging.getLogger(__name__)

OUTCOMES = ("approved", "skipped", "expired", "refused")
OPEN = ("pending", "approving")
MISSED = ("skipped", "expired")
SETTLE_POLL_MS = 15_000
READ_TIMEOUT_S = 5.0  # one result read may take this long (TradingConfig.settle_read_timeout_ms)
__all__ = ["APPROVE_SLACK", "AgentProposals"]  # APPROVE_SLACK lives in agent_rules (entry_cap); kept importable here
EPS = 1e-9
INPUT_KEYS = ("market", "window_start", "window_end", "ticker", "elapsed_s", "left_s", "btc", "strike", "gap",
              "gap_pct", "side", "ask", "ask_cap", "stop_index_below", "stop_index_above", "fair_up", "fee",
              "residual", "edge")
VIEW_KEYS = ("id", "session_id", "agent_name", "request_id", "rule", "rule_label", "market", "market_label", "ticker", "side",
             "stake", "max_price", "btc_stop", "ride_to_settlement", "contracts", "max_cost", "ask", "cost_at_ask",
             "could_make", "could_lose", "sentence", "line", "reason", "inputs", "note", "record", "ttl_s",
             "created_ts", "expires_ts", "window_start", "window_end", "state", "outcome", "would")


def _refused(code: str, reason: str) -> dict:
    return {"ok": False, "code": code, "reason": reason}


class AgentProposals:
    def __init__(self, sessions: AgentSessions, play: PlayService, *,
                 notify_fn: Callable[[str, dict], None] | None = None, id_fn: Callable[[], str] | None = None,
                 settle_poll_ms: int = SETTLE_POLL_MS) -> None:
        self.sessions, self.play = sessions, play
        self._notify = notify_fn
        self._id = id_fn or (lambda: uuid.uuid4().hex[:16])
        self.settle_poll_ms = settle_poll_ms
        self._rows: dict[str, dict] = {}  # proposal id -> its data (open ones and missed ones awaiting a result)
        self._jid: dict[str, int] = {}  # proposal id -> its journal row id
        self._by_request: dict[tuple[str, str], str] = {}  # (session id, request_id) -> proposal id
        self._next_read: dict[str, int] = {}  # proposal id -> next result read
        self._lock = asyncio.Lock()
        self._approved_fns: list[Callable[[dict, dict, int], None]] = []

    def on_approved(self, fn: Callable[[dict, dict, int], None]) -> None:
        """`fn(proposal, bracket view, now)` after an approved proposal's entry was sent (the runner's bookkeeping)."""
        self._approved_fns.append(fn)

    def off_approved(self, fn: Callable[[dict, dict, int], None]) -> None:
        """Remove a hook added by `on_approved` (a session's runner, once its session is over)."""
        if fn in self._approved_fns:
            self._approved_fns.remove(fn)

    # -- start-up ---------------------------------------------------------------------------------------------------
    def restore(self, now: int) -> None:
        """At start-up: an open proposal left by the last run expires ("the app restarted"); a missed one whose result
        is not scored yet is read again."""
        for r in self.sessions.repo.select(JOURNAL, {"kind": PROPOSAL_KIND}, order_by=["id"]):
            p = dict(r.get("data") or {})
            if not p.get("id"):
                continue
            self._jid[p["id"]] = int(r["id"])
            self._by_request[(p["session_id"], p["request_id"])] = p["id"]
            if p.get("state") in OPEN:
                self._rows[p["id"]] = p
                self._resolve(p, "expired", now, "expired", "The app restarted; nothing was sent.", by="system")
            elif p.get("state") in MISSED and (p.get("would") or {}).get("state") == "pending":
                self._rows[p["id"]] = p

    # -- reads ----------------------------------------------------------------------------------------------------------
    @staticmethod
    def view(p: dict) -> dict:
        return {k: p.get(k) for k in VIEW_KEYS}

    def get(self, proposal_id: str) -> dict | None:
        p = self._rows.get(proposal_id)
        if p is not None:
            return p
        jid = self._jid.get(proposal_id)
        rows = self.sessions.repo.select(JOURNAL, {"id": jid}, limit=1) if jid is not None else []
        return dict(rows[0]["data"]) if rows else None

    def pending(self, session_id: str | None = None) -> list[dict]:
        return [self.view(p) for p in self._rows.values() if p["state"] in OPEN
                and (session_id is None or p["session_id"] == session_id)]

    def recent(self, session_id: str, limit: int = 20) -> list[dict]:
        rows = self.sessions.repo.select(JOURNAL, {"session_id": session_id, "kind": PROPOSAL_KIND},
                                         order_by=["-id"], limit=limit)
        return [self.view(self._rows.get(r["data"].get("id")) or r["data"]) for r in rows]

    # -- create -------------------------------------------------------------------------------------------------------
    async def propose(self, now: int, session: dict, rule_id: str, market: str, m: dict) -> dict:
        """The proposal for this rule match: {ok, proposal, repeat?} or a refusal {ok False, code, reason, ticket?}
        (nothing is shown then; the runner journals it as the rule's refused attempt)."""
        sid = session["id"]
        lim = session["limits"]
        if lim.get("level") != "assist":
            raise ValueError(f"session {sid[:8]} is not an assist session (level {lim.get('level')!r})")
        rule = RULES[rule_id]
        request_id = f"rule:{rule_id}:{market}:{m['window_start']}"
        seen = self._by_request.get((sid, request_id))
        if seen is not None:
            return {"ok": True, "repeat": True, "proposal": self.view(self.get(seen) or {})}
        floor = rule.min_left_ms if rule.min_left_ms is not None else self._cutoff(market)
        expires = min(now + int(round(float(lim["proposal_ttl_s"]) * 1000)), int(m["window_end"]) - floor)
        if expires <= now:
            return _refused("too_late", "Too little time is left in this window to ask you; nothing was proposed.")
        per_trade = float(lim["per_trade"])
        stop = stop_of(m)
        spec = market_spec(market)
        svc = self._svc(market)
        if svc is None:
            return _refused("unavailable", f"{spec.label} is not available in this app; nothing was proposed.")
        v = self.play.now_view(now, spec.venue, span=spec.span, stake=per_trade)
        # you approve the price you were shown: the cap is the ask at proposal time + 2c, never above the rule's cap
        cap = entry_cap(rule, m.get("ask"))  # the one cap rule (agent_rules.entry_cap), as Auto and a setup
        if cap is None:
            return _refused("no_price", "The rule matched without a usable ask; nothing was proposed.")
        args = {"side": m["side"], "stake": per_trade, "order_type": "limit", "max_price": cap,
                "btc_stop": stop}
        prop = AgentTools._proposal(v, args, ride=True)  # the same ticket proposal place_entry builds for a rule
        if not prop["ok"]:
            return prop
        with assist_step(sid, "propose"):
            pre = await svc.preview(prop["proposal"], now)
        if not pre.get("ok"):
            return pre
        t = pre["ticket"]
        if not pre["would_send"]:
            g = pre.get("gate") or {}
            return {"ok": False, "code": g.get("code") or "refused", "reason": g.get("reason"), "ticket": t}
        if t["max_cost"] > per_trade + EPS or t["limit_price"] > cap + EPS:
            return _refused("outside_bounds", f"Re-priced now, its worst case is ${t['max_cost']:.2f} at up to "
                                              f"{cents(t['limit_price'])}, outside the {cents(cap)} cap or "
                                              f"the ${per_trade:.2f} per-trade cap; nothing was proposed.")
        ask = float(m["ask"])
        n = float(t["contracts"])
        cost = round(n * ask + order_fee(v, market, n, ask), 4)
        side_word = "Up" if m["side"] == "UP" else "Down"
        stop_txt = "no stop, it rides to settlement" if stop is None else f"BTC stop ${stop:,.0f}"
        p = {"id": self._id(), "session_id": sid, "agent_name": self.sessions.name_of(session),
             "request_id": request_id, "rule": rule_id,
             "rule_label": rule.label, "market": market, "market_label": spec.label, "strategy": VENUES[market],
             "env": t.get("env"), "ticker": t["ticker"], "side": m["side"], "stake": per_trade,
             "max_price": cap, "btc_stop": stop, "ride_to_settlement": True, "contracts": n,
             "max_cost": t["max_cost"], "ask": ask, "cost_at_ask": cost, "could_make": round(n - cost, 4),
             "could_lose": cost, "sentence": t.get("sentence"),
             "line": f"Buy {side_word} {usd(per_trade)} · max {cents(cap)}",
             "reason": f"{rule_id} is ready on {spec.label}. {stop_txt[0].upper() + stop_txt[1:]}.",
             "inputs": {k: m[k] for k in INPUT_KEYS if k in m}, "note": rule_note(m),
             "record": self._record(rule_id, market), "ttl_s": lim["proposal_ttl_s"], "created_ts": now,
             "expires_ts": expires, "window_start": int(m["window_start"]), "window_end": int(m["window_end"]),
             "state": "pending", "outcome": None, "would": None}
        jid = self.sessions.journal(PROPOSAL_KIND, p, now, session_id=sid)
        self._rows[p["id"]], self._jid[p["id"]] = p, int(jid)
        self._by_request[(sid, request_id)] = p["id"]
        self._tell("proposal_new", p)
        return {"ok": True, "proposal": self.view(p)}

    def _record(self, rule_id: str, market: str) -> dict:
        """The rule's live record on this market (the scorekeeper's tally), shown with the proposal."""
        try:
            trades = [t for t in self.sessions.rule_book.trades.all() if t["rule"] == rule_id and t["market"] == market]
            t = tally(trades)
        except Exception:
            log.warning("rule record for a proposal failed", exc_info=True)
            return {"error": "The rule's live record could not be read."}
        return {k: t[k] for k in ("trades", "wins", "win_pct", "break_even_pct", "net_usd", "pending", "open")}

    # -- the user's answer ------------------------------------------------------------------------------------------
    def _not_agent(self) -> dict | None:
        if agent_session() is not None or actor().startswith(AGENT_PREFIX):
            return _refused("agent_cannot_approve", "Only you can answer a proposal; an agent never can. Nothing was "
                                                    "sent.")
        return None

    def _open(self, proposal_id: str) -> tuple[dict | None, dict | None]:
        p = self.get(proposal_id)
        if p is None:
            return None, _refused("not_found", "There is no such proposal; nothing was sent.")
        if p["state"] not in OPEN:
            return None, {**_refused("already_answered", f"This proposal was already {p['state']}; nothing more was "
                                                         "sent."), "proposal": self.view(p)}
        if p["state"] == "approving":
            return None, _refused("in_progress", "This proposal is being sent now.")
        return p, None

    async def approve(self, proposal_id: str, now: int) -> dict:
        bad = self._not_agent()
        if bad is not None:
            return bad
        async with self._lock:
            p, bad = self._open(proposal_id)
            if bad is not None:
                return bad
            if now >= p["expires_ts"]:
                self._resolve(p, "expired", now, "expired", "The proposal expired before you approved it; nothing "
                                                            "was sent.")
                return {**_refused("expired", "This proposal expired; nothing was sent."), "proposal": self.view(p)}
            s = self.sessions.get(p["session_id"])
            if s is None or s["state"] != "running":
                why = "The agent session is not running" if s is None else f"The agent session is {s['state']}"
                return self._refuse(p, now, "session_not_running", why + "; nothing was sent.")
            p["state"] = "approving"
        try:
            out = await self._send(p, s, now)
        except Exception as e:  # recorded as the refusal; never retried
            log.exception("approving a proposal failed")
            out = _refused("error", f"{type(e).__name__}: {e}")
        async with self._lock:
            if out.get("ok"):
                bracket = out.get("bracket") or {}
                self._resolve(p, "approved", now, "approved", "Approved by you: the entry and its stop were sent.",
                              bracket_id=bracket.get("id"))
                self._rule_row(p, now, bracket.get("id"))
                for fn in self._approved_fns:
                    try:
                        fn(self.view(p), bracket, now)
                    except Exception:
                        log.warning("proposal approved hook failed", exc_info=True)
                return {**out, "proposal": self.view(p)}
            return self._refuse(p, now, out.get("code") or "refused", out.get("reason") or "Refused; nothing was sent.")

    async def _send(self, p: dict, s: dict, now: int) -> dict:
        """Prepare the ticket again now and approve it, as the slide does, inside assist step "approve"."""
        rule = RULES[p["rule"]]
        floor = rule.min_left_ms if rule.min_left_ms is not None else self._cutoff(p["market"])
        if p["window_end"] - now < floor:
            return _refused("too_late", "Too little time is left in this window for this rule; nothing was sent.")
        svc = self._svc(p["market"])
        if svc is None:
            return _refused("unavailable", f"{p['market_label']} is not available in this app; nothing was sent.")
        proposal = {"ticker": p["ticker"], "side": p["side"], "stake": p["stake"], "ride_to_settlement": True,
                    "window_end": p["window_end"], "fair_up": p["inputs"].get("fair_up"), "price_mode": "limit",
                    "max_price": p["max_price"]}
        if p["btc_stop"] is not None:
            proposal[stop_key(p["side"])] = p["btc_stop"]
        per_trade = float(s["limits"]["per_trade"])
        with assist_step(p["session_id"], "approve"):
            prep = await svc.prepare(proposal, now)
            if not prep.get("ok"):
                return prep
            t = prep["ticket"]
            stop = stop_of(t, p["side"])
            drift = []
            if t["limit_price"] > p["max_price"] + EPS:
                drift.append(f"its limit is {cents(t['limit_price'])}, above the proposal's max {cents(p['max_price'])}")
            if t["max_cost"] > per_trade + EPS:
                drift.append(f"its worst case is ${t['max_cost']:.2f}, above the ${per_trade:.2f} per-trade cap")
            if t["ticker"] != p["ticker"] or t["side"] != p["side"] or stop != p["btc_stop"]:
                drift.append("its market, side or BTC stop is not the proposal's")
            if drift:
                await svc.approve(prep["token"], "no", t, now)  # the token dies: nothing can be sent from it
                return _refused("outside_bounds", "Re-priced now, " + "; and ".join(drift) + ". Nothing was sent.")
            return await svc.approve(prep["token"], "yes", t, now)

    async def skip(self, proposal_id: str, now: int) -> dict:
        bad = self._not_agent()
        if bad is not None:
            return bad
        async with self._lock:
            p, bad = self._open(proposal_id)
            if bad is not None:
                return bad
            if now >= p["expires_ts"]:
                self._resolve(p, "expired", now, "expired", "The proposal expired; nothing was sent.")
            else:
                self._resolve(p, "skipped", now, "skipped", "Skipped by you; nothing was sent.")
            return {"ok": True, "proposal": self.view(p)}

    def expire(self, now: int) -> list[dict]:
        """Expire every open proposal past its time, or whose session is no longer running. Nothing is sent."""
        out = []
        for p in list(self._rows.values()):
            if p["state"] != "pending":
                continue
            if self.sessions.get(p["session_id"]) is None:
                self._resolve(p, "expired", now, "session_ended", "The agent session ended; nothing was sent.",
                              by="system")
            elif now >= p["expires_ts"]:
                self._resolve(p, "expired", now, "expired", "Not answered in time; nothing was sent.", by="system")
            else:
                continue
            out.append(self.view(p))
        return out

    def _refuse(self, p: dict, now: int, code: str, reason: str) -> dict:
        self._resolve(p, "refused", now, code, reason)
        return {**_refused(code, reason), "proposal": self.view(p)}

    def _resolve(self, p: dict, state: str, now: int, code: str, reason: str, *, by: str | None = None,
                 bracket_id: str | None = None) -> None:
        if state not in OUTCOMES:
            raise ValueError(f"unknown proposal outcome {state!r}")
        p["state"] = state
        p["outcome"] = {"state": state, "code": code, "reason": reason, "ts": now, "by": by or actor(),
                        "bracket_id": bracket_id}
        if state in MISSED:
            p["would"] = {"state": "pending", "result": None, "won": None, "pnl": None}
        else:
            self._rows.pop(p["id"], None)
        self._save(p)
        self.sessions.journal("proposal_outcome", {"proposal_id": p["id"], "rule": p["rule"],
                                                   "market": p["market"], **p["outcome"]}, now,
                              session_id=p["session_id"])
        self._tell("proposal_resolved", p)

    def _rule_row(self, p: dict, now: int, bracket_id: str | None) -> None:
        """The approved proposal as the rule's entry (the scorekeeper's link and the agent's code-exit lock)."""
        self.sessions.journal("rule", {"rule": p["rule"], "market": p["market"], "ok": True, "code": None,
                                       "reason": None, "repeat": False, "bracket_id": bracket_id,
                                       "inputs": p["inputs"], "request_id": p["request_id"], "note": p["note"],
                                       "source": "assist", "proposal_id": p["id"],
                                       "approved_by": p["outcome"]["by"]}, now, session_id=p["session_id"])

    # -- the missed ones' result --------------------------------------------------------------------------------------
    async def settle_due(self, now: int) -> list[dict]:
        """Score each skipped / expired proposal whose window has closed, once the venue's official result is final."""
        due = [p for p in self._rows.values() if p["state"] in MISSED and (p.get("would") or {}).get("state")
               == "pending" and now >= p["window_end"] and now >= self._next_read.get(p["id"], 0)]
        out = []
        for p in due:
            self._next_read[p["id"]] = now + self.settle_poll_ms
            svc = self._svc(p["market"])
            read = getattr((svc.gateways if svc is not None else {}).get(p["env"]), "market_result", None)
            if read is None:
                p["would"] = {"state": "unsupported", "result": None, "won": None, "pnl": None,
                              "reason": "This connection cannot read market results; check the venue."}
                out.append(self._scored(p, now))
                continue
            try:
                got = await asyncio.wait_for(read(p["ticker"]), READ_TIMEOUT_S) or {}
            except Exception as e:
                log.info("proposal result read failed (%s); retrying", type(e).__name__)
                continue
            if not got.get("final") or got.get("result") not in ("yes", "no"):
                continue
            won = (got["result"] == "yes") == (p["side"] == "UP")
            pnl = round((p["contracts"] if won else 0.0) - p["cost_at_ask"], 4)
            p["would"] = {"state": "scored", "result": got["result"], "won": won, "pnl": pnl,
                          "source": got.get("source")}
            out.append(self._scored(p, now))
        return out

    def _scored(self, p: dict, now: int) -> dict:
        self._rows.pop(p["id"], None)
        self._next_read.pop(p["id"], None)
        self._save(p)
        self.sessions.journal("proposal_scored", {"proposal_id": p["id"], "rule": p["rule"], "market": p["market"],
                                                  "outcome": p["state"], **p["would"]}, now,
                              session_id=p["session_id"])
        self._tell("proposal_resolved", p)
        return self.view(p)

    # -- helpers ------------------------------------------------------------------------------------------------------
    def _save(self, p: dict) -> None:
        jid = self._jid.get(p["id"])
        if jid is not None:
            self.sessions.repo.update(JOURNAL, {"id": jid}, {"data": p})

    def _svc(self, market: str) -> Any:
        v = self.play.venues.get(market_spec(market).play_key)
        return v.svc if v is not None else None

    @staticmethod
    def _cutoff(market: str) -> int:
        c = market_spec(market).entry_cutoff_ms
        return DEFAULT_CUTOFF_MS if c is None else int(c)

    def _tell(self, name: str, p: dict) -> None:
        if self._notify is None:
            return
        try:
            self._notify(name, self.view(p))
        except Exception:
            log.warning("proposal notify failed", exc_info=True)
