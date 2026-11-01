"""Assist level (agent_proposals): an armed rule's match becomes a proposal the user approves or skips; only the
user's Approve sends it, through the slide's prepare -> OrderGate -> approve path. A real AgentSessions / AgentTools /
TradingService on the shadow exchange (no network), the runner with a scripted fake harness and an injected clock."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from btc_signal_engine.adapters.http.agent_feed import AgUiFeed
from btc_signal_engine.application.agent_fleet import AgentFleet
from btc_signal_engine.adapters.http.agent_routes import register_agent_routes
from btc_signal_engine.adapters.http.push import FeedHub
from btc_signal_engine.application.actor import as_agent
from btc_signal_engine.application.agent_prompts import AgentPrompts
from btc_signal_engine.application.agent_proposals import AgentProposals
from btc_signal_engine.application.agent_tools import TOOLS
from btc_signal_engine.application.rule_scores import scorecard
from tests.agent.test_rules import RuleRig
from tests.agent.test_tools import HOUR, SID, entry
from tests.pc import as_actor, as_pc
from tests.trading.helpers import CLOSE, NOW, TICKER, W0

K = 80_000.0


def assist(store, **kw) -> RuleRig:
    r = RuleRig(store, level="assist", **kw)
    r.svc.heartbeat(NOW)  # a person at the screen (the slide's presence)
    return r


def run(coro):
    return asyncio.run(coro)


def the_proposal(r) -> dict:
    (p,) = r.proposals.pending(SID)
    return p


def outcomes(r) -> list[dict]:
    return r.journal("proposal_outcome")


# -- a match proposes, it never places ---------------------------------------------------------------------------------
def test_an_assist_match_creates_a_full_proposal_and_sends_nothing(store):
    r = assist(store)
    r.tick(1, step=0)
    assert r.buys() == [] and store.select("trading_brackets") == []
    p = the_proposal(r)
    assert p["rule"] == "H4" and p["market"] == "kalshi_15m" and p["side"] == "UP" and p["stake"] == 1.0
    assert p["max_price"] == 0.60 and p["btc_stop"] == K + 200  # the 58c ask + 2c, under H4's 95c cap and p["ride_to_settlement"] is True
    assert p["request_id"] == f"rule:H4:kalshi_15m:{W0}" and p["ticker"] == TICKER
    assert p["ttl_s"] == 20 and p["expires_ts"] == NOW + 20_000 and p["state"] == "pending"
    assert p["max_cost"] <= 1.0 + 1e-9 and p["contracts"] > 0
    # could make / lose at the 58c ask now (Kalshi's fee), the worst case stays max_cost
    assert p["ask"] == 0.58 and 0 < p["could_lose"] == p["cost_at_ask"] < p["max_cost"]
    assert p["could_make"] == round(p["contracts"] - p["cost_at_ask"], 4) > 0
    assert p["inputs"]["gap"] == 400 and p["note"].startswith("[rule:H4]")
    assert p["record"]["trades"] == 0 and "net_usd" in p["record"]
    assert p["line"] == "Buy Up $1.00 · max 60¢" and "H4 is ready" in p["reason"] and "BTC stop $80,200" in p["reason"]
    assert [n for n, _ in r.events_out] == ["proposal_new"]
    (rule,) = r.journal("rule")
    assert rule["ok"] is False and rule["code"] == "proposed" and rule["proposal_id"] == p["id"]
    assert not [d for d in r.journal("decision") if d.get("source") in ("rule", "assist")]


def test_auto_is_unchanged_it_places_and_proposes_nothing(store):
    r = RuleRig(store, level="auto")
    r.tick(1, step=0)
    assert len(r.buys()) == 1 and r.proposals.pending() == [] and r.journal("proposal") == []
    (rule,) = r.journal("rule")
    assert rule["ok"] and rule["bracket_id"]


def test_one_proposal_per_rule_market_window_and_its_request_id_is_idempotent(store):
    r = assist(store)
    r.tick(1, step=0)
    r.tick(3, step=0)
    assert len(r.journal("proposal")) == 1
    p = the_proposal(r)
    # asked again for the same request_id: the first proposal, marked repeat, nothing new prepared
    m = r.journal("rule")[0]["inputs"]
    match = {**m, "rule": "H4"}
    again = run(r.proposals.propose(NOW, r.ag.latest(), "H4", "kalshi_15m", match))
    assert again["repeat"] is True and again["proposal"]["id"] == p["id"]
    assert len(r.journal("proposal")) == 1 and r.buys() == []


# -- the user's answer ---------------------------------------------------------------------------------------------------
def test_approve_sends_the_entry_and_its_stop_in_one_bracket(store):
    r = assist(store)
    r.tick(1, step=0)
    p = the_proposal(r)
    out = run(r.proposals.approve(p["id"], NOW + 3_000))
    assert out["ok"], out
    assert len(r.buys()) == 1 and len(r.ex.posts()) == 1  # the buy only: no take-profit order
    (b,) = store.select("trading_brackets")
    assert b["session_id"] == SID and b["side"] == "UP" and b["stop_index_below"] == K + 200 and b["no_tp"]
    assert out["proposal"]["state"] == "approved" and out["proposal"]["outcome"]["bracket_id"] == b["id"]
    (o,) = outcomes(r)
    assert o["state"] == "approved" and o["bracket_id"] == b["id"] and not o["by"].startswith("agent:")
    ok_rule = [x for x in r.journal("rule") if x["ok"]]
    assert len(ok_rule) == 1 and ok_rule[0]["bracket_id"] == b["id"] and ok_rule[0]["source"] == "assist"
    d = [x for x in r.journal("decision") if x.get("source") == "assist"]
    assert len(d) == 1 and d[0]["action"] == "ENTRY" and d[0]["bracket_id"] == b["id"]
    assert "rule_entry" in [e["type"] for e in r.runner.events.pending()]
    # the person approved: the trading audit's approval is theirs, not the agent's
    approved = [a for a in store.select("trading_audit") if a["event"] == "approved"]
    assert approved and not any((a.get("actor") or "").startswith("agent:") for a in approved)
    # answered once: a second tap sends nothing
    again = run(r.proposals.approve(p["id"], NOW + 4_000))
    assert again["code"] == "already_answered" and len(r.buys()) == 1
    # the rule's code-exit locks hold for the approved bracket, as for an auto entry
    out = run(r.tools.call("set_btc_stop", {"bracket_id": b["id"], "level": None, "note": "x", "request_id": "s"},
                           NOW, session_id=SID))
    assert out["code"] == "rule_stop_locked"


def test_ttl_expiry_sends_nothing_and_approve_after_expiry_is_refused(store):
    r = assist(store)
    r.tick(1, step=0)
    p = the_proposal(r)
    r.runner.clock.t = NOW + 20_000
    run(r.runner.tick())
    assert r.proposals.pending() == [] and r.buys() == []
    (o,) = outcomes(r)
    assert o["state"] == "expired" and o["code"] == "expired"
    assert ("proposal_resolved", "expired") in [(n, v["state"]) for n, v in r.events_out]
    out = run(r.proposals.approve(p["id"], NOW + 21_000))
    assert out["ok"] is False and out["code"] == "already_answered" and r.buys() == []


def test_approve_at_the_expiry_instant_is_refused_and_resolves_it(store):
    r = assist(store)
    r.tick(1, step=0)
    p = the_proposal(r)
    out = run(r.proposals.approve(p["id"], p["expires_ts"]))
    assert (out["ok"], out["code"]) == (False, "expired") and r.buys() == []
    assert outcomes(r)[0]["state"] == "expired"


def test_approve_is_refused_when_the_ask_is_above_the_max_price(store):
    r = assist(store)
    r.tick(1, step=0)
    p = the_proposal(r)
    r.ex.market.set(yes_bid=0.55, no_bid=0.03)  # the UP ask is now 97c, above the 95c max
    out = run(r.proposals.approve(p["id"], NOW + 2_000))
    assert out["ok"] is False and r.buys() == []
    (o,) = outcomes(r)
    assert o["state"] == "refused" and o["code"] == out["code"] and o["reason"]


def test_approve_re_checks_the_session_budget(store):
    r = assist(store)
    r.tick(1, step=0)
    p = the_proposal(r)
    r.ag.latest()["limits"]["budget"] = 0.2  # spent elsewhere meanwhile: no room for this ticket's worst case
    out = run(r.proposals.approve(p["id"], NOW + 2_000))
    assert (out["ok"], out["code"]) == (False, "over_budget") and r.buys() == []
    assert outcomes(r)[0]["state"] == "refused"


def test_approve_needs_a_person_at_the_screen(store):
    r = RuleRig(store, level="assist")  # no heartbeat
    r.tick(1, step=0)
    p = the_proposal(r)
    out = run(r.proposals.approve(p["id"], NOW + 2_000))
    assert (out["ok"], out["code"]) == (False, "not_present") and r.buys() == []


def test_the_agent_can_never_approve(store):
    r = assist(store)
    r.tick(1, step=0)
    p = the_proposal(r)

    async def as_the_agent(fn):
        with as_agent(SID):
            return await fn(p["id"], NOW + 1_000)
    for fn in (r.proposals.approve, r.proposals.skip):
        out = run(as_the_agent(fn))
        assert out["code"] == "agent_cannot_approve"
    assert the_proposal(r)["state"] == "pending" and r.buys() == []
    # no tool in the registry answers proposals, and the agent's own entry is refused in assist
    names = {t.name for t in TOOLS}
    assert not any("approv" in n or "propos" in n or "skip" in n for n in names)
    out = run(r.tools.call("place_entry", entry(stake=1.0, max_worst_case=1.0), NOW, session_id=SID))
    assert out["code"] == "rules_own_entries"
    out = run(r.tools.call("place_entry", {**entry(stake=1.0, order_type="limit", max_price=0.95,
                                                   max_worst_case=1.0), "request_id": "x"},
                           NOW, session_id=SID, by_rule="H4"))  # even down the rule executor's own path
    assert out["code"] == "assist_needs_you" and r.buys() == []


def test_skip_sends_nothing_and_is_scored_at_settlement(store):
    r = assist(store)
    r.tick(1, step=0)
    p = the_proposal(r)
    out = run(r.proposals.skip(p["id"], NOW + 1_000))
    assert out["ok"] and out["proposal"]["state"] == "skipped" and r.buys() == []
    (o,) = outcomes(r)
    assert o["state"] == "skipped" and o["code"] == "skipped"
    assert run(r.proposals.settle_due(CLOSE - 1)) == []  # the window is still open
    assert run(r.proposals.settle_due(CLOSE + 1_000)) == []  # closed, no official result yet
    r.ex.market.settle("yes")  # UP won
    (scored,) = run(r.proposals.settle_due(CLOSE + 20_000))
    w = scored["would"]
    assert w["state"] == "scored" and w["result"] == "yes" and w["won"] is True
    assert w["pnl"] == round(p["contracts"] - p["cost_at_ask"], 4) > 0
    (row,) = r.journal("proposal_scored")
    assert row["proposal_id"] == p["id"] and row["outcome"] == "skipped" and row["pnl"] == w["pnl"]
    assert run(r.proposals.settle_due(CLOSE + 60_000)) == []  # scored once


def test_an_expired_proposal_is_scored_too_as_a_loss(store):
    r = assist(store)
    r.tick(1, step=0)
    p = the_proposal(r)
    r.proposals.expire(NOW + 30_000)
    r.ex.market.settle("no")
    (scored,) = run(r.proposals.settle_due(CLOSE + 1_000))
    assert scored["state"] == "expired" and scored["would"]["won"] is False
    assert scored["would"]["pnl"] == -p["cost_at_ask"]
    card = scorecard(store, r.ag.rule_book)
    h4 = next(x for x in card["rules"] if x["rule"] == "H4")["proposals"]
    assert (h4["proposed"], h4["expired"], h4["missed_scored"], h4["missed_won"]) == (1, 1, 1, 0)
    assert h4["missed_pnl_usd"] == -p["cost_at_ask"]


def test_a_proposal_expires_when_its_session_ends_and_on_restart(store):
    r = assist(store)
    r.tick(1, step=0)
    p = the_proposal(r)
    r.ag.stop(r.ag.latest()["id"], NOW + 1_000)
    r.proposals.expire(NOW + 1_000)
    assert outcomes(r)[0]["code"] == "session_ended" and r.proposals.pending() == []
    # a fresh service over the same journal (an app restart) keeps scoring the missed one
    again = AgentProposals(r.ag, r.play)
    again.restore(NOW + 2_000)
    r.ex.market.settle("yes")
    (scored,) = run(again.settle_due(CLOSE + 1_000))
    assert scored["id"] == p["id"] and scored["would"]["state"] == "scored"


def test_restart_expires_an_open_proposal(store):
    r = assist(store)
    r.tick(1, step=0)
    p = the_proposal(r)
    again = AgentProposals(r.ag, r.play)
    again.restore(NOW + 2_000)
    assert again.get(p["id"])["state"] == "expired" and again.pending() == []
    out = run(again.approve(p["id"], NOW + 3_000))
    assert out["code"] == "already_answered" and r.buys() == []


def test_proposal_ttl_is_the_sessions_option_and_never_past_the_rules_floor(store):
    r = assist(store, ttl=45)
    r.tick(1, step=0)
    assert the_proposal(r)["expires_ts"] == NOW + 45_000
    # H4's floor is 3:00 left: a match with 3:10 left waits 10 s, not 45
    m = {**r.journal("rule")[0]["inputs"], "rule": "H4", "window_start": W0 + 1, "window_end": NOW + 190_000}
    out = run(r.proposals.propose(NOW, r.ag.latest(), "H4", "kalshi_15m", m))
    assert out["ok"] and out["proposal"]["expires_ts"] == NOW + 10_000
    m = {**m, "window_start": W0 + 2, "window_end": NOW + 180_000}
    out = run(r.proposals.propose(NOW, r.ag.latest(), "H4", "kalshi_15m", m))
    assert (out["ok"], out["code"]) == (False, "too_late")


# -- launch ----------------------------------------------------------------------------------------------------------------
def test_launch_requires_a_valid_level(store):
    r = RuleRig(store, rules=())
    r.ag.stop(r.ag.latest()["id"], NOW)
    base = {"kind": "in_app", "venues": ["kalshi_15m"], "budget": 5.0, "per_trade": 1.0, "ends_ts": NOW + HOUR,
            "spend_cap": 5.0, "model": "claude-haiku-5-5", "rules": ["H4"]}
    for bad in ({}, {"level": None}, {"level": "manual"}, {"level": "AUTO"}, {"level": 1}):
        out = r.ag.launch({**base, **bad}, NOW)
        assert (out["ok"], out["code"]) == (False, "invalid_level"), bad
    out = r.ag.launch({**base, "level": "assist", "rules": []}, NOW)
    assert out["code"] == "invalid_level" and "rule" in out["reason"]
    for ttl in (9, 61, "20", True):
        out = r.ag.launch({**base, "level": "assist", "proposal_ttl_s": ttl}, NOW)
        assert out["code"] == "invalid", ttl
    out = r.ag.launch({**base, "level": "auto", "proposal_ttl_s": 20}, NOW)
    assert out["code"] == "invalid"
    out = r.ag.launch({**base, "level": "assist"}, NOW)
    assert out["ok"] and out["session"]["limits"]["level"] == "assist"
    assert out["session"]["limits"]["proposal_ttl_s"] == 20


# -- routes ------------------------------------------------------------------------------------------------------------------
def routes(r) -> tuple[TestClient, AgUiFeed]:
    prompts = AgentPrompts(r.store, "SYSTEM")
    prompts.seed(NOW)
    feed = AgUiFeed(FeedHub(), lambda: {"session": r.ag.latest_view(NOW)}, now_fn=lambda: NOW)
    app = FastAPI()
    clock = r.runner.clock
    register_agent_routes(app, sessions=r.ag, tools=r.tools, prompts=prompts, feed=feed, now_fn=clock.now_ms,
                          llm=r.llm, fleet=AgentFleet(r.ag, r.play, lambda: r.runner, clock=clock),
                          start_task=lambda name, factory: None, proposals=r.proposals)
    return TestClient(as_pc(app)), feed


def test_routes_list_approve_and_skip_and_refuse_an_agent_token(store):
    r = assist(store)
    r.tick(1, step=0)
    p = the_proposal(r)
    client, _ = routes(r)
    got = client.get("/api/agent/proposals").json()
    assert got["ok"] and [x["id"] for x in got["pending"]] == [p["id"]] and got["server_ms"]
    res = client.post("/api/agent/proposals/approve", json={"id": p["id"]}, headers={"X-Agent-Token": "anything"})
    assert res.status_code == 403 and res.json()["code"] == "agent_cannot_approve" and r.buys() == []
    res = client.post("/api/agent/call/approve_proposal", json={"id": p["id"]})
    assert res.json()["code"] == "agent_port" and r.buys() == []  # not the agent listener (and no such tool)
    agent = TestClient(as_actor(client.app.app, "agent-api"))
    res = agent.post("/api/agent/proposals/approve", json={"id": p["id"]})  # the agent listener's actor, no header
    assert res.status_code == 403 and res.json()["code"] == "person_required" and r.buys() == []
    res = TestClient(client.app.app).post("/api/agent/proposals/skip", json={"id": p["id"]})  # nobody: fails closed
    assert res.status_code == 403 and res.json()["code"] == "person_required"
    out = client.post("/api/agent/proposals/approve", json={"id": p["id"]}).json()
    assert out["ok"], out
    assert len(r.buys()) == 1
    assert client.post("/api/agent/proposals/skip", json={"id": p["id"]}).json()["code"] == "already_answered"
    assert client.post("/api/agent/proposals/approve", json={"id": "nope"}).json()["code"] == "not_found"


def test_proposals_for_an_unknown_session_are_404_on_both_routes(store):
    r = assist(store)
    client, _ = routes(r)
    for url in ("/api/agent/proposals?session_id=nope", "/api/agent/sessions/nope/proposals"):
        res = client.get(url)
        assert res.status_code == 404 and res.json()["code"] == "not_found", url
    got = client.get("/api/agent/proposals", params={"session_id": r.ag.latest()["id"]}).json()
    assert got["ok"] and got["pending"] == []


def test_launch_route_requires_the_level(store):
    from tests.agent.test_routes import World, launch_body
    w = World(store)
    out = w.pc.post("/api/agent/launch", json={k: v for k, v in launch_body().items() if k != "level"}).json()
    assert (out["ok"], out["code"]) == (False, "invalid_level") and w.r.ag.latest() is None
    out = w.pc.post("/api/agent/launch", json=launch_body(level="assist", rules=["H4"])).json()
    assert out["code"] == "assist_unavailable"  # this world has no proposals service: refused, never auto
