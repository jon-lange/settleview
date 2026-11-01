"""The Polymarket US research recorders (P1) against a fake SDK: same tables and columns as the old scripts, rows as
the old scripts wrote them, reconnect after a socket error, a clean stop (flush + close), settlement rows, and the
entry point's configuration checks. No network."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from btc_signal_engine.adapters.polymarket_us import recorders as rec
from btc_signal_engine.bootstrap import recorders as entry
from btc_signal_engine.bootstrap.config import Settings
from btc_signal_engine.bootstrap.logs import EX_CONFIG

NOW = 1_761_900_000  # 2025-10-31 08:40:00Z, a 5m boundary


class FakeWS:
    def __init__(self, owner: FakeClient) -> None:
        self.owner = owner
        self.handlers: dict[str, object] = {}
        self.subs: dict[str, tuple[str, list[str]]] = {}
        self.closed = False

    def on(self, event, cb):
        self.handlers[event] = cb

    async def connect(self):
        self.owner.connects += 1
        if self.owner.fail_connects > 0:
            self.owner.fail_connects -= 1
            raise ConnectionError("refused")

    async def subscribe(self, rid, kind, slugs):
        assert len(slugs) == 1 and rid not in self.subs
        self.subs[rid] = (kind, slugs)
        slug = slugs[0]
        if kind == "SUBSCRIPTION_TYPE_MARKET_DATA_LITE":
            self.handlers["market_data_lite"]({"marketDataLite": {
                "marketSlug": slug, "bestBid": {"value": "0.41"}, "bestAsk": "0.43", "bidDepth": 10, "askDepth": 12,
                "lastTradePx": {"value": "0.42"}}})
        else:
            self.handlers["trade"]({"trade": {"marketSlug": slug, "tradeTime": "2025-10-31T08:40:01.123456789Z",
                                              "price": {"value": "0.42"}, "quantity": "3", "maker": {"side": "BUY"}}})
        if self.owner.error_after_sub:
            self.owner.error_after_sub = False
            self.handlers["error"](OSError("socket closed"))

    async def unsubscribe(self, rid):
        self.subs.pop(rid)

    async def close(self):
        self.closed = True


class FakeClient:
    def __init__(self) -> None:
        self.connects = 0
        self.fail_connects = 0
        self.error_after_sub = False
        self.sockets: list[FakeWS] = []
        self.ws = self

    def markets(self):
        s = FakeWS(self)
        self.sockets.append(s)
        return s


def _tables(path) -> dict[str, list[str]]:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {n: [c[1] for c in con.execute(f"PRAGMA table_info({n})")] for n in names}
    finally:
        con.close()


def _rows(path, sql):
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _stop_after(ticks: list[int], n: int):
    def stop() -> bool:
        ticks[0] += 1
        return ticks[0] > n
    return stop


def test_quotes_write_the_old_tables_and_rows(tmp_path):
    db = tmp_path / "research" / rec.DB_NAME
    client = FakeClient()
    ticks = [0]
    asyncio.run(rec.run_quotes(db, lambda: client, stop=_stop_after(ticks, 12), spans=("5m",), tick_s=0.0,
                               now_s=lambda: NOW + 0.5, server_message=lambda e: False))
    assert _tables(db) == {
        "pm_trades": ["recv_ms", "trade_ms", "span", "slug", "price", "qty", "maker_side", "raw"],
        "pm_ws_quotes": ["recv_ms", "span", "slug", "bid", "ask", "bid_depth", "ask_depth", "last"]}
    q = _rows(db, "SELECT * FROM pm_ws_quotes ORDER BY slug")
    assert q == [(NOW * 1000 + 500, "5m", "cpc-btc-updown-5m-2025-10-31-0840z", 0.41, 0.43, 10.0, 12.0, 0.42),
                 (NOW * 1000 + 500, "5m", "cpc-btc-updown-5m-2025-10-31-0845z", 0.41, 0.43, 10.0, 12.0, 0.42)]
    t, = _rows(db, "SELECT trade_ms, price, qty, maker_side FROM pm_trades WHERE slug LIKE '%0840z'")
    assert t == (1_761_900_001_123, 0.42, 3.0, "BUY")
    # one subscription per market and type, on one connection; the socket was closed on stop
    assert client.connects == 1 and len(client.sockets[0].subs) == 4 and client.sockets[0].closed


def test_quotes_reconnect_after_a_socket_error(tmp_path):
    db = tmp_path / rec.DB_NAME
    client = FakeClient()
    client.fail_connects = 1
    client.error_after_sub = True
    ticks = [0]
    asyncio.run(rec.run_quotes(db, lambda: client, stop=_stop_after(ticks, 40), spans=("15m",), tick_s=0.0,
                               reconnect_s=0.0, now_s=lambda: NOW, server_message=lambda e: False))
    assert client.connects >= 3  # refused, then a socket error, then a healthy connection
    assert all(s.closed for s in client.sockets[1:])
    assert _rows(db, "SELECT count(*) FROM pm_ws_quotes")[0][0] >= 4


def test_a_server_message_is_not_a_dead_socket(tmp_path):
    db = tmp_path / rec.DB_NAME
    client = FakeClient()
    client.error_after_sub = True
    ticks = [0]
    asyncio.run(rec.run_quotes(db, lambda: client, stop=_stop_after(ticks, 10), spans=("1h",), tick_s=0.0,
                               now_s=lambda: NOW, server_message=lambda e: True))
    assert client.connects == 1


class FakeMarkets:
    def __init__(self, terms: dict[str, dict], not_found: type[Exception]) -> None:
        self.terms, self.not_found, self.asked = terms, not_found, []

    def retrieve_by_slug(self, slug):
        self.asked.append(slug)
        if slug not in self.terms:
            raise self.not_found(slug)
        return {"market": {"assetPriceTerms": self.terms[slug]}}


class NotFound(Exception):
    pass


def test_settle_writes_pm_windows_as_before(tmp_path):
    db = tmp_path / rec.DB_NAME
    now = NOW + 31  # 31 s after the 08:40 close of the 08:35 5m window
    c = type("C", (), {})()
    c.markets = FakeMarkets({"cpc-btc-updown-5m-2025-10-31-0835z": {"priceToBeat": {"value": "100"},
                                                                     "settlementPrice": "100"}}, NotFound)
    waits = []
    rec.run_settle(db, c, every_s=5.0, wait=lambda s: waits.append(s) or True, now_s=lambda: now, not_found=NotFound)
    assert set(_tables(db)) == {"pm_quotes", "pm_windows"}
    assert _tables(db)["pm_windows"] == ["slug", "span", "window_start", "window_end", "price_to_beat",
                                         "settlement_price", "result", "raw", "fetched_ms"]
    row, = _rows(db, "SELECT slug, span, window_start, window_end, price_to_beat, settlement_price, result, fetched_ms "
                     "FROM pm_windows")
    assert row == ("cpc-btc-updown-5m-2025-10-31-0835z", "5m", NOW - 300, NOW, 100.0, 100.0, "up", now * 1000)
    assert waits == [5.0]


def test_settle_gives_up_after_30_minutes(tmp_path):
    db = rec.open_db(tmp_path / rec.DB_NAME, rec.SETTLE_TABLES)
    c = type("C", (), {})()
    c.markets = FakeMarkets({}, NotFound)
    pending = {("5m", NOW - 300)}
    rec.settle_tick(db, c, pending, NOW + 1801, NotFound, 0)
    assert ("5m", NOW - 300) not in pending


def _settings(tmp_path, **kw) -> Settings:
    return Settings(_env_file=None, db_path=tmp_path / "btcflow.db", **kw)


def test_entry_check_says_where_it_writes(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("BTCFLOW_POLYMARKET_KEY_ID", raising=False)
    assert entry.main(["pm-settle", "--check"], settings=_settings(tmp_path)) == 0
    assert str(tmp_path / "research" / rec.DB_NAME) in capsys.readouterr().out
    assert not (tmp_path / "research").exists()  # --check writes nothing


def test_entry_research_dir_setting(tmp_path, capsys):
    s = _settings(tmp_path, research_dir=tmp_path / "elsewhere")
    assert entry.main(["pm-settle", "--check"], settings=s) == 0
    assert str(tmp_path / "elsewhere" / rec.DB_NAME) in capsys.readouterr().out


def test_entry_quotes_without_keys_is_a_config_error(tmp_path, caplog):
    assert entry.main(["pm-quotes", "--check"], settings=_settings(tmp_path)) == EX_CONFIG
    assert "not configured" in caplog.text


def test_entry_quotes_with_keys_checks_ok(tmp_path):
    s = _settings(tmp_path, polymarket_key_id="k", polymarket_secret_key="s")
    assert entry.main(["pm-quotes", "--check"], settings=s) == 0


def test_unknown_recorder_name_is_refused():
    with pytest.raises(SystemExit):
        entry.parse(["pm-everything"])


def test_stop_flag_sees_the_stop_file(tmp_path):
    f = tmp_path / "pm-quotes.stop"
    flag = entry.StopFlag(f)
    assert not flag() and not flag.wait(0.01)
    f.write_text("stop")
    assert flag() and flag.wait(10)


# -- a database outage never costs a socket, and the buffers stay bounded (B3#6) ------------------------------------
class _DownEngine:
    """A research engine whose every transaction fails as an unreachable database does."""

    def __init__(self) -> None:
        self.down = True
        self.tries = 0

    def begin(self):
        from sqlalchemy.exc import OperationalError
        self.tries += 1
        raise OperationalError("insert", {}, ConnectionRefusedError("connection refused"))


def test_a_failing_flush_still_closes_the_socket_and_keeps_the_rows():
    client = FakeClient()
    r = rec.QuoteRecorder(_DownEngine(), lambda: NOW * 1000)
    ticks = [0]
    asyncio.run(rec.run_lane_once(r, client, {"5m"}, stop=_stop_after(ticks, 5), now_s=lambda: NOW, tick_s=0.0,
                                  server_message=lambda e: False))
    assert client.connects == 1 and client.sockets[0].closed  # one connection all along, closed at the end
    assert len(r.q) == 2 and len(r.t) == 2  # nothing written, nothing lost


def test_the_socket_is_closed_even_when_the_last_flush_raises(monkeypatch):
    client = FakeClient()
    r = rec.QuoteRecorder(_DownEngine(), lambda: NOW * 1000)

    async def boom():
        raise RuntimeError("flush exploded")

    monkeypatch.setattr(r, "flush_async", boom)
    with pytest.raises(RuntimeError, match="flush exploded"):
        asyncio.run(rec.run_lane_once(r, client, {"5m"}, stop=lambda: False, now_s=lambda: NOW, tick_s=0.0,
                                      server_message=lambda e: False))
    assert client.sockets[0].closed


def test_buffers_are_capped_during_an_outage_and_count_what_they_drop(caplog, monkeypatch):
    metered: list[tuple] = []  # each drop is also an OpenTelemetry count (settleview.venue.dropped)
    monkeypatch.setattr(rec.telemetry, "dropped", lambda component, buffer, n=1: metered.append((component, buffer)))
    r = rec.QuoteRecorder(_DownEngine(), lambda: NOW * 1000, max_buffered=5)
    for i in range(12):
        r.on_lite({"marketDataLite": {"marketSlug": f"cpc-btc-updown-5m-x{i}", "bestBid": "0.4"}})
    assert asyncio.run(r.flush_async()) is False
    for i in range(12, 15):
        r.on_lite({"marketDataLite": {"marketSlug": f"cpc-btc-updown-5m-x{i}", "bestBid": "0.4"}})
    assert len(r.q) == 5 and r.dropped == 10
    assert [row[2] for row in r.q] == [f"cpc-btc-updown-5m-x{i}" for i in range(10, 15)]  # the newest kept
    assert metered == [("pm_quotes_recorder", "quotes")] * 10


def test_rows_kept_through_an_outage_are_written_once_it_ends(tmp_path):
    db = rec.open_db(tmp_path / rec.DB_NAME, rec.QUOTES_TABLES)
    r = rec.QuoteRecorder(_DownEngine(), lambda: NOW * 1000)
    r.on_lite({"marketDataLite": {"marketSlug": "cpc-btc-updown-5m-a", "bestBid": "0.4"}})
    assert asyncio.run(r.flush_async()) is False
    r.on_lite({"marketDataLite": {"marketSlug": "cpc-btc-updown-5m-b", "bestBid": "0.4"}})
    r.db = db  # the database is back
    assert asyncio.run(r.flush_async()) is True
    assert [x[0] for x in db.connect().exec_driver_sql("select slug from pm_ws_quotes order by slug")] == [
        "cpc-btc-updown-5m-a", "cpc-btc-updown-5m-b"]
    db.dispose()


# -- process-local liveness (B4#16) -------------------------------------------------------------------------------
def test_the_watchdog_and_the_settle_loop_beat(tmp_path):
    beats = []
    client = FakeClient()
    ticks = [0]
    asyncio.run(rec.run_quotes(tmp_path / rec.DB_NAME, lambda: client, stop=_stop_after(ticks, 8), spans=("5m",),
                               tick_s=0.0, now_s=lambda: NOW, server_message=lambda e: False,
                               beat=lambda: beats.append("q")))
    assert beats
    class Unsettled:  # every window still open
        class markets:  # noqa: N801
            @staticmethod
            def retrieve_by_slug(slug):
                raise KeyError(slug)

    passes = iter([False, False, True])
    rec.run_settle(tmp_path / rec.DB_NAME, Unsettled(), every_s=0, wait=lambda _s: next(passes), now_s=lambda: NOW,
                   not_found=KeyError, beat=lambda: beats.append("s"))
    assert beats.count("s") == 3


def test_ops_alive_reads_only_the_heartbeat_file(tmp_path, monkeypatch, capsys):
    import os
    import tempfile

    from btc_signal_engine.bootstrap import heartbeat, ops
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    p = heartbeat.path("pm-quotes")
    assert p.parent == tmp_path
    assert ops.main(["alive", "pm-quotes"]) == 1 and "no heartbeat" in capsys.readouterr().err
    heartbeat.beat(p)
    assert ops.main(["alive", "pm-quotes"]) == 0
    old = p.stat().st_mtime - 120
    os.utime(p, (old, old))
    assert ops.main(["alive", "pm-quotes"]) == 1 and "limit 60 s" in capsys.readouterr().err
    assert ops.main(["alive", "pm-quotes", "--max-age", "300"]) == 0


def test_a_main_loop_that_stops_beating_ends_the_process(tmp_path):
    """The process-local watch: a heartbeat file older than the limit ends the process with 75 (Compose never acts
    on an unhealthy container); a fresh file, no file, or a stopped watch never does."""
    import os
    import threading
    import time

    from btc_signal_engine.bootstrap import heartbeat

    p = tmp_path / "rec.alive"
    exits: list[int] = []
    fired = threading.Event()

    def exit_(code):
        exits.append(code)
        fired.set()

    heartbeat.beat(p)
    done = heartbeat.exit_when_stale(p, 60.0, check_s=0.01, exit=exit_)
    time.sleep(0.1)
    assert exits == []  # beaten just now
    old = time.time() - 120
    os.utime(p, (old, old))
    assert fired.wait(5) and exits == [heartbeat.EX_STALLED] and heartbeat.EX_STALLED == 75
    done.set()

    exits.clear()
    p.unlink()
    done = heartbeat.exit_when_stale(p, 60.0, check_s=0.01, exit=exit_)
    time.sleep(0.1)
    done.set()
    assert exits == []  # no file (the recorder removed it at its end): nothing to judge
