"""The trading adapter (signing, allow-list, series, V2 mapping, hosts) and the shadow exchange (real-book fills)."""

from __future__ import annotations

import asyncio
import base64

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from btc_signal_engine.adapters.kalshi.auth import KalshiAuth, ReadOnlyViolation
from btc_signal_engine.adapters.kalshi.shadow import LABEL, ShadowExchange, parse_book, shadow_trading
from btc_signal_engine.adapters.kalshi.trading import (BASE_URLS, KalshiTrading, OrderRejected, TradingAuth,
                                                        TradingViolation, book_order, trading_from_settings)
from tests.trading.helpers import NOW, TICKER, Exchange, Market, gateway, proposal, ready, service


def run(coro):
    return asyncio.run(coro)


# -- adapter ------------------------------------------------------------------------------------------
def test_hosts_by_environment():
    key = ed25519.Ed25519PrivateKey.generate()
    assert KalshiTrading(TradingAuth("k", key), env="demo").base_url == "https://external-api.demo.kalshi.co/trade-api/v2"
    assert KalshiTrading(TradingAuth("k", key), env="prod").base_url == "https://external-api.kalshi.com/trade-api/v2"
    assert BASE_URLS["demo"] != BASE_URLS["prod"]
    with pytest.raises(ValueError):
        KalshiTrading(TradingAuth("k", key), env="shadow")  # shadow only ever with the simulated transport
    with pytest.raises(ValueError):
        KalshiTrading(TradingAuth("k", key), env="live")


def test_settings_factory_needs_the_environment_key(tmp_path):
    assert trading_from_settings("demo", None, None, max_order_cost=5) is None
    bad = tmp_path / "bad.pem"
    bad.write_text("not a key")
    assert trading_from_settings("prod", "id", bad, max_order_cost=5) is None
    pem = tmp_path / "demo.pem"
    pem.write_bytes(ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    gw = trading_from_settings("demo", "demo-id", pem, max_order_cost=5)
    assert gw.env == "demo" and "demo-id" not in repr(gw)
    # prod/demo go through Kalshi's SDK on the environment's own host, signed by our (redacted) auth
    assert gw._backend.api_client.configuration.host == "https://external-api.demo.kalshi.co/trade-api/v2"
    assert "redacted" in repr(gw._backend.api_client.kalshi_auth) and "demo-id" not in repr(gw._backend)


def test_auth_signs_post_path_without_query_and_refuses_everything_off_the_allow_list():
    key = ed25519.Ed25519PrivateKey.generate()
    auth = TradingAuth("kid", key, clock_ms=lambda: 1700000000000)
    seen = []
    tr = httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json={}))
    with httpx.Client(transport=tr, base_url=BASE_URLS["demo"], auth=auth) as c:
        c.post("/portfolio/events/orders", json={})
        c.delete("/portfolio/events/orders/abc", params={"market_ticker": TICKER})
        sig = base64.b64decode(seen[1].headers["KALSHI-ACCESS-SIGNATURE"])
        key.public_key().verify(sig, b"1700000000000DELETE/trade-api/v2/portfolio/events/orders/abc")
        for method, path in [("POST", "/portfolio/orders"), ("DELETE", "/portfolio/events/orders"),
                             ("POST", "/portfolio/events/orders/batched"), ("PUT", "/portfolio/events/orders/x"),
                             ("POST", "/portfolio/orders/x/amend"), ("GET", "/portfolio/settlements"),
                             ("POST", "/api_keys")]:
            with pytest.raises(TradingViolation):
                c.request(method, path)
    assert len(seen) == 2


def test_read_only_link_still_refuses_writes():
    auth = KalshiAuth("k", ed25519.Ed25519PrivateKey.generate())
    seen = []
    with httpx.Client(transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200)),
                      base_url=BASE_URLS["prod"], auth=auth) as c:
        for m in ("POST", "DELETE"):
            with pytest.raises(ReadOnlyViolation):
                c.request(m, "/portfolio/events/orders")
    assert seen == []


@pytest.mark.parametrize("kw", [dict(ticker="KXETH15M-26OCT281315-15"), dict(ticker="KXBTC15MX"),
                                dict(count=0), dict(price=0.0), dict(price=1.0), dict(side="maybe"),
                                dict(count=100.0, price=0.5)])
def test_adapter_refuses_bad_orders_before_the_transport(kw):
    ex = Exchange(Market())
    gw = gateway(ex, "prod", max_order_cost=25.0)
    args = dict(ticker=TICKER, side="yes", action="buy", count=5.0, price=0.5, client_order_id="c1")
    args.update(kw)
    with pytest.raises(TradingViolation):
        run(gw.place_limit(**args))
    assert ex.seen == []


