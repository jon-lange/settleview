"""Agent entry rules ("rules open, code exits, the agent supervises"): the pure H4 / CUSHION100 matchers over a
now_view-shaped dict, and their execution by the runner on a real AgentSessions / AgentTools / TradingService over the
shadow exchange (no network), with a scripted fake harness and an injected clock."""

from __future__ import annotations

import asyncio

import pytest

from btc_signal_engine.application import agent_rules as R
from btc_signal_engine.application.agent_proposals import AgentProposals
from btc_signal_engine.application.agent_runner import AgentRunner
from btc_signal_engine.application.agent_sessions import AgentSessions
from btc_signal_engine.application.agent_tools import AgentTools
from btc_signal_engine.application.play import PlayService, PlayVenue
from tests.agent.fake_harness import FakeHarness, text, turn
from tests.agent.test_runner import Clock, _yield
from tests.agent.test_tools import CONFIG, HOUR, SID, entry, evaluation
from tests.trading.helpers import CLOSE, NOW, TICKER, W0, Exchange, Market
from tests.trading.test_strategy_scope import svc_for, up

K = 80_000.0


# -- the pure rules ---------------------------------------------------------------------------------------------------
def view(*, left_s=660, btc=K + 400, strike=K, yes_ask=0.58, no_ask=0.45, fresh=True, entry_open=True,
         available=True, fair_up=None, window_ms=900_000):
    now = CLOSE - left_s * 1000
    return {"now": now, "available": available,
            "window": {"start": CLOSE - window_ms, "end": CLOSE, "entry_open": entry_open},
            "market": {"ticker": TICKER, "yes_ask": yes_ask, "no_ask": no_ask, "fresh": fresh},
            "btc": {"btc": btc, "strike": strike, "gap": None if btc is None or strike is None else btc - strike},
            "call": {"fair_up": fair_up}}


def h4(v, market="kalshi_15m"):
    return R.h4(v, market)


def cushion100(v, market="kalshi_15m"):
    return R.cushion100(v, market)


def test_h4_time_band_is_10_to_12_minutes_left_inclusive():
    for left, hit in ((721, False), (720, True), (660, True), (600, True), (599, False)):
        assert (h4(view(left_s=left)) is not None) == hit, left


def test_h4_needs_btc_at_least_04_percent_from_the_strike():
    assert h4(view(btc=K + 320)) is not None  # 0.4% of 80,000 = $320
    assert h4(view(btc=K + 319.99)) is None
    assert h4(view(btc=K - 320)) is not None
    assert h4(view(btc=K - 319.99)) is None


def test_h4_ask_cap_is_95c_on_btcs_side():
    assert h4(view(yes_ask=0.95))["ask"] == 0.95
    assert h4(view(yes_ask=0.96)) is None
    assert h4(view(btc=K - 400, no_ask=0.96, yes_ask=0.10)) is None  # DOWN reads the NO ask, not YES
    assert h4(view(btc=K - 400, no_ask=0.95, yes_ask=0.99))["side"] == "DOWN"


def test_h4_side_and_half_gap_invalidation_stop():
    up_ = h4(view(btc=K + 400))
    assert up_["side"] == "UP" and up_["ask"] == 0.58 and up_["ask_cap"] == 0.95
    assert (up_["stop_index_below"], up_["stop_index_above"]) == (K + 200, None)
    dn = h4(view(btc=K - 500))
    assert dn["side"] == "DOWN" and dn["ask"] == 0.45
    assert (dn["stop_index_below"], dn["stop_index_above"]) == (None, K - 250)
    assert up_["rule"] == "H4" and up_["left_s"] == 660 and up_["elapsed_s"] == 240 and up_["gap"] == 400


def test_rules_need_a_fresh_quote_a_strike_btc_and_entry_open():
    for v in (view(fresh=False), view(entry_open=False), view(available=False), view(strike=None),
              view(btc=None), view(btc=K)):
        assert h4(v) is None and cushion100({**v, "now": CLOSE - 400_000}) is None


def test_cushion100_time_band_is_5_to_10_minutes_left_inclusive():
    for left, hit in ((601, False), (600, True), (450, True), (300, True), (299, False)):
        assert (cushion100(view(left_s=left, btc=K + 150, yes_ask=0.80)) is not None) == hit, left


