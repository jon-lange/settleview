"""The Record tab's numbers (application/record_summary.py, GET /api/record/summary): the same computation as the
Telegram daily summary (the notifier now calls it), over today, the last week or everything, local days."""

from __future__ import annotations

from datetime import UTC

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from btc_signal_engine.adapters.http.record_routes import register_record_routes
from btc_signal_engine.application import record_summary as rec
from tests.telegram.test_activity import LAUNCH, SID, World
from tests.trading.helpers import NOW

DAY = rec.DAY_MS


def mine(store, bid, *, closed_ts, cost=0.61, proceeds=0.0, side="DOWN", strategy="you"):
    """Your own Play trade (no session), closed and settled at `closed_ts`."""
    store.insert("trading_brackets", [{
        "id": bid, "env": "shadow", "session_id": None, "kind": "entry", "ticker": "T", "side": side,
        "window_end": closed_ts, "stake": 1.0, "contracts": 1.0, "limit_price": 0.6, "take_profit": 0.0, "stop": 0.0,
        "max_cost": 1.0, "ticket_hash": "h", "state": "closed", "filled": 1.0, "held": 0.0, "tp_filled": 0.0,
        "entry_avg": 0.6, "entry_cost": cost, "proceeds": proceeds, "exit_reason": "settled",
        "created_ts": closed_ts - 900_000, "updated_ts": closed_ts, "closed_ts": closed_ts, "strategy": strategy}],
        ignore_conflicts=False)


def world(store) -> World:
    """The daily summary test's day: your losing Play trade and a winning CUSHION100 rule trade, API $0.05."""
    w = World(store, launch=False)
    mine(store, "mine", closed_ts=NOW)
    w.ag.launch(LAUNCH, NOW)
    w.rule_entry()
    w.bracket("b1", state="closed", exit_reason="settled", proceeds=1.16, closed_ts=NOW)
    w.ag.note_usage(SID, NOW, api_usd=0.05)
    w.ag.stop(SID, NOW, "stopped")
    return w


def test_today_matches_the_daily_summary(store):
    w = world(store)
    s = rec.summary(store, NOW, "today", tz=UTC)
    day = w.c._day(NOW)  # the daily summary's own figures (−$0.45 · 2 trades · 1 won)
    assert (s["net"], s["trades"], s["wins"]) == (day["net"], day["trades"], day["wins"]) == (-0.45, 2, 1)
    assert s["you"] == {"trades": 1, "wins": 0, "net": -0.61}
    assert s["agent"] == {"trades": 1, "wins": 1, "net": 0.16}
    assert s["api"] == {"usd": 0.05, "wakes": 0, "sessions": 1}
    assert s["by_rule"] == [{"rule": "CUSHION100", "trades": 1, "wins": 1, "net": 0.16}]
    assert s["start_ms"] == rec.local_day_start(NOW, UTC) and s["range"] == "today"
    [a] = s["by_agent"]
    assert (a["name"], a["trades"], a["wins"], a["net"], a["api_usd"], a["net_after_api"], a["runs"]) ==         ("Kalshi 15m agent", 1, 1, 0.16, 0.05, 0.11, 1)


def test_every_agent_session_counts_and_each_agent_has_its_line(store):
    """Two agents running at once (Kalshi 15m and Polymarket 15m): both sessions' trades and API count, and By agent
    splits them."""
    w = world(store)
    ids = iter(["sess0002"])
    w.ag._id = lambda: next(ids)
    spec = {k: v for k, v in LAUNCH.items() if k != "venues"}
    out = w.ag.launch({**spec, "market": "polymarket_15m", "rules": []}, NOW)
    assert out["ok"], out
    w.bracket("b2", session_id="sess0002", strategy="you_poly", state="closed", exit_reason="settled",
              proceeds=0.0, closed_ts=NOW, filled=1.0, entry_cost=0.3)
    w.ag.note_usage("sess0002", NOW, api_usd=0.02)
    s = rec.summary(store, NOW, "today", tz=UTC)
    assert s["agent"] == {"trades": 2, "wins": 1, "net": -0.14}
    assert s["api"] == {"usd": 0.07, "wakes": 0, "sessions": 2}
    assert [(a["name"], a["trades"], a["net_after_api"]) for a in s["by_agent"]] ==         [("Kalshi 15m agent", 1, 0.11), ("Polymarket 15m agent", 1, -0.32)]


