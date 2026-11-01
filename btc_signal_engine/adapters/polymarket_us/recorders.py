"""The Polymarket US research recorders (read-only market data; no orders), both writing the research store:
`<research dir>/polymarket_us.db` (SQLite), or the `research` schema of the app's Postgres database when
BTCFLOW_DATABASE_URL is set (docs/plans/2026-10-31-0900-feat-postgres-plan.md G3; same tables and columns,
adapters/sqlite/research_tables.py). Moved from scripts/record_polymarket_us_ws.py and scripts/record_polymarket_us.py
(2026-10-31, docs/plans/2026-10-31-0830-ops-portable-runtime-plan.md P1) with the same tables, columns and behaviour.

- Quotes (`QuoteRecorder`, `run_quotes`): the authenticated markets WebSocket (official `polymarket_us` SDK).
  MARKET_DATA_LITE (best bid / ask, depth, last) and TRADE for the current and next BTC Up/Down windows (5m, 15m, 1h),
  one connection per window length (the server caps subscriptions per connection), re-subscribing at every window
  boundary. Every message is written:
    pm_ws_quotes(recv_ms, span, slug, bid, ask, bid_depth, ask_depth, last)
    pm_trades(recv_ms, trade_ms, span, slug, price, qty, maker_side, raw)
  A lane reconnects 5 s after any error. The public REST BBO is a ~30 s cached snapshot, too stale to record.
  A live connection does not mean quotes are flowing (2026-10-31 13:59-14:45 CDT: the server stopped every book
  message while trades kept coming, and the recorder sat on its open sockets recording nothing for 45 minutes;
  docs/solutions/2026-10-31-pm-quiet-book-vs-dead-book-feed.md). So each lane watches its own connection:
    * trades arriving with no quote (MARKET_DATA_LITE) since for TRADES_NO_BOOK_S (60 s), or no quote at all for
      NO_BOOK_S (120 s) while trades arrived in that time, is BOOK SILENCE: a WARNING naming the lane and the
      reason, and the lane reconnects (fresh socket, fresh subscriptions);
    * VENUE_OUTAGE_AFTER (3) book silences in a row with no quote on any of those connections is a venue-side book
      outage: said once at WARNING, the lane keeps reconnecting with a doubling wait (up to BOOK_SILENT_BACKOFF_MAX_S)
      and never spins (each try lasts at least TRADES_NO_BOOK_S). Trades are still recorded meanwhile, so the app's
      `data` alert can tell "the venue sends trades but no quotes" from "the recorder is down"
      (application/recorder_health.py compares the newest pm_trades and pm_ws_quotes rows);
    * no quote recorded by any lane for STALL_EXIT_S (10 min) despite reconnects, while no lane is in a venue
      outage, raises `RecorderStalled`: the process exits non-zero (75) and its restart policy starts it again
      (plain Docker does not restart an unhealthy container; the healthcheck alone changes nothing). In a venue
      outage it stays up: a restart cannot bring the venue's book back, and the trades keep being recorded.
- Settlement (`run_settle`): once a window has closed, its terms via the public SDK client
  (`markets.retrieve_by_slug`: assetPriceTerms.priceToBeat / settlementPrice) into
    pm_windows(slug, span, window_start, window_end, price_to_beat, settlement_price, result, raw, fetched_ms)
  (CF Benchmarks BRTI 60 s average, as Kalshi; flat = Up). `pm_quotes` is created for old readers, never written.

A database outage never costs a WebSocket: a failed write keeps its rows for the next flush (each buffer capped at
MAX_BUFFERED rows, the oldest dropped and counted, said at WARNING), the lane keeps its connection, and the socket is
always closed first when a lane ends, whatever the last flush does. Writes run in a worker thread, so a slow database
never stalls the lanes' event loop. Both recorders call `beat` from their main loop (the process-local liveness the
container healthcheck reads; bootstrap/heartbeat.py).

Both stop cleanly when `stop()` turns true (a signal or the supervisor's stop file; bootstrap/recorders.py): the
quotes recorder flushes what it holds and closes its sockets. The SDK client is passed in as a factory, so tests run
with a fake one; nothing here reads keys or prints them.
"""

from __future__ import annotations

import asyncio
import collections
import datetime as dt
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy import Engine, insert
from sqlalchemy.dialects import postgresql, sqlite