def test_cushion100_needs_100_dollars_past_the_strike_and_a_70_to_90c_ask():
    v = dict(left_s=450, yes_ask=0.80)
    assert cushion100(view(btc=K + 100, **v)) is not None
    assert cushion100(view(btc=K + 99.99, **v)) is None
    for ask, hit in ((0.69, False), (0.70, True), (0.90, True), (0.91, False)):
        assert (cushion100(view(left_s=450, btc=K + 150, yes_ask=ask)) is not None) == hit, ask
    dn = cushion100(view(left_s=450, btc=K - 150, no_ask=0.85, yes_ask=0.20))
    assert dn["side"] == "DOWN" and dn["ask_cap"] == 0.90
    assert (dn["stop_index_below"], dn["stop_index_above"]) == (None, K - 40)
    up_ = cushion100(view(left_s=450, btc=K + 150, yes_ask=0.80))
    assert (up_["stop_index_below"], up_["stop_index_above"]) == (K + 40, None)


def test_catalog_and_unknown_rule():
    assert [r["id"] for r in R.catalog()] == ["H4", "CUSHION100", "HPANIC", "LATERESID"]
    with pytest.raises(ValueError):
        R.evaluate("H5", "kalshi_15m", view())
    assert R.note(h4(view())).startswith("[rule:H4] UP at ask 0.58")


# -- execution by the runner -------------------------------------------------------------------------------------------
class RuleRig:
    """Rig with a settable BTC index (both the screen's and the stop monitor's)."""

    def __init__(self, store, *script, rules=("H4",), btc=K + 400, kind="in_app", venues=("kalshi_15m",),
                 one_bet=None, book=(0.55, 0.42), reactivate=(), level="auto", ttl=None):
        self.btc = btc
        self.ex = Exchange(Market(yes_bid=book[0], no_bid=book[1]))  # default: YES ask 58c, NO ask 45c
        self.svc = asyncio.run(up(svc_for(store, self.ex, "you", mode="shadow",
                                          index_price_fn=lambda: (NOW, self.btc), market_band_fn=lambda: 0.02,
                                          config=CONFIG), present=False))

        def quote_of(ex):
            def quote(now):
                yb, nb = ex.market.book["yes"][0][0], ex.market.book["no"][0][0]
                return {"ticker": TICKER, "yes_bid": yb, "yes_ask": round(1 - nb, 2), "no_bid": nb,
                        "no_ask": round(1 - yb, 2), "close_ts": CLOSE, "open_ts": W0, "age_ms": 300}
            return quote

        play_venues, services = {"kalshi": PlayVenue(self.svc, quote_fn=quote_of(self.ex))}, {"you": self.svc}
        self.pm_ex = self.pm_svc = None
        if "polymarket_15m" in venues:  # a second shadow book for Polymarket 15m (same window, same strike)
            self.pm_ex = Exchange(Market(yes_bid=book[0], no_bid=book[1]))
            self.pm_svc = asyncio.run(up(svc_for(store, self.pm_ex, "you_poly", mode="shadow",
                                                 index_price_fn=lambda: (NOW, self.btc), market_band_fn=lambda: 0.02,
                                                 config=CONFIG), present=False))
            play_venues["polymarket"] = PlayVenue(self.pm_svc, quote_fn=quote_of(self.pm_ex))
            services["you_poly"] = self.pm_svc
        self.play = PlayService(play_venues, evaluation_fn=evaluation,
                                index_fn=lambda: (NOW, self.btc),
                                index_series_fn=lambda a, b: [(W0 + i * 1000, K) for i in range(300)])
        self.ag = AgentSessions(store, services, id_fn=lambda: SID)
        for svc in services.values():
            svc._agent_gate_fn, svc._agent_takeover_fn = self.ag.check, self.ag.takeover
        self.store = store
        for rid in reactivate:  # a retired rule (HPANIC since 2026-10-30) armed for a test of its own logic
            self.ag.rule_book.set(rid, "active", NOW, by="user", reason="Reactivated for a test.")
        # one agent = one market (multi-agent): the session trades the first; any other venue's book is there for a
        # second agent session (tests/agent/test_fleet.py)
        spec = {"kind": kind, "market": venues[0], "budget": 5.0, "per_trade": 1.0, "ends_ts": NOW + HOUR,
                "spend_cap": 5.0, "model": "claude-haiku-5-5", "rules": list(rules), "level": level}
        if one_bet is not None:
            spec["one_bet_per_window"] = one_bet
        if ttl is not None:
            spec["proposal_ttl_s"] = ttl
        out = self.ag.launch(spec, NOW)
        assert out["ok"], out
        holder = {}
        self.tools = AgentTools(self.ag, self.play, events_fn=lambda sid: holder["runner"].events)
        self.llm = FakeHarness(*(script or (turn(text("Supervising the rule entry.")),)))
        self.rows = []
        self.events_out = []  # the proposals' feed events (name, view)
        self.proposals = AgentProposals(self.ag, self.play, notify_fn=lambda n, v: self.events_out.append((n, v)))
        self.runner = AgentRunner(self.ag, self.tools, self.play, self.llm,
                                  prompts_fn=lambda: ("You supervise.", "", {"system": 1}), clock=Clock(),
                                  notify_fn=self.rows.append, sleep=_yield, proposals=self.proposals)
        holder["runner"] = self.runner
        assert self.runner.attach(SID)

    def buys(self, ex=None):
        return [p for p in (ex or self.ex).posts() if p and "-entry" in p.get("client_order_id", "")]

    def journal(self, kind):
        return [j["data"] for j in self.ag.journal_rows(SID) if j["kind"] == kind]

    def tick(self, n=1, step=1_000):
        async def main():
            for _ in range(n):
                await self.runner.tick()
                self.runner.clock.t += step
        asyncio.run(main())


