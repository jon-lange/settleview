"""The quotes recorder when quotes stop but the connection stays alive (adapters/polymarket_us/recorders.py; live
2026-10-31 13:59-14:45 CDT: every book message stopped, trades kept coming, and the recorder recorded nothing for
45 minutes on its open sockets). Against a fake venue on a simulated clock: book silence is detected and the lane
reconnects, repeated silence becomes a venue outage (backoff, no spin, no exit), a recorder that records nothing for
10 minutes with no venue evidence exits 75, and the app's alert tells "trades but no quotes" apart. No network."""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3

import pytest

from btc_signal_engine.adapters.polymarket_us import recorders as rec
from btc_signal_engine.application.recorder_health import RecorderWatch, change_text
from btc_signal_engine.bootstrap import recorders as entry
from btc_signal_engine.bootstrap.config import Settings

NOW = 1_761_900_000  # 2025-10-31 08:40:00Z, a 5m boundary


class VenueWS:
    def __init__(self, venue: Venue) -> None:
        self.venue = venue
        self.h: dict = {}
        self.subs: dict[str, tuple[str, str]] = {}
        self.closed = False

    def on(self, event, cb):
        self.h[event] = cb

    async def connect(self):
        self.venue.connects += 1

    async def subscribe(self, rid, kind, slugs):
        self.subs[rid] = (kind, slugs[0])
        if kind == "SUBSCRIPTION_TYPE_MARKET_DATA_LITE" and self.venue.books:
            self.h["market_data_lite"](self.venue.lite(slugs[0]))  # the snapshot a new subscription gets

    async def unsubscribe(self, rid):
        self.subs.pop(rid, None)

    async def close(self):
        self.closed = True

    def slugs(self) -> set[str]:
        return {s for _, s in self.subs.values()}


class Venue:
    """The fake SDK client (`client.ws.markets()`) and the market: one simulated second per `tick`, a print on every
    subscribed market each second when `trades`, a quote change too when `books`."""

    def __init__(self, end_s: float, books: bool = True, trades: bool = True) -> None:
        self.t = float(NOW)
        self.end = NOW + end_s
        self.books, self.trades = books, trades
        self.connects = 0
        self.sockets: list[VenueWS] = []
        self.ws = self
        self.at: dict[float, dict] = {}  # simulated second -> changes to apply then

    def markets(self) -> VenueWS:
        ws = VenueWS(self)
        self.sockets.append(ws)
        return ws

    @staticmethod
    def lite(slug):
        return {"marketDataLite": {"marketSlug": slug, "bestBid": "0.41", "bestAsk": "0.43", "bidDepth": 10,
                                   "askDepth": 12}}

    @staticmethod
    def trade(slug):
        return {"trade": {"marketSlug": slug, "tradeTime": "2025-10-31T08:40:01.000Z", "price": "0.42",
                          "quantity": "1", "maker": {"side": "BUY"}}}

    def tick(self) -> None:
        self.t += 1
        for at in [a for a in self.at if a <= self.t]:
            for k, v in self.at.pop(at).items():
                setattr(self, k, v)
        for ws in self.sockets:
            if ws.closed:
                continue
            for slug in sorted(ws.slugs()):
                if self.trades:
                    ws.h["trade"](self.trade(slug))
                if self.books:
                    ws.h["market_data_lite"](self.lite(slug))

    def stop(self) -> bool:
        self.tick()
        return self.t >= self.end


def quotes(db, venue: Venue, **kw):
    state: dict = {}
    asyncio.run(rec.run_quotes(db, lambda: venue, stop=venue.stop, spans=("5m",), tick_s=0.0,
                               now_s=lambda: venue.t, server_message=lambda e: False, state=state, **kw))
    return state


def count(db, table) -> int:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def delays(caplog) -> list[float]:
    return [float(m) for m in re.findall(r"book silence on a live connection .*?reconnecting in (\d+) s", caplog.text)]


def test_book_silence_rules():
    s = {"started_ms": 0, "books": 1, "book_ms": 10_000, "unbooked_ms": 11_000, "trade_ms": 70_000}
    assert rec.book_silence(s, 71_000) is None  # trades for 60 s without a quote: at the limit, not over
    assert "trades arriving but no quotes for 61 s" in rec.book_silence(s, 72_000)
    # no quote at all for over 120 s while trades arrived (none followed a quote on this connection)
    s = {"started_ms": 0, "books": 0, "book_ms": None, "unbooked_ms": None, "trade_ms": 100_000}
    assert rec.book_silence(s, 120_000) is None
    assert "no quotes for 121 s while trades arrived" in rec.book_silence(s, 121_000)
    s["trade_ms"] = None  # nothing at all: not a book silence (the stall watchdog handles that)
    assert rec.book_silence(s, 500_000) is None