from btc_signal_engine.adapters.polymarket_us.markets import num, slug_for as _slug_for_ms, span_of
from btc_signal_engine.adapters.sqlite.research import create_research_tables, research_write_engine
from btc_signal_engine.adapters.sqlite.research_tables import (
    QUOTES_TABLES,
    SETTLE_TABLES,
    pm_trades,
    pm_windows,
    pm_ws_quotes,
)
from btc_signal_engine.adapters.sqlite.urls import DbTarget

log = logging.getLogger(__name__)

DB_NAME = "polymarket_us.db"
SPANS = {"5m": 300, "15m": 900, "1h": 3600}
RECONNECT_S = 5.0
TRADES_NO_BOOK_S = 60  # trades but no quote for this long on one connection: book silence (reconnect)
NO_BOOK_S = 120  # no quote at all for this long while trades arrived: book silence
VENUE_OUTAGE_AFTER = 3  # book silences in a row without a single quote: the venue's book feed is down
BOOK_SILENT_BACKOFF_MAX_S = 120.0  # the longest wait between reconnects during a book silence
STALL_EXIT_S = 600  # no quote from any lane for this long, and no venue outage: exit non-zero (restart)
EX_STALLED = 75  # sysexits.h EX_TEMPFAIL: the supervisor and Docker's restart policy start it again
SETTLE_AFTER_S = 30  # settles within about a minute of the close
SETTLE_GIVE_UP_S = 1800  # give up on a window 30 minutes after its close
MAX_BUFFERED = 200_000  # rows per buffer held through a database outage (quotes: hours of them), oldest dropped
FLUSH_WARN_EVERY_S = 60.0  # a failing flush is logged when it starts failing, then at most once a minute

# The SQLite DDL the recorder scripts used (research_tables.py has the same tables for both stores). Kept so tests can
# build a file exactly as the scripts did and old readers can see what they wrote.
QUOTES_SCHEMA = """
CREATE TABLE IF NOT EXISTS pm_ws_quotes (
  recv_ms INTEGER NOT NULL, span TEXT, slug TEXT NOT NULL, bid REAL, ask REAL, bid_depth REAL, ask_depth REAL, last REAL);
CREATE INDEX IF NOT EXISTS ix_pm_ws_quotes_slug_ts ON pm_ws_quotes (slug, recv_ms);
CREATE TABLE IF NOT EXISTS pm_trades (
  recv_ms INTEGER NOT NULL, trade_ms INTEGER, span TEXT, slug TEXT NOT NULL, price REAL, qty REAL, maker_side TEXT,
  raw TEXT);
CREATE INDEX IF NOT EXISTS ix_pm_trades_slug_ts ON pm_trades (slug, trade_ms);
"""

SETTLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS pm_quotes (
  ts_ms INTEGER NOT NULL, span TEXT NOT NULL, slug TEXT NOT NULL, window_start INTEGER NOT NULL,
  bid REAL, ask REAL, bid_depth REAL, ask_depth REAL, last REAL, shares_traded REAL,
  PRIMARY KEY (slug, ts_ms));
CREATE TABLE IF NOT EXISTS pm_windows (
  slug TEXT PRIMARY KEY, span TEXT NOT NULL, window_start INTEGER NOT NULL, window_end INTEGER NOT NULL,
  price_to_beat REAL, settlement_price REAL, result TEXT, raw TEXT, fetched_ms INTEGER);
"""


def slug_for(span: str, start: int) -> str:
    """The `span` market whose window starts at `start` (epoch seconds)."""
    return _slug_for_ms(start * 1000, span)


def wanted(now: int, spans=None) -> list[str]:
    """The current and next window's market of each span (`now` in epoch seconds)."""
    out = []
    for span, sec in SPANS.items():
        if spans is not None and span not in spans:
            continue
        st = now // sec * sec
        out += [slug_for(span, st), slug_for(span, st + sec)]
    return out


def open_db(target: DbTarget, tables) -> Engine:
    """The recorder's engine on `target` (a SQLite file or the Postgres URL), its tables created where missing."""
    engine = research_write_engine(target)
    create_research_tables(engine, tables)
    return engine


def _dicts(table, rows: list[tuple]) -> list[dict]:
    names = [c.name for c in table.columns]
    return [dict(zip(names, r, strict=True)) for r in rows]


