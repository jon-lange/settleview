"""Play screens over HTTP with fakes: /api/play/now's shape on Kalshi and Polymarket (DOWN = the short side converted
from the YES book), then the phone's flow through the existing trading routes with strategy=you (BUY UP ticket ->
approve -> in-trade advice -> EXIT NOW -> scoreboard and attempts). Simulated exchange only."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from btc_signal_engine.adapters.http.access import AccessGuard, PinStore, install_access
from btc_signal_engine.adapters.http.play_routes import register_play_routes
from btc_signal_engine.adapters.http.trading_routes import register_trading_routes
from btc_signal_engine.application.play import SERIES_POINTS, PlayService, PlayVenue
from btc_signal_engine.application.play_limits import PlayLimits
from btc_signal_engine.application.trading import PROD_CONFIRM, TradingConfig, TradingService
from btc_signal_engine.domain.settlement.fees import POLYMARKET_FEE_RATE, polymarket_fee
from tests.trading.helpers import CLOSE, NOW, TICKER, W0, Exchange, Market, gateway
from tests.trading.test_strategy_scope import svc_for, up
from tests.pc import paired

K15 = {"venue": "kalshi", "span": "15m"}  # /api/play/* require the tab explicitly

K = 80_000.0
SLUG = "cpc-btc-updown-15m-2026-10-28-1700z"


def evaluation(**kw) -> dict:
    ev = {"boundary": W0, "window_end": CLOSE, "fair_up": 0.58, "model": {"price": K + 40},
          "market": {"ticker": TICKER, "ts": NOW - 500, "yes_bid": 0.60, "yes_ask": 0.62, "no_bid": 0.38,
                     "no_ask": 0.40, "strike": K, "close_ts": CLOSE, "open_ts": W0},
          "pressure": [{"horizon": 60, "label": "1m", "lean_call": "UP"},
                       {"horizon": 300, "label": "5m", "lean_call": "SKIP"}]}
    ev.update(kw)
    return ev


def kalshi_quote(age=500):
    return lambda now: {"ticker": TICKER, "yes_bid": 0.60, "yes_ask": 0.62, "no_bid": 0.38, "no_ask": 0.40,
                        "close_ts": CLOSE, "open_ts": W0, "age_ms": age}


def poly_service(store) -> TradingService:
    store.insert("trading_settings", [{"key": "you_poly.mode", "value": {"mode": "shadow"}, "updated_ts": 0}],
                 ignore_conflicts=False)
    cfg = TradingConfig(enabled=True, fee_rate=POLYMARKET_FEE_RATE, series="cpc-btc-updown-15m", whole_contracts=True)
    return TradingService(store, {"pm_shadow": gateway(Exchange(Market()), "shadow"), "pm_prod": None}, cfg,
                          strategy="you_poly", modes={"shadow": "pm_shadow", "prod": "pm_prod"},
                          venue="Polymarket US", fee_fn=polymarket_fee)


def rig(store, ev=None, crowd=None, max_stake=5.0, index=(NOW, K + 45.0), quote=None, pm_quote=None, poly=True):
    """`max_stake`: the phone's user-set per-trade limit (PlayLimits; None = no limit)."""
    ex = Exchange(Market(yes_bid=0.60, no_bid=0.38))
    limits = PlayLimits(store)
    assert limits.set(max_stake, None, None, NOW - 1)["ok"]
    svc = asyncio.run(up(svc_for(store, ex, "you", index_price_fn=lambda: index, limits_fn=limits.for_trading)))
    venues = {"kalshi": PlayVenue(svc, quote_fn=quote or kalshi_quote(), crowd_fn=lambda: crowd),
              "polymarket": PlayVenue(poly_service(store) if poly else None, quote_fn=pm_quote or (lambda now: None),
                                      yes_only=True, missing="Polymarket US is not connected on this server.")}
    play = PlayService(venues, evaluation_fn=lambda: ev if ev is not None else evaluation(), index_fn=lambda: index,
                       index_series_fn=lambda a, b: [(W0, K), (NOW - 1000, K + 45)])
    app = FastAPI()
    register_trading_routes(app, {"you": svc}, lambda: NOW)
    register_play_routes(app, play, lambda: NOW, limits=limits)
    return TestClient(app), svc, ex


