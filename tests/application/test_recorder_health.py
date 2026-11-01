"""Recorder freshness (P4): the read-only research database adapter, the watch's states and changes (silent after
its limit, back, a missing database said once, a read error changing nothing), yesterday's coverage, the healthcheck
command, and the Telegram data alert and coverage line."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC

from btc_signal_engine.adapters.polymarket_us import recorders as rec
from btc_signal_engine.adapters.polymarket_us.markets import slug_for
from btc_signal_engine.adapters.sqlite.research import ResearchDb
from btc_signal_engine.application.notifier import RemoteControl
from btc_signal_engine.application.recorder_health import RecorderWatch, change_text, coverage_text
from btc_signal_engine.bootstrap import ops
from tests.telegram.fakes import Clock, FakeMessenger, pair

DAY = 86_400_000
T0 = 1_761_868_800_000  # 2025-10-31 00:00:00Z
NOW = T0 + 12 * 3_600_000  # noon


def make_db(tmp_path, quotes=(), windows=()):
    path = tmp_path / rec.DB_NAME
    con = sqlite3.connect(path)
    con.executescript(rec.QUOTES_SCHEMA + rec.SETTLE_SCHEMA)
    con.executemany("INSERT INTO pm_ws_quotes VALUES (?,?,?,?,?,?,?,?)",
                    [(ms, "5m", slug, 0.4, 0.6, 1, 1, 0.5) for ms, slug in quotes])
    con.executemany("INSERT INTO pm_windows VALUES (?,?,?,?,?,?,?,?,?)",
                    [(slug_for(st * 1000, "5m"), "5m", st, st + 300, 1.0, 1.0, "up", "{}", fetched)
                     for st, fetched in windows])
    con.commit()
    con.close()
    return path


class FakeData:
    def __init__(self, newest=None, missing=False, boom=False, cov=None):
        self.newest, self.missing, self.boom, self.cov = newest or {}, missing, boom, cov

    def newest_ms(self):
        if self.missing:
            raise FileNotFoundError("x")
        if self.boom:
            raise sqlite3.OperationalError("database is locked")
        return dict(self.newest)

    def coverage(self, start, end, span):
        if self.missing:
            raise FileNotFoundError("x")
        return self.cov


def test_adapter_reads_the_newest_rows_read_only(tmp_path):
    make_db(tmp_path, quotes=[(NOW - 5_000, "a"), (NOW - 1_000, "b")], windows=[(T0 // 1000, NOW - 60_000)])
    db = ResearchDb(tmp_path)
    assert db.newest_ms() == {"pm-quotes": NOW - 1_000, "pm-settle": NOW - 60_000, "pm-trades": None}
    con = sqlite3.connect(db.path)  # nothing was left behind or locked
    con.execute("INSERT INTO pm_trades VALUES (1,1,'5m','s',1,1,'BUY','{}')")
    con.commit()
    con.close()


def test_adapter_missing_database_raises_and_creates_nothing(tmp_path):
    db = ResearchDb(tmp_path / "nope")
    try:
        db.newest_ms()
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError")
    assert not (tmp_path / "nope").exists()


def test_adapter_coverage_counts_windows_with_a_quote_inside(tmp_path):
    starts = [T0 + i * 300_000 for i in range(288)]
    gap = set(range(100, 104)) | {200}  # 4 windows in a row, then 1
    quotes = [(st + 10_000, slug_for(st, "5m")) for i, st in enumerate(starts) if i not in gap]
    quotes.append((starts[100] - 10_000, slug_for(starts[100], "5m")))  # before its window: does not count
    make_db(tmp_path, quotes=quotes, windows=[(st // 1000, 0) for st in starts[:280]])
    c = ResearchDb(tmp_path).coverage(T0, T0 + DAY, 300_000)
    assert (c["windows"], c["recorded"], c["settled"]) == (288, 283, 280)
    assert c["missing_starts"] == [starts[i] for i in sorted(gap)]
    w = RecorderWatch(ResearchDb(tmp_path)).coverage(T0 + DAY + 5_000)  # "yesterday" is the UTC day before
    assert w["longest_gap_min"] == 20 and w["recorded"] == 283
    assert coverage_text(w) == ("Data yesterday (UTC): 5m windows 283/288 recorded · longest gap 20 min · "
                                "280/288 settled")


def test_status_states(tmp_path):
    d = FakeData({"pm-quotes": NOW - 121_000, "pm-settle": None})
    st = RecorderWatch(d).status(NOW)
    assert st["available"] and st["recorders"]["pm-quotes"]["state"] == "silent"
    assert st["recorders"]["pm-quotes"]["age_s"] == 121 and st["recorders"]["pm-settle"]["state"] == "empty"
    assert RecorderWatch(FakeData(missing=True)).status(NOW) == {"available": False,
                                                                 "reason": "polymarket_us.db not found",
                                                                 "recorders": {}}


def test_changes_silent_back_and_missing_once():
    d = FakeData({"pm-quotes": NOW - 1_000, "pm-settle": NOW + 10**9})  # settlement: always fresh here
    w = RecorderWatch(d)
    assert w.changes(NOW) == []
    d.newest["pm-quotes"] = NOW - 1_000  # no row for 2 minutes and a bit
    assert w.changes(NOW + 119_000) == []
    (c,) = w.changes(NOW + 125_000)
    assert (c.kind, c.recorder, c.age_ms) == ("silent", "pm-quotes", 126_000)
    assert w.changes(NOW + 600_000) == []  # said once
    d.newest["pm-quotes"] = NOW + 700_000
    (b,) = w.changes(NOW + 701_000)
    assert (b.kind, b.age_ms) == ("back", 701_000)  # the gap between the last row before and the first after
    assert change_text(b) == ("Quotes recorder back", "writing again after 12 min")
    d.missing = True
    assert [x.kind for x in w.changes(NOW + 800_000)] == ["missing"]
    assert w.changes(NOW + 900_000) == []  # never repeated


def test_a_read_error_changes_nothing():
    w = RecorderWatch(FakeData(boom=True))
    assert w.changes(NOW) == [] and w.status(NOW)["available"] is False


def test_the_settlement_limit_is_10_minutes():
    w = RecorderWatch(FakeData({"pm-quotes": NOW + 10**9, "pm-settle": NOW - 300_000}))
    assert w.changes(NOW) == []
    assert [c.recorder for c in w.changes(NOW + 301_000)] == ["pm-settle"]


def test_fresh_healthcheck(tmp_path):
    assert ops.fresh("pm-quotes", 120, tmp_path)[0] is False  # no database
    make_db(tmp_path, quotes=[(NOW - 30_000, "a")])
    assert ops.fresh("pm-quotes", 120, tmp_path, now_ms=NOW) == (True, "pm-quotes: newest row 30 s old")
    ok, why = ops.fresh("pm-quotes", 10, tmp_path, now_ms=NOW)
    assert not ok and "30 s old (limit 10 s)" in why
    assert ops.fresh("pm-settle", 600, tmp_path, now_ms=NOW) == (False, "pm-settle: no rows yet")


# -- the Telegram data alert and the daily coverage line ---------------------------------------------------------
def _remote(store, data):
    clock = Clock(NOW)
    c = RemoteControl(store, now_fn=clock, configured=True, tz=UTC, recorders=RecorderWatch(data))
    c.messenger = FakeMessenger()
    asyncio.run(pair(c))
    return c, clock


def test_data_alert_silent_then_back(store):
    d = FakeData({"pm-quotes": NOW - 1_000, "pm-settle": NOW - 1_000})
    c, clock = _remote(store, d)
    n0 = len(c.messenger.sent)
    asyncio.run(c.pump())
    assert len(c.messenger.sent) == n0  # all fresh
    clock.t = NOW + 180_000
    asyncio.run(c.pump())
    (silent,) = c.messenger.sent[n0:]
    assert silent["text"] == "⚠️ <b>Quotes recorder silent</b> · no Polymarket quotes for 3 min"
    assert silent["silent"] is False
    d.newest["pm-quotes"] = clock.t + 10_000
    clock.t += 20_000
    asyncio.run(c.pump())
    back = c.messenger.sent[-1]
    assert back["text"].startswith("🟢 <b>Quotes recorder back</b>") and back["silent"] is True


def test_data_alert_missing_database_once_and_can_be_turned_off(store):
    c, clock = _remote(store, FakeData(missing=True))
    n0 = len(c.messenger.sent)
    asyncio.run(c.pump())
    (m,) = c.messenger.sent[n0:]
    assert m["text"].startswith("⚠️ <b>No recorder data</b> · polymarket_us.db not found")
    clock.t += 60_000
    asyncio.run(c.pump())
    assert len(c.messenger.sent) == n0 + 1
    c2, _ = _remote(store, FakeData({"pm-quotes": None, "pm-settle": None}))
    c2.set_alerts({"data": False}, NOW)
    n1 = len(c2.messenger.sent)
    asyncio.run(c2.pump())
    assert len(c2.messenger.sent) == n1


def test_daily_summary_without_trades_carries_the_coverage_line(store):
    cov = {"windows": 288, "recorded": 287, "settled": 288, "missing_starts": [T0 - DAY + 5 * 300_000]}
    c, clock = _remote(store, FakeData({"pm-quotes": NOW, "pm-settle": NOW}, cov=cov))
    text = c.daily_text(NOW, data=True)
    assert text.startswith("🌙 <b>No trades today</b>")
    assert "5m windows 287/288 recorded · longest gap 5 min · 288/288 settled" in text
    assert c.daily_text(NOW) is None  # without the data alert: nothing, as before
    c3 = RemoteControl(store, now_fn=clock, tz=UTC)  # no recorders watched: nothing either
    assert c3.daily_text(NOW, data=True) is None
