"""SHADOW mode: a simulated Kalshi exchange behind the real trading client (no money, no keys, no gate).

Shadow runs the exact production code path: the same `KalshiTrading` client builds and signs the same V2 order
requests (with a throwaway key generated in memory); only the httpx transport differs. `ShadowExchange` answers
those requests by filling against Kalshi's REAL public order book at that moment
(GET https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}/orderbook, public, no auth). Nothing is ever
forwarded to an authenticated Kalshi endpoint; the only outbound calls are public market reads.

Fill rules (all prices are YES prices, as on the V2 book; YES asks are derived from NO bids: ask = 1 - no bid):
* A marketable order (any time in force) walks the opposite side of the real book level by level at each level's
  price, only through levels at or better than its limit, and only up to the size shown there; thin books give
  partial fills. Taker fee per level: domain `kalshi_fee` (0.07 x C x P x (1 - P)).
* immediate_or_cancel: the rest is canceled. fill_or_kill: all or nothing. good_till_canceled: the rest rests.
* post_only that would cross is rejected (HTTP 400), like Kalshi.
* reduce_only caps the size at the position it reduces.
* A RESTING order is assumed to be at the BACK of the queue at its price: it fills only when the real book
  trades THROUGH it (the real best bid rises strictly above our resting ask, or the real best ask falls strictly
  below our resting bid), for the size shown at those better levels, at our own limit price, as a maker (fee 0).
  A level merely touching our price never fills us. Resting orders are re-checked whenever the client polls
  (order status, order lists, positions) or cancels.
Positions and orders live in memory; a restart in shadow mode starts from an empty simulated account.

Settlement: GET /markets/{ticker} answers the market's `status` and `result` (the real public market's for Kalshi; for
another venue, `result_fn(ticker)` -> {status, result} replaces both, e.g. Polymarket's resolution). The first time a
market reads as determined (status determined | finalized | settled with result yes | no) the simulated account
settles it like the venue: resting orders in it are cancelled, its position leaves `positions` and `cash` receives $1
a contract on the winning side (no fee); the row goes to `settlements`. A market without a result settles nothing.

MAKER MODEL (KTD11; opt-in: pass `trades_fn`, as the Polymarket shadow does with its WebSocket prints). The rules
above stay for marketable orders; resting orders instead fill from Kalshi's public trade prints:
* Latency. A create is answered after `place_latency_ms` (150) and judged against the book at that moment: the
  order goes live then, post_only is checked then (a cross is a 400, as Kalshi answers it), and a marketable order
  takes the book as it is then. Cancels take effect after `cancel_latency_ms` (150): every print that arrived before
  that still fills the old order. (The request is held for the latency with
  the injected `sleep`, as the real round trip would be.)
* Queue. On going live `queue_ahead` = the size the real book shows at our price on our side. A print at our price that hits our side eats the queue ahead
  first; only what is left of it fills us. Cancels at our level are assumed to come from BEHIND us: queue_ahead is
  never reduced by a smaller displayed size. A print THROUGH our price (a taker selling below our bid, buying above
  our ask) means our whole level was taken: it fills us up to the print's size, whatever the queue. Prints on the
  other side, at better prices than ours, or block trades never fill us. Two of our own orders share a print's size
  (earlier order first); they never both fill from the same contracts.
* Fallback. When `trades_fn` returns None (trade feed down, gapped or not yet covering the period) the old "trades
  through the book" rule above is used and the fill is flagged `fill_model="book"`; the queue of every order on that
  market is reset to the back when prints come back. Trade-driven fills are `fill_model="trades"`; immediate fills
  of a marketable order are `fill_model="taker"`.
* Fees: taker `kalshi_fee`; maker `kalshi_fee(rate=maker_fee_rate)` (default 0, as seen on real KXBTC15M fills).
* Every fill goes to `fills` with the YES mid at the fill (the print's `mid` when the feed gives one, else the book's)
  and moves `cash` (buy YES / buy NO pays, closing receives; fees paid). GET /portfolio/fills serves them in the V2
  Fill shape (the lost-order check and the manual-exit P&L read them, as on Kalshi).

Streaming (adapters/kalshi/stream.py has the real one): every simulated fill and order change is also emitted to
`listeners` as the same `order` / `fill` events the Kalshi WebSocket stream produces (with the maker model, fills
also carry `fill_model` and `mid`), and `ShadowStream` polls the real public book of the subscribed markets (about
once a second) to emit `quote` events and to advance resting orders, so the application runs the identical stream
code path in shadow.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, UTC
from urllib.parse import parse_qs

import httpx
from cryptography.hazmat.primitives.asymmetric import ed25519

from btc_signal_engine.adapters.kalshi.stream import quote_from_top
from btc_signal_engine.adapters.kalshi.trading import PREFIX, KalshiTrading, TradingAuth, normalize_order
from btc_signal_engine.domain.settlement.fees import kalshi_fee

log = logging.getLogger(__name__)

PUBLIC_BASE = "https://api.elections.kalshi.com/trade-api/v2"
LABEL = "shadow (simulated fills on the real order book)"
EPS = 1e-9
ORDER_PATH = "/portfolio/events/orders/"

Book = dict  # {"yes": [(price, size), ...], "no": [(price, size), ...]} - bids on each side
TradesFn = Callable[[str, int], "list[dict] | None"]


def _fmt_c(x: float) -> str:
    return f"{x:.2f}"


def _fmt_p(x: float) -> str:
    return f"{x:.4f}"


def _floor2(x: float) -> float:
    return int(x * 100 + 1e-6) / 100


def _err(status: int, code: str, message: str = "") -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message}})


MARKET_META = ("ticker", "status", "open_time", "close_time", "result")  # all the simulated /markets/{ticker} answers
FINAL_STATUSES = ("determined", "finalized", "settled")


def book_payload(book: Book) -> dict:
    """A Book -> the documented GET /markets/{ticker}/orderbook payload (the inverse of `parse_book`)."""
    side = lambda k: [[_fmt_p(p), _fmt_p(s)] for p, s in book[k]]  # noqa: E731  (sub-cent sizes survive)
    return {"orderbook_fp": {"yes_dollars": side("yes"), "no_dollars": side("no")}}


def parse_book(payload: dict) -> Book:
    ob = payload.get("orderbook_fp") or {}
    side = lambda k: [(float(p), float(s)) for p, s in (ob.get(k) or [])]  # noqa: E731
    return {"yes": side("yes_dollars"), "no": side("no_dollars")}


def book_mid(book: Book) -> float | None:
    """YES mid of a book (None unless both sides have a level)."""
    yb = max((p for p, s in book["yes"] if s > 0), default=None)
    nb = max((p for p, s in book["no"] if s > 0), default=None)
    if yb is None or nb is None:
        return None
    return round((yb + 1 - nb) / 2, 4)


class ShadowExchange(httpx.AsyncBaseTransport):
    def __init__(self, *, book_fn: Callable[[str], Awaitable[Book]] | None = None,
                 market_fn: Callable[[str], Awaitable[dict]] | None = None,
                 public: httpx.AsyncClient | None = None, clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
                 trades_fn: TradesFn | None = None, place_latency_ms: int = 150, cancel_latency_ms: int = 150,
                 maker_fee_rate: float = 0.0, cash: float = 0.0, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 env: str = "shadow", taker_fee_fn: Callable[[float, float], float] | None = None,
                 result_fn: Callable[[str], Awaitable[dict | None]] | None = None):
        """`env` stamps every emitted stream event (another venue's shadow uses its own environment name, e.g.
        "pm_shadow"); `taker_fee_fn(contracts, price)` replaces the Kalshi taker fee (e.g. Polymarket's);
        `result_fn(ticker)` -> {status, result} (or None: not resolved) overrides the market's status and result."""
        self._result_fn = result_fn
        self.settlements: list[dict] = []  # every simulated settlement (ticker, result, position, payout)
        self.env = env
        self._taker_fee = taker_fee_fn or (lambda n, p: kalshi_fee(n, p))
        self._public = public
        self._book_fn = book_fn or self._real_book
        self._market_fn = market_fn or self._real_market
        self._clock = clock_ms
        self._trades_fn = trades_fn
        self.maker_model = trades_fn is not None  # off: the original book-through rule, no latency
        self.place_latency_ms = place_latency_ms if self.maker_model else 0
        self.cancel_latency_ms = cancel_latency_ms if self.maker_model else 0
        self.maker_fee_rate = maker_fee_rate
        self._sleep = sleep
        self.orders: dict[str, dict] = {}
        self.positions: dict[str, float] = {}  # ticker -> net YES contracts (negative = NO)
        self.cash = cash  # simulated account cash: fills and fees move it
        self.fills: list[dict] = []  # every simulated fill (fill_model, mid at fill, fee, cash after)
        self.requests: list[dict] = []  # every request the client sent (method, path, body), for audit and tests
        self.listeners: list[Callable[[dict], None]] = []  # stream consumers (ShadowStream)
        self._cursor: dict[str, int] = {}  # ticker -> prints up to this local time are processed
        self._seen: dict[str, dict] = {}  # ticker -> {print key: ts} already processed (dedupe at the cursor)

    def _emit(self, ev: dict) -> None:
        for fn in list(self.listeners):
            try:
                fn({**ev, "env": self.env})
            except Exception:
                log.warning("shadow stream consumer failed", exc_info=True)

    def _emit_order(self, o: dict) -> None:
        if self.listeners:
            self._emit({"type": "order", "order": normalize_order(self._order_obj(o)), "ts_ms": self._clock()})

    # -- real public data -----------------------------------------------------------------------
    def _client(self) -> httpx.AsyncClient:
        if self._public is None:
            self._public = httpx.AsyncClient(base_url=PUBLIC_BASE, timeout=5.0)
        return self._public

    async def _real_book(self, ticker: str) -> Book:
        r = await self._client().get(f"/markets/{ticker}/orderbook")
        r.raise_for_status()
        return parse_book(r.json())

    async def _real_market(self, ticker: str) -> dict:
        """Metadata only: the real /markets/{ticker} lags the book, so its prices are dropped (prices come from
        the book: the simulated /orderbook path)."""
        r = await self._client().get(f"/markets/{ticker}")
        r.raise_for_status()
        m = r.json()["market"]
        return {k: m.get(k) for k in MARKET_META}

    # -- matching -------------------------------------------------------------------------------
    @staticmethod
    def _levels(book: Book, book_side: str) -> list[tuple[float, float]]:
        """Opposite-side levels a `book_side` order would take, best first (YES prices)."""
        if book_side == "bid":  # buying YES takes YES asks = 1 - NO bids, cheapest first
            return sorted(((round(1 - p, 4), s) for p, s in book["no"]), key=lambda x: x[0])
        return sorted(book["yes"], key=lambda x: -x[0])  # selling YES hits YES bids, highest first

    @staticmethod
    def _better(book_side: str, level: float, limit: float, strict: bool = False) -> bool:
        if book_side == "bid":
            return level < limit - EPS if strict else level <= limit + EPS
        return level > limit + EPS if strict else level >= limit - EPS

    @staticmethod
    def _shown(book: Book, book_side: str, price: float) -> float:
        """Size the real book shows at `price` (YES) on our own side: the queue a new order joins behind."""
        if book_side == "bid":
            return sum(s for p, s in book["yes"] if abs(p - price) <= EPS)
        return sum(s for p, s in book["no"] if abs(p - (1 - price)) <= EPS)

    def _cash_move(self, ticker: str, book_side: str, price: float, n: float) -> float:
        """Cash change for a fill at YES `price`, as Kalshi books it: closing the opposite holding receives its
        price, opening buys YES at p or NO at 1 - p."""
        pos = self.positions.get(ticker, 0.0)
        if book_side == "bid":  # buy YES: first closes NO holdings (sells NO at 1 - p)
            close = min(n, max(-pos, 0.0))
            return close * (1 - price) - (n - close) * price
        close = min(n, max(pos, 0.0))  # sell YES: first closes YES holdings
        return close * price - (n - close) * (1 - price)

    def _apply(self, o: dict, fills: list[tuple[float, float]], maker: bool, *, fill_model: str | None = None,
               mid: float | None = None, ts: int | None = None, trade_id: str | None = None) -> None:
        for price, n in fills:
            if n <= 0:
                continue
            fee = (kalshi_fee(n, price, self.maker_fee_rate) if self.maker_fee_rate > 0 else 0.0) if maker \
                else self._taker_fee(n, price)
            at = self._clock() if ts is None else ts
            model = fill_model or ("taker" if not maker else "book")
            tid = trade_id or str(uuid.uuid4())
            if self.listeners:  # the Kalshi `fill` message, normalized like the real stream's
                ev = {"type": "fill", "order_id": o["order_id"], "client_order_id": o["client_order_id"],
                      "ticker": o["ticker"], "trade_id": tid, "count": round(n, 2),
                      "yes_price": round(price, 4), "fee": round(fee, 6),
                      "action": "buy" if o["book_side"] == "bid" else "sell", "side": "yes",
                      "is_taker": not maker, "post_position": None, "ts_ms": self._clock()}
                if self.maker_model:
                    ev.update(fill_model=model, mid=mid)
                self._emit(ev)
            self.cash = round(self.cash + self._cash_move(o["ticker"], o["book_side"], price, n) - fee, 6)
            outcome_price = price if o["book_side"] == "bid" else round(1 - price, 4)
            o["_filled"] = round(o["_filled"] + n, 2)
            o["_remaining"] = round(o["_remaining"] - n, 2)
            o["_cost_" + ("maker" if maker else "taker")] += outcome_price * n
            o["_yes_cost"] += price * n
            o["_fees_" + ("maker" if maker else "taker")] += fee
            self.positions[o["ticker"]] = round(self.positions.get(o["ticker"], 0.0)
                                                + (n if o["book_side"] == "bid" else -n), 2)
            self.fills.append({"order_id": o["order_id"], "client_order_id": o["client_order_id"],
                               "ticker": o["ticker"], "trade_id": tid, "book_side": o["book_side"],
                               "yes_price": round(price, 4), "count": round(n, 2), "fee": round(fee, 6),
                               "is_taker": not maker, "fill_model": model, "mid": mid, "ts": at,
                               "position_after": self.positions[o["ticker"]], "cash_after": self.cash})

    # -- resting orders -------------------------------------------------------------------------
    async def advance(self, ticker: str | None = None) -> None:
        """Bring resting orders (of one market, or all) up to now: fills, expiries."""
        await self._advance(ticker)

    async def _advance(self, ticker: str | None = None) -> None:
        if self.maker_model:
            await self._advance_maker(ticker)
            return
        resting = [o for o in self.orders.values() if o["status"] == "resting" and ticker in (None, o["ticker"])]
        books: dict[str, Book] = {}
        for o in resting:
            if o["ticker"] not in books:
                books[o["ticker"]] = await self._book_fn(o["ticker"])
            if o["status"] != "resting":  # changed while the book was fetched (a cancel or another advance)
                continue
            if self._expire(o, self._clock()):
                continue
            self._book_through(o, books[o["ticker"]])

    def _book_through(self, o: dict, book: Book) -> None:
        """The original rule: fill only for the size the real book shows strictly through our price."""
        through = [(lvl, s) for lvl, s in self._levels(book, o["book_side"])
                   if self._better(o["book_side"], lvl, o["price"], strict=True)]
        avail = _floor2(sum(s for _, s in through))
        n = min(o["_remaining"], avail)
        if n > 0:
            self._apply(o, [(o["price"], n)], maker=True, fill_model="book", mid=book_mid(book))
            if o["_remaining"] <= 0 and o["status"] == "resting":
                o["status"] = "executed"
            self._emit_order(o)

    def _expire(self, o: dict, now: int) -> bool:
        exp = o.get("_exp_ms")
        if exp is None or now < exp:
            return False
        o["_remaining"], o["status"] = 0.0, "canceled"
        self._emit_order(o)
        return True

    async def _advance_maker(self, ticker: str | None) -> None:
        tickers = sorted({o["ticker"] for o in self.orders.values()
                          if o["status"] == "resting" and ticker in (None, o["ticker"])})
        for t in tickers:
            book = await self._book_fn(t)
            now = self._clock()
            resting = [o for o in self.orders.values() if o["status"] == "resting" and o["ticker"] == t]
            if not resting:
                continue
            cursor = self._cursor.get(t, now)
            prints = self._trades_fn(t, cursor - 1)  # -1: prints in the cursor's own millisecond (deduped below)
            if prints is None:  # no trustworthy trade feed for this period: the book rule, flagged
                for o in resting:
                    if o["status"] == "resting" and not self._expire(o, now):
                        self._book_through(o, book)
                        o["_qreset"] = True
                self._cursor[t], self._seen[t] = now, {}
                continue
            for o in resting:
                if o.get("_qreset"):  # back from the fallback: back of the queue, prints from now on only
                    o["_qreset"] = False
                    o["_queue"], o["_from"] = self._shown(book, o["book_side"], o["price"]), now
            seen = self._seen.setdefault(t, {})
            mid_now = book_mid(book)
            for i, tr in enumerate(sorted(prints, key=lambda x: x["ts_ms"])):
                key = tr.get("trade_id") or (tr["ts_ms"], tr.get("yes_price"), tr.get("count"),
                                             tr.get("taker_book_side"), i)
                if key in seen or tr["ts_ms"] > now:
                    continue
                seen[key] = tr["ts_ms"]
                self._print(t, tr, mid_now)
                self._cursor[t] = max(self._cursor.get(t, tr["ts_ms"]), tr["ts_ms"])
            self._cursor.setdefault(t, cursor)
            for k in [k for k, ts in seen.items() if ts < self._cursor[t] - 1]:
                del seen[k]
            for o in resting:
                if o["status"] == "resting":
                    self._expire(o, now)

    def _print(self, ticker: str, tr: dict, mid_now: float | None) -> None:
        """One public print against our resting orders on `ticker`, in time priority."""
        if tr.get("is_block_trade") or not tr.get("count") or tr.get("yes_price") is None:
            return
        p, size, used = float(tr["yes_price"]), float(tr["count"]), 0.0
        for o in sorted((o for o in self.orders.values() if o["status"] == "resting" and o["ticker"] == ticker),
                        key=lambda o: (o["_from"], o["_seq"])):
            if tr["ts_ms"] <= o["_from"] or (o.get("_exp_ms") is not None and tr["ts_ms"] > o["_exp_ms"]):
                continue
            hits_us = "ask" if o["book_side"] == "bid" else "bid"  # the taker side that trades with our side
            if tr.get("taker_book_side") != hits_us:
                continue
            at = abs(p - o["price"]) <= EPS
            through = self._better(o["book_side"], p, o["price"], strict=True)
            if through:  # our whole level was taken; we get up to the print's size
                o["_queue"] = 0.0
                n = min(o["_remaining"], size - used)
            elif at:
                eat = min(o["_queue"], size)
                o["_queue"] = round(o["_queue"] - eat, 2)
                n = min(o["_remaining"], size - eat - used)
            else:
                continue
            n = _floor2(max(n, 0.0))
            if n <= 0:
                continue
            used += n
            self._apply(o, [(o["price"], n)], maker=True, fill_model="trades",
                        mid=tr.get("mid") if tr.get("mid") is not None else mid_now, ts=tr["ts_ms"],
                        trade_id=tr.get("trade_id"))
            if o["_remaining"] <= 0 and o["status"] == "resting":
                o["status"] = "executed"
            self._emit_order(o)

    def _order_obj(self, o: dict) -> dict:
        return {"order_id": o["order_id"], "user_id": "shadow", "client_order_id": o["client_order_id"],
                "ticker": o["ticker"], "outcome_side": "yes" if o["book_side"] == "bid" else "no",
                "book_side": o["book_side"], "type": "limit", "status": o["status"],
                "yes_price_dollars": _fmt_p(o["price"]), "no_price_dollars": _fmt_p(1 - o["price"]),
                "fill_count_fp": _fmt_c(o["_filled"]), "remaining_count_fp": _fmt_c(o["_remaining"]),
                "initial_count_fp": _fmt_c(o["_initial"]),
                "taker_fees_dollars": _fmt_p(o["_fees_taker"]), "maker_fees_dollars": _fmt_p(o["_fees_maker"]),
                "taker_fill_cost_dollars": _fmt_p(o["_cost_taker"]),
                "maker_fill_cost_dollars": _fmt_p(o["_cost_maker"]), "exchange_index": 0}

    def _take(self, book: Book, book_side: str, price: float, qty: float) -> list[tuple[float, float]]:
        """Walk the opposite side of `book` up to `qty` through levels at or better than `price`."""
        fills, left = [], qty
        for lvl, s in self._levels(book, book_side):
            if left <= 0 or not self._better(book_side, lvl, price):
                break
            n = min(left, _floor2(s))
            fills.append((lvl, round(n, 2)))
            left = round(left - n, 2)
        return fills

    def _go_live(self, o: dict, book: Book) -> None:
        now = self._clock()
        if not any(x["status"] == "resting" and x["ticker"] == o["ticker"] for x in self.orders.values()):
            self._cursor[o["ticker"]], self._seen[o["ticker"]] = now, {}  # nothing older can matter
        o["_from"], o["_queue"] = now, self._shown(book, o["book_side"], o["price"])

    async def _create(self, body: dict) -> httpx.Response:
        if self.place_latency_ms:
            await self._sleep(self.place_latency_ms / 1000)  # the order reaches the book this much later
        coid = body.get("client_order_id")
        if coid and any(o["client_order_id"] == coid for o in self.orders.values()):
            return httpx.Response(409, json={"error": {"code": "order_already_exists"}})
        ticker, book_side, tif = body["ticker"], body["side"], body["time_in_force"]
        price, count = float(body["price"]), float(body["count"])
        exp = body.get("expiration_time")
        if exp is not None and tif != "good_till_canceled":
            return _err(400, "invalid_order", "expiration_time needs good_till_canceled")
        book = await self._book_fn(ticker)
        levels = [(lvl, s) for lvl, s in self._levels(book, book_side) if self._better(book_side, lvl, price)]
        if body.get("reduce_only") and tif != "immediate_or_cancel":
            return httpx.Response(400, json={"error": {"code": "invalid_order", "message": "reduce_only needs IOC"}})
        if body.get("post_only") and levels:
            return httpx.Response(400, json={"error": {"code": "post_only_cross", "message": "order would cross"}})
        qty = count
        if body.get("reduce_only"):
            pos = self.positions.get(ticker, 0.0)
            qty = min(count, max(-pos, 0.0) if book_side == "bid" else max(pos, 0.0))
        fills = self._take(book, book_side, price, qty)
        filled = round(sum(n for _, n in fills), 2)
        if tif == "fill_or_kill" and filled < count - EPS:
            fills, filled = [], 0.0
        oid = str(uuid.uuid4())
        o = {"order_id": oid, "client_order_id": coid or "", "ticker": ticker, "book_side": book_side, "price": price,
             "_initial": count, "_filled": 0.0, "_remaining": count, "_cost_taker": 0.0, "_cost_maker": 0.0, "_yes_cost": 0.0,
             "_fees_taker": 0.0, "_fees_maker": 0.0, "status": "resting",
             "_post_only": bool(body.get("post_only")), "_seq": len(self.orders),
             "_exp_ms": int(exp) * 1000 if exp else None}
        self._apply(o, fills, maker=False, mid=book_mid(book))
        if tif == "good_till_canceled" and o["_remaining"] > 0 and not body.get("reduce_only"):
            o["status"] = "resting"
            self._go_live(o, book)
        else:
            o["_remaining"] = 0.0
            o["status"] = "executed" if o["_filled"] > 0 else "canceled"
        self.orders[oid] = o
        self._emit_order(o)
        out = {"order_id": oid, "client_order_id": coid, "fill_count": _fmt_c(o["_filled"]),
               "remaining_count": _fmt_c(o["_remaining"]), "ts_ms": self._clock()}
        if o["_filled"] > 0:
            out["average_fill_price"] = _fmt_p(o["_yes_cost"] / o["_filled"])
            out["average_fee_paid"] = f"{o['_fees_taker'] / o['_filled']:.6f}"  # responses emit up to 6 dp
        return httpx.Response(201, json=out)

    async def _resting_after_latency(self, oid: str) -> dict | None:
        """Wait the cancel latency, let every print up to then hit the order, return it if still resting."""
        if self.cancel_latency_ms:
            await self._sleep(self.cancel_latency_ms / 1000)
        o = self.orders.get(oid)
        if o:
            await self._advance(o["ticker"])
        return o if o and o["status"] == "resting" else None

    def _cancel_now(self, o: dict) -> dict:
        reduced, o["_remaining"], o["status"] = o["_remaining"], 0.0, "canceled"
        self._emit_order(o)
        return {"order_id": o["order_id"], "client_order_id": o["client_order_id"],
                "reduced_by": _fmt_c(reduced), "ts_ms": self._clock()}

    async def _cancel(self, oid: str) -> httpx.Response:
        o = await self._resting_after_latency(oid)
        if o is None:
            return httpx.Response(404, json={"error": {"code": "not_found"}})
        return httpx.Response(200, json=self._cancel_now(o))

    # -- transport --------------------------------------------------------------------------------
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if not path.startswith(PREFIX):
            return httpx.Response(404)
        path = path[len(PREFIX):]
        body = json.loads(request.content) if request.content else None
        self.requests.append({"method": request.method, "path": path, "body": body})
        q = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        m = request.method
        if m == "POST" and path == "/portfolio/events/orders":
            return await self._create(body)
        if m == "DELETE" and path.startswith(ORDER_PATH):
            return await self._cancel(path.rsplit("/", 1)[1])
        if m == "GET" and path.startswith("/portfolio/orders/"):
            o = self.orders.get(path.rsplit("/", 1)[1])
            if not o:
                return httpx.Response(404, json={"error": {"code": "not_found"}})
            await self._advance(o["ticker"])
            return httpx.Response(200, json={"order": self._order_obj(o)})
        if m == "GET" and path == "/portfolio/orders":
            await self._advance(q.get("ticker"))
            rows = [self._order_obj(o) for o in self.orders.values()
                    if q.get("ticker") in (None, o["ticker"]) and q.get("status") in (None, o["status"])]
            return httpx.Response(200, json={"orders": rows, "cursor": ""})
        if m == "GET" and path == "/portfolio/positions":
            await self._advance()
            rows = [{"ticker": t, "position_fp": _fmt_c(p)} for t, p in self.positions.items() if abs(p) > EPS]
            return httpx.Response(200, json={"market_positions": rows, "cursor": ""})
        if m == "GET" and path == "/portfolio/fills":  # the simulated account's own fills, V2 Fill shape
            since = int(q["min_ts"]) * 1000 if q.get("min_ts") else None
            rows = [{"fill_id": f["trade_id"], "trade_id": f["trade_id"], "order_id": f["order_id"],
                     "ticker": f["ticker"], "market_ticker": f["ticker"], "book_side": f["book_side"],
                     "count_fp": _fmt_c(f["count"]), "yes_price_dollars": _fmt_p(f["yes_price"]),
                     "fee_cost": f"{f['fee']:.6f}", "is_taker": f["is_taker"],
                     "created_time": datetime.fromtimestamp(f["ts"] / 1000, tz=UTC).isoformat()}
                    for f in self.fills if q.get("ticker") in (None, f["ticker"]) and (since is None or f["ts"] >= since)]
            return httpx.Response(200, json={"fills": rows, "cursor": ""})
        if m == "GET" and path.startswith("/markets/") and path.endswith("/orderbook"):
            return httpx.Response(200, json=book_payload(await self._book_fn(path.split("/")[2])))
        if m == "GET" and path.startswith("/markets/"):
            ticker = path.rsplit("/", 1)[1]
            mk = {k: v for k, v in (await self._market_fn(ticker)).items() if k in MARKET_META}  # no (stale) prices
            if self._result_fn is not None:
                res = await self._result_fn(ticker) or {}
                if res.get("result") in ("yes", "no"):
                    mk.update(status=res.get("status"), result=res["result"])
                else:
                    mk["result"] = None
            if mk.get("result") in ("yes", "no") and mk.get("status") in FINAL_STATUSES:
                self.settle(ticker, mk["result"])
            return httpx.Response(200, json={"market": {k: mk.get(k) for k in MARKET_META}})
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    def settle(self, ticker: str, result: str) -> dict | None:
        """Settle `ticker` at `result` ("yes" | "no") once, like the venue: cancel its resting orders, pay $1 a
        contract to the winning side, drop the position. Returns the settlement row (None if already settled)."""
        if result not in ("yes", "no") or any(s["ticker"] == ticker for s in self.settlements):
            return None
        for o in self.orders.values():
            if o["ticker"] == ticker and o["status"] == "resting":
                self._cancel_now(o)
        pos = self.positions.pop(ticker, 0.0)
        payout = round(max(pos, 0.0) if result == "yes" else max(-pos, 0.0), 2)
        self.cash = round(self.cash + payout, 6)
        row = {"ticker": ticker, "result": result, "position": pos, "payout": payout, "ts": self._clock(),
               "cash_after": self.cash}
        self.settlements.append(row)
        return row

    async def aclose(self) -> None:
        if self._public is not None:
            await self._public.aclose()


def book_quote(ticker: str, book: Book, ts_ms: int) -> dict:
    """Top of a real public book as a stream quote (YES ask = 1 - best NO bid)."""
    yb = max((p for p, s in book["yes"] if s > 0), default=None)
    nb = max((p for p, s in book["no"] if s > 0), default=None)
    return quote_from_top(ticker, yb, None if nb is None else 1 - nb, ts_ms)


class ShadowStream:
    """The `TradingStream` for shadow: quotes from polling the real public book, order/fill events from the
    simulated exchange. Same event shapes as `KalshiStream`."""

    def __init__(self, exchange: ShadowExchange, on_event: Callable[[dict], None], *,
                 clock_ms: Callable[[], int] = lambda: int(time.time() * 1000), poll_s: float = 1.0,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.env = exchange.env
        self._ex = exchange
        self._emit_cb = on_event
        self._clock = clock_ms
        self._poll_s = poll_s
        self._sleep = sleep
        self._want: set[str] = set()
        self._closed = False
        self._running = False
        self._failing = False
        self._last_msg_ms: int | None = None
        self._last_health: tuple = (False, True)  # emit only real changes

    def __repr__(self) -> str:
        return "ShadowStream()"

    def subscribe_tickers(self, tickers: Iterable[str]) -> None:
        self._want = {t for t in tickers if t}

    def health(self) -> dict:
        return {"connected": self._running, "last_msg_ms": self._last_msg_ms,
                "stale": (not self._running) or self._failing}

    def close(self) -> None:
        self._closed = True

    def _emit(self, ev: dict) -> None:
        try:
            self._emit_cb({**ev, "env": self.env})
        except Exception:
            log.warning("shadow stream consumer failed", exc_info=True)

    def _health_changed(self) -> None:
        h = self.health()
        if (h["connected"], h["stale"]) != self._last_health:
            self._last_health = (h["connected"], h["stale"])
            self._emit({"type": "health", **h})

    async def poll_once(self) -> None:
        failing = False
        for t in sorted(self._want):
            try:
                book = await self._ex._book_fn(t)
                self._emit({"type": "quote", **book_quote(t, book, self._clock())})
                await self._ex._advance(t)  # resting orders fill against the real book; events via listeners
            except Exception as exc:
                failing = True
                log.warning("shadow book poll for %s failed (%s)", t, type(exc).__name__)
        self._failing = failing
        self._last_msg_ms = self._clock()
        self._health_changed()

    async def run(self) -> None:
        self._ex.listeners.append(self._emit_cb)
        self._running = True
        self._emit({"type": "resync", "reason": "connected"})
        try:
            while not self._closed:
                await self.poll_once()
                await self._sleep(self._poll_s)
        finally:
            self._running = False
            if self._emit_cb in self._ex.listeners:
                self._ex.listeners.remove(self._emit_cb)
            self._health_changed()


def shadow_trading(*, max_order_cost: float, exchange: ShadowExchange | None = None,
                   series: str | None = None) -> KalshiTrading:
    """The production trading client wired to the simulated exchange (throwaway in-memory signing key). `series`
    replaces the KXBTC15M guard for another venue's markets simulated on this exchange (e.g. Polymarket slugs)."""
    auth = TradingAuth("shadow", ed25519.Ed25519PrivateKey.generate())
    kw = {"series": series} if series else {}
    return KalshiTrading(auth, env="shadow", transport=exchange or ShadowExchange(), max_order_cost=max_order_cost,
                         **kw)
