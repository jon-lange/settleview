"""Test doubles for the polymarket_us SDK. NO network: the fake client keeps a one-market book and an account in
memory and answers with the documented response shapes (orders.create -> {id, executions[]}, positions keyed by
slug, ...). Errors are the SDK's own exception classes built on a local httpx.Response."""

from __future__ import annotations

import datetime as dt
import itertools

import httpx
from polymarket_us import (APIConnectionError, AuthenticationError, BadRequestError, InternalServerError,
                           NotFoundError, RateLimitError)

from btc_signal_engine.adapters.polymarket_us.markets import slug_for
from btc_signal_engine.domain.settlement.fees import polymarket_fee

W0 = 1793275200000  # 2026-10-29 12:00Z
CLOSE = W0 + 900_000
NOW = W0 + 330_000  # 9.5 minutes left
SLUG = slug_for(W0)
NEXT = slug_for(W0 + 900_000)


def http_error(cls, status: int, message: str = "nope"):
    resp = httpx.Response(status, request=httpx.Request("POST", "https://api.polymarket.us/v1/orders"))
    return cls(message, response=resp, body={"message": message})


def amount(x: float) -> dict:
    return {"value": f"{x:.2f}", "currency": "USD"}


class Clock:
    def __init__(self, t: int = NOW) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t


class FakeOrders:
    def __init__(self, acct: FakePolymarket) -> None:
        self.a = acct

    async def create(self, params: dict) -> dict:
        a = self.a
        a.calls.append(("create", dict(params)))
        if a.fail_next:
            exc, a.fail_next = a.fail_next, None
            if isinstance(exc, tuple):  # (exception, place_anyway): the order WAS placed, the answer lost
                exc, placed = exc
                if placed:
                    a._fill(params)
            raise exc
        return a._fill(params)

    async def cancel(self, order_id: str, params: dict) -> None:
        a = self.a
        a.calls.append(("cancel", order_id, dict(params)))
        if a.cancel_error is not None:
            raise a.cancel_error
        o = a.orders.get(order_id)
        if o is None or o["state"] not in ("ORDER_STATE_NEW", "ORDER_STATE_PARTIALLY_FILLED"):
            raise http_error(NotFoundError, 404, "order not found")
        o["state"], o["leavesQuantity"] = "ORDER_STATE_CANCELED", 0

    async def retrieve(self, order_id: str) -> dict:
        self.a.calls.append(("retrieve", order_id))
        o = self.a.orders.get(order_id)
        if o is None:
            raise http_error(NotFoundError, 404, "order not found")
        return {"order": dict(o)}

    async def list(self, params: dict | None = None) -> dict:
        self.a.calls.append(("list", params))
        slugs = (params or {}).get("slugs")
        return {"orders": [dict(o) for o in self.a.orders.values()
                           if o["state"] in ("ORDER_STATE_NEW", "ORDER_STATE_PARTIALLY_FILLED")
                           and (not slugs or o["marketSlug"] in slugs)]}


class FakePortfolio:
    def __init__(self, acct: FakePolymarket) -> None:
        self.a = acct

    async def positions(self, params: dict | None = None) -> dict:
        self.a.calls.append(("positions", params))
        if self.a.positions_error is not None:
            raise self.a.positions_error
        pos = self.a.pos if self.a.frozen is None else self.a.frozen  # frozen = a lagging positions list
        return {"positions": {s: {"netPosition": str(int(n)), "qtyAvailable": str(abs(int(n)))}
                              for s, n in pos.items() if n}, "eof": True}


    async def activities(self, params: dict | None = None) -> dict:
        self.a.calls.append(("activities", params))
        return {"activities": list(self.a.activities), "eof": True}


class FakeAccount:
    def __init__(self, acct: FakePolymarket) -> None:
        self.a = acct

    async def balances(self) -> dict:
        return {"balances": [{"currentBalance": self.a.cash, "buyingPower": self.a.cash}]}


