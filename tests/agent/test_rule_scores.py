"""The rule scorekeeper and code-enforced retirement (2026-10-30): the per (rule, market) live tally from the journal's
rule rows and their brackets (closed only; settling is pending), the backtest reference beside it, the persisted rule
status (HPANIC seeded retired by the user), auto-retirement at 30 closed trades with net <= $0 or at net <= -$5, the
launch refusing a retired rule, a running session dropping a rule retired mid-session, and the PC-only route that
retires or (with a typed confirmation) reactivates a rule."""

from __future__ import annotations

import asyncio

import pytest

from btc_signal_engine.application import rule_scores as S
from btc_signal_engine.application.agent_rules import RULES
from tests.agent.test_rules import K, RuleRig
from tests.agent.test_tools import HOUR, SID
from tests.trading.helpers import CLOSE, NOW

K15, P15, P5 = "kalshi_15m", "polymarket_15m", "polymarket_5m"
OLD = "oldsess0"


def bracket(bid, *, filled=1.0, cost=0.70, proceeds=0.0, exit_fees=0.0, tp_fees=0.0, state="closed",
            created=NOW - 10 * HOUR, closed=None, reason="settled"):
    return {"id": bid, "filled": filled, "entry_cost": cost, "proceeds": proceeds, "exit_fees": exit_fees,
            "tp_fees": tp_fees, "state": state, "created_ts": created,
            "closed_ts": (created + 900_000 if closed is None else closed) if state == "closed" else None,
            "exit_reason": reason if state == "closed" else None}


def trades_of(*bs, rule="H4", market=K15):
    return [S.trade_of({"rule": rule, "market": market}, b) for b in bs]


# -- the tally (pure) -------------------------------------------------------------------------------------------------
def test_tally_wins_break_even_from_the_price_paid_incl_fees_and_net_incl_settled_proceeds():
    t = trades_of(
        bracket("a", filled=2.0, cost=1.40, proceeds=2.00, created=1_000, closed=2_000),  # settled win +$0.60
        bracket("b", filled=1.5, cost=0.98, proceeds=0.0, created=3_000, closed=4_000),  # settled loss -$0.98
        bracket("c", filled=1.0, cost=0.80, proceeds=0.50, exit_fees=0.02, created=5_000, closed=6_000,
                reason="stop"),  # stopped out: -$0.32
    )
    x = S.tally(t)
    assert x["trades"] == 3 and x["wins"] == 1 and x["win_pct"] == pytest.approx(33.33, abs=0.01)
    assert x["contracts"] == 4.5
    # average price paid per contract incl. fees = the hold-to-settlement break-even win rate
    assert x["avg_price"] == pytest.approx(3.18 / 4.5, abs=1e-4)
    assert x["break_even_pct"] == pytest.approx(70.67, abs=0.01)
    assert x["net_usd"] == pytest.approx(-0.70, abs=1e-9)
    assert x["net_c"] == pytest.approx(-0.70 / 4.5 * 100, abs=0.01)
    assert x["max_drawdown_usd"] == pytest.approx(1.30, abs=1e-9)  # +0.60 peak, then -0.98, -0.32
    assert x["last_trade_ts"] == 5_000 and x["pending"] == 0 and x["open"] == 0


def test_settling_counts_as_pending_not_as_a_trade_and_unfilled_is_no_trade():
    t = trades_of(bracket("a", proceeds=1.0),
                  bracket("s", state="settling", proceeds=0.0, created=NOW),
                  bracket("o", state="open", created=NOW - 5),
                  bracket("u", filled=0.0, cost=None, reason="unfilled"))
    x = S.tally(t)
    assert x["trades"] == 1 and x["wins"] == 1 and x["pending"] == 1 and x["open"] == 1
    assert x["net_usd"] == pytest.approx(0.30) and x["last_trade_ts"] == NOW


def test_an_empty_tally_has_no_rates():
    x = S.tally([])
    assert x["trades"] == 0 and x["win_pct"] is None and x["break_even_pct"] is None and x["net_c"] is None
    assert x["net_usd"] == 0 and x["max_drawdown_usd"] == 0 and x["last_trade_ts"] is None


