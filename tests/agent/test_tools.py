"""Agent tool registry (U4): the strict schemas, the reads an agent decides from, and the writes that go through the
ordinary prepare -> OrderGate -> approve path as the running session's agent.

A real TradingService on the shadow exchange (no network) behind a real PlayService; the agent session is real."""

from __future__ import annotations

import asyncio
import json

from btc_signal_engine.application.agent_sessions import AgentSessions
from btc_signal_engine.application.agent_tools import AgentTools
from btc_signal_engine.application.play import PlayService, PlayVenue
from btc_signal_engine.application.trading import TradingConfig
from tests.trading.helpers import CLOSE, NOW, TICKER, W0, Exchange, Market
from tests.trading.test_strategy_scope import svc_for, up

HOUR = 3_600_000
K = 80_000.0
SID = "sess0001abcd"
CONFIG = TradingConfig(enabled=True, max_stake_per_trade=25.0, max_dollars_per_window=50.0, max_dollars_per_day=100.0)


def evaluation() -> dict:
    return {"boundary": W0, "window_end": CLOSE, "fair_up": 0.58, "model": {"price": K + 40},
            "market": {"ticker": TICKER, "strike": K, "close_ts": CLOSE, "open_ts": W0},
            "pressure": [{"horizon": 60, "label": "1m", "lean_call": "UP"},
                         {"horizon": 300, "label": "5m", "lean_call": "SKIP"}]}


class Rig:
    def __init__(self, store, *, odds_fn=None, events=None):
        self.ex = Exchange(Market(yes_bid=0.55, no_bid=0.42))  # YES ask 58c, NO ask 45c
        # market protection 2c: a market buy's limit is the ask + 2c
        self.svc = asyncio.run(up(svc_for(store, self.ex, "you", mode="shadow", index_price_fn=lambda: (NOW, K + 45),
                                          market_band_fn=lambda: 0.02, config=CONFIG), present=False))
        book = self.ex.market

        def quote(now):  # the screen's quote follows the same book
            yb, nb = book.book["yes"][0][0], book.book["no"][0][0]
            return {"ticker": TICKER, "yes_bid": yb, "yes_ask": round(1 - nb, 2), "no_bid": nb,
                    "no_ask": round(1 - yb, 2), "close_ts": CLOSE, "open_ts": W0, "age_ms": 300}

        self.play = PlayService({"kalshi": PlayVenue(self.svc, quote_fn=quote)}, evaluation_fn=evaluation,
                                index_fn=lambda: (NOW, K + 45),
                                index_series_fn=lambda a, b: [(W0 + i * 1000, K + i % 50) for i in range(300)])
        self.ag = AgentSessions(store, {"you": self.svc}, id_fn=lambda: SID)
        self.svc._agent_gate_fn, self.svc._agent_takeover_fn = self.ag.check, self.ag.takeover
        self.tools = AgentTools(self.ag, self.play, odds_fn=odds_fn, events_fn=lambda sid: events)
        self.store = store

    def launch(self, **kw):
        spec = {"kind": "in_app", "venues": ["kalshi_15m"], "budget": 20.0, "per_trade": 10.0,
                "ends_ts": NOW + HOUR, "spend_cap": 5.0, "model": "claude-haiku-5-5", "level": "auto"}
        spec.update(kw)
        assert self.ag.launch(spec, NOW)["ok"]
        return self

    def call(self, name, args, now=NOW, session_id=SID):
        return asyncio.run(self.tools.call(name, args, now, session_id=session_id))

    def buys(self):
        return [p for p in self.ex.posts() if p and "-entry" in p.get("client_order_id", "")]


def entry(**kw):
    a = {"market": "kalshi_15m", "side": "UP", "stake": 5.0, "order_type": "market", "max_price": 0.60,
         "max_worst_case": 6.0, "note": "UP 58c, BTC $45 over the strike; invalid below the strike.",
         "request_id": "r1"}
    a.update(kw)
    return a


