"""Polymarket US live market data from the authenticated markets WebSocket (official SDK: `client.ws.markets()`).

Why WebSocket only: the public REST BBO (`/v1/markets/{slug}/bbo`) is a ~30 s cached snapshot (it changed on ~3% of
1-second samples; a penny-test IOC at the REST ask did not fill, at the WebSocket ask + 1c it filled at once,
2026-10-29). So nothing here ever prices from REST: when a market's cached book is not SERVABLE (below), `quote`
answers None and `book` raises `StaleMarketData` naming the condition that failed, and every order path refuses.

Servable (docs/solutions/2026-10-31-pm-quiet-book-vs-dead-book-feed.md). The server sends a book / top message only
when the book changes: on a quiet market 2-18 s between messages is normal (live 2026-10-31), so "the last message is
at most 2 s old" alone called valid quiet books stale (~600 trading-feed flips in 4 h). And on 2026-10-31 13:59-14:45
CDT the server stopped sending every book message (MARKET_DATA and MARKET_DATA_LITE) while TRADE messages kept coming
on the same live connections, so "the connection is alive" alone does not mean "the book is current". A market's
cached book or top (each record on its own) is servable when it is from a connection that is up and (stream-managed)
still carries the market's subscription, and either
  (a) it arrived within MAX_AGE_MS (2 s), or
  (b) the QUIET path, all of: the connection is alive (any message on it, trades and heartbeats included, within
      CONN_LIVE_MS, 15 s); the market's TRADE subscription on that connection is confirmed and has covered it since
      before the record arrived (so a print after the record could not have been missed); NO trade for the market has
      arrived after the record (a print moves or consumes the book, so a book older than a print is outdated; a print
      without a following book update within TRADE_GRACE_MS, 2 s, is reported as a broken book feed); and the record
      is at most QUIET_MAX_MS (30 s) old.
A trade after the record refuses the quiet path at once (not only after the grace): the book is then known to be
older than the market. Within 2 s of the record, (a) still serves it, exactly as before this rule.

`PolymarketMarketData` is the cache (pure state, fed message by message):
* `marketData` (SUBSCRIPTION_TYPE_MARKET_DATA): the market's full book, bids and offers at YES prices; each message is
  taken as a complete snapshot of the book (the SDK documents it as "full order book").
* `marketDataLite` (SUBSCRIPTION_TYPE_MARKET_DATA_LITE, ~10 msg/s per market, measured p99.9 gap 0.8 s): bestBid,
  bestAsk and (when sent) bidDepth / askDepth.
* `trade` (SUBSCRIPTION_TYPE_TRADE): public prints (YES price, quantity, taker intent).
The freshest of the full book and the lite top gives the quote; the full book (or, failing that, the lite top with
its depth) gives the levels a shadow exchange walks. Book format for `ShadowExchange` (Kalshi shape): {"yes": YES bids,
"no": NO bids}, a NO bid at 1 - p for every YES offer at p.

`trades_since(slug, ts)` follows `BookCache`: the prints that arrived after local time `ts`, or None when the cache
cannot vouch it saw every print since then (not subscribed to that market's trades since then, a disconnect, or
the connection silent for more than CONN_LIVE_MS). Taker side: BUY_LONG / SELL_SHORT take YES offers ("bid" in Kalshi
terms), SELL_LONG / BUY_SHORT hit YES bids ("ask").

`PolymarketMarketStream` runs a POOL of markets WebSockets. The server allows at most `MAX_SUBS_PER_CONN` (10)
subscriptions per connection and refuses the rest ("max subscriptions per connection reached"; not documented, measured
live 2026-10-29: after 15m + 5m windows were added, 4 markets x 3 types = 12 subscriptions on one socket got exactly 2
refusals, and every roll after that was refused in part). So each connection carries at most `MARKETS_PER_CONN` (3)
markets (9 subscriptions), counting a market it is still dropping, so the cap holds during a roll too: the pool
subscribes MARKET_DATA + MARKET_DATA_LITE + TRADE for the current and next windows of every live span plus any market a
trading service watches, puts each new market on a connection with room (opening another when none has room), drops
markets no longer wanted (the next window was subscribed a whole window ahead, so a roll never leaves a gap), and
closes a connection once it carries nothing. Each connection reconnects on its own with exponential backoff (1 s
doubling to 30 s, full jitter so the pool's sockets never reconnect in lockstep, reset after a minute connected;
adapters/retry.py) and only its own markets go stale meanwhile.

A refused subscription is never left looking live: a server error naming one of our request ids marks that market
unsubscribed on its connection (quote None, trades_since None, listed under health()["unsubscribed"]), its other
subscriptions are dropped and it is retried with backoff (1 s doubling to 30 s), wherever there is room. A "max
subscriptions" refusal also lowers the per-connection cap, and one we cannot attribute to a request rebuilds that
connection under the lower cap (its subscription state is unknown). Trade coverage for a market starts only once its
first book or top arrives on that connection (a market silent for `CONFIRM_MS` is logged once); that confirmation is
also what lets a quiet book be served (it proves the TRADE subscription that would reveal an outdated book is live).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any

from btc_signal_engine.adapters.polymarket_us.markets import current_slugs, is_series, num, window_of
from btc_signal_engine.adapters.retry import Backoff
from btc_signal_engine.domain.timestamps import iso_to_ms_or_none
from btc_signal_engine.domain.book_freshness import (CONN_LIVE_MS, QUIET_MAX_MS,  # noqa: F401 - TRADE_GRACE_MS: re-exported
                                                    TRADE_GRACE_MS, unservable_reason)

log = logging.getLogger(__name__)

# Freshness (the module docstring, "Servable"). Every limit is on the cache's own (wall) clock.
MAX_AGE_MS = 2_000  # a book / top this recent is servable outright (the rule before 2026-10-31)
# QUIET_MAX_MS (30 s: the REST BBO's own staleness, no better than it), CONN_LIVE_MS (15 s) and TRADE_GRACE_MS (2 s)
# come from domain/book_freshness.py, the rule shared with the Kalshi perps book.
SUB_TYPES = ("SUBSCRIPTION_TYPE_MARKET_DATA", "SUBSCRIPTION_TYPE_MARKET_DATA_LITE", "SUBSCRIPTION_TYPE_TRADE")
MAX_SUBS_PER_CONN = 10  # server limit per markets WebSocket (measured live 2026-10-29, see the module docstring)
MARKETS_PER_CONN = MAX_SUBS_PER_CONN // len(SUB_TYPES)  # 3 markets = 9 subscriptions, one spare
CONFIRM_MS = 5_000  # a new subscription must send a book or top within this, or it is treated as refused
MAIN = "main"  # the connection id when the cache is fed directly (no stream pool)
_TAKER_BOOK_SIDE = {"ORDER_INTENT_BUY_LONG": "bid", "ORDER_INTENT_SELL_SHORT": "bid",
                    "ORDER_INTENT_SELL_LONG": "ask", "ORDER_INTENT_BUY_SHORT": "ask"}


class StaleMarketData(RuntimeError):
    """No Polymarket market data servable to price from (never falls back to the cached REST BBO); the message says
    which condition failed."""


def _px(x: float | None) -> float | None:
    return round(x, 4) if x is not None and 0.0 < x < 1.0 else None


def _iso_ms(s: Any) -> int | None:
    """A Polymarket time (UTC; "Z" may be missing) -> epoch ms, None when absent or unreadable (domain/timestamps.py)."""
    return iso_to_ms_or_none(s, naive="utc")


def _levels(rows: Any) -> list[tuple[float, float]]:
    out = []
    for lv in rows or ():
        p, q = num((lv or {}).get("px")), num((lv or {}).get("qty"))
        if p is not None and q is not None and 0.0 < p < 1.0 and q > 0:
            out.append((round(p, 4), q))
    return out


class PolymarketMarketData:
    def __init__(self, *, clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
                 max_age_ms: int = MAX_AGE_MS, keep_ms: int = 120_000) -> None:
        self._clock = clock_ms
        self.max_age_ms = max_age_ms
        self._keep = keep_ms
        self._conns: dict[str, dict] = {}  # connection id -> {"up": bool, "last_msg_ms": int | None}
        # Stream-managed mode (a PolymarketMarketStream feeds this cache): a market's data counts only while it is
        # subscribed (not refused) on the connection it arrived on. Fed directly (tests, tools), the connection's
        # up / down state alone decides.
        self.managed = False
        self._subscribed: dict[str, str] = {}  # slug -> connection id carrying its subscriptions
        self._wanted: set[str] = set()  # slugs the stream wants live (health lists the ones not subscribed)
        self._book: dict[str, dict] = {}  # slug -> {"bids", "offers", "ms", "seq", "state", "conn"}
        self._lite: dict[str, dict] = {}  # slug -> {"bid", "ask", "bid_depth", "ask_depth", "ms", "seq", "conn"}
        self._trades: dict[str, deque] = {}
        self._msgs = 0  # every message gets the next number: "a print after this book" is exact, not by timestamp
        self._last_trade: dict[str, tuple[int, int]] = {}  # slug -> (message number, local ms) of its newest print
        self._unbooked: dict[str, tuple[int, int]] = {}  # slug -> its first print since its last book / top message
        self._covered: dict[str, tuple[int, str]] = {}  # slug -> (every print since this local time is kept, conn)
        self._pruned: dict[str, int] = {}
        self._seq = 0
        self.listeners: list[Callable[[str], None]] = []  # called with the slug whenever its top may have changed

    # -- connection state (set by the stream runner) ---------------------------------------------
    @property
    def connected(self) -> bool:
        """Every connection is up (and there is at least one)."""
        return bool(self._conns) and all(c["up"] for c in self._conns.values())

    @property
    def last_msg_ms(self) -> int | None:
        """The oldest connection's last message (None while any connection has had none)."""
        lasts = [c["last_msg_ms"] for c in self._conns.values()]
        return None if not lasts or None in lasts else min(lasts)

    def set_connected(self, on: bool, conn: str = MAIN) -> None:
        c = self._conns.setdefault(conn, {"up": False, "last_msg_ms": None})
        c["up"] = bool(on)
        if not on:
            self._conn_lost(conn)

    def drop_conn(self, conn: str) -> None:
        """A pool connection closed for good (it carries no market any more)."""
        self._conn_lost(conn)
        self._conns.pop(conn, None)

    def _conn_lost(self, conn: str) -> None:
        for s in [s for s, (_, c) in self._covered.items() if c == conn]:
            del self._covered[s]
        for s in [s for s, c in self._subscribed.items() if c == conn]:
            del self._subscribed[s]

    def set_wanted(self, slugs: Iterable[str]) -> None:
        self._wanted = set(slugs)

    def mark_subscribed(self, slug: str, conn: str) -> None:
        """`slug`'s subscriptions were sent on `conn` (and not refused): its data from `conn` counts."""
        self._subscribed[slug] = conn

    def mark_unsubscribed(self, slug: str, conn: str) -> None:
        """`slug` is no longer subscribed on `conn` (dropped or refused): unless another connection has taken it over,
        its quote reads None and its trades are not vouched for."""
        if self._subscribed.get(slug) == conn:
            del self._subscribed[slug]
        if slug in self._covered and self._covered[slug][1] == conn:
            del self._covered[slug]

    def cover_trades(self, slug: str, conn: str, since_ms: int | None = None) -> None:
        """`slug`'s TRADE subscription on `conn` is live (confirmed): coverage starts at `since_ms` (when the
        subscription was sent: every print on the socket since then was kept) or now (kept if already running there)."""
        if slug not in self._covered or self._covered[slug][1] != conn:
            self._covered[slug] = (self._clock() if since_ms is None else since_ms, conn)

    def data_since(self, slug: str, conn: str, since_ms: int) -> bool:
        """A book or top for `slug` arrived on `conn` at or after `since_ms` (the subscription is flowing)."""
        return any(r is not None and r["conn"] == conn and r["ms"] >= since_ms
                   for r in (self._book.get(slug), self._lite.get(slug)))

    def trades_subscribed(self, slugs: Iterable[str], conn: str = MAIN) -> None:
        """The TRADE subscription on `conn` now covers exactly `slugs` (coverage starts now for new ones)."""
        live = set(slugs)
        for s in [s for s, (_, c) in self._covered.items() if c == conn and s not in live]:
            del self._covered[s]
        for s in live:
            self.cover_trades(s, conn)

    def _conn_alive(self, conn: str | None, now: int) -> bool:
        """Up, and some message (any type) arrived on it within CONN_LIVE_MS."""
        c = self._conns.get(conn) if conn is not None else None
        return (c is not None and c["up"] and c["last_msg_ms"] is not None
                and now - c["last_msg_ms"] <= CONN_LIVE_MS)

    def health(self) -> dict:
        """The feed health right now (no hysteresis: the trading stream adds that, adapters/polymarket_us/stream.py).

        Stream-managed, it judges only the markets the app needs NOW (`needed_now`): wanted markets whose window
        has opened, i.e. the current windows plus markets a trading service holds. HARD problems (`connected` False,
        listed under `hard`): one of them is not subscribed (refused, retrying, no room yet) or its connection is
        down. SOFT problems: an open window's book is not servable (the module docstring; the reason is listed: a
        silent connection, a quiet book over QUIET_MAX_MS, prints without book updates...; a window in its first
        QUIET_MAX_MS with no book yet is not counted, though it is never priced). A quiet book that is still
        servable is not a problem: the server sends a book only when it changes (live 2026-10-31: 2-18 s gaps). A
        held market whose window has closed may legitimately go quiet: only its subscription and connection count.
        Next windows never count: their connection can be silent until they open (live 2026-10-29 20:46: a pool
        socket carrying only next windows made the whole feed "stale" and blocked every Polymarket entry). `stale` =
        any problem. `last_msg_ms` is the oldest last message among the connections carrying open windows. Fed
        directly (no stream), every connection counts (stale when one is down or silent for CONN_LIVE_MS).
        `unsubscribed`: wanted markets with no live subscription (their quotes read None). `connections`: per
        connection, up / last message / silent (no message within CONN_LIVE_MS) / markets."""
        now = self._clock()
        conns = {cid: {"connected": c["up"], "last_msg_ms": c["last_msg_ms"], "stale": not self._conn_alive(cid, now),
                       "markets": sorted(s for s, x in self._subscribed.items() if x == cid)}
                 for cid, c in self._conns.items()}
        if not self.managed:
            stale = not conns or any(c["stale"] for c in conns.values())
            return {"connected": self.connected, "last_msg_ms": self.last_msg_ms, "stale": stale,
                    "hard": [] if self.connected else ["a connection is down"], "unsubscribed": [],
                    "connections": conns}
        needed, open_now = self.needed_now(now)
        hard, soft = [], []
        carrying_open: set[str] = set()
        for s in sorted(needed):
            cid = self._subscribed.get(s)
            c = self._conns.get(cid) if cid is not None else None
            if c is None:
                hard.append(f"{s}: not subscribed")
            elif not c["up"]:
                hard.append(f"{s}: {cid} down")
            elif s in open_now:
                carrying_open.add(cid)
                if self._book.get(s) is None and self._lite.get(s) is None and now - window_of(s)[0] < QUIET_MAX_MS:
                    continue  # a window that has just opened may not have sent its first book yet (no quote meanwhile)
                why = self.unservable_reason(s, now)
                if why is not None:
                    soft.append(f"{s}: {why}")
        lasts = [self._conns[cid]["last_msg_ms"] for cid in carrying_open]
        last = None if not lasts or None in lasts else min(lasts)
        return {"connected": bool(needed) and not hard, "last_msg_ms": last,
                "stale": not needed or bool(hard or soft), "problems": hard + soft, "hard": hard,
                "unsubscribed": sorted(self._wanted - set(self._subscribed)), "connections": conns}

    def needed_now(self, now: int) -> tuple[set[str], set[str]]:
        """(wanted markets whose window has opened, the subset still open): what entries and exits depend on now."""
        needed, open_now = set(), set()
        for s in self._wanted:
            w = window_of(s)
            if w is not None and w[0] <= now:
                needed.add(s)
                if now < w[1]:
                    open_now.add(s)
        return needed, open_now

    # -- messages ------------------------------------------------------------------------------------
    def on_message(self, msg: dict, conn: str = MAIN) -> None:
        """One decoded markets-WebSocket message (any type) received on connection `conn`."""
        now = self._clock()
        c = self._conns.setdefault(conn, {"up": False, "last_msg_ms": None})
        if c["last_msg_ms"] is not None and now - c["last_msg_ms"] > CONN_LIVE_MS:
            for s, (_, x) in list(self._covered.items()):  # this connection was not alive: prints may have been missed
                if x == conn:
                    self._covered[s] = (now, conn)
        c["last_msg_ms"] = now
        self._msgs += 1
        seq = self._msgs
        if not isinstance(msg, dict):
            return
        if isinstance(msg.get("marketData"), dict):
            m = msg["marketData"]
            slug = m.get("marketSlug")
            if is_series(slug):
                self._book[slug] = {"bids": sorted(_levels(m.get("bids")), key=lambda x: -x[0]),
                                    "offers": sorted(_levels(m.get("offers")), key=lambda x: x[0]),
                                    "ms": now, "seq": seq, "state": m.get("state"), "conn": conn}
                self._unbooked.pop(slug, None)
                self._notify(slug)
        elif isinstance(msg.get("marketDataLite"), dict):
            m = msg["marketDataLite"]
            slug = m.get("marketSlug")
            if is_series(slug):
                self._lite[slug] = {"bid": _px(num(m.get("bestBid"))), "ask": _px(num(m.get("bestAsk"))),
                                    "bid_depth": num(m.get("bidDepth")), "ask_depth": num(m.get("askDepth")),
                                    "ms": now, "seq": seq, "conn": conn}
                self._unbooked.pop(slug, None)
                self._notify(slug)
        elif isinstance(msg.get("trade"), dict):
            self._on_trade(msg["trade"], now, seq)
        self._prune(now)

    def _notify(self, slug: str) -> None:
        for fn in list(self.listeners):
            try:
                fn(slug)
            except Exception:
                log.warning("polymarket market-data listener failed", exc_info=True)

    def _on_trade(self, t: dict, now: int, seq: int) -> None:
        slug = t.get("marketSlug")
        price, qty = num(t.get("price")), num(t.get("quantity"))
        if not is_series(slug) or price is None or not qty:
            return
        self._seq += 1
        q = self._trades.setdefault(slug, deque())
        q.append({"trade_id": f"pm{self._seq}", "ticker": slug, "yes_price": float(price), "count": float(qty),
                  "taker_book_side": _TAKER_BOOK_SIDE.get((t.get("taker") or {}).get("intent")), "ts_ms": now,
                  "exchange_ts_ms": _iso_ms(t.get("tradeTime")), "mid": self._mid(slug, now)})  # the book just before
        self._last_trade[slug] = (seq, now)  # from here on the cached book is older than a print (module docstring)
        self._unbooked.setdefault(slug, (seq, now))

    def _prune(self, now: int) -> None:
        for slug, q in self._trades.items():
            while q and q[0]["ts_ms"] < now - self._keep:
                self._pruned[slug] = q.popleft()["ts_ms"]
        if self.managed:  # a new slug every 5 and 15 minutes: forget each one once it is over and unsubscribed
            for slug in {*self._trades, *self._pruned, *self._covered, *self._last_trade} - set(self._subscribed):
                w = window_of(slug)
                if w is not None and w[1] <= now:
                    for d in (self._trades, self._pruned, self._covered, self._last_trade, self._unbooked):
                        d.pop(slug, None)
        if len(self._last_trade) > 64:  # fed directly (no stream): bounded all the same
            for s in sorted(self._last_trade, key=lambda s: self._last_trade[s][1])[:-64]:
                del self._last_trade[s]
                self._unbooked.pop(s, None)
        if len(self._book) > 16:
            for s in sorted(self._book, key=lambda s: self._book[s]["ms"])[:-16]:
                del self._book[s]
        if len(self._lite) > 16:
            for s in sorted(self._lite, key=lambda s: self._lite[s]["ms"])[:-16]:
                del self._lite[s]

    # -- reads ------------------------------------------------------------------------------------
    def _why_not(self, slug: str, rec: dict | None, now: int, what: str = "book or top") -> str | None:
        """None when this cached book / top is servable (the module docstring, "Servable"), else why not."""
        if rec is None:
            return f"no {what} received"
        cid = rec["conn"]
        c = self._conns.get(cid)
        if c is None or not c["up"]:
            return f"its connection {cid} is down"
        if self.managed and self._subscribed.get(slug) != cid:
            return f"not subscribed on its connection {cid}"
        lt = self._last_trade.get(slug)
        first = self._unbooked.get(slug)
        cov = self._covered.get(slug)
        return unservable_reason(  # the shared rule (domain/book_freshness.py), also the perps book's
            now_ms=now, book_ms=rec["ms"], book_seq=rec["seq"], conn_last_ms=c["last_msg_ms"],
            last_trade_seq=None if lt is None else lt[0], unbooked_ms=None if first is None else first[1],
            covered_since_ms=cov[0] if cov is not None and cov[1] == cid else None, what=what,
            conn=f"connection {cid}", sub_on=cid, max_age_ms=self.max_age_ms)

    def _servable(self, slug: str, rec: dict | None, now: int) -> bool:
        return self._why_not(slug, rec, now) is None

    def unservable_reason(self, slug: str, now: int | None = None, *, depth: bool = False) -> str | None:
        """None when `slug` has a servable book or top (quote answers; with `depth`, a top counts only when it carries
        depth, as for `book`), else why not (both records' reasons)."""
        now = self._clock() if now is None else now
        book = self._why_not(slug, self._book.get(slug), now, "full book")
        lite = self._lite.get(slug)
        top = self._why_not(slug, lite, now, "top")
        if top is None and depth and (lite["bid_depth"] is None or lite["ask_depth"] is None):
            top = "the top carries no depth"
        if book is None or top is None:
            return None
        return f"full book: {book}; top: {top}"

    def top(self, slug: str, now: int | None = None) -> tuple[float | None, float | None, int] | None:
        """(YES bid, YES ask, local ms of the data) from the freshest servable source, or None when none is."""
        now = self._clock() if now is None else now
        b, lite = self._book.get(slug), self._lite.get(slug)
        cands = []
        if self._servable(slug, b, now):
            cands.append((b["ms"], _px(b["bids"][0][0]) if b["bids"] else None,
                          _px(b["offers"][0][0]) if b["offers"] else None))
        if self._servable(slug, lite, now):
            cands.append((lite["ms"], lite["bid"], lite["ask"]))
        if not cands:
            return None
        ms, bid, ask = max(cands, key=lambda c: c[0])
        return bid, ask, ms

    def _mid(self, slug: str, now: int) -> float | None:
        t = self.top(slug, now)
        if not t or t[0] is None or t[1] is None:
            return None
        return round((t[0] + t[1]) / 2, 4)

    def quote(self, slug: str, now: int | None = None) -> dict | None:
        """The TradingService quote dict (YES and NO sides, market status and window), or None when not servable.
        `servable` True says this cache judged it (Play and the agents take that verdict instead of an age limit of
        their own); `basis` is "fresh" (within MAX_AGE_MS) or "quiet" (the quiet path)."""
        now = self._clock() if now is None else now
        t = self.top(slug, now)
        w = window_of(slug)
        if t is None or w is None:
            return None
        bid, ask, ms = t
        state = (self._book.get(slug) or {}).get("state")
        status = "open" if (state is None or "OPEN" in str(state).upper()) and w[0] <= now < w[1] else "closed"
        return {"ticker": slug, "yes_bid": bid, "yes_ask": ask, "no_bid": _px(None if ask is None else 1 - ask),
                "no_ask": _px(None if bid is None else 1 - bid), "status": status, "open_ts": w[0],
                "close_ts": w[1], "ts_ms": ms, "age_ms": now - ms, "source": "ws", "servable": True,
                "basis": "fresh" if now - ms <= self.max_age_ms else "quiet"}

    async def book(self, slug: str) -> dict:
        """The live book as {"yes": YES bids, "no": NO bids} (Kalshi shape, for ShadowExchange). Raises
        StaleMarketData, naming the failed condition, when no servable full book or lite top with depth exists."""
        now = self._clock()
        b, lite = self._book.get(slug), self._lite.get(slug)
        if self._servable(slug, b, now):
            return {"yes": list(b["bids"]), "no": [(round(1 - p, 4), q) for p, q in b["offers"]]}
        if self._servable(slug, lite, now) and lite["bid_depth"] is not None and lite["ask_depth"] is not None:
            yes = [(lite["bid"], lite["bid_depth"])] if lite["bid"] is not None and lite["bid_depth"] > 0 else []
            no = ([(round(1 - lite["ask"], 4), lite["ask_depth"])]
                  if lite["ask"] is not None and lite["ask_depth"] > 0 else [])
            return {"yes": yes, "no": no}
        why = self.unservable_reason(slug, now, depth=True)
        raise StaleMarketData(f"no servable Polymarket book for {slug}: {why}")

    async def market(self, slug: str) -> dict:
        """GET /markets/{ticker}-shaped metadata for ShadowExchange (status and window; never prices)."""
        w = window_of(slug)
        if w is None:
            raise StaleMarketData(f"{slug!r} is not a Polymarket BTC 15-minute market")
        now = self._clock()
        iso = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        return {"ticker": slug, "status": "active" if w[0] <= now < w[1] else "closed", "open_time": iso(w[0]),
                "close_time": iso(w[1])}

    def trades_since(self, slug: str, ts: int) -> list[dict] | None:
        now = self._clock()
        cov = self._covered.get(slug)
        if cov is None or cov[0] > ts or not self._conn_alive(cov[1], now):
            return None
        if ts < self._pruned.get(slug, -1):
            return None
        return [dict(x) for x in self._trades.get(slug, ()) if x["ts_ms"] > ts]