def test_every_rule_has_a_backtest_reference_citing_its_learnings_row():
    assert set(S.BACKTEST) == set(RULES)
    assert S.BACKTEST["HPANIC"]["row"] == "H-PANIC" and S.BACKTEST["HPANIC"]["net_c"] == -4.09
    assert S.BACKTEST["HPANIC"]["break_even_pct"] == 20.5
    assert S.BACKTEST["LATERESID"]["row"] == "LATE_RESIDUAL" and S.BACKTEST["H4"]["row"] == "H4"
    assert S.BACKTEST["CUSHION100"]["row"] == "EXP-CUSHION100"


# -- the persisted status -----------------------------------------------------------------------------------------------
def seed(store, rule, market, n, *, cost=0.70, proceeds=0.0, start=NOW - 20 * HOUR, session=OLD, prefix=None):
    """n closed rule trades of an older session: journal rule rows + their brackets."""
    prefix = prefix or f"{rule}-{market}-{start}"
    rows, js = [], []
    for i in range(n):
        bid = f"{prefix}-{i}"[:40]
        created = start + i * 900_000
        rows.append({"id": bid, "env": "demo", "strategy": "you", "session_id": session, "kind": "entry",
                     "ticker": "T", "side": "UP", "window_end": created + 900_000, "stake": 1.0, "contracts": 1.0,
                     "limit_price": 0.95, "take_profit": 0.99, "stop": 0.0, "max_cost": 1.0, "ticket_hash": "h",
                     "state": "closed", "filled": 1.0, "entry_avg": cost, "entry_cost": cost, "held": 0.0,
                     "tp_filled": 0.0, "tp_fees": 0.0, "exit_fees": 0.0, "proceeds": proceeds,
                     "exit_reason": "settled", "created_ts": created, "updated_ts": created + 900_000,
                     "closed_ts": created + 900_000})
        js.append({"session_id": session, "ts": created, "kind": "rule",
                   "data": {"rule": rule, "market": market, "ok": True, "bracket_id": bid}})
    store.insert("trading_brackets", rows)
    store.insert("agent_journal", js)


def test_hpanic_is_seeded_retired_by_the_user(store):
    book = S.RuleBook(store)
    st = book.status("HPANIC")
    assert st["status"] == "retired" and st["by"] == "user" and st["reason"] == "0/3 live, killed in backtest"
    assert st["at"] == S.HPANIC_RETIRED_TS
    assert all(book.status(r)["status"] == "active" for r in ("H4", "CUSHION100", "LATERESID"))
    book.seed(NOW)
    (row,) = store.select("strategy_settings", S.SETTING)
    assert row["value"]["HPANIC"]["status"] == "retired"
    assert S.RuleBook(store).retired("HPANIC")  # persisted, read back by a fresh book


def test_auto_retire_at_30_closed_trades_with_net_at_or_below_zero(store):
    book = S.RuleBook(store)
    seed(store, "H4", K15, 29, cost=0.50, proceeds=0.50, prefix="a")  # net $0
    assert book.enforce(NOW) == [] and not book.retired("H4")  # 29 trades: not yet
    seed(store, "H4", P15, 1, cost=0.50, proceeds=0.50, start=NOW - 2 * HOUR, prefix="b")
    (ch,) = book.enforce(NOW)  # all markets together: 30 trades, net <= 0
    assert ch["rule"] == "H4" and ch["status"] == "retired" and ch["by"] == "auto" and ch["at"] == NOW
    assert "30 closed trades" in ch["reason"]
    assert book.retired("H4") and S.RuleBook(store).retired("H4")
    assert book.enforce(NOW + 1) == []  # once


def test_thirty_trades_with_a_positive_net_stay_active(store):
    book = S.RuleBook(store)
    seed(store, "CUSHION100", K15, 30, cost=0.80, proceeds=1.0)  # 30 wins
    assert book.enforce(NOW) == [] and not book.retired("CUSHION100")