class FakePolymarket:
    """An in-memory Polymarket US account over one controllable YES book (bid, ask, sizes)."""

    def __init__(self, bid: float = 0.69, ask: float = 0.70, size: int = 50) -> None:
        self.bid, self.ask, self.size = bid, ask, size
        self.pos: dict[str, int] = {}
        self.orders: dict[str, dict] = {}
        self.cash = 100.0
        self.calls: list[tuple] = []
        self.fail_next = None
        self.cancel_error = None
        self.positions_error = None
        self.frozen: dict | None = None  # set: positions() keeps answering this stale snapshot
        # portfolio activities: every fill of the account (ours included, as on the venue) plus trades a test adds
        self.activities: list[dict] = []
        self.clock = lambda: NOW  # the time stamped on activity trades (tests pass their Clock)
        self._trades = itertools.count(1)
        self._ids = itertools.count(1)
        self.orders_api, self.portfolio, self.account = FakeOrders(self), FakePortfolio(self), FakeAccount(self)
        self.closed = False

    def creates(self) -> list[dict]:
        return [c[1] for c in self.calls if c[0] == "create"]

    def _fill(self, p: dict) -> dict:
        oid = f"ord{next(self._ids)}"
        slug, intent, qty = p["marketSlug"], p["intent"], int(p["quantity"])
        px = float(p["price"]["value"])
        buys_yes = intent in ("ORDER_INTENT_BUY_LONG", "ORDER_INTENT_SELL_SHORT")
        crosses = px >= self.ask - 1e-9 if buys_yes else px <= self.bid + 1e-9
        order = {"id": oid, "marketSlug": slug, "intent": intent, "price": amount(px), "quantity": qty,
                 "cumQuantity": 0, "leavesQuantity": qty, "tif": p["tif"], "state": "ORDER_STATE_NEW"}
        execs = []
        if p.get("participateDontInitiate") and crosses:
            order.update(state="ORDER_STATE_REJECTED", leavesQuantity=0)
            self.orders[oid] = order
            return {"id": oid, "executions": [{"type": "EXECUTION_TYPE_REJECTED", "order": dict(order),
                                               "orderRejectReason": "ORD_REJECT_REASON_WOULD_CROSS"}]}
        if crosses:
            n = min(qty, self.size)
            fill_px = self.ask if buys_yes else self.bid
            fee = polymarket_fee(n, fill_px)
            self.pos[slug] = self.pos.get(slug, 0) + (n if buys_yes else -n)
            order.update(cumQuantity=n, leavesQuantity=qty - n, avgPx=amount(fill_px),
                         commissionNotionalTotalCollected=amount(fee),
                         state="ORDER_STATE_FILLED" if n == qty else "ORDER_STATE_PARTIALLY_FILLED")
            tid = f"t{next(self._trades)}"
            execs.append({"type": "EXECUTION_TYPE_FILL" if n == qty else "EXECUTION_TYPE_PARTIAL_FILL",
                          "order": dict(order), "lastPx": amount(fill_px), "lastShares": str(n),
                          "commissionNotionalCollected": amount(fee), "tradeId": tid})
            when = dt.datetime.fromtimestamp(self.clock() / 1000, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            self.activities.append({"type": "ACTIVITY_TYPE_TRADE", "trade": {
                "id": tid, "marketSlug": slug, "price": amount(fill_px), "qty": str(n), "createTime": when}})
        if p["tif"] != "TIME_IN_FORCE_GOOD_TILL_CANCEL" and order["leavesQuantity"]:
            order.update(leavesQuantity=0, state="ORDER_STATE_FILLED" if order["cumQuantity"] == qty
                         else "ORDER_STATE_CANCELED")
            execs.append({"type": "EXECUTION_TYPE_CANCELED", "order": dict(order)})
        self.orders[oid] = order
        if not execs:
            execs.append({"type": "EXECUTION_TYPE_NEW", "order": dict(order)})
        if p["tif"] == "TIME_IN_FORCE_GOOD_TILL_CANCEL" and not p.get("synchronousExecution"):
            return {"id": oid}
        return {"id": oid, "executions": execs}

    async def close(self) -> None:
        self.closed = True


def client(**kw) -> FakeClient:
    return FakeClient(FakePolymarket(**kw))


class FakeClient:
    """The SDK client surface PolymarketTrading uses: .orders, .portfolio, .account, .close()."""

    def __init__(self, acct: FakePolymarket) -> None:
        self.acct = acct
        self.orders, self.portfolio, self.account = acct.orders_api, acct.portfolio, acct.account

    async def close(self) -> None:
        await self.acct.close()


class FakeWS:
    """SDK websocket surface: on / connect / subscribe / unsubscribe / close; `emit` drives events."""

    def __init__(self, fail_connect: bool = False) -> None:
        self.handlers: dict[str, list] = {}
        self.subs: list[tuple] = []
        self.unsubs: list[str] = []
        self.connected = False
        self.closed = False
        self.fail_connect = fail_connect

    def on(self, event, fn):
        self.handlers.setdefault(event, []).append(fn)
        return self

    async def connect(self):
        if self.fail_connect:
            raise OSError("refused")
        self.connected = True

    async def subscribe(self, rid, typ, slugs=None):
        self.subs.append((rid, typ, list(slugs) if slugs else None))

    async def unsubscribe(self, rid):
        self.unsubs.append(rid)

    async def close(self):
        self.closed = True

    def emit(self, event, *args):
        for fn in self.handlers.get(event, []):
            fn(*args)


__all__ = ["APIConnectionError", "AuthenticationError", "BadRequestError", "InternalServerError", "NotFoundError",
           "RateLimitError"]


def lite(slug: str, bid: float | None, ask: float | None, bid_depth=None, ask_depth=None) -> dict:
    m = {"marketSlug": slug}
    if bid is not None:
        m["bestBid"] = amount(bid)
    if ask is not None:
        m["bestAsk"] = amount(ask)
    if bid_depth is not None:
        m["bidDepth"] = bid_depth
    if ask_depth is not None:
        m["askDepth"] = ask_depth
    return {"requestId": "x", "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA_LITE", "marketDataLite": m}


def full_book(slug: str, bids: list[tuple[float, float]], offers: list[tuple[float, float]], state=None) -> dict:
    m = {"marketSlug": slug, "bids": [{"px": amount(p), "qty": str(q)} for p, q in bids],
         "offers": [{"px": amount(p), "qty": str(q)} for p, q in offers]}
    if state:
        m["state"] = state
    return {"requestId": "x", "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA", "marketData": m}


def trade(slug: str, price: float, qty: float, taker_intent: str) -> dict:
    return {"requestId": "x", "subscriptionType": "SUBSCRIPTION_TYPE_TRADE",
            "trade": {"marketSlug": slug, "price": amount(price), "quantity": {"value": str(qty)},
                      "tradeTime": "2026-10-29T12:05:30.123456Z", "maker": {"side": "x", "intent": "x"},
                      "taker": {"side": "x", "intent": taker_intent}}}