# -- the registry -----------------------------------------------------------------------------------------------------
def test_tool_list_is_name_sorted_strict_and_deterministic(store):
    t = Rig(store).tools
    listed = t.tools()
    names = [x["name"] for x in listed]
    assert names == sorted(names) and len(names) == len(set(names))
    for want in ("observe", "get_signals", "get_odds", "get_btc_series", "get_scoreboard", "get_journal",
                 "quote_entry", "place_entry", "place_exit", "set_take_profit", "set_btc_stop", "quote_perp_entry",
                 "place_perp_entry", "place_perp_exit", "place_perp_trigger", "set_alarms", "snooze",
                 "update_notes", "end_session", "record_decision"):
        assert want in names
    for x in listed:
        assert set(x) == {"name", "description", "input_schema"}
        s = x["input_schema"]
        assert s["type"] == "object" and s["additionalProperties"] is False and "required" in s
    rd = next(x for x in listed if x["name"] == "record_decision")["input_schema"]
    assert set(rd["required"]) == set(rd["properties"])  # every field explicit, null when unknown
    for w in ("place_entry", "place_exit", "set_take_profit", "set_btc_stop"):
        req =next(x for x in listed if x["name"] == w)["input_schema"]["required"]
        assert "note" in req and "request_id" in req
    assert t.tools_json() == Rig(store).tools.tools_json() == json.dumps(listed, sort_keys=True,
                                                                         separators=(",", ":"))


def test_unknown_tool_extra_argument_or_unknown_venue_is_invalid_and_prepares_nothing(store):
    r = Rig(store).launch()
    for name, args in (("buy_everything", {}), ("place_entry", {**entry(), "leverage": 5}),
                       ("place_entry", entry(market="poly")), ("place_entry", entry(side="up")),
                       ("place_entry", entry(stake=True)), ("place_entry", entry(order_type="stop")),
                       ("quote_entry", {"market": "kalshi_15m", "side": "UP", "stake": 5.0, "order_type": "market",
                                        "max_price": 0.6, "extra": 1})):
        out = r.call(name, args)
        assert (out["ok"], out["code"]) == (False, "invalid") and out["reason"], (name, args)
    assert r.svc._tickets == {} and r.buys() == []
    assert not [a for a in store.select("trading_audit") if a["event"] == "proposed"]


# -- reads ------------------------------------------------------------------------------------------------------------
def test_observe_has_every_listed_field_for_allowed_markets_and_none_for_others(store):
    r = Rig(store).launch()
    out = r.call("observe", {})
    assert out["ok"] and set(out["markets"]) == {"kalshi_15m"}  # polymarket_15m / 5m are not this session's
    m = out["markets"]["kalshi_15m"]
    for k in ("window", "market", "call", "signals", "cone", "btc", "entry", "positions", "btc_series", "btc_levels",
              "mode", "limits", "stake", "ready", "reason"):
        assert k in m, k
    assert m["window"]["end"] == CLOSE and m["window"]["seconds_left"] == 600 and m["window"]["entry_open"]
    assert m["market"]["yes_ask"] == 0.58 and m["market"]["fresh"] is True and m["market"]["ticker"] == TICKER
    assert m["call"]["agree"] and "action" in m["call"]
    assert [s["key"] for s in m["signals"]] == ["market", "model", "pressure_60", "pressure_300", "pressure_900",
                                                "crowd"]
    assert m["btc"]["gap"] == 45.0
    up_ = m["entry"]["UP"]
    for k in ("bid", "ask", "contracts", "if_wins", "if_loses", "take_profit", "market"):
        assert k in up_, k
    assert up_["market"]["worst_case_cost"] > 0 and up_["market"]["max_price"] == 0.60  # the 58c ask + 2c protection
    assert m["stake"]["value"] == 10.0  # sized at the session's per-trade cap
    assert 20 <= len(m["btc_series"]) <= 31 and m["btc_levels"]["hi"] is not None
    assert "balances" in out
    s = out["session"]
    assert s["state"] == "running" and s["modes"] == {"kalshi_15m": "PAPER"}
    assert s["budget"] == {"total": 20.0, "realized": 0.0, "at_risk": 0.0, "pending_settlement": 0.0, "used": 0.0,
                           "left": 20.0}
    assert s["per_trade"] == 10.0 and s["seconds_left"] == 3600 and s["api_usd"] == 0 and s["wakes"] == 0
    assert len(json.dumps(out)) < 40_000  # compact: well under 10k tokens