def test_a_rule_entry_places_one_1_dollar_order_with_its_btc_stop_and_wakes_the_agent(store):
    r = RuleRig(store)
    r.tick(5)
    assert len(r.buys()) == 1
    (b,) = store.select("trading_brackets")
    assert b["session_id"] == SID and b["side"] == "UP" and b["state"] == "open"
    assert b["stop_index_below"] == K + 200 and b["stop_index_above"] is None  # set in the same ticket
    assert b["max_cost"] <= 1.0 + 1e-9 and b["take_profit"] == 0.99
    (rule,) = r.journal("rule")
    assert rule["ok"] and rule["rule"] == "H4" and rule["market"] == "kalshi_15m"
    assert rule["request_id"] == f"rule:H4:kalshi_15m:{W0}"
    assert rule["inputs"]["gap"] == 400 and rule["note"].startswith("[rule:H4]")
    tool = [t for t in r.journal("tool") if t["name"] == "place_entry"]
    assert len(tool) == 1 and tool[0]["args"]["note"].startswith("[rule:H4]")
    # the matched 58c ask + 2c (APPROVE_SLACK), never the rule's 95c cap: the same protection as Assist
    assert tool[0]["args"]["max_price"] == 0.60 and tool[0]["args"]["max_worst_case"] == 1.0

    d = [x for x in r.journal("decision") if x.get("source") == "rule"]
    assert len(d) == 1 and d[0]["action"] == "ENTRY" and d[0]["p_source"] == "experimental"
    assert d[0]["rule"] == "H4" and d[0]["market"] == "kalshi_15m" and d[0]["side"] == "UP" and d[0]["thesis"].startswith("[rule:H4]")
    wakes = r.journal("wake")
    assert wakes and "rule_entry" in [e["type"] for e in wakes[0]["trigger"]]
    assert "rule_entry" not in [e["type"] for w in wakes[1:] for e in w["trigger"]]
    assert any(x["kind"] == "rule" for x in r.rows)
    # the agent sees the armed rule in its memory and the rule note in its tool note
    q = r.llm.requests[0]
    assert '"rules":[{"id":"H4"' in q.prompt and "rules_own_entries" in q.system


