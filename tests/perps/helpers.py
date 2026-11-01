"""Test rig for the perps trading service. NO network: `FakeMargin` is an in-memory Kalshi perps account with the
MarginClient's method names and return shapes (Decimals for fixed-point fields, "_ts" epoch ms for date-times), and
`FakeQuotes` stands in for the margin stream's PerpQuoteCache. Figures follow the real-money check of 2026-10-30
(LEARNINGS "Kalshi perps (margin) API facts"): BTC ~$7.797 a contract, taker fee 0.12% rounded up to $0.0001 per fill,
leverage estimates ~6.85x long / ~6.38x short, initial margin multiplier 1.3, liquidation threshold 1.0, the BTC
notional risk limit $500, the whole-position bracket's fired leg a system IOC with client id "etr:<trigger_id>:0"."""

from __future__ import annotations

import json
from decimal import ROUND_CEILING, Decimal

from btc_signal_engine.adapters.kalshi.margin_stream import PerpQuote
from btc_signal_engine.application.perp_trading import PerpConfig, PerpTradingService

NOW = 1793350800000  # 2026-10-30 09:00:00 UTC
DAY0 = NOW - NOW % 86_400_000
TICKER = "KXBTCPERP"
ENV = "perp_prod"
WRITES = ("create_order", "set_exit_trigger", "update_exit_trigger", "delete_exit_triggers", "delete_exit_trigger",
          "cancel_order", "decrease_order")
TAKER = Decimal("0.0012")