def test_observe_without_a_session_shows_every_market_and_no_session(store):
    out = Rig(store).call("observe", {}, session_id=None)
    assert out["ok"] and set(out["markets"]) == {"kalshi_15m", "polymarket_15m", "polymarket_5m"}
    assert out["session"] is None and out["markets"]["polymarket_5m"]["available"] is False


def test_signals_say_flips_are_not_available_yet_and_odds_come_only_from_the_app(store):
    r = Rig(store).launch()
    sig = r.call("get_signals", {"market": "kalshi_15m"})
    assert sig["ok"] and len(sig["signals"]) == 6 and sig["flips"] is None and "not available yet" in sig["flips_note"]
    odds = r.call("get_odds", {"market": "kalshi_15m"})
    assert (odds["ok"], odds["code"]) == (False, "not_available") and "not available yet" in odds["reason"].lower()
    series = {"ticker": TICKER, "points": [{"ts": W0, "yes_bid": 0.5, "yes_ask": 0.52}]}
    r2 = Rig(store, odds_fn=lambda key, now: series if key == "kalshi_15m" else None)
    assert r2.call("get_odds", {"market": "kalshi_15m"}, session_id=None) == {"ok": True, "market": "kalshi_15m",
                                                                                "odds": series}
    btc = r.call("get_btc_series", {"market": "kalshi_15m"})
    assert btc["ok"] and len(btc["btc_series"]) == 300 and btc["btc_levels"]["open"] == K and "cone" in btc


def test_quote_entry_gives_the_ticket_numbers_and_leaves_no_token(store):
    r = Rig(store).launch()
    q = r.call("quote_entry", {"market": "kalshi_15m", "side": "UP", "stake": 5.0, "order_type": "market"})
    assert q["ok"] and q["would_send"] is True, q
    t = q["ticket"]
    assert t["limit_price"] == 0.60 and t["sized_at"] == 0.58 and t["contracts"] > 0 and t["max_cost"] > 5.0
    assert "token" not in q and r.svc._tickets == {} and r.buys() == []


# -- writes -----------------------------------------------------------------------------------------------------------
def test_place_entry_refused_when_the_repriced_ticket_breaks_the_agents_bounds(store):  # Covers AE3
    r = Rig(store).launch()
    r.ex.market.set(0.62, 0.36)  # the ask moved from 58c to 64c since the agent looked
    out = r.call("place_entry", entry(max_price=0.62))
    assert (out["ok"], out["code"]) == (False, "outside_your_bounds"), out
    assert "66¢" in out["reason"] and out["ticket"]["limit_price"] == 0.66 and out["ticket"]["sized_at"] == 0.64
    assert r.buys() == [] and r.svc._tickets == {} and store.select("trading_brackets") == []
    out = r.call("place_entry", entry(request_id="r2", max_price=0.70, max_worst_case=1.0))  # worst case over $1
    assert out["code"] == "outside_your_bounds" and "worst case" in out["reason"] and r.buys() == []


def test_place_entry_within_bounds_fills_once_stamped_with_the_session(store):
    r = Rig(store).launch()
    out = r.call("place_entry", entry())
    assert out["ok"] and out["bracket"]["state"] == "open", out
    assert len(r.buys()) == 1
    b = store.select("trading_brackets")[0]
    assert b["session_id"] == SID and b["take_profit"] == 0.99  # the screen's take profit, as the phone sends
    again = r.call("place_entry", entry(note="retry"))  # same request_id and order: the first answer, marked repeat
    assert again == {**out, "repeat": True} and len(r.buys()) == 1
    rows = [j for j in r.ag.journal_rows(SID) if j["kind"] == "tool"]
    assert [j["data"]["name"] for j in rows] == ["place_entry", "place_entry"]
    assert rows[0]["data"]["args"]["note"].startswith("UP 58c") and rows[0]["data"]["ok"] is True
    assert rows[1]["data"]["repeat"] is True
    obs = r.call("observe", {})
    pos = obs["markets"]["kalshi_15m"]["positions"]
    assert len(pos) == 1 and pos[0]["owner"] == "agent" and pos[0]["advice"]
    assert obs["session"]["budget"]["at_risk"] == b["max_cost"]