class BookSilence(RuntimeError):
    """A lane's connection is alive but sends no quotes (see the module docstring); the lane reconnects."""

    def __init__(self, reason: str, books: int) -> None:
        super().__init__(reason)
        self.books = books  # quotes this connection did receive before going silent


class RecorderStalled(RuntimeError):
    """No quote recorded for STALL_EXIT_S despite reconnects, and no venue outage: the process should exit."""


class QuoteRecorder:
    """Buffers WebSocket messages as rows; `flush` / `flush_async` write and commit them (once a second from each
    lane). Each buffer holds at most `max_buffered` rows: past that the oldest is dropped and counted (`dropped`).
    `last_quote_ms`: when the newest quote was received (any lane), None before the first."""

    def __init__(self, db: Engine, now_ms: Callable[[], int] = lambda: int(time.time() * 1000), *,
                 max_buffered: int = MAX_BUFFERED) -> None:
        self.db = db
        self.now_ms = now_ms
        self.max_buffered = max_buffered
        self.q: collections.deque[tuple] = collections.deque()
        self.t: collections.deque[tuple] = collections.deque()
        self.last_quote_ms: int | None = None
        self.dropped = 0
        self._failing_since: float | None = None
        self._warned = 0.0

    def _add(self, buf: collections.deque, row: tuple) -> None:
        buf.append(row)
        while len(buf) > self.max_buffered:
            buf.popleft()
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 10_000 == 0:
                log.warning("quotes recorder: buffer full (%d rows) while the database is not taking writes; "
                            "dropping the oldest (%d dropped so far)", self.max_buffered, self.dropped)

    def on_lite(self, d: dict) -> None:
        m = d.get("marketDataLite") or {}
        slug = m.get("marketSlug")
        if slug:
            self.last_quote_ms = self.now_ms()
            self._add(self.q, (self.now_ms(), span_of(slug), slug, num(m.get("bestBid")), num(m.get("bestAsk")),
                               num(m.get("bidDepth")), num(m.get("askDepth")), num(m.get("lastTradePx"))))

    def on_trade(self, d: dict) -> None:
        t = d.get("trade") or {}
        slug = t.get("marketSlug")
        if not slug:
            return
        tm = t.get("tradeTime")
        try:
            trade_ms = int(dt.datetime.fromisoformat(tm[:26].rstrip("Z") + "+00:00").timestamp() * 1000) if tm else None
        except ValueError:
            trade_ms = None
        self._add(self.t, (self.now_ms(), trade_ms, span_of(slug), slug, num(t.get("price")), num(t.get("quantity")),
                           (t.get("maker") or {}).get("side"), json.dumps(t)[:2000]))

    def _take(self) -> tuple[list[tuple], list[tuple]]:
        q, t = list(self.q), list(self.t)
        self.q.clear()
        self.t.clear()
        return q, t

    def _write(self, q: list[tuple], t: list[tuple]) -> None:
        with self.db.begin() as c:  # one transaction for both tables
            if q:
                c.execute(insert(pm_ws_quotes), _dicts(pm_ws_quotes, q))
            if t:
                c.execute(insert(pm_trades), _dicts(pm_trades, t))

    def _put_back(self, q: list[tuple], t: list[tuple], exc: BaseException) -> None:
        """A failed write: its rows go back in front of what arrived meanwhile (still capped)."""
        for buf, rows in ((self.q, q), (self.t, t)):
            newer = list(buf)
            buf.clear()
            for row in rows + newer:
                self._add(buf, row)
        now = time.monotonic()
        if self._failing_since is None or now - self._warned >= FLUSH_WARN_EVERY_S:
            first = self._failing_since is None
            self._failing_since = self._failing_since if not first else now
            self._warned = now
            log.warning("quotes recorder: writing %d quotes and %d trades failed (%s: %s); kept for the next flush "
                        "(%s)", len(q), len(t), type(exc).__name__, str(exc).splitlines()[0][:200] if str(exc) else "",
                        "the database is not taking writes" if first else
                        f"failing for {now - self._failing_since:.0f} s")

    def _written(self) -> None:
        if self._failing_since is not None:
            log.warning("quotes recorder: writes work again after %.0f s", time.monotonic() - self._failing_since)
            self._failing_since = None

    def flush(self) -> None:
        """Write what is buffered now, in this thread; raises (and keeps the rows) when the write fails."""
        q, t = self._take()
        if not (q or t):
            return
        try:
            self._write(q, t)
        except Exception as exc:
            self._put_back(q, t, exc)
            raise
        self._written()

    async def flush_async(self) -> bool:
        """The lanes' flush: the buffers are taken on the loop, written in a worker thread, and put back (logged,
        never raised) when the write fails. True when everything taken was written."""
        q, t = self._take()
        if not (q or t):
            return True
        try:
            await asyncio.to_thread(self._write, q, t)
        except Exception as exc:
            self._put_back(q, t, exc)
            return False
        self._written()
        return True