def test_play_now_shape(store):
    crowd = {"ticker": TICKER, "lean": 0.4, "label": "more buying Up", "note": "Descriptive only.", "up_usd": 41_000.0,
             "down_usd": 12_000.0}
    c, svc, _ = rig(store, crowd=crowd, max_stake=1.0)
    v = c.get("/api/play/now", params=K15).json()
    assert v["venue"] == "kalshi" and v["strategy"] == "you" and v["venues"] == {"kalshi": True, "polymarket": True}
    assert v["window"] == {"start": W0, "end": CLOSE, "seconds_left": 600, "ticker": TICKER,
                           "entry_cutoff_ms": 120_000, "entry_open": True, "phase": "normal", "settle_op": "≥"}
    assert v["server_now_ms"] == NOW
    call = v["call"]
    assert call["action"] == "ENTER UP" and call["headline"] == "ENTER UP" and "61%" in call["reason"]
    assert call["lean"] == "UP" and call["mid_up"] == 0.61 and call["fair_up"] == 0.58 and call["model_lean"] == "UP"
    leans = {s["key"]: s["lean"] for s in v["signals"]}
    assert leans == {"market": "UP", "model": "UP", "pressure_60": "UP", "pressure_300": None, "pressure_900": None,
                     "crowd": "UP"}
    assert v["btc"]["btc"] == K + 45 and v["btc"]["gap"] == 45.0 and v["btc"]["flip_move"] == -45.0
    up_, down = v["entry"]["UP"], v["entry"]["DOWN"]
    assert up_["ideal_entry"] == 0.61 and up_["max_price"] == 0.63 and up_["take_profit"] == 0.99
    assert up_["stop_index_below"] is None and up_["suggested_stop"] == K - 10 and up_["stop_distance"] == 55.0 and up_["buyable"]
    assert up_["contracts"] == 1.54 and up_["max_loss"] <= 1.0  # $1 at 63c incl. Kalshi's fee
    assert down["ideal_entry"] == 0.39 and down["stop_index_above"] is None and down["suggested_stop"] == max(K + 10, round(v["btc"]["btc"] + 25, 2))
    assert "later feature" in v["entry"]["note"] and v["exit"]["take_profit"] == 0.99
    assert v["exit"]["near_stop_usd"] == 20.0
    assert v["stake"]["default"] == 1.0 and v["stake"]["max"] == 1.0 and not v["stake"]["whole_contracts"]
    assert v["mode"]["mode"] == "prod" and v["mode"]["real_money"] is True and v["ready"] is True
    assert v["positions"] == [] and v["btc_series"] == [[W0, K], [NOW - 1000, K + 45]]  # the chart, always
    assert call["agree"] == {"n": 4, "of": 6, "side": "UP"}
    cr = v["signals"][-1]
    assert cr["key"] == "crowd" and cr["up_usd"] == 41_000.0 and cr["down_usd"] == 12_000.0
    assert v["btc_levels"] == {"hi": K + 45, "lo": K, "open": K}
    assert c.get("/api/play/now", params={**K15, "stake": 50}).json()["stake"]["value"] == 1.0  # clamped to the cap


def test_coin_flip_waits(store):
    c, _, _ = rig(store, quote=lambda now: {"ticker": TICKER, "yes_bid": 0.49, "yes_ask": 0.52, "close_ts": CLOSE,
                                             "no_bid": 0.48, "no_ask": 0.51, "age_ms": 100})
    call = c.get("/api/play/now", params=K15).json()["call"]
    assert call["action"] == "WAIT" and call["headline"] == "WAIT / coin flip"


def test_play_now_stale_quote_and_other_markets_crowd(store):
    c, _, _ = rig(store, crowd={"ticker": "OTHER", "lean": 0.9}, index=None, quote=kalshi_quote(age=60_000))
    v = c.get("/api/play/now", params=K15).json()
    assert v["ready"] is False and "No live Kalshi price" in v["reason"] and v["call"]["action"] == "WAIT"
    assert v["entry"]["UP"]["buyable"] is False and v["entry"]["UP"]["ask"] is None
    assert v["btc"]["source"] == "model" and v["btc"]["btc"] == K + 40
    assert [s for s in v["signals"] if s["key"] == "crowd"][0]["lean"] is None


