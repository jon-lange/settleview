"""Backfill Polymarket US BTC Up/Down windows (5m / 15m / 1h): market terms, settlement and price history.

Public, read-only market data (no keys, no orders). Walks back from the last closed window, one window at a time,
until markets stop existing (a full day of consecutive 404s) or `--days` (default 30) is reached. For every window:

  GET /v1/market/slug/{slug}       -> status, outcomePrices, assetPriceTerms (priceToBeat, settlementPrice, ...)
  GET /v1/price-history            -> symbol=slug, custom range [start-120, end+120], fidelity=1 (finest available)

Writes to its own file, data/research/polymarket_us_history.db (the live recorder's polymarket_us.db is untouched):

  pmh_windows(slug, span, window_start, window_end, http_status, status, outcome_prices, price_to_beat,
              settlement_price, result, result_outcome, n_points, raw_terms, fetched_ms)
  pmh_points(slug, ts, long_price, short_price, raw)

`result` = 'up' if settlementPrice >= priceToBeat (flat = Up), else 'down'; when priceToBeat is missing it falls back to
the previous window's settlementPrice (the two are the same BRTI fixing). `result_outcome` is what the market itself
resolved to (outcomePrices ["1","0"] = Up/Yes). History prices are display prices: longPrice = Yes, normally the best
ask; shortPrice = No, normally 1 - best bid (docs.polymarket.us). They are not trades.

Resumable: windows already in pmh_windows (any http_status) are skipped; --retry-missing re-asks for 404 rows and
for windows stored before their settlement price was published.
Rate: a shared limiter keeps the whole process at or below --rps (default 4.5) requests/second; 429s back off.

    python scripts/dev.py script scripts/backfill_polymarket_us.py                     # 15m, 5m, 1h, 30 days
    python scripts/dev.py script scripts/backfill_polymarket_us.py --spans 15m --days 3

dev.py mounts every existing database under data/research/ read-only, so it writes only a new file: resuming into an
existing polymarket_us_history.db (or --retry-missing) needs that file made writable on purpose, outside dev.py.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from polymarket_us import PolymarketUS
from polymarket_us.errors import APIConnectionError, APIStatusError, PolymarketUSError

SPANS = {"5m": 300, "15m": 900, "1h": 3600}
DB = Path(__file__).resolve().parents[1] / "data" / "research" / "polymarket_us_history.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS pmh_windows (
  slug TEXT PRIMARY KEY, span TEXT NOT NULL, window_start INTEGER NOT NULL, window_end INTEGER NOT NULL,
  http_status INTEGER, status TEXT, outcome_prices TEXT, price_to_beat REAL, settlement_price REAL,
  result TEXT, result_outcome TEXT, n_points INTEGER, raw_terms TEXT, fetched_ms INTEGER);
CREATE INDEX IF NOT EXISTS pmh_windows_span ON pmh_windows(span, window_start);
CREATE TABLE IF NOT EXISTS pmh_points (
  slug TEXT NOT NULL, ts INTEGER NOT NULL, long_price REAL, short_price REAL, raw TEXT,
  PRIMARY KEY (slug, ts));
"""


def slug_for(span: str, start: int) -> str:
    return f"cpc-btc-updown-{span}-{dt.datetime.fromtimestamp(start, dt.UTC).strftime('%Y-%m-%d-%H%Mz')}"


def num(x) -> float | None:
    if isinstance(x, dict):
        x = x.get("value")
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


class Limiter:
    def __init__(self, rps: float) -> None:
        self.gap, self.next, self.lock = 1.0 / rps, 0.0, threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next)
            self.next = t + self.gap
        if t > now:
            time.sleep(t - now)

    def penalise(self, sec: float) -> None:
        with self.lock:
            self.next = max(self.next, time.monotonic() + sec)


class Resp:
    """The two things fetch() reads from an answer, filled from the official SDK's parsed result or its error."""

    def __init__(self, status_code: int, body: dict | None) -> None:
        self.status_code, self._body = status_code, body or {}

    def json(self) -> dict:
        return self._body


def get(c: PolymarketUS, lim: Limiter, path: str, params: dict | None = None) -> Resp | None:
    """GET through the official SDK (`client.get`, public, no key): 429/5xx back off and retry, other statuses are
    returned as-is, connection errors retry."""
    for attempt in range(6):
        lim.wait()
        try:
            return Resp(200, c.get(path, query=params))
        except APIStatusError as e:
            code = getattr(e, "status_code", 0) or 0
            if code == 429 or code >= 500:
                back = float(2 ** attempt)
                print(f"  {code} on {path}; backing off {back:.0f}s", flush=True)
                lim.penalise(back)
                continue
            return Resp(code, None)
        except (APIConnectionError, PolymarketUSError):
            time.sleep(1 + attempt)
    return None


