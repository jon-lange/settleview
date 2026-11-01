"""Several Play tabs side by side: ticket routes reach the named strategy's own service (a ticket only approves on the
service that prepared it), one shared live feed watches every service's markets, the crowd summary, and the
connectivity guard."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from btc_signal_engine.adapters.http.trading_routes import register_trading_routes
from tests.pc import as_pc
from tests.trading.helpers import NOW, TICKER, Exchange, Market
from tests.trading.test_strategy_scope import svc_for, up


def run(coro):
    return asyncio.run(coro)


def test_ticket_routes_take_a_strategy_and_a_ticket_approves_only_where_it_was_prepared(store):
    ex = Exchange(Market())
    other = run(up(svc_for(store, ex, "you_poly", mode="sandbox")))
    you = run(up(svc_for(store, ex, "you", mode="sandbox")))
    app = FastAPI()
    register_trading_routes(app, {"you_poly": other, "you": you}, lambda: NOW)
    c = TestClient(as_pc(app))
    body = {"ticker": TICKER, "side": "UP", "stake": 2.0, "max_price": 0.58, "take_profit": 0.70,
            "stop_index_below": 79_000.0, "strategy": "you"}
    prep = c.post("/api/trading/prepare", json=body).json()
    assert prep["ok"], prep
    yes = {"token": prep["token"], "decision": "yes", "ticket": prep["ticket"]}
    wrong = c.post("/api/trading/approve", json={**yes, "strategy": "you_poly"})
    assert wrong.json()["ok"] is False and ex.writes() == []
    # a missing strategy is a 422 and an empty one unknown_strategy: never some service by default
    assert c.post("/api/trading/approve", json=yes).status_code == 422
    assert c.post("/api/trading/approve", json={**yes, "strategy": ""}).json()["code"] == "unknown_strategy"
    no_strategy = {k: v for k, v in body.items() if k != "strategy"}
    assert c.post("/api/trading/prepare", json=no_strategy).status_code == 422
    assert c.post("/api/trading/prepare", json={**body, "strategy": ""}).json()["code"] == "unknown_strategy"
    assert c.post("/api/trading/mode", json={"mode": "shadow"}).status_code == 422
    assert c.post("/api/trading/mode", json={"mode": "shadow", "strategy": ""}).json()["code"] == "unknown_strategy"
    assert other.mode == "sandbox" and ex.writes() == [] and store.select("trading_brackets") == []
    prep = c.post("/api/trading/prepare", json=body).json()
    r = c.post("/api/trading/approve", json={"token": prep["token"], "decision": "yes", "ticket": prep["ticket"],
                                             "strategy": "you"}).json()
    assert r["ok"], r
    [b] = store.select("trading_brackets")
    assert b["strategy"] == "you" and b["stop_index_below"] == 79_000.0
    assert c.post("/api/trading/prepare", json={**body, "strategy": "nope"}).json()["code"] == "unknown_strategy"


def test_a_shared_stream_watches_every_services_markets():
    from btc_signal_engine.bootstrap.app import SharedStream

    class St:
        env = "prod"

        def __init__(self):
            self.subs = []

        def subscribe_tickers(self, t):
            self.subs.append(set(t))

        def health(self):
            return {"connected": True, "stale": False}

    st = St()
    shared = SharedStream(st)
    other, you = shared.view("you_poly"), shared.view("you")
    you.subscribe_tickers({"A"})
    other.subscribe_tickers(set())  # the other tab has nothing in prod: this tab's market stays watched
    assert st.subs[-1] == {"A"} and you.env == "prod" and other.health()["connected"]
    other.subscribe_tickers({"B"})
    assert st.subs[-1] == {"A", "B"}


def test_crowd_summary_is_descriptive_and_flags_a_price_move_with_btc_flat():
    from btc_signal_engine.domain.strategy.crowd import crowd_summary
    now = 1_000_000
    prints = [{"ts_ms": now - 50_000, "yes_price": 0.60, "count": 100.0, "taker_book_side": "bid"},
              {"ts_ms": now - 30_000, "yes_price": 0.55, "count": 2000.0, "taker_book_side": "ask"},  # $900 Down: whale
              {"ts_ms": now - 5_000, "yes_price": 0.50, "count": 10.0, "taker_book_side": "ask"},
              {"ts_ms": now - 90_000, "yes_price": 0.70, "count": 999.0, "taker_book_side": "bid"}]  # outside the window
    c = crowd_summary(prints, now, btc_move=4.0)
    assert c["trades"] == 3 and c["up_usd"] == 60.0 and c["down_usd"] == 905.0
    assert [w["side"] for w in c["whales"]] == ["DOWN"] and c["price_move_c"] == -10.0
    assert "crowd-driven" in c["label"] and "Descriptive only" in c["note"]
    assert crowd_summary([], now)["label"] == "quiet"


def test_connectivity_guard_records_lost_and_restored_once_each(store):
    ex = Exchange(Market())
    svc = run(up(svc_for(store, ex, "you", mode="sandbox")))
    svc.note_connectivity(False, NOW, "For 30 s the BTC index is stale.")
    svc.note_connectivity(False, NOW + 5000, "still stale")  # no duplicate
    svc.note_connectivity(True, NOW + 9000, "fresh again")
    ev = [e["event"] for e in store.select("trading_audit") if e["event"].startswith("connectivity")]
    assert ev == ["connectivity_lost", "connectivity_restored"]