def test_concurrent_entries_of_one_session_share_its_budget_and_exactly_one_is_sent(store):  # plan U3
    # One agent = one market (multi-agent): two entries racing in one session still pass its lock one at a time.
    r = Rig(store).launch(budget=7.0, per_trade=6.0)  # room for one $5 entry, not two

    async def both():
        return await asyncio.gather(
            r.tools.call("place_entry", entry(request_id="k1"), NOW, session_id=SID),
            r.tools.call("place_entry", entry(request_id="k2"), NOW, session_id=SID))
    outs = asyncio.run(both())

    assert sorted(o["ok"] for o in outs) == [False, True], outs
    assert next(o for o in outs if not o["ok"])["code"] in ("over_budget", "max_open")
    assert len(r.buys()) == 1 and len(store.select("trading_brackets")) == 1


def test_a_session_on_two_markets_is_refused(store):
    r = Rig(store)
    out = r.ag.launch({"kind": "in_app", "venues": ["kalshi_15m", "polymarket_15m"], "budget": 5.0, "per_trade": 1.0,
                       "ends_ts": NOW + HOUR, "spend_cap": 5.0, "model": "claude-haiku-5-5", "level": "auto"}, NOW)
    assert (out["ok"], out["code"]) == (False, "one_market")


def test_a_request_id_reused_for_a_different_action_is_refused_and_does_nothing(store):
    r = Rig(store).launch()
    out = r.call("place_entry", entry())
    assert out["ok"] and len(r.buys()) == 1
    bid = out["bracket"]["id"]
    other = r.call("place_entry", entry(side="DOWN", max_price=0.47))  # same request_id "r1", a different order
    assert other["ok"] is False and other["code"] == "request_id_reused" and len(r.buys()) == 1
    ex = r.call("place_exit", {"bracket_id": bid, "note": "thesis broken", "request_id": "r1"})  # a different tool
    assert ex["ok"] is False and ex["code"] == "request_id_reused"
    assert store.select("trading_brackets")[0]["state"] == "open"  # the position was not sold
    rows = [j["data"] for j in r.ag.journal_rows(SID) if j["kind"] == "tool"]
    assert [(x["name"], x["code"], x["repeat"]) for x in rows][1:] == [("place_entry", "request_id_reused", True),
                                                                       ("place_exit", "request_id_reused", True)]


def test_a_disallowed_market_or_an_ended_session_sends_nothing(store):
    r = Rig(store).launch()
    out = r.call("place_entry", entry(market="polymarket_15m"))
    assert out["code"] == "venue_not_allowed" and r.buys() == []
    r.ag.stop(r.ag.latest()["id"], NOW)
    out = r.call("place_entry", entry(request_id="r9"))
    assert (out["ok"], out["code"]) == (False, "no_agent_session") and r.buys() == []
    assert r.call("place_entry", entry(request_id="r10"), session_id=None)["code"] == "no_agent_session"


def test_exit_and_adjust_own_position(store):
    r = Rig(store).launch()
    bid = r.call("place_entry", entry())["bracket"]["id"]
    tp = r.call("set_take_profit", {"bracket_id": bid, "price": 0.9, "note": "lock in", "request_id": "t1"})
    assert tp["ok"] and tp["bracket"]["take_profit"] == 0.9, tp
    st = r.call("set_btc_stop", {"bracket_id": bid, "level": K - 20, "note": "stop under strike", "request_id": "s1"})
    assert st["ok"] and st["bracket"]["stop_index_below"] == K - 20, st
    ex = r.call("place_exit", {"bracket_id": bid, "note": "thesis broken", "request_id": "x1"})
    assert ex["ok"], ex
    assert store.select("trading_brackets")[0]["state"] == "closed"


