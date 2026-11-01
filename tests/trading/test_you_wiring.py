"""Bootstrap wiring of the Play app: only the phone services ("you", plus "you_poly" / "you_poly5" with Polymarket
keys) on the shared gateways, the Coach evaluation, the loops Play needs, the routes Play calls (and none of the
retired ones), every phone service knowing the retired bots' past orders, and a live bracket nothing monitors is
warned about at start."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from btc_signal_engine.adapters.http.app import create_app
from btc_signal_engine.bootstrap.app import Runtime
from btc_signal_engine.bootstrap.config import Settings
from tests.pc import paired

K15 = {"venue": "kalshi", "span": "15m"}  # /api/play/* require the tab explicitly


def build(tmp_path, name="you.db", **kw) -> Runtime:
    return Runtime(Settings(_env_file=None, db_path=tmp_path / name, trading_stream=False, **kw))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("BTCFLOW_TRADING_STREAM", "BTCFLOW_API_HOST", "BTCFLOW_ACCESS_PIN", "BTCFLOW_POLYMARKET_KEY_ID",
              "BTCFLOW_POLYMARKET_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def rt(tmp_path):
    r = build(tmp_path)
    yield r
    r.writer.close()


def test_only_the_phone_service_is_built(rt):
    you = rt.trading_services["you"]
    assert set(rt.trading_services) == {"you"} and you is rt.you_trading and you.strategy == "you"
    assert you.gateways == {e: rt.trading_gws[e] for e in ("demo", "shadow", "prod")}
    assert you._index_price_fn == rt.live_price
    # the phone's own user-set limits (read live; default none / none) and the cash backstop
    assert you._limits_fn == rt.play_limits.for_trading and you.cash_backstop
    assert you.limits()[0] == {"per_trade": None, "per_window": None, "per_day": None}
    assert all(gw.max_order_cost == rt.settings.trading_order_hard_ceiling
               for gw in rt.trading_gws.values() if gw is not None)
    assert you.mode == "sandbox"  # the service's safe default on a first start
    assert rt.play.venues["kalshi"].svc is you
    assert rt.play.venues["polymarket"].svc is None and rt.you_poly_trading is None  # no Polymarket keys
    for gone in ("trading", "pnw_trading", "lab", "board", "jobs", "picks", "portfolio", "live_view"):
        assert not hasattr(rt, gone), gone


def test_the_loops_are_the_ones_play_needs(rt):
    assert set(rt.loops()) == {"writer", "decision", "periodic", "kalshi_live", "stream", "you", "balance",
                               "connectivity", "rv_p95", "stop_file", "memory", "maintenance"}
    # the daily backup + trade archive (U14): Coinbase trades older than 60 days by default, next to the database
    assert rt.maintenance.retention_days == rt.settings.trades_retention_days == 60
    assert rt.maintenance.archive_path == rt.settings.db_path.with_name("you-archive.db")


def test_play_and_login_routes_are_registered_and_the_retired_ones_are_gone(rt):
    HOST = f"http://127.0.0.1:{rt.settings.api_port}"  # a Host on the allow-list (U8)
    c = paired(TestClient(create_app(rt.services()), client=("127.0.0.1", 1), base_url=HOST), rt.settings.data_dir)
    v = c.get("/api/play/now", params=K15).json()
    assert v["strategy"] == "you" and v["ready"] is False and v["positions"] == []
    assert c.get("/api/trading/status", params={"strategy": "you"}).json()["strategy"] == "you"
    assert c.get("/api/play/scoreboard", params=K15).json()["record"]["trades"] == 0
    assert c.get("/api/play/limits").status_code == 200
    assert c.get("/api/coach/pause").json()["paused"] is False
    assert c.get("/api/coach/series", params={"window": "current"}).status_code == 200
    assert c.post("/api/trading/heartbeat", json={"visible": True}).json()["present"] is True
    assert c.post("/api/trading/kill").json()["killed"] is True
    assert c.post("/api/trading/resume").json()["killed"] is False
    assert c.get("/api/trading/status", params={"strategy": "taker"}).json()["code"] == "unknown_strategy"
    for path in ("/api/live", "/api/picks", "/api/jobs", "/api/performance", "/api/rulesets", "/api/coach",
                 "/api/strategies", "/api/modes", "/api/portfolio", "/api/trading/auto", "/api/trading/session",
                 "/api/trading/audit", "/api/trading/trades", "/api/glossary", "/api/settled"):
        assert c.get(path).status_code in (404, 405), path  # 405: the static app's GET-only mount answers POSTs
    remote = TestClient(create_app(rt.services()), client=("10.0.0.9", 1), follow_redirects=False,
                        base_url=HOST)
    assert remote.get("/api/play/now", params=K15).status_code == 403  # no PIN set: phone access is off


def test_the_phone_service_knows_the_retired_bots_past_orders(rt):
    rows = [{"bracket_id": "b", "role": "entry", "ticker": "T", "side": "yes", "action": "buy", "count": 1.0,
             "price": 0.5, "tif": "ioc", "client_order_id": f"c-{s}", "order_id": f"x-{s}", "status": "executed",
             "created_ts": 1, "updated_ts": 1, "strategy": s} for s in ("taker", "pnw_cushion", "you")]
    rt.store.insert("trading_orders", rows)
    assert rt._other_order_ids("you") == {"c-taker", "x-taker", "c-pnw_cushion", "x-pnw_cushion"}
    assert rt.you_trading._known_order_ids_fn() == rt._other_order_ids("you")
    # asked about the ids being judged only: those of them the other strategies placed (by order id or coid)
    assert rt.you_trading._known_order_ids_fn({"x-taker", "c-pnw_cushion", "x-you", "c-you", "zzz"}) == {
        "x-taker", "c-pnw_cushion"}
    assert rt.you_trading._known_order_ids_fn(set()) == set()


def test_a_live_bracket_nothing_monitors_is_warned_about_at_start(tmp_path, caplog, monkeypatch):
    lg = logging.getLogger("btc_signal_engine")  # other tests may have detached it (dictConfig / propagate)
    monkeypatch.setattr(lg, "disabled", False)
    monkeypatch.setattr(lg, "propagate", True)
    first = build(tmp_path, "w.db")
    first.store.insert("trading_brackets", [{
        "id": "b1", "env": "prod", "strategy": "pnw_cushion", "kind": "entry", "ticker": "KXBTC15M-26OCT291200-00",
        "side": "UP", "window_end": 1, "stake": 1.0, "contracts": 1.0, "limit_price": 0.7, "take_profit": 0.99,
        "stop": 0.0, "max_cost": 0.71, "ticket_hash": "h", "state": "open", "filled": 1.0, "held": 1.0,
        "tp_filled": 0.0, "proceeds": 0.0, "exit_fees": 0.0, "exit_filled": 0.0, "created_ts": 1, "updated_ts": 1}])
    first.writer.close()
    with caplog.at_level(logging.WARNING, logger="btc_signal_engine"):
        again = build(tmp_path, "w.db")
    again.writer.close()
    assert "pnw_cushion has a live bracket b1" in caplog.text and "nothing on this server monitors it" in caplog.text


def test_default_mode_prod_without_credentials_falls_back(store):
    from btc_signal_engine.application.trading import TradingConfig, TradingService
    svc = TradingService(store, {"demo": None, "shadow": object(), "prod": None}, TradingConfig(enabled=True),
                         strategy="you", default_mode="prod")
    assert svc.mode == "sandbox"  # production credentials missing: never starts in production
    assert store.select("trading_settings", {"key": "you.mode"})[0]["value"] == {"mode": "sandbox"}
    again = TradingService(store, {"demo": None, "shadow": object(), "prod": None}, TradingConfig(enabled=True),
                           strategy="you", default_mode="shadow")
    assert again.mode == "sandbox"  # the stored mode wins over the default after the first start