def D(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def fee(price, count) -> Decimal:
    """Kalshi's taker fee: 0.12% of notional rounded UP to $0.0001."""
    return (D(price) * D(count) * TAKER).quantize(Decimal("0.0001"), rounding=ROUND_CEILING)


class FakeRejected(Exception):
    """Kalshi answered 4xx: certainly no effect (the MarginRejected contract)."""

    definitive = True

    def __init__(self, status: int, code: str, message: str = "") -> None:
        self.status = status
        self.body = json.dumps({"code": code, "message": message or code})
        super().__init__(f"Kalshi rejected the request (HTTP {status}): {self.body}")


class FakeUnknown(Exception):
    """A lost reply: the outcome is not known (the MarginUnknown contract)."""

    definitive = False

    def __init__(self, status: int = 0, body: str = "response lost") -> None:
        self.status = status
        self.body = body
        super().__init__(f"outcome unknown (HTTP {status}): {body}")


class FakeMargin:
    """One perps account (subaccount 0 is Settleview's; others are read-only isolated slots)."""

    def __init__(self, env: str = ENV, *, bid="7.7969", ask="7.7971", size="100", equity="50.0000",
                 clock=lambda: NOW) -> None:
        self.env = env
        self.subaccount = 0
        self.ticker = TICKER if env == "perp_prod" else "KXBTCPERP1"
        self.bid, self.ask = D(bid), D(ask)
        self.bid_size = self.ask_size = D(size)
        self.equity, self.available = D(equity), D(equity)
        self.clock = clock
        self.calls: list[tuple[str, dict]] = []
        self.pos: dict[int, dict] = {}  # subaccount -> {position (signed), entry_price, fees, unrealized_pnl}
        self.stale_positions: list[dict] | None = None  # set: positions() answers this (Kalshi's one-step lag)
        self.orders_by_coid: dict[str, dict] = {}
        self.fills_list: list[dict] = []  # REST MarginFill shape, newest last
        self.triggers: list[dict] = []
        self.funding: list[dict] = []
        self.kalshi_liq: Decimal | None = None
        self.trading_active = True
        self.market_status = "active"
        self.risk_limit = D("500.0000")
        self.risk_limit_error: Exception | None = None
        self.fail: dict[str, list[Exception]] = {}  # method -> exceptions raised before acting (definitive)
        self.lose_create_reply = False  # the order IS placed, then the reply is lost
        self.resolve_create_reply = False  # answer like MarginClient._resolve (409 / lost reply found in the lists)
        self.before_create = None  # async hook(client_order_id) run inside create_order before the order is placed
        self.after_create = None  # async hook(client_order_id) run after it is placed and filled, before the reply
        self.fill_limit: Decimal | None = None  # at most this many contracts fill per order
        self.reject_stop_past: Decimal | None = None  # a long's stop at or below this answers 400 past liquidation
        self.resting_foreign: list[dict] = []
        self._n = 0
        self._t = 0

    # -- recording -------------------------------------------------------------------------------------------------
    def _rec(self, name: str, **kw) -> None:
        self.calls.append((name, kw))
        if self.fail.get(name):
            raise self.fail[name].pop(0)

    def writes(self) -> list[tuple[str, dict]]:
        return [c for c in self.calls if c[0] in WRITES]

    def creates(self) -> list[dict]:
        return [kw for name, kw in self.calls if name == "create_order"]

    def _id(self, p: str) -> str:
        self._n += 1
        return f"{p}{self._n}"

    def _updated(self) -> int:
        self._t += 1
        return NOW + self._t

    # -- account state ---------------------------------------------------------------------------------------------
    def held(self, sub: int = 0) -> Decimal:
        return self.pos.get(sub, {}).get("position", Decimal(0))

    def set_position(self, sub: int, signed, entry, fees="0", unrealized="0", margin_used="1") -> None:
        self.pos[sub] = {"position": D(signed), "entry_price": D(entry), "fees": D(fees),
                         "unrealized_pnl": D(unrealized), "margin_used": D(margin_used)}

    def _apply(self, delta: Decimal, price: Decimal, fees: Decimal) -> Decimal:
        """Change subaccount 0's position by `delta` at `price`; returns the realized P&L of any reduced part."""
        p = self.pos.setdefault(0, {"position": Decimal(0), "entry_price": Decimal(0), "fees": Decimal(0),
                                    "unrealized_pnl": Decimal(0), "margin_used": Decimal(0)})
        q, e = p["position"], p["entry_price"]
        realized = Decimal(0)
        if q == 0 or (q > 0) == (delta > 0):
            new = q + delta
            p["entry_price"] = ((abs(q) * e + abs(delta) * price) / abs(new)).quantize(Decimal("0.0001"))
        else:
            closed = min(abs(delta), abs(q))
            realized = (price - e) * closed * (1 if q > 0 else -1)
            new = q + delta
            if new == 0:
                p["entry_price"] = Decimal(0)
        p["position"] = new
        p["fees"] += fees
        self.equity += realized - fees
        self.available = self.equity
        if new == 0:
            self.triggers = []  # a whole-position trigger vanishes when the position is flat
        return realized

    def _fill(self, order_id: str, side: str, count: Decimal, price: Decimal, source: str) -> dict:
        f = fee(price, count)
        entry = self.pos.get(0, {}).get("entry_price", Decimal(0)) or price
        realized = self._apply(count if side == "bid" else -count, price, f)
        row = {"fill_id": self._id("fill-"), "order_id": order_id, "is_taker": True, "side": side, "count": count,
               "created_ts": self.clock(), "ticker": self.ticker, "price": price, "entry_price": entry, "fees": f,
               "realized_pnl": realized.quantize(Decimal("0.0001")), "order_source": source}
        self.fills_list.append(row)
        return row

    def system_fill(self, client_order_id: str, count, price, *, reason: str) -> dict:
        """Kalshi closes `count` contracts itself (a fired trigger "etr:<id>:0" or a liquidation); returns the stream
        `fill` event the margin stream would emit."""
        side = "ask" if self.held() > 0 else "bid"
        oid = self._id("sys-")
        row = self._fill(oid, side, D(count), D(price), "system")
        self.orders_by_coid[client_order_id] = {"order_id": oid, "client_order_id": client_order_id,
                                                "ticker": self.ticker, "side": side, "price": D(price),
                                                "fill_count": D(count), "remaining_count": Decimal(0),
                                                "order_source": "system", "order_reason": reason}
        return stream_fill(row, client_order_id, self.held(), "system", self.env)

    # -- MarginClient reads ----------------------------------------------------------------------------------------
    async def exchange_status(self) -> dict:
        self._rec("exchange_status")
        return {"exchange_active": True, "trading_active": self.trading_active}

    async def market(self, ticker: str) -> dict:
        self._rec("market", ticker=ticker)
        return {"ticker": ticker, "status": self.market_status, "tick_size": Decimal("0.0001"),
                "contract_size": Decimal("0.0001"), "fractional_trading_enabled": False,
                "long_leverage_estimates": {"1000": 6.8473, "10000": 6.5},
                "short_leverage_estimates": {"1000": 6.38, "10000": 6.1},
                "funding_cap": 0.02, "funding_limit_unit": "percentage"}

    async def risk_parameters(self) -> dict:
        self._rec("risk_parameters")
        return {"liquidation_margin_ratio_threshold": 1.0, "queue_entry_margin_ratio_threshold": 0.91,
                "initial_margin_multiplier": {self.ticker: 1.3}}

    async def fee_tiers(self) -> dict:
        self._rec("fee_tiers")
        return {"maker_fee_rates": {self.ticker: 0.0002}, "taker_fee_rates": {self.ticker: 0.0012}}

    async def notional_risk_limit(self) -> dict:
        self._rec("notional_risk_limit")
        if self.risk_limit_error is not None:
            raise self.risk_limit_error
        return {"notional_value_risk_limits_by_market_ticker": {self.ticker: self.risk_limit},
                "current_usage_by_market_ticker": {}, "total_current_usage": Decimal(0)}

    async def balance(self, *, compute_available_balance: bool) -> dict:
        self._rec("balance", compute_available_balance=compute_available_balance)
        return {"settled_funds": self.equity, "subaccount_balances": [
            {"subaccount": 0, "account_equity": self.equity,
             "available_balance": self.available if compute_available_balance else Decimal(0)},
            {"subaccount": 64, "account_equity": Decimal(0), "available_balance": Decimal(0)}]}

    def _positions(self) -> list[dict]:
        return [{"subaccount": s, "market_ticker": self.ticker, **p} for s, p in sorted(self.pos.items())]

    async def positions(self, *, subaccount: int | None = None, ticker: str | None = None) -> list[dict]:
        self._rec("positions", subaccount=subaccount, ticker=ticker)
        rows = self.stale_positions if self.stale_positions is not None else self._positions()
        return [dict(r) for r in rows if subaccount is None or r["subaccount"] == subaccount]

    async def risk(self) -> dict:
        self._rec("risk")
        q = self.held()
        rows = [] if q == 0 else [{"subaccount": 0, "market_ticker": self.ticker, "position": q,
                                   "mark_price": self.bid, "position_notional": abs(q) * self.bid,
                                   "estimated_liquidation_price": self.kalshi_liq}]
        return {"positions": rows}

    async def orders(self, *, ticker=None, subaccount=None, status=None, min_ts=None, max_ts=None, limit=None,
                     cursor=None) -> dict:
        self._rec("orders", ticker=ticker, subaccount=subaccount, status=status)
        if status == "resting":
            return {"orders": [dict(o) for o in self.resting_foreign], "cursor": ""}
        return {"orders": [dict(o) for o in self.orders_by_coid.values()], "cursor": ""}

    async def order(self, order_id: str) -> dict:
        self._rec("order", order_id=order_id)
        for o in self.orders_by_coid.values():
            if o["order_id"] == order_id:
                return dict(o)
        raise FakeRejected(404, "not_found")

    async def fills(self, *, subaccount=None, min_ts=None, max_ts=None, limit=None, cursor=None) -> dict:
        self._rec("fills", subaccount=subaccount, min_ts=min_ts)
        rows = [f for f in self.fills_list if min_ts is None or f["created_ts"] >= min_ts * 1000]
        return {"fills": [dict(f) for f in reversed(rows)], "cursor": ""}

    async def funding_estimate(self, ticker: str) -> dict:
        self._rec("funding_estimate", ticker=ticker)
        return {"market_ticker": ticker, "funding_rate": 0.0001, "mark_price": self.bid,
                "next_funding_ts": DAY0 + 12 * 3_600_000}

    async def funding_history(self, *, start_date, end_date, ticker=None, subaccount=None) -> list[dict]:
        self._rec("funding_history", start_date=start_date, end_date=end_date, ticker=ticker, subaccount=subaccount)
        return [dict(f) for f in self.funding]

    async def exit_triggers(self, ticker: str, *, kind=None) -> list[dict]:
        self._rec("exit_triggers", ticker=ticker)
        return [dict(t) for t in self.triggers]

    # -- MarginClient writes ---------------------------------------------------------------------------------------
    async def create_order(self, *, ticker, side, count, price, client_order_id, time_in_force, reduce_only,
                           purpose) -> dict:
        self._rec("create_order", ticker=ticker, side=side, count=count, price=price, client_order_id=client_order_id,
                  time_in_force=time_in_force, reduce_only=reduce_only, purpose=purpose)
        assert isinstance(count, (Decimal, str, int)) and not isinstance(count, float)
        assert isinstance(price, (Decimal, str)) and ticker == self.ticker
        if purpose == "exit":
            assert reduce_only
        if self.before_create is not None:
            await self.before_create(client_order_id)
        if client_order_id in self.orders_by_coid:
            o = self.orders_by_coid[client_order_id]
            return {"order_id": o["order_id"], "client_order_id": client_order_id, "fill_count": o["fill_count"],
                    "remaining_count": o["remaining_count"], "average_fill_price": None, "average_fee_paid": None,
                    "duplicate": True}
        count, price = D(count), D(price)
        crosses = price >= self.ask if side == "bid" else price <= self.bid
        n = min(count, self.ask_size if side == "bid" else self.bid_size) if crosses else Decimal(0)
        if self.fill_limit is not None:
            n = min(n, self.fill_limit)
        if reduce_only:
            q = self.held()
            opposite = (q > 0 and side == "ask") or (q < 0 and side == "bid")
            n = min(n, abs(q)) if opposite else Decimal(0)
        if time_in_force == "fill_or_kill" and n < count:
            n = Decimal(0)
        oid = self._id("ord-")
        px = self.ask if side == "bid" else self.bid
        fees = Decimal(0)
        if n > 0:
            fees = self._fill(oid, side, n, px, "user")["fees"]
        self.orders_by_coid[client_order_id] = {"order_id": oid, "client_order_id": client_order_id,
                                                "ticker": ticker, "side": side, "price": price, "fill_count": n,
                                                "remaining_count": Decimal(0), "order_source": "user"}
        if self.after_create is not None:
            await self.after_create(client_order_id)
        if self.resolve_create_reply:  # MarginClient._resolve: found in the order list, which carries no averages
            o = self.orders_by_coid[client_order_id]
            return {"order_id": oid, "client_order_id": client_order_id, "fill_count": n,
                    "remaining_count": Decimal(0), "average_fill_price": None, "average_fee_paid": None,
                    "duplicate": True, "order": dict(o)}
        if self.lose_create_reply:
            self.lose_create_reply = False
            raise FakeUnknown()
        return {"order_id": oid, "client_order_id": client_order_id, "fill_count": n, "remaining_count": Decimal(0),
                "average_fill_price": px if n > 0 else None,
                "average_fee_paid": (fees / n).quantize(Decimal("0.000001")) if n > 0 else None, "duplicate": False}

    def _check_legs(self, stop_loss_price, take_profit_price) -> None:
        if stop_loss_price is None and take_profit_price is None:
            raise AssertionError("a trigger write needs a leg")
        if self.reject_stop_past is not None and stop_loss_price is not None and \
                D(stop_loss_price) <= self.reject_stop_past:
            raise FakeRejected(400, "stop_loss_past_liquidation", "stop loss is past the liquidation price")

    async def set_exit_trigger(self, ticker, *, stop_loss_price=None, take_profit_price=None) -> dict:
        self._rec("set_exit_trigger", ticker=ticker, stop_loss_price=stop_loss_price,
                  take_profit_price=take_profit_price)
        self._check_legs(stop_loss_price, take_profit_price)
        tid = self.triggers[0]["id"] if self.triggers else self._id("trg-")
        t = {"id": tid, "ticker": ticker, "kind": "bracket", "status": "active", "count": Decimal("0.00"),
             "filled_count": Decimal("0.00"),
             "stop_loss_price": None if stop_loss_price is None else D(stop_loss_price),
             "take_profit_price": None if take_profit_price is None else D(take_profit_price),
             "created_ts": NOW, "updated_ts": self._updated()}
        self.triggers = [t]
        return dict(t)

    async def update_exit_trigger(self, ticker, trigger_id, *, stop_loss_price=None, take_profit_price=None) -> dict:
        self._rec("update_exit_trigger", ticker=ticker, trigger_id=trigger_id, stop_loss_price=stop_loss_price,
                  take_profit_price=take_profit_price)
        self._check_legs(stop_loss_price, take_profit_price)
        t = next(t for t in self.triggers if t["id"] == trigger_id)
        t.update(stop_loss_price=None if stop_loss_price is None else D(stop_loss_price),
                 take_profit_price=None if take_profit_price is None else D(take_profit_price),
                 updated_ts=self._updated())
        return dict(t)

    async def delete_exit_triggers(self, ticker, *, kind=None) -> None:
        self._rec("delete_exit_triggers", ticker=ticker)
        self.triggers = []

    def edit_trigger_elsewhere(self, **legs) -> None:
        """The user edits the bracket in Kalshi's app: same id, new updated_time."""
        self.triggers[0].update({k: D(v) for k, v in legs.items()}, updated_ts=self._updated())


def stream_fill(row: dict, client_order_id: str | None, post_position: Decimal, source: str, env: str) -> dict:
    """The margin stream's `fill` event for a REST fill row (trade_id = fill_id, as in the production capture)."""
    return {"type": "fill", "env": env, "trade_id": row["fill_id"], "order_id": row["order_id"],
            "client_order_id": client_order_id, "ticker": row["ticker"], "is_taker": True, "side": row["side"],
            "price": float(row["price"]), "count": float(row["count"]), "fee": float(row["fees"]),
            "post_position": float(post_position), "subaccount": 0, "order_source": source,
            "ts_ms": row["created_ts"]}


class FakeQuotes:
    """The PerpQuoteCache read surface: quote() / age_ms() / why_none()."""

    def __init__(self, fx: FakeMargin) -> None:
        self.fx = fx
        self.why: str | None = None
        self.age = 120
        self.mark: float | None = 7.7970
        self.funding_rate: float | None = 0.0001

    def quote(self) -> PerpQuote | None:
        if self.why is not None:
            return None
        return PerpQuote(self.fx.ticker, float(self.fx.bid), float(self.fx.ask), float(self.fx.bid_size),
                         float(self.fx.ask_size), self.age, 7.7970, 7.7965, self.mark, self.mark,
                         self.funding_rate, DAY0 + 12 * 3_600_000, 300)

    def age_ms(self) -> int | None:
        return self.age

    def why_none(self) -> str | None:
        return self.why

    def stale(self, age_ms: int) -> None:
        self.age = age_ms
        self.why = f"quote {age_ms / 1000:.1f} s old"


class Ids:
    def __init__(self, prefix: str = "id") -> None:
        self.n = 0
        self.prefix = prefix

    def __call__(self) -> str:
        self.n += 1
        return f"{self.prefix}{self.n:04d}"


class Limits:
    """A limits_fn: {"per_trade", "per_window", "per_day"}; `broken` raises like an unreadable setting."""

    def __init__(self, per_trade=None, per_day=None) -> None:
        self.value = {"per_trade": per_trade, "per_window": per_trade, "per_day": per_day}
        self.broken = False

    def __call__(self) -> dict:
        if self.broken:
            raise ValueError("per_trade is 'abc' (expected a dollar amount or \"none\")")
        return dict(self.value)


def service(store, fx: FakeMargin, *, mode: str = ENV, config: PerpConfig | None = None, limits=None,
            committed_today_fn=None, quotes: FakeQuotes | None = None, now: int = NOW) -> PerpTradingService:
    if not store.select("trading_settings", {"key": "perps.mode"}):
        store.insert("trading_settings", [{"key": "perps.mode", "value": {"mode": mode}, "updated_ts": 0}],
                     ignore_conflicts=False)
    q = quotes or FakeQuotes(fx)
    return PerpTradingService(store, {fx.env: fx}, config or PerpConfig(prod_enabled=True),
                              quotes={fx.env: q}, limits_fn=limits, committed_today_fn=committed_today_fn,
                              id_fn=Ids(), token_fn=Ids("tok"), now=now)


async def ready(svc: PerpTradingService, now: int = NOW) -> PerpTradingService:
    r = await svc.reconcile(now)
    assert r["ok"], r
    svc.heartbeat(now)
    return svc


def entry(**kw) -> dict:
    """An entry request: $10 at 3x long (30 / 7.7971 -> 3 contracts), market-style, no stop."""
    r = {"action": "entry", "side": "long", "collateral": 10.0, "leverage": 3.0, "stop_loss": None,
         "take_profit": None, "fill": "market", "limit_price": None, "band": None}
    r.update(kw)
    return r


async def approve(svc, prep, now: int = NOW, **change):
    assert prep["ok"], prep
    return await svc.approve(prep["token"], {**prep["ticket"], **change}, now)


async def open_long(svc, fx, now: int = NOW, **kw):
    out = await approve(svc, await svc.prepare(entry(**kw), now), now)
    assert out["ok"], out
    return out