def test_polymarket_shape_with_short_side_from_the_yes_book(store):
    pm = lambda now: {"ticker": SLUG, "yes_bid": 0.30, "yes_ask": 0.33, "close_ts": CLOSE, "open_ts": W0,  # noqa: E731
                      "age_ms": 300, "source": "ws"}
    c, _, _ = rig(store, pm_quote=pm)
    v = c.get("/api/play/now", params={**K15, "venue": "polymarket"}).json()
    assert v["venue"] == "polymarket" and v["strategy"] == "you_poly" and v["window"]["ticker"] == SLUG
    m = v["market"]
    assert m["no_bid"] == 0.67 and m["no_ask"] == 0.70 and m["mid_up"] == 0.315  # DOWN = 1 - YES ask / bid
    assert v["call"]["action"] == "ENTER DOWN" and "68%" in v["call"]["reason"]
    down = v["entry"]["DOWN"]
    assert down["ask"] == 0.70 and down["bid"] == 0.67 and down["ideal_entry"] == 0.68 and down["max_price"] == 0.71
    assert down["stop_index_above"] is None and down["suggested_stop"] == max(K + 10, round(v["btc"]["btc"] + 25, 2)) and down["take_profit"] == 0.99  # Kalshi's strike (same BRTI), kept >= $25 from BTC
    assert down["contracts"] == 6.0 and down["max_loss"] == round(6 * 0.71 + polymarket_fee(6, 0.71), 2)
    assert v["stake"]["whole_contracts"] and v["stake"]["fee_rate"] == POLYMARKET_FEE_RATE
    assert v["mode"]["mode"] == "shadow" and v["mode"]["real_money"] is False and v["mode"]["venue"] == "Polymarket US"
    assert v["ready"] is True


def test_polymarket_without_a_live_price_or_without_the_venue(store):
    c, _, _ = rig(store)
    v = c.get("/api/play/now", params={**K15, "venue": "polymarket"}).json()
    assert v["ready"] is False and v["reason"].startswith("No live Polymarket price") and v["market"] is None
    assert not v["entry"]["UP"]["buyable"] and not v["entry"]["DOWN"]["buyable"]
    c2, _, _ = rig(store, poly=False)
    v2 = c2.get("/api/play/now", params={**K15, "venue": "polymarket"}).json()
    assert v2["available"] is False and "not connected" in v2["reason"]
    assert c2.get("/api/play/now", params={**K15, "venue": "binance"}).status_code == 422


def test_phone_flow_buy_up_advice_exit_now_and_scoreboard(store):
    c, svc, ex = rig(store)
    v = c.get("/api/play/now", params=K15).json()
    e = v["entry"]["UP"]
    prep = c.post("/api/trading/prepare", json={
        "strategy": "you", "ticker": v["window"]["ticker"], "side": "UP", "stake": v["stake"]["default"],
        "max_price": e["max_price"], "take_profit": e["take_profit"], "stop_index_below": e["stop_index_below"],
        "window_end": v["window"]["end"], "fair_up": v["call"]["fair_up"]}).json()
    assert prep["ok"], prep
    t = prep["ticket"]
    assert t["strategy"] == "you" and t["limit_price"] == 0.63 and t["take_profit"] == 0.99
    assert t["stop_index_below"] is None and t["stop"] is None and t["contracts"] == e["contracts"]  # no automatic stop
    r = c.post("/api/trading/approve", json={"strategy": "you", "token": prep["token"], "decision": "yes",
                                             "ticket": t}).json()
    assert r["ok"] and r["bracket"]["state"] == "open", r
    pos = c.get("/api/play/now", params=K15).json()["positions"]
    assert len(pos) == 1 and pos[0]["stop_level"] is None and pos[0]["advice"]["stop_distance"] is None
    st = c.post("/api/trading/stop", json={"strategy": "you", "bracket_id": pos[0]["id"], "level": K - 10}).json()
    assert st["ok"], st  # the user opts in to a stop after entry
    pos = c.get("/api/play/now", params=K15).json()["positions"]
    assert pos[0]["stop_level"] == K - 10
    adv = pos[0]["advice"]
    assert adv["action"] == "HOLD" and adv["stop_distance"] == 55.0 and adv["if_wins"] == pos[0]["held"]
    assert adv["pnl_if_loses"] < 0
    assert c.get("/api/play/now", params=K15).json()["btc_series"] == [[W0, K], [NOW - 1000, K + 45]]
    assert pos[0]["entry_btc"] in (K, K + 45)
    assert c.get("/api/trading/status", params={"strategy": "pnw_poly"}).json()["code"] == "unknown_strategy"
    px = c.post("/api/trading/prepare_exit", json={"strategy": "you", "bracket_id": pos[0]["id"]}).json()
    assert px["ok"], px
    out = c.post("/api/trading/approve", json={"strategy": "you", "token": px["token"], "decision": "yes",
                                               "ticket": px["ticket"]}).json()
    assert out["ok"], out
    sb = c.get("/api/play/scoreboard", params=K15).json()
    assert sb["mode"] == "prod" and sb["record"]["trades"] == 1 and sb["record"]["losses"] == 1  # sold 60 < paid 62
    a = sb["attempts"]
    assert a["n"] == 1 and a["points"][0]["result"] == "L" and a["points"][0]["cum"] == a["points"][0]["pnl"] < 0
    assert a["points"][0]["venue"] == "kalshi" and a["points"][0]["side"] == "UP"
    cal = a["calibration"]  # bought UP with the model at 58% UP: followed the call, and lost
    assert cal["followed"]["losses"] == 1 and cal["against"]["trades"] == 0 and cal["unknown"] == 0
    assert c.get("/api/play/scoreboard", params={**K15, "mode": "shadow"}).json()["record"]["trades"] == 0
    assert c.get("/api/play/scoreboard", params={**K15, "both": True, "last": 5}).json()["attempts"]["n"] == 1
    assert c.get("/api/play/scoreboard", params={**K15, "venue": "polymarket"}).json()["record"]["trades"] == 0
    assert c.get("/api/play/scoreboard", params={**K15, "mode": "nope"}).status_code == 422