def _is_server_message(e: Any) -> bool:
    from polymarket_us.errors import WebSocketError
    return isinstance(e, WebSocketError)


def book_silence(seen: dict, now_ms: int) -> str | None:
    """Why this connection counts as book-silent now (module docstring), or None. `seen`: {"started_ms", "books",
    "book_ms" (newest quote), "unbooked_ms" (first trade since then), "trade_ms" (newest trade)}."""
    if seen["unbooked_ms"] is not None and now_ms - seen["unbooked_ms"] > TRADES_NO_BOOK_S * 1000:
        return (f"trades arriving but no quotes for {(now_ms - seen['unbooked_ms']) / 1000:.0f} s "
                f"(limit {TRADES_NO_BOOK_S} s)")
    last_book = seen["book_ms"] if seen["book_ms"] is not None else seen["started_ms"]
    if (now_ms - last_book > NO_BOOK_S * 1000 and seen["trade_ms"] is not None
            and now_ms - seen["trade_ms"] <= NO_BOOK_S * 1000):
        return f"no quotes for {(now_ms - last_book) / 1000:.0f} s while trades arrived (limit {NO_BOOK_S} s)"
    return None


async def run_lane_once(rec: QuoteRecorder, client: Any, spans: set[str], *, stop: Callable[[], bool],
                        now_s: Callable[[], float] = time.time, tick_s: float = 1.0,
                        server_message: Callable[[Any], bool] = _is_server_message,
                        seen: dict | None = None) -> None:
    """One markets-WebSocket connection for `spans` until an error (raised), book silence (`BookSilence`, module
    docstring) or `stop()` (returns). `seen` (optional, filled in): what this connection received."""
    ms = lambda: int(now_s() * 1000)  # noqa: E731
    seen = {} if seen is None else seen
    seen.update(started_ms=ms(), books=0, trades=0, book_ms=None, unbooked_ms=None, trade_ms=None)

    def on_lite(d: dict) -> None:
        rec.on_lite(d)
        seen["books"] += 1
        seen["book_ms"], seen["unbooked_ms"] = ms(), None

    def on_trade(d: dict) -> None:
        rec.on_trade(d)
        t = ms()
        seen["trades"] += 1
        seen["trade_ms"] = t
        if seen["unbooked_ms"] is None:
            seen["unbooked_ms"] = t

    ws = client.ws.markets()
    errors: list[str] = []
    ws.on("market_data_lite", on_lite)
    ws.on("trade", on_trade)

    def on_error(e) -> None:  # a server error message is not a dead socket
        if server_message(e):
            log.warning("server message: %s", str(e)[:200])
        else:
            errors.append(str(e)[:200])

    ws.on("error", on_error)
    await ws.connect()
    subs: dict[str, list[str]] = {}  # one subscription per market: the server rejects duplicates in a list
    n = 0
    try:
        while not stop():
            want = set(wanted(int(now_s()), spans))
            # Drop finished windows FIRST: the server caps subscriptions per connection ("max subscriptions per
            # connection reached" at 10:15 when old + new were briefly held together). A ~1 s gap is fine here.
            for slug in sorted(set(subs) - want):
                for rid in subs.pop(slug):
                    try:
                        await ws.unsubscribe(rid)
                    except Exception:  # the socket's own error ends the connection; a refused unsubscribe is harmless
                        log.debug("pm-quotes: unsubscribe %s failed", rid, exc_info=True)
            for slug in sorted(want - set(subs)):
                n += 1
                await ws.subscribe(f"p{n}", "SUBSCRIPTION_TYPE_MARKET_DATA_LITE", [slug])
                await ws.subscribe(f"t{n}", "SUBSCRIPTION_TYPE_TRADE", [slug])
                subs[slug] = [f"p{n}", f"t{n}"]
                log.info("subscribed %s", slug)
            await asyncio.sleep(tick_s)
            await rec.flush_async()  # a database outage keeps the rows and this socket
            if errors:
                raise RuntimeError(errors[-1])
            why = book_silence(seen, ms())
            if why is not None:
                raise BookSilence(why, seen["books"])
    finally:
        try:
            await ws.close()  # first, and whatever the flush below does: never a leaked socket
        finally:
            await rec.flush_async()