def test_the_rules_request_id_is_idempotent_across_ticks_in_the_same_window(store):
    r = RuleRig(store)
    r.tick(1)
    assert len(r.buys()) == 1
    # even if the runner forgot it tried (and the position were gone), the same window's request_id sends nothing
    args = {**entry(stake=1.0, order_type="limit", max_price=0.60, max_worst_case=1.0, btc_stop=K + 200),
            "request_id": f"rule:H4:kalshi_15m:{W0}", "note": "[rule:H4] again"}
    out = asyncio.run(r.tools.call("place_entry", args, NOW, session_id=SID, by_rule="H4"))
    assert out.get("repeat") is True and len(r.buys()) == 1


def test_no_rule_entry_when_not_armed(store):
    r = RuleRig(store, rules=())
    r.tick(5)
    assert r.buys() == [] and r.journal("rule") == []


def test_no_rule_entry_while_the_session_holds_that_market(store):
    r = RuleRig(store)
    args = {**entry(stake=1.0, order_type="limit", max_price=0.95, max_worst_case=1.0), "request_id": "held"}
    assert asyncio.run(r.tools.call("place_entry", args, NOW, session_id=SID, by_rule="H4"))["ok"]
    r.tick(5)
    assert len(r.buys()) == 1 and r.journal("rule") == []


def test_no_rule_entry_with_under_3_minutes_left(store):
    r = RuleRig(store)
    r.runner.clock.t = CLOSE - 179_000
    rows = asyncio.run(r.runner.rules_tick(r.runner.now()))
    assert rows == [] and r.buys() == []


def test_no_rule_entry_while_paused_or_stopping(store):
    r = RuleRig(store)
    r.ag.pause(r.ag.latest()["id"], NOW, "test pause")
    assert asyncio.run(r.runner.rules_tick(NOW)) == [] and r.buys() == []
    assert r.ag.resume(r.ag.latest()["id"], NOW)["ok"]
    r.ag.begin_stop(r.ag.latest()["id"])
    assert asyncio.run(r.runner.rules_tick(NOW)) == [] and r.buys() == []


def test_a_refused_rule_entry_is_journaled_and_not_retried_in_that_window(store):
    r = RuleRig(store, btc=K - 400)  # DOWN at the 45c NO ask...
    r.ex.market.set(yes_bid=0.03, no_bid=0.42)  # ...but now the NO ask is 97c: no H4 match at all
    r.tick(3, step=0)
    assert r.journal("rule") == []
    r.ex.market.set(yes_bid=0.55, no_bid=0.42)
    # force a refusal: a per-trade cap too small to buy any contract
    r.ag.latest()["limits"]["per_trade"] = 0.003
    r.tick(3, step=0)
    (rule,) = r.journal("rule")
    assert rule["ok"] is False and rule["reason"] and r.buys() == []
    assert not [d for d in r.journal("decision") if d.get("source") == "rule"]


# -- the single writer: the agent's own opening tools ------------------------------------------------------------------
def test_agent_place_entry_is_refused_while_rules_are_armed(store):
    r = RuleRig(store)
    out = asyncio.run(r.tools.call("place_entry", entry(stake=1.0, max_worst_case=1.0), NOW, session_id=SID))
    assert out == {"ok": False, "code": "rules_own_entries",
                   "reason": "Entries in this session are opened only by the armed rules; you supervise."}
    out = asyncio.run(r.tools.call("place_perp_entry", {}, NOW, session_id=SID))
    assert out["code"] == "rules_own_entries"
    assert r.buys() == []
    # the supervising tools stay the agent's
    out = asyncio.run(r.tools.call("update_notes", {"text": "H4: 0 trades"}, NOW, session_id=SID))
    assert out["ok"]


def test_agent_place_entry_works_as_before_with_no_rules(store):
    r = RuleRig(store, rules=())
    out = asyncio.run(r.tools.call("place_entry", entry(stake=0.5, max_worst_case=1.0), NOW, session_id=SID))
    assert out["ok"], out
    assert len(r.buys()) == 1


def test_place_entry_btc_stop_must_not_be_already_reached(store):
    r = RuleRig(store, rules=())
    out = asyncio.run(r.tools.call("place_entry", entry(stake=1.0, max_worst_case=1.0, btc_stop=K + 500), NOW,
                                   session_id=SID))
    assert out["code"] == "invalid" and "already reached" in out["reason"] and r.buys() == []