def test_all_quiet_rule():
    q = rec.ALL_QUIET_S * 1000
    # quotes then nothing at all: the newest message of either kind counts
    s = {"started_ms": 0, "books": 3, "book_ms": 5_000, "unbooked_ms": None, "trade_ms": None, "trades": 0}
    assert rec.book_silence(s, 5_000 + q) is None  # at the limit, not over
    assert rec.book_silence(s, 5_000 + q + 1_000) == (
        f"all quiet: no message of any kind for {rec.ALL_QUIET_S + 1} s (limit {rec.ALL_QUIET_S} s)")
    s["trade_ms"] = 20_000  # a later trade is a message: quiet counts from it (and is under the trades-no-book limits)
    s["unbooked_ms"] = 20_000
    assert rec.book_silence(s, 20_000 + q) is None
    assert "all quiet" in rec.book_silence(s, 20_000 + q + 1_000)
    # trades only (no quote yet) then nothing: also all quiet, before NO_BOOK_S
    s = {"started_ms": 0, "books": 0, "book_ms": None, "unbooked_ms": 1_000, "trade_ms": 1_000, "trades": 1}
    assert "all quiet" in rec.book_silence(s, 1_000 + q + 1_000)
    # steady messages: never all quiet
    s = {"started_ms": 0, "books": 500, "book_ms": 499_000, "unbooked_ms": None, "trade_ms": 498_000, "trades": 500}
    assert rec.book_silence(s, 500_000) is None
    # nothing received yet on this connection: never all quiet, however long (the stall watchdog's job)
    s = {"started_ms": 0, "books": 0, "book_ms": None, "unbooked_ms": None, "trade_ms": None, "trades": 0}
    assert rec.book_silence(s, 100 * q) is None


def test_steady_messages_keep_one_connection(tmp_path, caplog):
    v = Venue(end_s=200)
    with caplog.at_level(logging.WARNING):
        quotes(tmp_path / rec.DB_NAME, v)
    assert v.connects == 1 and "all quiet" not in caplog.text


def test_a_socket_gone_all_quiet_mid_window_reconnects(tmp_path, caplog):
    """Quotes and trades flow, then the socket goes fully silent mid-window (nothing until the server's 1001 close at
    the next boundary, live): the lane reconnects after ALL_QUIET_S instead of waiting for that close."""
    db = tmp_path / rec.DB_NAME
    v = Venue(end_s=150)
    v.at[NOW + 20] = {"books": False, "trades": False}
    v.at[NOW + 100] = {"books": True, "trades": True}
    closed_at: list[float] = []
    markets = v.markets

    def tracked():
        ws = markets()
        close = ws.close

        async def close_and_note():
            closed_at.append(v.t)
            await close()

        ws.close = close_and_note
        return ws

    v.markets = tracked
    with caplog.at_level(logging.WARNING):
        state = quotes(db, v)
    assert "5m lane: book silence on a live connection (all quiet: no message of any kind for" in caplog.text
    assert v.connects >= 2 and v.sockets[0].closed
    assert NOW + 20 + rec.ALL_QUIET_S <= closed_at[0] <= NOW + 20 + rec.ALL_QUIET_S + 3  # at the limit, not later
    assert state["5m"]["venue_outage"] is False
    assert state["5m"]["seen"]["books"] > 0  # the fresh connection records again once the venue resumes


def test_trades_without_quotes_reconnect_the_lane(tmp_path, caplog):
    db = tmp_path / rec.DB_NAME
    v = Venue(end_s=200)
    v.at[NOW + 20] = {"books": False}  # the venue stops sending quotes 20 s in; trades keep coming
    v.at[NOW + 120] = {"books": True}  # and sends them again later
    with caplog.at_level(logging.WARNING):
        state = quotes(db, v)
    assert v.connects >= 2  # the silent connection was replaced
    assert "5m lane: book silence on a live connection (trades arriving but no quotes for" in caplog.text
    assert v.sockets[0].closed
    assert state["5m"]["venue_outage"] is False
    assert count(db, "pm_trades") > 100  # every print was recorded throughout


def test_a_venue_outage_backs_off_never_spins_and_does_not_exit(tmp_path, caplog):
    db = tmp_path / rec.DB_NAME
    v = Venue(end_s=25 * 60, books=False)  # 25 minutes of trades and no quote at all
    with caplog.at_level(logging.WARNING):
        state = quotes(db, v, reconnect_s=5.0)  # no RecorderStalled: the venue shows a book outage
    st = state["5m"]
    assert st["venue_outage"] and st["silences"] >= rec.VENUE_OUTAGE_AFTER
    assert caplog.text.count("Polymarket-side book outage") == 1  # said once
    d = delays(caplog)
    assert d[:5] == [5, 10, 20, 40, 80] and max(d) == rec.BOOK_SILENT_BACKOFF_MAX_S  # doubling, capped
    assert v.connects <= 25 * 60 // rec.TRADES_NO_BOOK_S + 1  # each try lasts at least 60 s: no spinning
    assert count(db, "pm_ws_quotes") == 0 and count(db, "pm_trades") > 1000