def ws_error_is_fatal(e) -> bool:
    """The SDK emits "error" both for a server error MESSAGE on a live socket (`WebSocketError`, e.g. a subscription
    to a window that is not listed yet at a boundary) and when its message loop dies (other exceptions). Only the
    second means the connection is gone; a server message is logged and the socket kept (live 2026-10-29: treating
    every error as fatal dropped the feed for ~1 s at each window boundary)."""
    try:
        from polymarket_us.errors import WebSocketError
    except Exception:  # pragma: no cover - SDK layout changed: be safe and reconnect
        return True
    return not isinstance(e, WebSocketError)


class _Conn:
    """One pooled markets WebSocket: the markets the pool assigned to it and the subscriptions live on it."""

    def __init__(self, cid: str) -> None:
        self.id = cid
        self.target: set[str] = set()  # markets the pool assigned here
        self.subs: dict[str, list[str]] = {}  # market -> request ids sent on the live socket and not refused
        self.rids: dict[str, str] = {}  # request id -> market
        self.pending: dict[str, int] = {}  # market -> local ms its subscriptions were sent (no data seen yet)
        self.quiet: set[str] = set()  # pending markets already reported as sending nothing
        self.unsub: list[str] = []  # request ids to cancel at the next pass (a refused market's other types)
        self.up = False
        self.closing = False
        self.restart: str | None = None  # set to rebuild the socket (its subscription state is unknown)
        self.task: asyncio.Task | None = None
        self.reconnects = 0

    def load(self) -> int:
        """Markets this socket carries or will carry (a market still being dropped counts: the cap holds mid-roll)."""
        return len(self.target | set(self.subs))