def test_play_now_balances(store):
    """Each venue's account reading from the app's balance loops: fresh numbers, a stale or missing reading carries no
    numbers (the phone says "balance unavailable"), and `both` only when both are fresh."""
    ex = Exchange(Market(yes_bid=0.60, no_bid=0.38))
    svc = asyncio.run(up(svc_for(store, ex, "you", index_price_fn=lambda: (NOW, K))))
    readings = {"kalshi": {"cash": 86.18, "positions": 1.5, "at": NOW - 60_000},
                "polymarket": {"cash": 120.56, "at": NOW - 1_000}}
    venues = {"kalshi": PlayVenue(svc, quote_fn=kalshi_quote(), balance_fn=lambda: readings["kalshi"]),
              "polymarket": PlayVenue(poly_service(store), yes_only=True, balance_fn=lambda: readings["polymarket"])}
    play = PlayService(venues, evaluation_fn=evaluation, index_fn=lambda: (NOW, K))
    app = FastAPI()
    register_play_routes(app, play, lambda: NOW)
    cl = TestClient(app)
    b = cl.get("/api/play/now", params=K15).json()["balances"]
    assert b["kalshi"] == {"cash": 86.18, "positions": 1.5, "age_ms": 60_000, "fresh": True, "total": 87.68}
    assert b["polymarket"] == {"cash": 120.56, "positions": None, "age_ms": 1_000, "fresh": True, "total": 120.56}
    assert b["both"] == 208.24
    readings["kalshi"] = {"cash": 86.18, "positions": 1.5, "at": NOW - 300_001}  # older than 5 minutes
    b = cl.get("/api/play/now", params={**K15, "venue": "polymarket"}).json()["balances"]
    assert b["kalshi"] == {"cash": None, "positions": None, "age_ms": 300_001, "fresh": False, "total": None}
    assert b["both"] is None and b["polymarket"]["fresh"]
    readings["polymarket"] = None  # never read
    b = cl.get("/api/play/now", params=K15).json()["balances"]
    assert b["polymarket"] == {"cash": None, "positions": None, "age_ms": None, "fresh": False, "total": None}


def test_play_now_entry_cutoff_for_you(store):
    """Strategy "you" uses the configured 2-minute cutoff (its profile's min_left_ms is None): at 2:00 left or less
    the window is closed to entries, the clock is "late" and BUY is off with the reason."""
    _, svc, _ = rig(store)
    assert svc.min_left_ms() == 120_000
    late = CLOSE - 120_000
    app = FastAPI()
    register_play_routes(app, PlayService({"kalshi": PlayVenue(svc, quote_fn=kalshi_quote())},
                                          evaluation_fn=evaluation, index_fn=lambda: (late, K + 45.0)), lambda: late)
    v = TestClient(app).get("/api/play/now", params=K15).json()
    assert v["window"]["entry_open"] is False and v["window"]["phase"] == "late" and v["server_now_ms"] == late
    assert v["ready"] is False and v["reason"] == "Too late to enter this window."