def test_auto_retire_at_once_when_net_reaches_minus_5_dollars(store):
    book = S.RuleBook(store)
    seed(store, "LATERESID", K15, 7, cost=0.70, prefix="a")  # -$4.90
    assert book.enforce(NOW) == []
    seed(store, "LATERESID", P15, 1, cost=0.10, start=NOW - HOUR, prefix="b")  # -$5.00
    (ch,) = book.enforce(NOW)
    assert ch["rule"] == "LATERESID" and ch["by"] == "auto" and "−$5" in ch["reason"]  # the minus sign (domain/fmt.py)


def test_reactivation_starts_a_fresh_trial(store):
    book = S.RuleBook(store)
    seed(store, "H4", K15, 30, cost=0.95)
    assert book.enforce(NOW)
    out = book.set("H4", "active", NOW + 1, by="user", reason="Reactivated by you.")
    assert out["status"] == "active" and out["since"] == NOW + 1
    assert book.enforce(NOW + 2) == []  # the trades before the reactivation do not count again
    card = S.scorecard(store, book)
    h4 = next(r for r in card["rules"] if r["rule"] == "H4")
    assert h4["live"]["trades"] == 30 and h4["trial"]["trades"] == 0 and h4["bar"] == {"trades": 0, "of": 30}


def test_the_scorecard_rows_are_rule_by_market_with_live_and_backtest(store):
    book = S.RuleBook(store)
    seed(store, "H4", K15, 2, cost=0.90, proceeds=1.0)
    card = S.scorecard(store, book)
    assert card["auto"] == {"trades": 30, "net_at_or_below": 0.0, "loss": -5.0}
    assert [(r["rule"], r["market"]) for r in card["rows"]] == [
        (rid, m) for rid, rule in RULES.items() for m in rule.markets]
    row = next(r for r in card["rows"] if (r["rule"], r["market"]) == ("H4", K15))
    assert row["live"]["trades"] == 2 and row["live"]["net_c"] == pytest.approx(10.0)
    assert row["status"]["status"] == "active" and row["backtest"]["net_c"] == 5.4 and row["market_label"]
    hp = next(r for r in card["rules"] if r["rule"] == "HPANIC")
    assert hp["status"]["status"] == "retired"


# -- launch and the running session --------------------------------------------------------------------------------------
BASE = {"kind": "in_app", "venues": [K15], "budget": 5.0, "per_trade": 1.0, "ends_ts": NOW + HOUR,
        "spend_cap": 5.0, "model": "claude-haiku-5-5", "level": "auto"}


def test_launch_refuses_a_retired_rule_in_plain_words(store):
    r = RuleRig(store, rules=())
    r.ag.stop(r.ag.latest()["id"], NOW)
    out = r.ag.launch({**BASE, "rules": ["H4", "HPANIC"]}, NOW)
    assert (out["ok"], out["code"]) == (False, "rule_retired")
    assert "HPANIC" in out["reason"] and "0/3 live, killed in backtest" in out["reason"]
    assert r.ag.latest() is None


def test_launch_enforces_a_crossing_found_after_the_last_session(store):
    r = RuleRig(store, rules=())
    r.ag.stop(r.ag.latest()["id"], NOW)
    seed(store, "CUSHION100", K15, 6, cost=0.90)  # -$5.40, settled after its session ended
    out = r.ag.launch({**BASE, "rules": ["CUSHION100"]}, NOW)
    assert (out["ok"], out["code"]) == (False, "rule_retired") and r.ag.rule_book.status("CUSHION100")["by"] == "auto"


def test_a_running_session_stops_evaluating_a_rule_retired_mid_session(store):
    r = RuleRig(store, rules=("H4",))
    r.ag.rule_book.set("H4", "retired", NOW, by="user", reason="Retired by you.")
    r.tick(3)
    assert r.buys() == [] and r.journal("rule") == []


def test_a_retired_rules_open_position_is_unaffected(store):
    r = RuleRig(store)
    r.tick(1, step=0)
    (b,) = store.select("trading_brackets")
    r.ag.rule_book.set("H4", "retired", NOW, by="user", reason="Retired by you.")
    r.tick(2)
    (after,) = store.select("trading_brackets")
    assert after["state"] == "open" and after["stop_index_below"] == b["stop_index_below"] == K + 200