class PolymarketMarketStream:
    """Runs the pool of markets WebSockets into a `PolymarketMarketData` (see the module docstring). `ws_factory()`
    returns a fresh SDK `MarketsWebSocket` (`client.ws.markets()`), one per pooled connection; tests pass a fake with
    the same `on/connect/subscribe/unsubscribe/close` surface."""

    def __init__(self, ws_factory: Callable[[], Any], cache: PolymarketMarketData, *,
                 clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
                 sleep: Callable[[float], Any] = asyncio.sleep, loop_s: float = 0.25,
                 backoff_s: tuple[float, float] = (1.0, 30.0), markets_per_conn: int = MARKETS_PER_CONN,
                 confirm_ms: int = CONFIRM_MS) -> None:
        self._factory = ws_factory
        self.cache = cache
        cache.managed = True
        self._clock = clock_ms
        self._sleep = sleep
        self._loop_s = loop_s
        self._backoff = backoff_s
        self._confirm_ms = confirm_ms
        self.per_conn = max(1, markets_per_conn)
        self._extra: dict[str, set[str]] = {}
        self._conns: dict[str, _Conn] = {}
        self._tasks: set[asyncio.Task] = set()
        self._retry: dict[str, tuple[int, Backoff]] = {}  # market -> (not before local ms, its backoff)
        self._closed = False
        self._cid = 0
        self._n = 0
        self.reconnects = 0
        self.refused = 0

    def __repr__(self) -> str:
        return "PolymarketMarketStream()"

    def want(self, name: str, slugs: Iterable[str]) -> None:
        """Markets a consumer (e.g. a trading service) needs besides the current and next windows."""
        self._extra[name] = {s for s in slugs if is_series(s)}

    def wanted(self, now: int) -> list[str]:
        out = set(current_slugs(now))
        for v in self._extra.values():
            out |= v
        return sorted(out)

    def connections(self) -> dict[str, set[str]]:
        """Connection id -> the markets it carries or is assigned (diagnostics, tests)."""
        return {cid: c.target | set(c.subs) for cid, c in self._conns.items()}

    def health(self) -> dict:
        return self.cache.health()

    def close(self) -> None:
        self._closed = True

    # -- the pool -------------------------------------------------------------------------------------
    async def run(self) -> None:
        try:
            while not self._closed:
                self._assign(self._clock())
                await self._sleep(self._loop_s)
        finally:
            for c in self._conns.values():
                c.closing = True
            tasks = list(self._tasks)
            for t in tasks:
                t.cancel()
            for t in tasks:
                try:
                    await t
                except asyncio.CancelledError:  # cancelled just above: the expected end
                    pass
                except Exception:  # already failed: it logged and cleaned up
                    log.debug("polymarket markets task ended with an error at close", exc_info=True)

    def _assign(self, now: int) -> None:
        """Place every wanted market on a connection with room, take unwanted ones off, close empty connections."""
        want = set(self.wanted(now))
        self.cache.set_wanted(want)
        for slug in [s for s in self._retry if s not in want]:
            del self._retry[slug]
        for c in self._conns.values():
            c.target &= want
            while len(c.target) > self.per_conn:  # the cap was lowered after a refusal: move the excess elsewhere
                c.target.discard(max(c.target))
        placed: set[str] = set()
        for c in self._conns.values():
            placed |= c.target
        for slug in sorted(want - placed):
            r = self._retry.get(slug)
            if r is not None and now < r[0]:
                continue  # refused recently: listed as unsubscribed in health until the retry
            self._room().target.add(slug)
        for cid, c in list(self._conns.items()):
            if not c.target and not c.subs:
                c.closing = True
                del self._conns[cid]

    def _room(self) -> _Conn:
        """The fullest connection that still has room (fewest sockets), or a new one."""
        open_ = [c for c in self._conns.values() if not c.closing and c.load() < self.per_conn]
        if open_:
            return max(open_, key=lambda c: (c.load(), c.up, -int(c.id.rsplit("-", 1)[-1])))
        self._cid += 1
        c = _Conn(f"pm-ws-{self._cid}")
        self._conns[c.id] = c
        c.task = asyncio.create_task(self._run_conn(c))
        self._tasks.add(c.task)
        c.task.add_done_callback(self._tasks.discard)
        return c

    # -- one connection ------------------------------------------------------------------------------
    async def _run_conn(self, c: _Conn) -> None:
        backoff = Backoff(*self._backoff)
        try:
            while not (self._closed or c.closing):
                started = self._clock()
                try:
                    await self._conn_once(c)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # reconnect after any failure; the error text never carries key material
                    log.warning("polymarket markets websocket %s stopped (%s: %s); reconnecting within %.0f s",
                                c.id, type(exc).__name__, str(exc)[:120], backoff.ceiling)
                    # A socket that never opened is registered only now (down): opening a new pool socket does not
                    # flag the whole feed stale while it connects, but one that fails does.
                    self.cache.set_connected(False, c.id)
                if self._closed or c.closing:
                    break
                c.reconnects += 1
                self.reconnects += 1
                if self._clock() - started > 60_000:
                    backoff.reset()
                await self._sleep(backoff.next())
        finally:
            self.cache.drop_conn(c.id)

    async def _conn_once(self, c: _Conn) -> None:
        ws = self._factory()
        failed: list[str] = []
        ws.on("message", lambda msg: self.cache.on_message(msg, conn=c.id))

        def on_error(e=None) -> None:
            if e is not None and not ws_error_is_fatal(e):
                self._server_error(c, e)
                return
            failed.append(type(e).__name__ if e is not None else "error")

        ws.on("error", on_error)
        ws.on("close", lambda *a: failed.append("closed"))
        await ws.connect()
        c.up, c.restart = True, None
        self.cache.set_connected(True, c.id)
        try:
            while not (self._closed or c.closing):
                await self._reconcile(c, ws)
                if failed:
                    raise ConnectionError(failed[-1])
                if c.restart:
                    raise ConnectionError(c.restart)
                await self._sleep(self._loop_s)
        finally:
            c.up = False
            c.subs.clear()
            c.rids.clear()
            c.pending.clear()
            c.quiet.clear()
            c.unsub.clear()
            self.cache.set_connected(False, c.id)  # its markets read stale and unsubscribed until re-subscribed
            try:
                await ws.close()
            except Exception:
                log.debug("polymarket markets websocket %s close failed", c.id, exc_info=True)

    async def _reconcile(self, c: _Conn, ws: Any) -> None:
        """Subscribe this socket's new markets (one request per market and type: the server rejects a request naming
        a market already subscribed), drop the ones taken off it (after the adds: no gap), cancel what a refusal left
        behind, and confirm new subscriptions by their first data."""
        now = self._clock()
        for slug in sorted(c.target - set(c.subs)):
            self._n += 1
            rids = [f"pm{t.split('_')[-1].lower()}{self._n}" for t in SUB_TYPES]
            c.subs[slug] = rids
            c.rids.update((r, slug) for r in rids)
            c.pending[slug] = now
            self.cache.mark_subscribed(slug, c.id)
            for rid, typ in zip(rids, SUB_TYPES, strict=True):
                if slug not in c.subs:  # refused while we were still sending (handlers run inline)
                    break
                await ws.subscribe(rid, typ, [slug])
        for slug in sorted(set(c.subs) - c.target):
            rids = c.subs.pop(slug)
            c.pending.pop(slug, None)
            for r in rids:
                c.rids.pop(r, None)
            self.cache.mark_unsubscribed(slug, c.id)
            c.unsub.extend(rids)
        while c.unsub:
            rid = c.unsub.pop(0)
            try:
                await ws.unsubscribe(rid)
            except Exception:
                log.debug("unsubscribe %s failed", rid, exc_info=True)
        for slug, t0 in list(c.pending.items()):
            if self.cache.data_since(slug, c.id, t0):
                del c.pending[slug]
                c.quiet.discard(slug)
                self.cache.cover_trades(slug, c.id, since_ms=t0)  # every print since the subscribe was kept
                self._retry.pop(slug, None)
            elif now - t0 > self._confirm_ms and slug not in c.quiet:
                # Not refused, just silent (e.g. a next window with no book yet): its quote stays None and its
                # trades unvouched until data arrives; re-subscribing would only risk a gap when it opens.
                c.quiet.add(slug)
                log.info("polymarket markets websocket %s: no market data for %s within %d ms of subscribing; "
                            "no quote or trade coverage until it sends", c.id, slug, self._confirm_ms)

    def _server_error(self, c: _Conn, e: Any) -> None:
        """A server error message on a live socket: a refusal of one of our requests marks that market unsubscribed
        and retries it; a "max subscriptions" refusal also lowers the per-connection cap."""
        text = str(e)[:200]
        rid = getattr(e, "request_id", None)
        slug = c.rids.get(rid) if rid else None
        if "max subscriptions" in text.lower():
            live = len(c.subs) - 1  # the refused market (known or not) is not live
            cap = max(1, min(self.per_conn, live))
            if cap < self.per_conn:
                log.warning("polymarket markets websocket %s hit the server's subscription limit with %d markets: "
                            "at most %d markets per connection from now on", c.id, len(c.subs), cap)
                self.per_conn = cap
            if slug is None:  # cannot tell which request was refused: rebuild this socket under the lower cap
                log.warning("polymarket markets websocket %s server message: %s (request %r is not one of ours): "
                            "reconnecting", c.id, text, rid)
                c.restart = "unattributed subscription refusal"
                return
        if slug is None:
            log.warning("polymarket markets websocket %s server message: %s", c.id, text)
            return
        self._refuse(c, slug, text, refused_rid=rid)

    def _refuse(self, c: _Conn, slug: str, why: str, refused_rid: str | None = None) -> None:
        rids = c.subs.pop(slug, None)
        if rids is None:
            return
        c.pending.pop(slug, None)
        for r in rids:
            c.rids.pop(r, None)
        c.unsub.extend(r for r in rids if r != refused_rid)
        c.target.discard(slug)
        self.cache.mark_unsubscribed(slug, c.id)
        backoff = self._retry[slug][1] if slug in self._retry else Backoff(*self._backoff)
        delay = backoff.next()  # full jitter, doubling per refusal (adapters/retry.py)
        self._retry[slug] = (self._clock() + int(delay * 1000), backoff)
        self.refused += 1
        log.warning("polymarket markets websocket %s: subscription for %s refused (%s); marked unsubscribed (no "
                    "quote), retrying in %.0f s", c.id, slug, why, delay)