def test_btc_series_is_thinned_and_tolerates_a_failing_source():
    svc = PlayService({}, evaluation_fn=lambda: evaluation(),
                      index_series_fn=lambda a, b: [(t, K + (t - a) / 1000) for t in range(a, b, 1000)])
    s, lv = svc._series(W0, W0 + 899_000)
    assert len(s) <= SERIES_POINTS and s[0] == [W0, K] and s[-1] == [W0 + 899_000, K + 899]
    assert lv == {"hi": K + 899, "lo": K, "open": K}  # from the full series, not the thinned one

    def boom(a, b):
        raise RuntimeError("store down")
    assert PlayService({}, evaluation_fn=lambda: None, index_series_fn=boom)._series(W0, NOW) == ([], {"hi": None, "lo": None, "open": None})
    v = PlayService({}, evaluation_fn=lambda: evaluation(), index_series_fn=boom).now_view(NOW, "kalshi", span="15m")
    assert v["available"] is False and v["btc_series"] == [] and v["btc_levels"]["hi"] is None


def test_play_routes_need_an_explicit_venue_and_span(store):
    c, _, _ = rig(store)
    for route in ("/api/play/now", "/api/play/scoreboard"):
        assert c.get(route).status_code == 422
        assert c.get(route, params={"venue": "kalshi"}).status_code == 422  # no span: never 15m by default
        assert c.get(route, params={"span": "15m"}).status_code == 422  # no venue: never Kalshi by default
        assert c.get(route, params={"venue": "kalshi", "span": "30m"}).status_code == 422
        assert c.get(route, params=K15).status_code == 200


def test_both_venues_scoreboard_labels_each_trade_by_its_venue_key(store):
    from btc_signal_engine.application.play import PlayService, PlayVenue

    class Svc:
        modes, mode, strategy = {"shadow": "shadow"}, "shadow", "x"

        def __init__(self, n):
            self.n = n

        def trades(self, since, now, limit):
            return [{"env": "shadow", "ts": NOW - 1000 * self.n, "side": "UP", "pnl": 0.1, "state": "closed",
                     "entry_avg": 0.5, "filled": 1.0, "closed_ts": NOW - 1000 * self.n}]

    play = PlayService({"kalshi": PlayVenue(Svc(1)), "polymarket": PlayVenue(Svc(2), yes_only=True),
                        "polymarket_5m": PlayVenue(Svc(3), yes_only=True)}, evaluation_fn=lambda: {})
    sb = play.scoreboard(NOW, "polymarket", span="5m", both=True)
    assert sorted(p["venue"] for p in sb["attempts"]["points"]) == ["kalshi", "polymarket", "polymarket_5m"]


def test_fifteen_minute_pressure_is_the_engines_own_lean(store):
    c, _, _ = rig(store, ev=evaluation(engine={"lean": {"call": "DOWN"}}))
    sig = {s["key"]: s for s in c.get("/api/play/now", params=K15).json()["signals"]}
    assert [k for k in sig if k.startswith("pressure_")] == ["pressure_60", "pressure_300", "pressure_900"]
    assert sig["pressure_900"]["name"] == "15m pressure" and sig["pressure_900"]["lean"] == "DOWN"