# -- launch -------------------------------------------------------------------------------------------------------------
def test_launch_validates_rules_strictly(store):
    r = RuleRig(store, rules=())
    r.ag.stop(r.ag.latest()["id"], NOW)
    base = {"kind": "in_app", "venues": ["kalshi_15m"], "budget": 5.0, "per_trade": 1.0, "ends_ts": NOW + HOUR,
            "spend_cap": 5.0, "model": "claude-haiku-5-5", "level": "auto"}
    for rules in (["H5"], "H4", [1], ["H4", "H4"], None):
        out = r.ag.launch({**base, "rules": rules}, NOW)
        assert (out["ok"], out["code"]) == (False, "invalid"), rules
    out = r.ag.launch({**base, "kind": "external", "rules": ["H4"]}, NOW)
    assert out["code"] == "invalid" and "in-app" in out["reason"]
    out = r.ag.launch({**base, "venues": ["polymarket_15m"], "rules": ["H4"]}, NOW)
    assert out["ok"] is False
    out = r.ag.launch({**base, "rules": ["H4", "CUSHION100"]}, NOW)
    assert out["ok"] and out["session"]["limits"]["rules"] == ["H4", "CUSHION100"]


def test_routes_list_the_rules_and_refuse_an_unknown_rule_id(store):
    from tests.agent.test_routes import World, launch_body
    w = World(store)
    st = w.pc.get("/api/agent/status").json()
    assert [x["id"] for x in st["rules"]] == ["H4", "CUSHION100", "HPANIC", "LATERESID"]
    assert all(x["description"] and x["markets"] for x in st["rules"])
    out = w.pc.post("/api/agent/launch", json=launch_body(rules=["H9"])).json()
    assert (out["ok"], out["code"]) == (False, "invalid") and "H9" in out["reason"] and w.r.ag.latest() is None
    out = w.pc.post("/api/agent/launch", json=launch_body()).json()
    assert out["ok"] and out["session"]["limits"]["rules"] == []  # default: none armed


# -- the code exits of a rule bracket: the agent may only tighten; a person keeps full control ---------------------------
def _rule_bracket(store):
    r = RuleRig(store)
    r.tick(1, step=0)
    (b,) = store.select("trading_brackets")
    assert b["stop_index_below"] == K + 200
    return r, b["id"]


def _agent(r, name, args, rid):
    return asyncio.run(r.tools.call(name, {**args, "note": "test", "request_id": rid}, NOW, session_id=SID))


def _stop_of(store, bid):
    return store.select("trading_brackets", {"id": bid})[0]["stop_index_below"]


def test_agent_cannot_remove_or_loosen_a_rule_brackets_btc_stop(store):
    r, bid = _rule_bracket(store)
    out = _agent(r, "set_btc_stop", {"bracket_id": bid, "level": None}, "s1")
    assert out["code"] == "rule_stop_locked" and "removed" in out["reason"]
    out = _agent(r, "set_btc_stop", {"bracket_id": bid, "level": K + 150}, "s2")  # further from BTC (K+400)
    assert out["code"] == "rule_stop_locked" and "tightened" in out["reason"]
    assert _stop_of(store, bid) == K + 200


def test_agent_may_tighten_a_rule_brackets_btc_stop(store):
    r, bid = _rule_bracket(store)
    out = _agent(r, "set_btc_stop", {"bracket_id": bid, "level": K + 300}, "s3")
    assert out["ok"], out
    assert _stop_of(store, bid) == K + 300


def test_agent_cannot_change_a_rule_brackets_take_profit(store):
    r, bid = _rule_bracket(store)
    out = _agent(r, "set_take_profit", {"bracket_id": bid, "price": 0.80}, "tp1")
    assert out["code"] == "rule_stop_locked"
    assert store.select("trading_brackets", {"id": bid})[0]["take_profit"] == 0.99