def test_the_runner_auto_retires_when_a_closing_trade_crosses_and_tells_the_panel(store):
    r = RuleRig(store, btc=K + 400)
    seed(store, "H4", P15, 29, cost=0.50, proceeds=0.50)  # older session: 29 trades, net $0
    r.tick(1, step=0)
    (b,) = [x for x in store.select("trading_brackets") if x["session_id"] == SID]
    asyncio.run(r.svc.tick(CLOSE))
    r.ex.market.settle("no")  # UP lost
    asyncio.run(r.svc.tick(CLOSE + 15_000))
    assert store.select("trading_brackets", {"id": b["id"]})[0]["state"] == "closed"
    assert not r.ag.rule_book.retired("H4")
    r.runner.clock.t = CLOSE + 15_000
    asyncio.run(r.runner.tick())
    st = r.ag.rule_book.status("H4")
    assert st["status"] == "retired" and st["by"] == "auto" and st["at"] == CLOSE + 15_000
    (j,) = r.journal("rule_status")
    assert j["rule"] == "H4" and j["status"] == "retired" and j["by"] == "auto"
    assert [x["kind"] for x in r.rows if x.get("kind") == "rule_status"] == ["rule_status"]


def test_the_agents_memory_shows_each_armed_rules_status(store):
    r = RuleRig(store)
    r.ag.rule_book.set("H4", "retired", NOW, by="user", reason="Retired by you.")
    mem = r.runner._memory(NOW, [])
    (rule,) = mem["session"]["rules"]
    assert rule["id"] == "H4" and rule["status"] == "retired"


# -- the routes -------------------------------------------------------------------------------------------------------
def test_status_marks_retired_rules_and_the_scorecard_route_reads(store):
    from tests.agent.test_routes import World
    w = World(store)
    st = w.pc.get("/api/agent/status").json()
    by = {x["id"]: x for x in st["rules"]}
    assert by["HPANIC"]["status"]["status"] == "retired" and by["H4"]["status"]["status"] == "active"
    card = w.pc.get("/api/agent/scorecard").json()
    assert card["ok"] and {r["rule"] for r in card["rules"]} == set(RULES)


def test_retire_and_reactivate_from_the_pc_reactivation_needs_the_typed_phrase(store):
    from tests.agent.test_routes import World, launch_body
    w = World(store)
    out = w.pc.post("/api/agent/rules/status", json={"rule": "H4", "status": "retired"}).json()
    assert out["ok"] and out["rule"]["status"] == "retired" and out["rule"]["by"] == "user"
    assert w.r.ag.rule_book.retired("H4")
    for bad in (None, "yes", "REACTIVATE"):
        out = w.pc.post("/api/agent/rules/status", json={"rule": "HPANIC", "status": "active", "confirm": bad}).json()
        assert (out["ok"], out["code"]) == (False, "confirm_required") and "REACTIVATE HPANIC" in out["reason"]
    assert w.r.ag.rule_book.retired("HPANIC")
    out = w.pc.post("/api/agent/rules/status",
                    json={"rule": "HPANIC", "status": "active", "confirm": "REACTIVATE HPANIC"}).json()
    assert out["ok"] and out["rule"]["status"] == "active"
    out = w.pc.post("/api/agent/launch", json=launch_body(rules=["HPANIC"])).json()
    assert out["ok"], out
    for body in ({"rule": "H9", "status": "retired"}, {"rule": "H4", "status": "paused"}):
        out = w.pc.post("/api/agent/rules/status", json=body).json()
        assert (out["ok"], out["code"]) == (False, "invalid")


def test_a_phone_cannot_retire_or_reactivate_a_rule(store):
    from tests.agent.test_routes import World
    w = World(store, access=True)
    phone = w.phone()
    res = phone.post("/api/agent/rules/status", json={"rule": "HPANIC", "status": "active",
                                                      "confirm": "REACTIVATE HPANIC"})
    assert res.status_code == 403 and w.r.ag.rule_book.retired("HPANIC")
    assert phone.get("/api/agent/scorecard").json()["ok"]  # reading is fine