def fetch(c: PolymarketUS, lim: Limiter, span: str, start: int) -> tuple[tuple, list[tuple]] | None:
    slug, end = slug_for(span, start), start + SPANS[span]
    r = get(c, lim, f"/v1/market/slug/{slug}")
    if r is None:
        return None  # transient failure; not stored, so a re-run retries it
    now_ms = int(time.time() * 1000)
    if r.status_code != 200:
        return (slug, span, start, end, r.status_code, None, None, None, None, None, None, 0, None, now_ms), []
    m = r.json().get("market") or {}
    t = m.get("assetPriceTerms") or {}
    ptb, sp = num(t.get("priceToBeat")), num(t.get("settlementPrice"))
    try:
        op = json.loads(m.get("outcomePrices") or "null")
    except ValueError:
        op = None
    res_out = None
    if isinstance(op, list) and len(op) == 2:
        res_out = "up" if op[0] in ("1", 1) and op[1] in ("0", 0) else ("down" if op[0] in ("0", 0) and op[1] in ("1", 1) else None)
    h = get(c, lim, "/v1/price-history", {"symbol": slug, "timestamp.startTimestamp": start - 120,
                                          "timestamp.endTimestamp": end + 120, "fidelity": 1})
    if h is None or h.status_code != 200:
        return None
    pts = [(slug, int(p["timestamp"]), num(p.get("longPrice")), num(p.get("shortPrice")), json.dumps(p))
           for p in h.json().get("history") or [] if "timestamp" in p]
    win = (slug, span, start, end, 200, m.get("status"), m.get("outcomePrices"), ptb, sp, None, res_out, len(pts),
           json.dumps(t)[:6000], now_ms)
    return win, pts


def fill_results(db: sqlite3.Connection) -> None:
    """result from priceToBeat (or the previous window's settlement when priceToBeat is null), flat = Up."""
    db.execute("""
      UPDATE pmh_windows SET result = (
        SELECT CASE WHEN w.settlement_price IS NULL THEN NULL
                    WHEN COALESCE(w.price_to_beat, p.settlement_price) IS NULL THEN NULL
                    WHEN w.settlement_price >= COALESCE(w.price_to_beat, p.settlement_price) THEN 'up' ELSE 'down' END
        FROM pmh_windows w LEFT JOIN pmh_windows p ON p.span = w.span AND p.window_end = w.window_start
        WHERE w.slug = pmh_windows.slug)
      WHERE http_status = 200""")
    db.commit()


def backfill(db: sqlite3.Connection, c: PolymarketUS, lim: Limiter, span: str, days: float, workers: int,
             retry_missing: bool) -> None:
    sec = SPANS[span]
    now = int(time.time())
    last = now // sec * sec - sec  # last fully closed window
    if now < last + sec + 90:
        last -= sec  # give the latest one time to settle
    floor = now - int(days * 86400)
    # a 200 row with no settlement yet counts as missing for --retry-missing (it is re-fetched)
    have = {r[0]: (r[1] if r[2] is not None or r[1] != 200 else -1) for r in db.execute(
        "SELECT slug, http_status, settlement_price FROM pmh_windows WHERE span = ?", (span,))}
    stop_after = 86400 // sec  # a full day of consecutive misses = markets stopped existing
    misses, done, t0, start = 0, 0, time.time(), last
    batch = workers * 8
    while start >= floor and misses < stop_after:
        todo, cursor = [], start
        while len(todo) < batch and cursor >= floor:
            s = slug_for(span, cursor)
            if s not in have or (retry_missing and have[s] != 200):
                todo.append(cursor)
            elif have[s] in (200, -1):
                misses = 0
            cursor -= sec
        with ThreadPoolExecutor(workers) as ex:
            got = list(ex.map(lambda st: fetch(c, lim, span, st), todo))
        rows = [g for g in got if g]
        with db:
            db.executemany("INSERT OR REPLACE INTO pmh_windows VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [w for w, _ in rows])
            db.executemany("INSERT OR IGNORE INTO pmh_points VALUES (?,?,?,?,?)", [p for _, ps in rows for p in ps])
        for _st, g in sorted(zip(todo, got, strict=True), key=lambda x: -x[0]):  # newest first, as walked
            if g is None:
                continue
            misses = 0 if g[0][4] == 200 else misses + 1
        done += len(todo)
        n200 = sum(1 for g in rows if g[0][4] == 200)
        print(f"{span}: down to {slug_for(span, cursor + sec)[-16:]}  fetched {done}  (+{n200} ok, "
              f"{len(rows) - n200} missing, {len(todo) - len(rows)} failed)  misses-in-a-row {misses}  "
              f"{done / max(1e-9, time.time() - t0):.1f} win/s", flush=True)
        start = cursor
    why = "markets stopped existing" if misses >= stop_after else f"{days}-day limit"
    print(f"{span}: finished ({why}) at {slug_for(span, start + sec)}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spans", default="15m,5m,1h")
    ap.add_argument("--days", type=float, default=30.0)
    ap.add_argument("--rps", type=float, default=4.5)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--retry-missing", action="store_true")
    a = ap.parse_args()
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB, timeout=30)
    db.executescript(SCHEMA)
    lim = Limiter(a.rps)
    c = PolymarketUS(timeout=15.0, max_retries=0)  # official SDK, public (no key); retries are ours (above)
    for span in a.spans.split(","):
        backfill(db, c, lim, span.strip(), a.days, a.workers, a.retry_missing)
    fill_results(db)
    for r in db.execute("""SELECT span, SUM(http_status=200), SUM(http_status<>200), MIN(CASE WHEN http_status=200
                           THEN slug END), MAX(CASE WHEN http_status=200 THEN slug END), SUM(n_points)
                           FROM pmh_windows GROUP BY span"""):
        print(r)


if __name__ == "__main__":
    main()