def test_a_person_can_still_change_a_rule_brackets_stop_and_take_profit(store):
    r, bid = _rule_bracket(store)
    out = asyncio.run(r.svc.set_index_stop(bid, None, NOW))  # the person's own path, not the agent's
    assert out["ok"], out
    assert _stop_of(store, bid) is None
    out = asyncio.run(r.svc.set_take_profit(bid, 0.80, NOW))
    assert out["ok"], out
    assert bid in (r.ag.latest()["taken_over"] or [])  # the person's change is a takeover, as before


# -- rule positions ride to settlement: no take-profit order, paid by the official result -----------------------------

def test_a_rule_entry_sends_exactly_one_buy_and_no_take_profit_order(store):
    r = RuleRig(store)
    r.tick(5)
    posts = r.ex.posts()
    assert len(posts) == 1 and "-entry" in posts[0]["client_order_id"]  # the buy, and nothing else
    (b,) = store.select("trading_brackets")
    assert b["state"] == "open" and b["no_tp"] and b["tp_order_id"] is None and b["stop_index_below"] == K + 200
    asyncio.run(r.svc.tick(NOW + 30_000))  # the monitor never places one either
    assert len(r.ex.posts()) == 1
    (view,) = r.svc.status(NOW + 30_000)["open"]
    assert view["take_profit"] is None and view["no_tp"] is True


def test_a_rule_position_held_to_the_close_books_1_dollar_a_contract_when_it_wins(store):
    r = RuleRig(store)
    r.tick(1, step=0)
    (b,) = store.select("trading_brackets")
    held = b["held"]
    assert held > 0
    asyncio.run(r.svc.tick(CLOSE))
    assert store.select("trading_brackets")[0]["state"] == "settling"
    r.runner._scan(CLOSE, push=True)  # result pending: no close or settled event yet
    assert not {"settled", "position_closed"} & {e["type"] for e in r.runner.events.pending()}
    parts = r.ag.budget_parts(SID)
    assert parts["at_risk"] == parts["pending_settlement"] == round(b["entry_cost"], 4) and parts["realized"] == 0
    r.ex.market.settle("yes")  # UP won
    asyncio.run(r.svc.tick(CLOSE + 15_000))
    row = store.select("trading_brackets")[0]
    assert row["state"] == "closed" and row["exit_reason"] == "settled" and row["proceeds"] == held
    assert len(r.ex.posts()) == 1  # still only the buy
    parts = r.ag.budget_parts(SID)
    assert parts["at_risk"] == 0 and parts["realized"] == round(b["entry_cost"] - held, 4) < 0  # a gain frees budget
    r.runner._scan(CLOSE + 15_000, push=True)
    (ev,) = [e for e in r.runner.events.pending() if e["type"] == "settled"]
    (info,) = ev["latest"]["brackets"]  # the result itself, not only that it closed (code review 2026-10-30 #5)
    assert info["held"] == held and info["proceeds"] == held
    assert info["pnl"] == round(held - b["entry_cost"] - (row.get("tp_fees") or 0.0) - (row.get("exit_fees") or 0.0), 4)


# -- one price-cap rule for Auto, Assist and a setup light (P1 batch C, B2 "Auto vs Assist price caps differ") -----
def test_entry_cap_is_the_matched_ask_plus_two_cents_never_above_the_rules_cap():
    h4 = R.RULES["H4"]  # ask cap 95c
    assert R.APPROVE_SLACK == 0.02
    assert R.entry_cap(h4, 0.58) == 0.60
    assert R.entry_cap(h4, 0.94) == 0.95  # 96c would be above the rule's own cap
    assert R.entry_cap(R.RULES["HPANIC"], 0.34) == 0.35
    for bad in (None, "", "x", 0, 1.0, -0.1, float("nan")):
        assert R.entry_cap(h4, bad) is None, bad  # no usable ask: no cap, nothing is sent


def test_auto_assist_and_setups_all_take_the_one_cap_rule():
    import inspect

    from btc_signal_engine.application import agent_proposals, agent_runner, setups
    for mod in (agent_runner, agent_proposals, setups):
        src = inspect.getsource(mod)
        assert "entry_cap(" in src, mod.__name__
        assert "APPROVE_SLACK, 2)" not in src, mod.__name__  # no private copy of the formula left
    assert agent_proposals.APPROVE_SLACK is R.APPROVE_SLACK