async def run_quotes(db_path: DbTarget, client_factory: Callable[[], Any], *, stop: Callable[[], bool],
                     spans: tuple[str, ...] = tuple(SPANS), reconnect_s: float = RECONNECT_S, tick_s: float = 1.0,
                     now_s: Callable[[], float] = time.time,
                     server_message: Callable[[Any], bool] = _is_server_message,
                     stall_exit_s: float = STALL_EXIT_S, venue_outage_after: int = VENUE_OUTAGE_AFTER,
                     state: dict | None = None, beat: Callable[[], None] | None = None,
                     max_buffered: int = MAX_BUFFERED) -> None:
    """Every span's lane until `stop()`; each lane reconnects `reconnect_s` after any failure (longer, doubling,
    during a book silence). Raises `RecorderStalled` when no quote was recorded for `stall_exit_s` and no lane is in
    a venue outage (module docstring). `state` (optional, filled in): per lane {silences, venue_outage, reason}.
    `beat` (the process-local liveness) is called every tick from the watchdog."""
    db = open_db(db_path, QUOTES_TABLES)
    ms = lambda: int(now_s() * 1000)  # noqa: E731
    rec = QuoteRecorder(db, ms, max_buffered=max_buffered)
    state = {} if state is None else state
    started = ms()

    def venue_outage(st: dict) -> bool:
        """The lane declared a venue-side book outage and its current connection has still had no quote."""
        return bool(st.get("venue_outage")) and not st.get("seen", {}).get("books")

    async def wait(seconds: float) -> None:
        waited = 0.0
        while waited < seconds and not stop():
            await asyncio.sleep(min(tick_s, seconds - waited) if tick_s > 0 else 0)
            waited += tick_s if tick_s > 0 else seconds

    async def lane(span: str) -> None:
        # One connection per window length: the server caps subscriptions per connection, and 6 markets x 2 types
        # on one connection exceeded it ("max subscriptions per connection reached", 2026-10-29).
        st = state.setdefault(span, {"silences": 0, "venue_outage": False, "reason": None, "seen": {}})
        while not stop():
            seen: dict = {}
            st["seen"] = seen  # the live connection's counts: a quote on it ends a venue outage (venue_outage())
            delay = reconnect_s
            try:
                await run_lane_once(rec, client_factory(), {span}, stop=stop, now_s=now_s, tick_s=tick_s,
                                    server_message=server_message, seen=seen)
            except BookSilence as exc:
                # A connection that had quotes before going silent starts a new run; one that never got any extends it.
                st["silences"] = st["silences"] + 1 if exc.books == 0 else 1
                st["reason"] = str(exc)
                delay = min(reconnect_s * 2 ** (st["silences"] - 1), max(reconnect_s, BOOK_SILENT_BACKOFF_MAX_S))
                if st["silences"] >= venue_outage_after and not st["venue_outage"]:
                    st["venue_outage"] = True
                    log.warning("%s lane: still no quotes after %d reconnects while trades keep arriving: a "
                                "Polymarket-side book outage; trades are still recorded, retrying every %.0f s at "
                                "most", span, st["silences"], max(reconnect_s, BOOK_SILENT_BACKOFF_MAX_S))
                log.warning("%s lane: book silence on a live connection (%s); reconnecting in %.0f s (%d in a row)",
                            span, exc, delay, st["silences"])
                silenced = True
            except Exception as exc:  # reconnect after any failure
                log.warning("%s websocket stopped (%s: %s); reconnecting in %.0f s", span, type(exc).__name__,
                            str(exc)[:200], reconnect_s)
                silenced = False
            else:
                break  # stop() was asked
            if seen.get("books") and not silenced:  # that connection recorded quotes and ended for another reason
                if st["venue_outage"]:
                    log.warning("%s lane: quotes are flowing again", span)
                st.update(silences=0, venue_outage=False, reason=None)
            await wait(delay)

    async def watchdog() -> None:  # cancelled once every lane has stopped
        while True:
            if beat is not None:
                beat()
            await asyncio.sleep(tick_s)
            quiet_s = (ms() - (rec.last_quote_ms if rec.last_quote_ms is not None else started)) / 1000
            if quiet_s > stall_exit_s and not any(venue_outage(st) for st in state.values()):
                log.error("quotes recorder: no quote recorded for %.0f s despite reconnects, and no lane shows a "
                          "venue-side book outage: exiting so the restart policy starts a fresh process", quiet_s)
                raise RecorderStalled(f"no quote recorded for {quiet_s:.0f} s")

    lanes = [asyncio.ensure_future(lane(span)) for span in spans]
    dog = asyncio.ensure_future(watchdog())
    try:
        done, _ = await asyncio.wait([dog, *lanes], return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            t.result()  # RecorderStalled (or a lane's unexpected error) propagates
        await asyncio.gather(*lanes)
    finally:
        for t in (dog, *lanes):
            t.cancel()
        for t in (dog, *lanes):
            try:
                await t
            except asyncio.CancelledError:  # cancelled just above: the expected end
                pass
            except Exception:  # already reported above
                log.debug("pm-quotes: a task ended with an error at stop", exc_info=True)
        try:
            rec.flush()
        except Exception:  # already logged; what is left is lost with the process
            log.warning("quotes recorder: %d quotes and %d trades could not be written at the stop", len(rec.q),
                        len(rec.t))
        db.dispose()
        log.info("quotes recorder stopped")


def settle(client: Any, span: str, start: int, now_ms: int, not_found: type[BaseException]) -> tuple | None:
    """The pm_windows row of the `span` window starting at `start` (epoch s), or None while it is not settled."""
    slug = slug_for(span, start)
    try:
        body = client.markets.retrieve_by_slug(slug)
    except not_found:
        return None
    m = body.get("market", body) if isinstance(body, dict) else {}
    t = m.get("assetPriceTerms") or {}
    ptb, sp = num(t.get("priceToBeat")), num(t.get("settlementPrice"))
    if sp is None:
        return None  # not settled yet
    result = "up" if ptb is not None and sp >= ptb else ("down" if ptb is not None else None)
    return (slug, span, start, start + SPANS[span], ptb, sp, result, json.dumps(t)[:4000], now_ms)


def _replace_window(db: Engine, row: tuple) -> None:
    """Insert or replace a pm_windows row (by slug), on either store."""
    ins = (postgresql.insert if db.dialect.name == "postgresql" else sqlite.insert)(pm_windows)
    vals = _dicts(pm_windows, [row])[0]
    stmt = ins.values(vals).on_conflict_do_update(
        index_elements=["slug"], set_={k: ins.excluded[k] for k in vals if k != "slug"})
    with db.begin() as c:
        c.execute(stmt)


def settle_tick(db: Engine, client: Any, pending: set[tuple[str, int]], now: int,
                not_found: type[BaseException], fetched_ms: int | None = None) -> None:
    """One pass: queue each span's just-closed window, store every settled one, give up after 30 minutes."""
    for span, sec in SPANS.items():
        pending.add((span, now // sec * sec - sec))  # the window that just closed
    for span, start in list(pending):
        if now < start + SPANS[span] + SETTLE_AFTER_S:
            continue
        s = settle(client, span, start, fetched_ms if fetched_ms is not None else int(time.time() * 1000), not_found)
        if s:
            _replace_window(db, s)
            pending.discard((span, start))
        elif now > start + SPANS[span] + SETTLE_GIVE_UP_S:
            pending.discard((span, start))


def run_settle(db_path: DbTarget, client: Any, *, every_s: float, wait: Callable[[float], bool],
               now_s: Callable[[], float] = time.time, not_found: type[BaseException] | None = None,
               beat: Callable[[], None] | None = None) -> None:
    """Settlement passes every `every_s` until `wait(every_s)` returns True (a stop was requested); `beat` (the
    process-local liveness) is called once per pass."""
    if not_found is None:
        from polymarket_us.errors import PolymarketUSError
        not_found = PolymarketUSError
    db = open_db(db_path, SETTLE_TABLES)
    pending: set[tuple[str, int]] = set()
    try:
        while True:
            if beat is not None:
                beat()
            t = now_s()
            settle_tick(db, client, pending, int(t), not_found, int(t * 1000))
            if wait(every_s):
                break
    finally:
        db.dispose()
        log.info("settlement recorder stopped")