def test_week_and_all_reach_further_back_and_today_does_not(store):
    world(store)
    mine(store, "old3", closed_ts=NOW - 3 * DAY, cost=0.5, proceeds=1.0, side="UP")  # 3 days ago: a win
    mine(store, "old30", closed_ts=NOW - 30 * DAY, cost=0.4)  # a month ago
    today = rec.summary(store, NOW, "today", tz=UTC)
    week = rec.summary(store, NOW, "week", tz=UTC)
    every = rec.summary(store, NOW, "all", tz=UTC)
    assert (today["trades"], week["trades"], every["trades"]) == (2, 3, 4)
    assert week["you"] == {"trades": 2, "wins": 1, "net": -0.11}
    assert every["net"] == round(-0.45 + 0.5 - 0.4, 2)
    assert week["start_ms"] == rec.local_day_start(NOW, UTC) - 6 * DAY  # today + the six days before
    assert every["start_ms"] == 0


def test_a_rule_a_session_ran_with_shows_with_no_trades(store):
    w = World(store, launch=False)
    w.ag.launch({**LAUNCH, "rules": ["CUSHION100", "H4"]}, NOW)
    s = rec.summary(store, NOW, "today", tz=UTC)
    assert s["trades"] == 0 and s["net"] == 0
    assert [(x["rule"], x["trades"]) for x in s["by_rule"]] == [("CUSHION100", 0), ("H4", 0)]


def test_an_unknown_range_is_an_error_never_a_default(store):
    with pytest.raises(ValueError, match="unknown range"):
        rec.summary(store, NOW, "month")


def test_the_route_needs_a_known_range(store):
    world(store)
    app = FastAPI()
    register_record_routes(app, store, lambda: NOW, tz=UTC)
    c = TestClient(app)
    r = c.get("/api/record/summary", params={"range": "today"}).json()
    assert r["ok"] and (r["net"], r["trades"], r["wins"]) == (-0.45, 2, 1)
    assert c.get("/api/record/summary").status_code == 422
    assert c.get("/api/record/summary", params={"range": "month"}).status_code == 422


def test_record_all_links_read_incrementally_equal_a_full_read(store):
    """B5#8: RuleLinks folds the journal's rule / decision rows as rule_links does, then reads only the new ones."""
    w = world(store)
    links = rec.RuleLinks(store)
    links.OVERLAP = 1  # this small journal: the re-read overlap would cover it all
    assert links.all() == rec.rule_links(store)
    w.ag.journal("decision", {"action": "ENTRY", "rule": "H4", "market": "kalshi_15m", "bracket_id": "late"}, NOW,
                 session_id=SID)
    w.ag.journal("rule", {"rule": "CUSHION100", "ok": True, "bracket_id": "late", "market": "kalshi_15m"}, NOW,
                 session_id=SID)
    seen = []
    real = store.select

    def select(table, where=None, **kw):
        seen.append((table, kw.get("filters")))
        return real(table, where, **kw)
    store.select = select
    got = links.all()
    assert got == rec.rule_links(store) and got["late"]["rule"] == "CUSHION100"  # a rule entry wins
    ((_, filters),) = [x for x in seen if x[0] == "agent_journal"][:1]
    assert any(col == "id" and op == ">" and v > 0 for col, op, v in filters)  # not the whole journal again
    assert rec.summary(store, NOW, "all", all_links=links) == rec.summary(store, NOW, "all")