def test_play_now_where_its_headed_fields(store):
    """The "where it's headed" data: the cone from the Coach's model (15m) and from fair_fn (another span), each
    side's plan payoff, and an open position's outlook (market vs model chance, hold value, give-up)."""
    model = {"price": K + 40, "sigma_per_s": 5.0, "at": NOW - 1000, "window_end": CLOSE, "mean_close": K + 40,
             "sd_close": 110.0}
    age = {"ms": 500}
    c, svc, _ = rig(store, ev=evaluation(model=model), quote=lambda now: kalshi_quote(age["ms"])(now))
    v = c.get("/api/play/now", params=K15).json()
    assert v["cone"] == {"now_ms": NOW, "end_ms": CLOSE, "model_at": NOW - 1000, "btc": K + 45, "strike": K,
                         "sigma_per_s": 5.0, "sd_close": 110.0, "mean_close": K + 40,
                         "bands": {"1": [K - 70, K + 150], "2": [K - 180, K + 260]}}
    up_ = v["entry"]["UP"]
    assert up_["if_loses"] == -up_["max_loss"] and up_["if_wins"] == round(up_["contracts"] + up_["if_loses"], 2)
    e = up_
    prep = c.post("/api/trading/prepare", json={
        "strategy": "you", "ticker": v["window"]["ticker"], "side": "UP", "stake": v["stake"]["default"],
        "max_price": e["max_price"], "take_profit": e["take_profit"], "stop_index_below": None,
        "window_end": v["window"]["end"], "fair_up": v["call"]["fair_up"]}).json()
    r = c.post("/api/trading/approve", json={"strategy": "you", "token": prep["token"], "decision": "yes",
                                             "ticket": prep["ticket"]}).json()
    assert r["ok"], r
    pos = c.get("/api/play/now", params=K15).json()["positions"][0]
    o, held = pos["outlook"], pos["held"]
    assert o["chance_now"] == 0.61 and o["chance_bid"] == 0.60 and o["model_chance"] == 0.58 and o["disagree"] is False
    assert o["hold_value"] == round(0.61 * held, 2) and o["sell_now"] == pos["advice"]["sell_now"]
    assert o["give_up"] == round(o["hold_value"] - o["sell_now"], 2)
    # a stale quote: no market chance, no hold value; the model's chance stays
    age["ms"] = 60_000
    v2 = c.get("/api/play/now", params=K15).json()
    o2 = v2["positions"][0]["outlook"]
    assert o2["chance_now"] is None and o2["hold_value"] is None and o2["give_up"] is None and o2["model_chance"] == 0.58
    assert v2["entry"]["UP"]["if_wins"] is None and v2["entry"]["UP"]["if_loses"] is None  # no ask: no plan
    # an old model reading (or the evaluation without the cone numbers) draws no cone
    assert v2["cone"] is not None
    c3, _, _ = rig(store, ev=evaluation(model={**model, "at": NOW - 60_000}))
    assert c3.get("/api/play/now", params=K15).json()["cone"] is None
    c4, _, _ = rig(store)
    assert c4.get("/api/play/now", params=K15).json()["cone"] is None


def test_play_now_cone_on_its_own_span_uses_fair_fn(store):
    ex = Exchange(Market(yes_bid=0.60, no_bid=0.38))
    svc = asyncio.run(up(svc_for(store, ex, "you", index_price_fn=lambda: (NOW, K))))
    end5 = NOW - NOW % 300_000 + 300_000
    f = {"fair_up": 0.7, "price": K + 20, "sigma": 4.0, "at": NOW, "window_end": end5, "mean_close": K + 20,
         "sd_close": 50.0}
    seen = []
    v5 = PlayVenue(svc, quote_fn=lambda now: None, yes_only=True, window_ms=300_000, strike_fn=lambda start: K,
                   fair_fn=lambda now, start, end, strike: seen.append((start, end, strike)) or f)
    play = PlayService({"polymarket_5m": v5}, evaluation_fn=evaluation, index_fn=lambda: (NOW, K + 20))
    v = play.now_view(NOW, "polymarket", span="5m")
    assert seen == [(end5 - 300_000, end5, K)]
    assert v["call"]["fair_up"] == 0.7 and v["cone"]["sigma_per_s"] == 4.0 and v["cone"]["sd_close"] == 50.0
    assert v["cone"]["end_ms"] == end5 and v["cone"]["bands"]["2"] == [K - 80, K + 120]


def test_play_now_reads_the_index_series_and_fair_odds_once_per_second(store):
    """Every phone and tab polling /api/play/now within one whole second shares one index-series read and one
    fair-odds read (per window); the next second reads again."""
    ex = Exchange(Market(yes_bid=0.60, no_bid=0.38))
    svc = asyncio.run(up(svc_for(store, ex, "you", index_price_fn=lambda: (NOW, K))))
    reads = {"series": 0, "fair": 0}

    def series(a, b):
        reads["series"] += 1
        return [(a, K), (b - 1, K + 10)]

    def fair(now, start, end, strike):
        reads["fair"] += 1
        return {"fair_up": 0.6, "at": now, "window_end": end}

    v5 = PlayVenue(svc, yes_only=True, window_ms=300_000, strike_fn=lambda start: K, fair_fn=fair)
    play = PlayService({"polymarket_5m": v5}, evaluation_fn=evaluation, index_fn=lambda: (NOW, K),
                       index_series_fn=series)
    a = play.now_view(NOW + 100, "polymarket", span="5m")
    b = play.now_view(NOW + 900, "polymarket", span="5m")
    assert reads == {"series": 1, "fair": 1}
    assert a["btc_series"] == b["btc_series"] and b["call"]["fair_up"] == 0.6
    play.now_view(NOW + 1000, "polymarket", span="5m")
    assert reads == {"series": 2, "fair": 2}


