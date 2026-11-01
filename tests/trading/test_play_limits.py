"""The phone strategies' user-set trade limits (PlayLimits, default none / none) and the production cash backstop:
the setting's storage and strict reading, raise / remove needs the exact confirm phrase (lower does not), every change
audited, the limits enforced live after a change (no restart), an unreadable setting refusing entries visibly, and
with no limits every production order (a market buy's worst case too) bounded by fresh venue cash, a missing or stale
balance refusing. Simulated exchange only: nothing reaches a venue."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from btc_signal_engine.adapters.http.access import AccessGuard, PinStore, install_access
from btc_signal_engine.adapters.http.play_routes import register_play_routes
from btc_signal_engine.application.play import PlayService, PlayVenue
from btc_signal_engine.application.play_limits import LIMITS_CONFIRM, SETTING, PlayLimits
from btc_signal_engine.application.trading import BALANCE_MAX_AGE_MS, TradingConfig
from tests.trading.helpers import CLOSE, NOW, TICKER, W0, Exchange, Market
from tests.trading.test_strategy_scope import svc_for, up
from tests.pc import paired

CFG = TradingConfig(enabled=True, max_open_positions=5)  # several entries, so the dollar limits are what refuses


def run(coro):
    return asyncio.run(coro)


def limit_buy(stake=2.0, **kw) -> dict:
    p = {"ticker": TICKER, "side": "UP", "stake": stake, "max_price": 0.59, "take_profit": 0.99, "stop": None,
         "window_end": CLOSE}
    p.update(kw)
    return p


def market_buy(stake=2.0) -> dict:
    return {"ticker": TICKER, "side": "UP", "stake": stake, "price_mode": "market", "take_profit": 0.99,
            "stop": None, "window_end": CLOSE}


async def phone(store, limits: PlayLimits, *, mode="prod", balance=(1_000.0, NOW), m: Market | None = None):
    ex = Exchange(m or Market())  # YES 55/58c
    svc = await up(svc_for(store, ex, "you", mode=mode, config=CFG, limits_fn=limits.for_trading,
                           cash_backstop=True, balance_fn=lambda: balance))
    return ex, svc


async def buy(svc, p: dict, now: int = NOW) -> dict:
    prep = await svc.prepare(p, now)
    if not prep["ok"]:
        return prep
    return await svc.approve(prep["token"], "yes", prep["ticket"], now + 200)


def audit_rows(store) -> list[dict]:
    return store.select("trading_audit", {"event": "limits_changed"}, order_by=["ts"])


# -- the setting ----------------------------------------------------------------------------------------------------
def test_default_is_no_limits(store):
    lim = PlayLimits(store)
    v = lim.view()
    assert (v["per_trade"], v["per_day"], v["set_at"], v["error"]) == (None, None, None, None)
    assert v["confirm_phrase"] == LIMITS_CONFIRM == "I understand this raises my limit"
    assert lim.for_trading() == {"per_trade": None, "per_window": None, "per_day": None}
    assert store.select("strategy_settings", SETTING) == []  # nothing written until the user sets them


def test_lowering_needs_no_confirm_raising_or_removing_does_and_every_change_is_audited(store):
    lim = PlayLimits(store)
    r = lim.set(50, 250, None, 1_000)  # none -> a number is a lowering
    assert r["ok"] and r["changed"] and (r["per_trade"], r["per_day"], r["set_at"]) == (50.0, 250.0, 1_000)
    assert store.select("strategy_settings", SETTING)[0]["value"] == {"per_trade": 50.0, "per_day": 250.0}
    assert lim.set(20, 250, None, 2_000)["ok"]  # lower per trade
    r = lim.set(30, 250, None, 3_000)  # raise
    assert not r["ok"] and r["code"] == "confirm_required" and r["raises"] == ["per_trade"]
    assert LIMITS_CONFIRM in r["reason"] and "$20.00 -> $30.00" in r["reason"]
    assert not lim.set(30, 250, "i understand this raises my limit", 3_000)["ok"]  # exact phrase only
    assert not lim.set(20, None, None, 3_000)["ok"]  # removing a limit is a raise
    assert lim.current().per_trade == 20.0 and lim.current().per_day == 250.0  # nothing changed
    r = lim.set(30, None, LIMITS_CONFIRM, 4_000)
    assert r["ok"] and (r["per_trade"], r["per_day"]) == (30.0, None)
    assert store.select("strategy_settings", SETTING)[0]["value"] == {"per_trade": 30.0, "per_day": "none"}
    assert lim.set(30, None, None, 5_000) == {"ok": True, "changed": False, **lim.view()}  # same value: no row
    rows = audit_rows(store)
    assert [r["code"] for r in rows] == ["lowered", "lowered", "raised"]
    assert rows[0]["detail"]["old"] == {"per_trade": None, "per_day": None}
    assert rows[0]["detail"]["new"] == {"per_trade": 50.0, "per_day": 250.0}
    assert rows[2]["detail"] == {"old": {"per_trade": 20.0, "per_day": 250.0},
                                 "new": {"per_trade": 30.0, "per_day": None}, "raised": ["per_trade", "per_day"],
                                 "confirmed": True}
    assert all(r["strategy"] == "play" and r["env"] == "all" for r in rows)
    assert [h["ts"] for h in lim.history()] == [4_000, 2_000, 1_000]


def test_invalid_values_are_refused(store):
    lim = PlayLimits(store)
    for bad in (0, -5, float("nan"), float("inf"), "10", True, 0.001):
        r = lim.set(bad, None, LIMITS_CONFIRM, 1)
        assert not r["ok"] and r["code"] == "invalid", bad
    assert lim.view()["set_at"] is None


def test_unreadable_setting_refuses_entries_visibly_and_needs_confirm_to_fix(store):
    lim = PlayLimits(store)
    for garbage in ({"per_trade": "lots", "per_day": "none"}, {"per_trade": 5}, "none", {"per_trade": -1, "per_day": 9},
                    {"per_trade": None, "per_day": "none"}):
        if store.select("strategy_settings", SETTING):
            store.update("strategy_settings", SETTING, {"value": garbage, "updated_ts": 7})
        else:
            store.insert("strategy_settings", [{**SETTING, "value": garbage, "updated_ts": 7}])
        v = lim.view()
        assert v["error"].startswith("Trade limits setting unreadable: ") and v["per_trade"] is None, garbage

    async def go():
        ex, svc = await phone(store, lim)
        r = await buy(svc, limit_buy())
        assert not r["ok"] and r["code"] == "limits_unreadable"
        assert r["reason"].startswith("Trade limits setting unreadable: ")
        assert svc.market_terms(2.0, 0.58, NOW)["fits"] is False
        play = PlayService({"kalshi": PlayVenue(svc, quote_fn=lambda now: {
            "ticker": TICKER, "yes_bid": 0.55, "yes_ask": 0.58, "no_bid": 0.42, "no_ask": 0.45, "close_ts": CLOSE,
            "open_ts": W0, "age_ms": 100})}, evaluation_fn=lambda: {"boundary": W0, "window_end": CLOSE})
        v = play.now_view(NOW, "kalshi", span="15m")
        assert v["ready"] is False and v["reason"].startswith("Trade limits setting unreadable")
        assert v["stake"]["sentence"].startswith("Trade limits setting unreadable")
        assert not [w for w in ex.writes() if "-entry" in str((w["body"] or {}).get("client_order_id"))]
    run(go())
    r = lim.set(10, None, None, 8)
    assert not r["ok"] and r["code"] == "confirm_required"  # cannot be shown to be a lowering
    r = lim.set(10, None, LIMITS_CONFIRM, 8)
    assert r["ok"] and r["error"] is None
    assert audit_rows(store)[-1]["detail"]["old"]["unreadable"]


# -- enforcement, live --------------------------------------------------------------------------------------------
def test_per_trade_and_per_day_apply_to_the_next_order_without_a_restart(store):
    lim = PlayLimits(store)

    async def go():
        ex, svc = await phone(store, lim)
        assert (await buy(svc, limit_buy(stake=20.0)))["ok"]  # no limits: $20 goes through
        assert lim.set(5, None, None, NOW)["ok"]
        r = await buy(svc, limit_buy(stake=6.0), NOW + 1_000)
        assert not r["ok"] and r["code"] == "over_trade_limit" and "$5.00 per-trade limit" in r["reason"]
        r = await buy(svc, market_buy(stake=4.0), NOW + 1_000)  # $4 at the ask, up to ~$6.60 at 99c
        assert not r["ok"] and r["code"] == "over_trade_limit" and "at the market" in r["reason"].lower()
        assert lim.set(30, 25, LIMITS_CONFIRM, NOW + 2_000)["ok"]
        used = svc.exposure("prod", NOW + 3_000)["day"]
        assert 19 < used <= 20
        r = await buy(svc, limit_buy(stake=6.0), NOW + 3_000)
        assert not r["ok"] and r["code"] == "over_day_limit" and "$25.00 daily limit" in r["reason"]
        assert (await buy(svc, limit_buy(stake=4.0), NOW + 3_000))["ok"]
        # per window = per trade ($10): the window already holds $20+, so even $1 more is over it
        assert lim.set(10, None, None, NOW + 4_000)["ok"] is False  # removing the day limit needs the phrase
        assert lim.set(10, None, LIMITS_CONFIRM, NOW + 4_000)["ok"]
        r = await buy(svc, limit_buy(stake=1.0), NOW + 5_000)
        assert not r["ok"] and r["code"] == "over_window_limit"
        assert lim.set(None, None, LIMITS_CONFIRM, NOW + 6_000)["ok"]
        assert (await buy(svc, limit_buy(stake=1.0), NOW + 7_000))["ok"]
    run(go())


# -- the cash backstop ----------------------------------------------------------------------------------------------
def test_no_limit_stake_is_bounded_by_fresh_cash_market_worst_case_too(store):
    lim = PlayLimits(store)

    async def go():
        ex, svc = await phone(store, lim, balance=(10.0, NOW - 1_000))
        r = await buy(svc, limit_buy(stake=12.0))
        assert not r["ok"] and r["code"] == "over_cash" and "more than your $10.00 Kalshi cash" in r["reason"]
        # $8 at the 58c ask fits the cash, but its worst case (every contract at 99c + fee) does not
        r = await buy(svc, market_buy(stake=8.0))
        assert not r["ok"] and r["code"] == "over_cash" and "at worst" in r["reason"]
        t = svc.market_terms(8.0, 0.58, NOW)
        assert t["fits"] is False and "$10.00 Kalshi cash" in t["why"]
        assert svc.market_terms(5.0, 0.58, NOW)["fits"] is True
        assert (await buy(svc, market_buy(stake=5.0)))["ok"]
        assert (await buy(svc, limit_buy(stake=9.0)))["ok"]  # the backstop is per order, not cumulative
    run(go())


def test_missing_or_stale_balance_refuses_in_production_only(store):
    lim = PlayLimits(store)

    async def go():
        for bal in ((None, None), (50.0, None), (50.0, NOW - BALANCE_MAX_AGE_MS - 1)):
            ex, svc = await phone(store, lim, balance=bal)
            r = await buy(svc, limit_buy())
            assert not r["ok"] and r["code"] == "balance_unknown", bal
            assert r["reason"].startswith("Balance unknown: can't check the order fits your")
            assert svc.market_terms(2.0, 0.58, NOW)["fits"] is False
        _, sh = await phone(store, lim, mode="shadow", balance=(None, None))  # no money moves: no cash check
        cash, r = sh.cash_cap("shadow", NOW)
        assert cash is None and r.ok and (await buy(sh, limit_buy()))["ok"]
    run(go())


def test_play_stake_block_states_the_limit_and_the_cash(store):
    lim = PlayLimits(store)

    async def go():
        ex, svc = await phone(store, lim, balance=(90.09, NOW - 5_000))
        q = {"ticker": TICKER, "yes_bid": 0.55, "yes_ask": 0.58, "no_bid": 0.42, "no_ask": 0.45, "close_ts": CLOSE,
             "open_ts": W0, "age_ms": 100}
        play = PlayService({"kalshi": PlayVenue(svc, quote_fn=lambda now: q)},
                           evaluation_fn=lambda: {"boundary": W0, "window_end": CLOSE})
        st = play.now_view(NOW, "kalshi", span="15m", stake=500)["stake"]
        assert st["max"] is None and st["cap"] == 90.09 and st["cash_cap"] == 90.09 and st["cash_applies"]
        assert st["sentence"] == "No per-trade limit · up to your $90.09 cash" and st["value"] == 90.09
        assert lim.set(50, None, None, NOW)["ok"]
        v = play.now_view(NOW, "kalshi", span="15m")
        assert v["stake"]["max"] == 50.0 and v["stake"]["cap"] == 50.0
        assert v["stake"]["sentence"] == "Up to $50.00 a trade (your limit) · up to your $90.09 cash"
        assert v["limits"] == {"per_trade": 50.0, "per_day": None, "day_used": 0.0, "error": None}
        caps = play.balance_caps(NOW)
        assert caps["kalshi"]["applies"] and caps["kalshi"]["cash"] == 90.09 and caps["kalshi"]["mode"] == "prod"
    run(go())


# -- HTTP: the PC and a signed-in phone -----------------------------------------------------------------------------
def _app(store, now_fn):
    lim = PlayLimits(store)
    ex = Exchange(Market())
    svc = run(up(svc_for(store, ex, "you", mode="shadow", config=CFG, limits_fn=lim.for_trading, cash_backstop=True)))
    play = PlayService({"kalshi": PlayVenue(svc)}, evaluation_fn=lambda: {})
    app = FastAPI()
    register_play_routes(app, play, now_fn, limits=lim)
    install_access(app, AccessGuard(PinStore(store), session_hours=12, clock_ms=now_fn), api_host="0.0.0.0",
                   port=8000, lan_ip_fn=lambda: "192.168.1.10",
                   allowed_hosts="192.168.1.10")
    return app, svc


def test_limits_routes_for_the_pc_and_a_signed_in_phone(store):
    app, svc = _app(store, lambda: NOW)
    pc = paired(TestClient(app, client=("127.0.0.1", 1), follow_redirects=False, base_url="http://127.0.0.1:8000"))
    g = pc.get("/api/play/limits").json()
    assert (g["per_trade"], g["per_day"], g["set_at"], g["error"]) == (None, None, None, None)
    assert g["confirm_phrase"] == LIMITS_CONFIRM and g["history"] == []
    assert g["balance_cap"]["kalshi"]["applies"] is False and g["balance_cap"]["kalshi"]["mode"] == "shadow"
    r = pc.post("/api/play/limits", json={"per_trade": 25, "per_day": 100}).json()
    assert r["ok"] and (r["per_trade"], r["per_day"], r["set_at"]) == (25.0, 100.0, NOW) and len(r["history"]) == 1
    assert svc.limits()[0] == {"per_trade": 25.0, "per_window": 25.0, "per_day": 100.0}  # live, same object
    r = pc.post("/api/play/limits", json={"per_trade": None, "per_day": 100}).json()
    assert not r["ok"] and r["code"] == "confirm_required" and r["confirm_phrase"] == LIMITS_CONFIRM
    for bad in ({"per_trade": 5}, {"per_trade": 0, "per_day": None}, {"per_trade": "x", "per_day": None}, {}):
        assert pc.post("/api/play/limits", json=bad).status_code == 422, bad
    # a phone: refused until a PIN is set and it signs in, then the same route works (not loopback-only)
    phone = TestClient(app, client=("192.168.1.50", 1), follow_redirects=False, base_url="http://192.168.1.10:8000")
    assert phone.get("/api/play/limits").status_code == 403
    assert pc.post("/api/access/pin", json={"pin": "482916", "pin_again": "482916"}).json()["ok"]
    assert phone.get("/api/play/limits").status_code == 401
    phone.post("/login", data={"pin": "482916"})
    assert phone.get("/api/play/limits").json()["per_trade"] == 25.0
    # a phone trades within the limits set on the PC (R10): it may lower them, never raise or remove one
    only_pc = {"detail": "Only from the PC itself."}
    for raise_ in ({"per_trade": 40, "per_day": 100}, {"per_trade": None, "per_day": 100},
                   {"per_trade": 25, "per_day": None}, {"per_trade": 10, "per_day": 200}):
        r = phone.post("/api/play/limits", json={**raise_, "confirm": LIMITS_CONFIRM})
        assert r.status_code == 403 and r.json() == only_pc, raise_
    assert PlayLimits(store).current().as_dict() == {"per_trade": 25.0, "per_day": 100.0}
    r = phone.post("/api/play/limits", json={"per_trade": 10, "per_day": 100}).json()
    assert r["ok"] and (r["per_trade"], r["per_day"]) == (10.0, 100.0)
    r = pc.post("/api/play/limits", json={"per_trade": 40, "per_day": None, "confirm": LIMITS_CONFIRM}).json()
    assert r["ok"] and (r["per_trade"], r["per_day"]) == (40.0, None)


# -- market protection setting -------------------------------------------------------------------------------------
def test_market_band_default_set_strict_and_audited(store):
    from btc_signal_engine.application.play_limits import BAND_SETTING, DEFAULT_BAND_CENTS
    lim = PlayLimits(store)
    assert lim.market_band_cents() == DEFAULT_BAND_CENTS == 15 and lim.market_band() == 0.15
    assert lim.set_market_band(7, NOW)["code"] == "invalid"
    assert lim.set_market_band(True, NOW)["code"] == "invalid"
    r = lim.set_market_band(5, NOW)
    assert r["ok"] and r["changed"] and lim.market_band() == 0.05
    assert lim.set_market_band(None, NOW + 1)["ok"] and lim.market_band() is None
    audit = store.select("trading_audit", {"event": "market_band_changed"})
    assert [a["detail"]["new"] for a in audit] == [5, None]
    store.update("strategy_settings", BAND_SETTING, {"value": 12})
    assert lim.band_view()["readable"] is False
    try:
        lim.market_band()
        raise AssertionError("an unreadable band must raise")
    except ValueError:
        pass


def test_market_band_route(store):
    app, _ = _app(store, lambda: NOW)
    c = paired(TestClient(app, client=("127.0.0.1", 1), follow_redirects=False, base_url="http://127.0.0.1:8000"))
    assert c.get("/api/play/limits").json()["market_band"]["cents"] == 15
    assert c.post("/api/play/market_band", json={}).status_code == 422  # cents is required (null = up to 99c)
    r = c.post("/api/play/market_band", json={"cents": 25}).json()
    assert r["ok"] and r["market_band"]["cents"] == 25
    assert c.post("/api/play/market_band", json={"cents": 3}).json()["code"] == "invalid"


def test_a_phone_may_narrow_the_market_band_but_not_widen_it(store):
    app, _ = _app(store, lambda: NOW)
    pc = paired(TestClient(app, client=("127.0.0.1", 1), follow_redirects=False, base_url="http://127.0.0.1:8000"))
    assert pc.post("/api/access/pin", json={"pin": "482916", "pin_again": "482916"}).json()["ok"]
    phone = TestClient(app, client=("192.168.1.50", 1), follow_redirects=False, base_url="http://192.168.1.10:8000")
    phone.post("/login", data={"pin": "482916"})
    lim = PlayLimits(store)
    assert lim.market_band_cents() == 15  # the default
    for wider in (25, None):  # a wider band raises the worst price a Market buy can fill at
        r = phone.post("/api/play/market_band", json={"cents": wider})
        assert r.status_code == 403 and r.json() == {"detail": "Only from the PC itself."}, wider
    assert lim.market_band_cents() == 15
    assert phone.post("/api/play/market_band", json={"cents": 5}).json()["market_band"]["cents"] == 5
    assert phone.post("/api/play/market_band", json={"cents": 10}).status_code == 403
    assert pc.post("/api/play/market_band", json={"cents": None}).json()["market_band"]["cents"] is None
    assert phone.post("/api/play/market_band", json={"cents": 25}).json()["market_band"]["cents"] == 25


def test_a_phone_cannot_change_an_unreadable_band_or_limits_but_the_pc_can_fix_them(store):
    from btc_signal_engine.application.play_limits import BAND_SETTING
    app, _ = _app(store, lambda: NOW)
    pc = paired(TestClient(app, client=("127.0.0.1", 1), follow_redirects=False, base_url="http://127.0.0.1:8000"))
    assert pc.post("/api/access/pin", json={"pin": "482916", "pin_again": "482916"}).json()["ok"]
    phone = TestClient(app, client=("192.168.1.50", 1), follow_redirects=False, base_url="http://192.168.1.10:8000")
    phone.post("/login", data={"pin": "482916"})
    only_pc = {"detail": "Only from the PC itself."}
    lim = PlayLimits(store)
    # an unreadable band cannot be shown to narrow: even the narrowest choice is refused to a phone
    store.insert("strategy_settings", [{**BAND_SETTING, "value": 12, "updated_ts": 7}])
    assert lim.band_view()["readable"] is False
    for cents in (5, 25, None):
        r = phone.post("/api/play/market_band", json={"cents": cents})
        assert r.status_code == 403 and r.json() == only_pc, cents
    assert store.select("strategy_settings", BAND_SETTING)[0]["value"] == 12  # nothing written
    r = pc.post("/api/play/market_band", json={"cents": 15}).json()
    assert r["ok"] and r["market_band"]["cents"] == 15 and r["market_band"]["readable"] is True
    # the same for unreadable limits: a phone is refused (even a "lowering"), the PC fixes them with the phrase
    store.insert("strategy_settings", [{**SETTING, "value": {"per_trade": "lots", "per_day": "none"}, "updated_ts": 7}])
    assert lim.view()["readable"] is False
    for body in ({"per_trade": 1, "per_day": 1}, {"per_trade": None, "per_day": None, "confirm": LIMITS_CONFIRM}):
        r = phone.post("/api/play/limits", json=body)
        assert r.status_code == 403 and r.json() == only_pc, body
    assert lim.view()["readable"] is False
    r = pc.post("/api/play/limits", json={"per_trade": 10, "per_day": 50}).json()
    assert not r["ok"] and r["code"] == "confirm_required"
    r = pc.post("/api/play/limits", json={"per_trade": 10, "per_day": 50, "confirm": LIMITS_CONFIRM}).json()
    assert r["ok"] and (r["per_trade"], r["per_day"], r["error"]) == (10.0, 50.0, None)