def test_quotes_returning_end_the_venue_outage(tmp_path, caplog):
    db = tmp_path / rec.DB_NAME
    v = Venue(end_s=20 * 60, books=False)
    v.at[NOW + 8 * 60] = {"books": True}
    with caplog.at_level(logging.WARNING):
        state = quotes(db, v)
    st = state["5m"]
    assert "Polymarket-side book outage" in caplog.text
    assert st["seen"]["books"] > 0  # the live connection records quotes: no longer an outage for the watchdog
    assert count(db, "pm_ws_quotes") > 100


def test_nothing_recorded_for_10_minutes_without_venue_evidence_raises_stalled(tmp_path, caplog):
    db = tmp_path / rec.DB_NAME
    v = Venue(end_s=30 * 60, books=False, trades=False)  # a socket that is open and sends nothing at all
    with caplog.at_level(logging.ERROR), pytest.raises(rec.RecorderStalled, match="no quote recorded for 60"):
        quotes(db, v)
    assert v.t < NOW + 11 * 60  # it gave up at 10 minutes, not at the end
    assert "exiting so the restart policy starts a fresh process" in caplog.text
    assert all(ws.closed for ws in v.sockets)  # the sockets were closed on the way out


def test_the_entry_point_exits_75_when_the_recorder_stalls(tmp_path, monkeypatch, caplog):
    async def stalled(*a, **kw):
        raise rec.RecorderStalled("no quote recorded for 601 s")

    monkeypatch.setattr(rec, "run_quotes", stalled)
    s = Settings(_env_file=None, db_path=tmp_path / "btcflow.db", polymarket_key_id="k", polymarket_secret_key="s")
    assert entry.main(["pm-quotes"], settings=s) == rec.EX_STALLED == 75
    assert "pm-quotes exiting with 75" in caplog.text


class FakeData:
    def __init__(self, newest):
        self.newest = newest

    def newest_ms(self):
        return dict(self.newest)

    def coverage(self, *a):
        return None


def test_the_data_alert_says_the_venue_sends_trades_but_no_quotes():
    now = NOW * 1000
    w = RecorderWatch(FakeData({"pm-quotes": now - 300_000, "pm-settle": now - 60_000, "pm-trades": now - 2_000}))
    (c,) = w.changes(now)
    head, detail = change_text(c)
    assert head == "Quotes recorder silent"
    assert detail.startswith("no Polymarket quotes for 5 min; trades are still arriving")
    assert "sending trades but no quotes" in detail
    assert w.status(now)["recorders"]["pm-quotes"]["reason"] == "the venue is sending trades but no quotes"
    # both silent: the recorder itself is down, said plainly
    w = RecorderWatch(FakeData({"pm-quotes": now - 300_000, "pm-settle": now - 60_000, "pm-trades": now - 300_000}))
    (c,) = w.changes(now)
    assert change_text(c) == ("Quotes recorder silent", "no Polymarket quotes for 5 min")
    assert "reason" not in w.status(now)["recorders"]["pm-quotes"]


def test_a_wedged_lane_stops_the_heartbeat_and_raises_stalled(tmp_path, caplog):
    """One lane's connect never returns while the other lane records: the recorder exits 75 after LANE_STUCK_S
    instead of beating (healthy) forever on the working lane."""
    v = Venue(end_s=30 * 60)
    markets = v.markets

    class Hung(VenueWS):
        async def connect(self):
            await asyncio.Event().wait()  # a WebSocket handshake that never completes

    def pick():
        ws = markets()
        if len(v.sockets) == 2:  # the second lane's first socket hangs
            ws.__class__ = Hung
        return ws

    v.markets = pick
    with caplog.at_level(logging.ERROR), pytest.raises(rec.RecorderStalled, match="lane has not turned for 18"):
        asyncio.run(rec.run_quotes(tmp_path / rec.DB_NAME, lambda: v, stop=v.stop, spans=("5m", "15m"), tick_s=0.0,
                                   now_s=lambda: v.t, server_message=lambda e: False))
    assert NOW + rec.LANE_STUCK_S <= v.t < NOW + rec.LANE_STUCK_S + 5  # at the limit, not at the end
    assert "lane has not turned for" in caplog.text and count(tmp_path / rec.DB_NAME, "pm_ws_quotes") > 100