def test_v2_book_mapping():
    assert book_order("yes", "buy", 0.58) == ("bid", "0.5800")
    assert book_order("yes", "sell", 0.7) == ("ask", "0.7000")
    assert book_order("no", "buy", 0.45) == ("ask", "0.5500")
    assert book_order("no", "sell", 0.6) == ("bid", "0.4000")


def test_kalshi_rejection_is_definitive_and_nothing_retries():
    calls = []

    def handler(r):
        calls.append(r)
        return httpx.Response(400, json={"error": {"code": "invalid_order"}})
    gw = KalshiTrading(TradingAuth("k", ed25519.Ed25519PrivateKey.generate()), env="demo",
                       transport=httpx.MockTransport(handler))
    with pytest.raises(OrderRejected) as e:
        run(gw.place_limit(ticker=TICKER, side="yes", action="buy", count=1, price=0.5, client_order_id="x"))
    assert e.value.definitive and len(calls) == 1


def test_get_order_rides_out_kalshis_brief_404_right_after_a_write():
    # Seen in production: GET /portfolio/orders/{id} returned 404 just after a cancel, then 200 seconds later.
    order = {"order_id": "o1", "client_order_id": "c1", "status": "canceled", "side": "yes", "action": "buy",
             "fill_count_fp": "0", "remaining_count_fp": "0"}
    calls = []

    def handler(r):
        calls.append(r)
        return httpx.Response(404) if len(calls) < 3 else httpx.Response(200, json={"order": order})

    async def no_sleep(_):
        return None
    gw = KalshiTrading(TradingAuth("k", ed25519.Ed25519PrivateKey.generate()), env="demo",
                       transport=httpx.MockTransport(handler), sleep=no_sleep)
    assert run(gw.get_order("o1"))["status"] == "canceled" and len(calls) == 3


def test_get_order_still_raises_when_the_order_never_appears():
    async def no_sleep(_):
        return None
    gw = KalshiTrading(TradingAuth("k", ed25519.Ed25519PrivateKey.generate()), env="demo",
                       transport=httpx.MockTransport(lambda r: httpx.Response(404)), sleep=no_sleep)
    with pytest.raises(httpx.HTTPStatusError):
        run(gw.get_order("missing"))


def test_sandbox_routes_to_the_demo_host(store):
    async def go():
        ex = Exchange(Market())
        svc = await ready(service(store, ex, mode="sandbox"))
        prep = await svc.prepare(proposal(), NOW)
        await svc.approve(prep["token"], "yes", prep["ticket"], NOW)
        assert ex.posts()
        assert all(r["path"].startswith("/trade-api/v2/") for r in ex.seen)
        assert svc.gateways["demo"].base_url.startswith("https://external-api.demo.kalshi.co")
        assert svc.gateways["prod"] is None
    run(go())


def test_sandbox_reprices_to_the_demo_ask_keeping_bracket_distances(store):
    async def go():
        ex = Exchange(Market(yes_bid=0.50, no_bid=0.45))  # demo YES ask 55c, Coach saw 58c
        svc = await ready(service(store, ex, mode="sandbox"))
        t = (await svc.prepare(proposal(), NOW))["ticket"]
        assert t["repriced_for_demo"] and t["limit_price"] == 0.55 and t["take_profit"] == 0.67 and t["stop"] == 0.40
        assert "demo exchange" in t["mode_label"]
    run(go())


# -- shadow -------------------------------------------------------------------------------------------
def test_parse_real_orderbook_payload():
    b = parse_book({"orderbook_fp": {"no_dollars": [["0.2200", "1516.00"]], "yes_dollars": [["0.7300", "6420.65"]]}})
    assert b == {"yes": [(0.73, 6420.65)], "no": [(0.22, 1516.0)]}


def test_shadow_entry_partially_fills_on_a_thin_book(store):
    async def go():
        m = Market()
        m.set(0.55, 0.42, depth=([(0.55, 100.0)], [(0.42, 2.0), (0.41, 1.5), (0.30, 500.0)]))  # asks 58c x2, 59c x1.5
        ex = ShadowExchange(book_fn=m.book_fn, market_fn=m.market_fn)
        gw = shadow_trading(max_order_cost=25, exchange=ex)
        res = await gw.place_limit(ticker=TICKER, side="yes", action="buy", count=8.0, price=0.59,
                                   client_order_id="a", tif="immediate_or_cancel")
        assert res["filled"] == 3.5 and res["remaining"] == 0 and res["status"] == "executed"
        assert res["avg_price"] == pytest.approx((2 * 0.58 + 1.5 * 0.59) / 3.5, abs=1e-4)
        assert ex.positions[TICKER] == 3.5
        res2 = await gw.place_limit(ticker=TICKER, side="yes", action="buy", count=8.0, price=0.57,
                                    client_order_id="b")
        assert res2["filled"] == 0  # nothing offered at <= 57c
    run(go())