# -- PC-only changes (R10): a phone trades within the limits set on the PC -----------------------------------------
PIN = "482916"
ONLY_PC = {"detail": "Only from the PC itself."}


def pc_and_phone(app):
    """The app behind the access middleware: the PC (loopback) sets a PIN, a phone on the LAN signs in with it."""
    install_access(app, AccessGuard(PinStore(None), session_hours=12, clock_ms=lambda: NOW), api_host="0.0.0.0",
                   port=8000, lan_ip_fn=lambda: "192.168.1.10", allowed_hosts="192.168.1.10")
    pc = paired(TestClient(app, client=("127.0.0.1", 1), follow_redirects=False, base_url="http://127.0.0.1:8000"))
    assert pc.post("/api/access/pin", json={"pin": PIN, "pin_again": PIN}).json()["ok"]
    phone = TestClient(app, client=("192.168.1.50", 1), follow_redirects=False, base_url="http://192.168.1.10:8000")
    assert phone.post("/login", data={"pin": PIN}).status_code == 303
    return pc, phone


def test_a_phone_can_switch_to_production_only_with_the_typed_confirmation_and_back(store):
    ex = Exchange(Market())
    svc = TradingService(store, {"demo": gateway(ex, "demo"), "prod": gateway(ex, "prod")},
                         TradingConfig(enabled=True), strategy="you")
    app = FastAPI()
    register_trading_routes(app, {"you": svc}, lambda: NOW)
    pc, phone = pc_and_phone(app)
    to_prod = {"mode": "prod", "confirm": PROD_CONFIRM, "strategy": "you"}
    r = phone.post("/api/trading/mode", json={"mode": "prod", "confirm": "yes", "strategy": "you"})
    assert r.status_code == 200 and not r.json()["ok"] and svc.mode == "sandbox"
    assert phone.post("/api/trading/mode", json=to_prod).json()["mode"] == "prod"
    r = phone.post("/api/trading/mode", json={"mode": "shadow", "strategy": "you"})
    assert r.status_code == 200 and r.json()["ok"] and svc.mode == "shadow"
    assert ex.writes() == []


def test_a_phone_can_turn_the_kill_switch_on_but_not_off(store):
    c, svc, _ = rig(store)
    pc, phone = pc_and_phone(c.app)
    assert phone.post("/api/trading/kill").json()["killed"] is True
    r = phone.post("/api/trading/resume")
    assert r.status_code == 403 and r.json() == ONLY_PC
    assert pc.get("/api/trading/status", params={"strategy": "you"}).json()["killed"] is True
    assert pc.post("/api/trading/resume").json()["killed"] is False


def test_polymarket_quiet_book_is_fresh_by_the_cache_verdict_not_play_s_own_age_limit(store):
    """A Polymarket quote carries the cache's `servable` verdict (a quiet book up to 30 s old on a live connection,
    adapters/polymarket_us/market_data.py). Play takes it instead of QUOTE_MAX_AGE_MS, so Play, the agents and the
    order path agree: 12 s old and servable is live; the cache answering None is not."""
    q = {"ticker": SLUG, "yes_bid": 0.30, "yes_ask": 0.33, "close_ts": CLOSE, "open_ts": W0, "age_ms": 12_000,
         "source": "ws", "servable": True, "basis": "quiet"}
    c, _, _ = rig(store, pm_quote=lambda now: q)
    v = c.get("/api/play/now", params={**K15, "venue": "polymarket"}).json()
    assert v["ready"] is True and v["market"]["fresh"] is True and v["entry"]["DOWN"]["ask"] == 0.70
    c, _, _ = rig(store, pm_quote=lambda now: None)  # the cache refused it (not servable): no price, no BUY
    v = c.get("/api/play/now", params={**K15, "venue": "polymarket"}).json()
    assert v["ready"] is False and v["entry"]["DOWN"]["ask"] is None