# -- session tools ----------------------------------------------------------------------------------------------------
def test_notes_alarms_and_end_session(store):
    from btc_signal_engine.application.agent_events import AgentEvents
    ev = AgentEvents()
    r = Rig(store, events=ev).launch()
    assert r.call("update_notes", {"text": "p(UP)=0.55 experimental"})["ok"]
    assert r.tools.notes(SID) == "p(UP)=0.55 experimental"
    a = r.call("set_alarms", {"alarms": [{"kind": "btc_below", "value": K - 30},
                                         {"kind": "mid_above", "value": 0.7, "market": "kalshi_15m"}]})
    assert a["ok"] and ev.alarms()[1] == {"kind": "mid_above", "value": 0.7, "venue": "kalshi", "span": "15m"}
    assert r.call("snooze", {"until_event": "nonsense"})["code"] == "invalid"
    assert r.call("snooze", {"until_ms": NOW + 60_000})["ok"] and ev.snoozed()["until_ms"] == NOW + 60_000
    out = r.call("end_session", {"reason": "No edge today."})
    assert out["ok"] and r.ag.latest() is None
    row = store.select("agent_sessions")[0]
    assert (row["state"], row["end_reason"]) == ("ended", "agent") and ev.alarms() == []
    kinds = [(j["kind"], j["data"].get("name")) for j in r.ag.journal_rows(SID)]
    assert ("tool", "end_session") in kinds and ("notes", None) in kinds


def test_alarms_without_an_event_engine_and_perps_are_not_available(store):
    r = Rig(store).launch()
    assert r.call("set_alarms", {"alarms": []})["code"] == "not_available"
    for name in ("quote_perp_entry", "place_perp_entry", "place_perp_exit", "place_perp_trigger"):
        out = r.call(name, {})
        assert out == {"ok": False, "code": "not_available", "reason": "Perps for agents come in a later step."}


# -- record_decision --------------------------------------------------------------------------------------------------
def decision(**kw):
    a = {"market": "kalshi_15m", "action": "NO_TRADE", "side": None, "p_estimate": 0.56, "p_low": 0.52,
         "p_high": 0.60, "p_source": "market_implied", "market_p": 0.58, "edge_cents": -2.4,
         "pass_reason": "NO_VALIDATED_EDGE", "invalidation": "BTC moves $40 past the strike.",
         "thesis": "The market price is the probability; no edge after fees.", "next_check": "2 min before close"}
    a.update(kw)
    return a


def test_record_decision_stores_one_decision_row_with_server_fields(store):
    r = Rig(store).launch()
    mid = r.call("observe", {})["markets"]["kalshi_15m"]["call"]["mid_up"]
    out = asyncio.run(r.tools.call("record_decision", decision(), NOW, session_id=SID, wake_id=7))
    assert out["ok"], out
    rows = r.tools.decisions(SID)
    assert len(rows) == 1
    d = rows[0]
    assert {k: d[k] for k in decision()} == decision()
    assert d["ts"] == NOW and d["wake_id"] == 7 and d["source"] == "agent" and d["note"] is None
    assert (d["window_start"], d["window_end"]) == (W0, CLOSE) and mid is not None and d["mid_up"] == mid
    assert isinstance(d["id"], int) and r.tools.decisions(SID, after_id=d["id"]) == []
    # a portfolio-level decision has no market, so no window or mid
    out = r.call("record_decision", decision(market=None, action="HOLD", pass_reason=None, p_estimate=None,
                                             p_low=None, p_high=None, p_source="unknown", market_p=None,
                                             edge_cents=None, next_check=None))
    assert out["ok"], out
    d2 = r.tools.decisions(SID)[-1]
    assert d2["wake_id"] is None and (d2["window_start"], d2["window_end"], d2["mid_up"]) == (None, None, None)
    assert [x["action"] for x in r.tools.decisions(SID)] == ["NO_TRADE", "HOLD"]


def test_record_decision_cross_field_rules_are_invalid_and_store_nothing(store):
    r = Rig(store).launch()
    for args in (decision(pass_reason=None),  # NO_TRADE needs its reason
                 decision(p_low=0.57),  # p_low above p_estimate
                 decision(p_high=0.55),  # p_high below p_estimate
                 decision(p_estimate=None, p_low=0.6, p_high=0.5),
                 decision(action="ENTRY", side="UP"),  # a pass reason only on NO_TRADE / HOLD
                 decision(pass_reason="BORED"), decision(action="BUY"), decision(p_estimate=1.2),
                 decision(thesis="x" * 601), decision(thesis=""),
                 {k: v for k, v in decision().items() if k != "invalidation"},
                 {**decision(), "confidence": 0.9}):
        out = r.call("record_decision", args)
        assert (out["ok"], out["code"]) == (False, "invalid") and out["reason"], args
    assert r.call("record_decision", decision(market="polymarket_5m"))["code"] == "venue_not_allowed"
    assert r.tools.decisions(SID) == []