def test_shadow_and_prod_send_identical_order_intents(store):
    async def go():
        bodies = {}
        for mode in ("prod", "shadow"):
            m = Market()
            ex = Exchange(m)
            svc = await ready(service(store, ex, mode=mode))
            prep = await svc.prepare(proposal(), NOW)
            await svc.approve(prep["token"], "yes", prep["ticket"], NOW)
            m.set(0.43, 0.50)
            await svc.tick(NOW + 3000)
            bodies[mode] = [(r["method"], r["path"].rsplit("/", 1)[0] if r["method"] == "DELETE" else r["path"],
                             {k: v for k, v in (r["body"] or {}).items() if k != "client_order_id"})
                            for r in ex.writes()]
            store.update("trading_brackets", {"env": mode}, {"state": "closed"})  # next run starts clean
            store.update("trading_settings", {"key": "you.mode"}, {"value": {"mode": "sandbox"}})
        assert bodies["prod"] == bodies["shadow"] and len(bodies["prod"]) == 4
    run(go())


def test_shadow_stop_walks_the_real_bid_side_within_the_cap(store):
    async def go():
        m = Market()
        ex = ShadowExchange(book_fn=m.book_fn, market_fn=m.market_fn)
        gw = shadow_trading(max_order_cost=25, exchange=ex)
        ex.positions[TICKER] = 8.0
        m.set(0, 0, depth=([(0.43, 3.0), (0.40, 2.0), (0.37, 50.0)], [(0.50, 100.0)]))
        res = await gw.place_limit(ticker=TICKER, side="yes", action="sell", count=10.0, price=0.38,
                                   client_order_id="s", reduce_only=True)
        assert res["filled"] == 5.0  # 3 @43c + 2 @40c; the 37c bids are past the 5c cap
        assert res["avg_price"] == pytest.approx((3 * 0.43 + 2 * 0.40) / 5, abs=1e-4)
        assert ex.positions[TICKER] == 3.0
        res = await gw.place_limit(ticker=TICKER, side="yes", action="sell", count=10.0, price=0.30,
                                   client_order_id="s2", reduce_only=True)
        assert res["filled"] == 3.0 and ex.positions[TICKER] == 0  # reduce-only: never more than held
    run(go())


def test_shadow_resting_tp_waits_at_the_back_of_the_queue():
    async def go():
        m = Market()
        ex = ShadowExchange(book_fn=m.book_fn, market_fn=m.market_fn)
        gw = shadow_trading(max_order_cost=25, exchange=ex)
        ex.positions[TICKER] = 5.0
        r = await gw.place_limit(ticker=TICKER, side="yes", action="sell", count=5.0, price=0.70,
                                 client_order_id="tp", tif="good_till_canceled", post_only=True)
        assert r["status"] == "resting"
        m.set(0.70, 0.25, size=100.0)  # bid only TOUCHES 70c: back of the queue, no fill
        assert (await gw.get_order(r["order_id"]))["filled"] == 0
        m.set(0.71, 0.25, size=2.0)  # trades through by 2 contracts
        o = await gw.get_order(r["order_id"])
        assert o["filled"] == 2.0 and o["fees"] == 0.0 and o["status"] == "resting"
        m.set(0.72, 0.25, size=50.0)
        o = await gw.get_order(r["order_id"])
        assert o["filled"] == 5.0 and o["status"] == "executed"
        # post_only that would cross is rejected, like Kalshi
        with pytest.raises(OrderRejected):
            await gw.place_limit(ticker=TICKER, side="yes", action="sell", count=1.0, price=0.70,
                                 client_order_id="tp2", tif="good_till_canceled", post_only=True)
    run(go())


def test_shadow_needs_no_keys(store):
    async def go():
        ex = Exchange(Market())
        svc = await ready(service(store, ex, mode="shadow"))
        prep = await svc.prepare(proposal(), NOW)
        r = await svc.approve(prep["token"], "yes", prep["ticket"], NOW)
        assert r["ok"] and r["bracket"]["env"] == "shadow"
        assert "simulated fills on the real order book" in svc.status(NOW)["label"]
        assert LABEL == "shadow (simulated fills on the real order book)"
    run(go())
