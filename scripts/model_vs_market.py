"""Model vs market: does the Coach's fair value carry information that Kalshi's own price does not? (read-only)

Pre-registered in docs/research/2026-10-28-model-vs-market.md. Read-only on the app: data/btcflow.db is opened with
mode=ro, nothing is ordered, and everything this script writes goes under data/research/.

Run from the repo root with the project venv:
  python scripts/model_vs_market.py fetch-markets   settled KXBTC15M markets and official results (Kalshi public API)
  python scripts/model_vs_market.py fetch-candles   1-minute top-of-book candles (robustness check only)
  python scripts/model_vs_market.py fetch-panel     latest prints before each tau, every market
  python scripts/model_vs_market.py fetch-full      full trade tapes for the stratified random sample
  python scripts/model_vs_market.py analyze         tests A-D -> data/research/results.md and CSVs
  python scripts/model_vs_market.py all             all of the above, in order (fetches resume where they stopped)

Flags: --start/--end (UTC dates, inclusive), --sample-frac, --seed, --rate/--max-rate/--workers (API pacing),
--boot (bootstrap replicates), --app-db, --out-dir.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, UTC
from functools import lru_cache
from pathlib import Path

import httpx
import numpy as np
from scipy.special import expit, ndtr, ndtri

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from btc_signal_engine.domain.coach import rules as R  # noqa: E402
from btc_signal_engine.domain.coach.fair_value import HISTORY_MS, fair_from_index  # noqa: E402
from btc_signal_engine.domain.models.events import IndexSecond  # noqa: E402
from btc_signal_engine.domain.models.numbers import to_float  # noqa: E402
from btc_signal_engine.domain.settlement.fees import kalshi_fee  # noqa: E402
from btc_signal_engine.domain.settlement.settlement import window_average  # noqa: E402

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = "KXBTC15M"
TAUS = (12, 10, 8, 6, 4, 3, 2, 1)  # minutes before close
LOOKBACK_MS = 30_000  # quote proxies use prints in [t - 30s, t)
PANEL_FETCH_S = 45  # the panel fetch looks back at most 45s before tau
MODEL_LAG_MS = 1_000  # the model only sees index seconds strictly before t - 1s
TAPE_S = 820  # "full" tapes cover the last 13m40s: the strategy scan (13:00 -> 2:00) + 30s quote lookback + margin
CHECK_PANEL = 30  # sampled markets whose panel is also fetched directly, to check the two routes agree
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
log = logging.getLogger("model_vs_market")


# ------------------------------------------------------------------------------------------------ helpers
def day_ms(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp()) * 1000


def iso_ms(s: str) -> int:
    return (datetime.fromisoformat(s.replace("Z", "+00:00")) - EPOCH) // timedelta(milliseconds=1)


def utc_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d")


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS markets(
  mkt INTEGER PRIMARY KEY, ticker TEXT UNIQUE NOT NULL, open_ts INTEGER NOT NULL, close_ts INTEGER NOT NULL,
  result TEXT NOT NULL, expiration_value REAL, expiration_value_raw TEXT, floor_strike REAL, settlement_ts INTEGER,
  day TEXT NOT NULL, sampled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS trades(mkt INTEGER NOT NULL, ts_ms INTEGER NOT NULL, yes_price REAL NOT NULL,
  count REAL NOT NULL, taker_yes INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS panel_trades(mkt INTEGER NOT NULL, tau INTEGER NOT NULL, ts_ms INTEGER NOT NULL,
  yes_price REAL NOT NULL, count REAL NOT NULL, taker_yes INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS candles(mkt INTEGER NOT NULL, end_ts INTEGER NOT NULL, yes_bid REAL, yes_ask REAL,
  price REAL, volume REAL, PRIMARY KEY(mkt, end_ts));
CREATE TABLE IF NOT EXISTS done(kind TEXT NOT NULL, mkt INTEGER NOT NULL, n INTEGER, blocks INTEGER,
  PRIMARY KEY(kind, mkt));
CREATE VIEW IF NOT EXISTS trades_v AS
  SELECT m.ticker, t.ts_ms, t.yes_price, t.count, CASE t.taker_yes WHEN 1 THEN 'yes' ELSE 'no' END AS taker_side
  FROM trades t JOIN markets m USING(mkt);
CREATE VIEW IF NOT EXISTS panel_trades_v AS
  SELECT m.ticker, t.tau, t.ts_ms, t.yes_price, t.count, CASE t.taker_yes WHEN 1 THEN 'yes' ELSE 'no' END AS taker_side
  FROM panel_trades t JOIN markets m USING(mkt);
"""


def full_order(c: sqlite3.Connection, seed: int) -> list[int]:
    """The sampled markets in a fixed random order (the order full tapes are fetched in)."""
    ids = [r[0] for r in c.execute("SELECT mkt FROM markets WHERE sampled=1 ORDER BY mkt")]
    return [ids[i] for i in np.random.default_rng(seed + 1).permutation(len(ids))]


def research_db(out_dir: Path) -> sqlite3.Connection:
    out_dir.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(out_dir / "kalshi_trades.db", timeout=60)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.executescript(SCHEMA)
    return c


def app_db(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def set_meta(c: sqlite3.Connection, key: str, value) -> None:
    c.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))


def get_meta(c: sqlite3.Connection, key: str, default=None):
    r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return default if r is None else json.loads(r[0])


# ------------------------------------------------------------------------------------------------ API pacing
class Limiter:
    """Shared pacing for all workers: additive increase every 50 successes, x0.7 and a 2s pause on HTTP 429."""

    def __init__(self, rate: float, max_rate: float) -> None:
        self.rate, self.max_rate = rate, max_rate
        self.next = 0.0
        self.lock = threading.Lock()
        self.streak = self.n = self.n429 = 0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next)
            self.next = t + 1.0 / self.rate
        if t > now:
            time.sleep(t - now)

    def ok(self) -> None:
        with self.lock:
            self.n += 1
            self.streak += 1
            if self.streak >= 50:
                self.streak = 0
                self.rate = min(self.max_rate, self.rate + 0.2)

    def throttled(self) -> None:
        with self.lock:
            self.n += 1
            self.n429 += 1
            self.streak = 0
            self.rate = max(1.0, self.rate * 0.7)
            self.next = max(self.next, time.monotonic() + 2.0)


class Api:
    def __init__(self, limiter: Limiter) -> None:
        self.lim = limiter
        self.local = threading.local()

    def _client(self) -> httpx.Client:
        if not hasattr(self.local, "c"):
            self.local.c = httpx.Client(base_url=BASE_URL, timeout=30.0)
        return self.local.c

    def get(self, path: str, params: dict) -> dict:
        for attempt in range(12):
            self.lim.wait()
            try:
                r = self._client().get(path, params=params)
            except httpx.HTTPError as e:
                log.warning("GET %s failed (%s); retrying", path, e)
                time.sleep(min(30, 2 ** attempt))
                continue
            if r.status_code == 200:
                self.lim.ok()
                return r.json()
            if r.status_code == 429:
                self.lim.throttled()
                continue
            if r.status_code >= 500:
                time.sleep(min(30, 2 ** attempt))
                continue
            r.raise_for_status()
        raise RuntimeError(f"GET {path} {params} kept failing")


def parse_trades(trades: list[dict]) -> tuple[list[tuple[int, float, float, int]], int]:
    """API trades (newest first) -> chronological (ts_ms, yes_price, count, taker_yes); block trades dropped."""
    rows, blocks = [], 0
    for x in reversed(trades):
        if x.get("is_block_trade"):
            blocks += 1
            continue
        side = x.get("taker_outcome_side") or x.get("taker_side")
        rows.append((iso_ms(x["created_time"]), float(x["yes_price_dollars"]), float(x["count_fp"]),
                     1 if side == "yes" else 0))
    return rows, blocks


def run_units(units, work, store, *, workers: int, label: str, lim: Limiter) -> None:
    """Run `work(unit)` on a thread pool; `store(unit, result)` on this thread (the only SQLite writer)."""
    t0 = time.time()
    total = len(units)
    if not total:
        log.info("%s: nothing to do", label)
        return
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(work, u): u for u in units}
        for k, f in enumerate(as_completed(futs), 1):
            store(futs[f], f.result())
            if k % 25 == 0 or k == total:
                el = time.time() - t0
                log.info("%s: %d/%d units, %.0fs elapsed, ETA %.0f min, %d requests (%.2f/s), %d x 429, rate %.2f/s",
                         label, k, total, el, el / k * (total - k) / 60, lim.n, lim.n / max(el, 1e-9), lim.n429,
                         lim.rate)


# ------------------------------------------------------------------------------------------------ fetch steps
def fetch_markets(args, c: sqlite3.Connection) -> None:
    lim = Limiter(args.rate, args.max_rate)
    api = Api(lim)
    start, end = day_ms(args.start), day_ms(args.end) + 86_400_000
    params = {"series_ticker": SERIES, "status": "settled", "limit": 1000,
              "min_close_ts": start // 1000 + 900, "max_close_ts": end // 1000 + 900}
    got, cursor = [], None
    while True:
        p = dict(params, **({"cursor": cursor} if cursor else {}))
        d = api.get("/markets", p)
        got += d.get("markets") or []
        cursor = d.get("cursor")
        if not cursor or not d.get("markets"):
            break
    kept, excluded = [], {}
    for m in got:
        o, cl = iso_ms(m["open_time"]), iso_ms(m["close_time"])
        if not (start <= o < end):
            continue
        res = m.get("result") or ""
        if res not in ("yes", "no") or m.get("market_type", "binary") != "binary":
            excluded[res or "none"] = excluded.get(res or "none", 0) + 1
            continue
        raw = m.get("expiration_value")
        kept.append((m["ticker"], o, cl, res, to_float(raw), None if raw is None else str(raw),
                     to_float(m.get("floor_strike")),
                     iso_ms(m["settlement_ts"]) if m.get("settlement_ts") else None, utc_day(o)))
    kept.sort(key=lambda r: r[1])
    for r in kept:
        c.execute("""INSERT INTO markets(ticker, open_ts, close_ts, result, expiration_value, expiration_value_raw,
                     floor_strike, settlement_ts, day) VALUES (?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(ticker) DO UPDATE SET result=excluded.result,
                     expiration_value=excluded.expiration_value, expiration_value_raw=excluded.expiration_value_raw,
                     floor_strike=excluded.floor_strike, settlement_ts=excluded.settlement_ts""", r)
    set_meta(c, "markets_excluded", excluded)
    set_meta(c, "period", [args.start, args.end])
    # Stratified random sample for the full tapes: round(frac * n) markets per UTC day, fixed seed.
    prev = get_meta(c, "sample")
    want = {"frac": args.sample_frac, "seed": args.seed}
    if prev and prev != want and not args.resample:
        raise SystemExit(f"sample already drawn with {prev}; pass --resample to redraw (full tapes would be stale)")
    if prev != want or args.resample or not c.execute("SELECT count(*) FROM markets WHERE sampled=1").fetchone()[0]:
        rng = np.random.default_rng(args.seed)
        c.execute("UPDATE markets SET sampled=0")
        for (day,) in c.execute("SELECT DISTINCT day FROM markets ORDER BY day").fetchall():
            ids = [r[0] for r in c.execute("SELECT mkt FROM markets WHERE day=? ORDER BY open_ts", (day,))]
            pick = rng.permutation(len(ids))[: int(round(args.sample_frac * len(ids)))]
            c.executemany("UPDATE markets SET sampled=1 WHERE mkt=?", [(ids[i],) for i in pick])
        set_meta(c, "sample", want)
    c.commit()
    # Cross-check against the app's own copy of Kalshi's results (read-only).
    a = app_db(Path(args.app_db))
    app = {t: (res, fs, ev) for t, res, fs, ev in
           a.execute("SELECT ticker, result, floor_strike, expiration_value FROM kalshi_markets")}
    mism = {"result": 0, "floor_strike": 0, "expiration_value": 0, "missing_in_app": 0}
    for t, res, fs, ev in c.execute("SELECT ticker, result, floor_strike, expiration_value FROM markets"):
        if t not in app:
            mism["missing_in_app"] += 1
            continue
        ar, afs, aev = app[t]
        mism["result"] += ar != res
        mism["floor_strike"] += (afs is None) != (fs is None) or (afs is not None and abs(afs - fs) > 0.005)
        mism["expiration_value"] += (aev is None) != (ev is None) or (aev is not None and abs(aev - ev) > 0.005)
    set_meta(c, "app_crosscheck", mism)
    c.commit()
    n, ns = c.execute("SELECT count(*), sum(sampled) FROM markets").fetchone()
    log.info("markets: %d kept (%d sampled for full tapes), excluded %s, app cross-check %s", n, ns, excluded, mism)


def fetch_candles(args, c: sqlite3.Connection) -> None:
    lim = Limiter(args.rate, args.max_rate)
    api = Api(lim)
    have = {r[0] for r in c.execute("SELECT mkt FROM done WHERE kind='candles'")}
    ms = [r for r in c.execute("SELECT mkt, ticker, open_ts, close_ts FROM markets ORDER BY open_ts") if r[0] not in have]
    by_ticker = {t: (k, o, cl) for k, t, o, cl in ms}
    chunks, cur = [], []
    for m in ms:  # the API caps a batch at 10,000 candles = markets x minutes spanned
        nxt = cur + [m]
        if cur and len(nxt) * ((nxt[-1][3] - nxt[0][2]) // 60_000 + 2) > 9_000:
            chunks.append(cur)
            nxt = [m]
        cur = nxt
    chunks += [cur] if cur else []
    for chunk in chunks:
        d = api.get("/markets/candlesticks", {
            "market_tickers": ",".join(t for _, t, _, _ in chunk), "period_interval": 1,
            "start_ts": min(o for _, _, o, _ in chunk) // 1000, "end_ts": max(cl for _, _, _, cl in chunk) // 1000})
        rows, seen = [], set()
        for m in d.get("markets") or []:
            t = m.get("market_ticker") or m.get("ticker")
            if t not in by_ticker:
                continue
            k = by_ticker[t][0]
            seen.add(k)
            for cs in m.get("candlesticks") or []:
                rows.append((k, int(cs["end_period_ts"]) * 1000, to_float((cs.get("yes_bid") or {}).get("close_dollars")),
                             to_float((cs.get("yes_ask") or {}).get("close_dollars")),
                             to_float((cs.get("price") or {}).get("close_dollars")), to_float(cs.get("volume_fp"))))
        c.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?)", rows)
        c.executemany("INSERT OR REPLACE INTO done VALUES ('candles', ?, ?, 0)",
                      [(k, sum(1 for r in rows if r[0] == k)) for k in seen])
        c.commit()
    log.info("candles: %d rows for %d markets", *c.execute("SELECT count(*), count(DISTINCT mkt) FROM candles").fetchone())


def fetch_panel(args, c: sqlite3.Connection) -> None:
    """For every market and tau: the latest prints before tau (pages of 1,000, <= 45s back), paging further only
    while a taker side has no print within 30s and the page has not reached 30s back."""
    lim = Limiter(args.rate, args.max_rate)
    api = Api(lim)
    have = {r[0] for r in c.execute("SELECT mkt FROM done WHERE kind='panel'")}
    full = {r[0] for r in c.execute("SELECT mkt FROM done WHERE kind='full'")}
    # Sampled markets get their panel from the full tape (same prints); CHECK_PANEL of them are also fetched here
    # so the two routes can be compared. --panel-missing covers sampled markets whose full tape was not fetched
    # (time box), --panel-all every market.
    check = set(full_order(c, args.seed)[:CHECK_PANEL])
    units = [r for r in c.execute("SELECT mkt, ticker, close_ts, sampled FROM markets ORDER BY open_ts")
             if r[0] not in have and (args.panel_all or not r[3] or r[0] in check
                                      or (args.panel_missing and r[0] not in full))]
    units = [units[i][:3] for i in np.random.default_rng(args.seed + 2).permutation(len(units))]  # random order
    units = units[: args.max_units] if args.max_units else units

    def work(u):
        k, ticker, close = u
        out, blocks = [], 0
        for tau in TAUS:
            tau_ms = close - tau * 60_000
            cursor, seen = None, set()
            while True:
                p = {"ticker": ticker, "min_ts": tau_ms // 1000 - PANEL_FETCH_S, "max_ts": tau_ms // 1000, "limit": 1000}
                if cursor:
                    p["cursor"] = cursor
                d = api.get("/markets/trades", p)
                tr = d.get("trades") or []
                rows, b = parse_trades(tr)
                blocks += b
                rows = [r for r in rows if r[0] < tau_ms]
                out += [(k, tau, *r) for r in rows]
                seen |= {r[3] for r in rows if r[0] >= tau_ms - LOOKBACK_MS}
                cursor = d.get("cursor")
                if not cursor or not tr or len(seen) == 2 or (rows and rows[0][0] < tau_ms - LOOKBACK_MS):
                    break
        return out, blocks

    def store(u, res):
        out, blocks = res
        out.sort(key=lambda r: (r[1], r[2]))
        c.executemany("INSERT INTO panel_trades VALUES (?,?,?,?,?,?)", out)
        c.execute("INSERT OR REPLACE INTO done VALUES ('panel', ?, ?, ?)", (u[0], len(out), blocks))
        c.commit()

    run_units(units, work, store, workers=args.workers, label="panel", lim=lim)
    c.execute("CREATE INDEX IF NOT EXISTS ix_panel ON panel_trades(mkt, tau, ts_ms)")
    c.commit()


def fetch_full(args, c: sqlite3.Connection) -> None:
    lim = Limiter(args.rate, args.max_rate)
    api = Api(lim)
    have = {r[0] for r in c.execute("SELECT mkt FROM done WHERE kind='full'")}
    info = dict(c.execute("SELECT mkt, ticker FROM markets").fetchall())
    close = dict(c.execute("SELECT mkt, close_ts FROM markets").fetchall())
    # Random order, so a run cut short still leaves a random subsample of the stratified sample.
    units = [(k, info[k], close[k]) for k in full_order(c, args.seed) if k not in have]
    units = units[: args.max_units] if args.max_units else units
    set_meta(c, "full_tape_seconds_before_close", TAPE_S)
    c.commit()

    until = datetime.fromisoformat(args.full_until).replace(tzinfo=UTC).timestamp() if args.full_until else None

    def work(u):
        k, ticker, close_ts = u
        if until is not None and time.time() >= until:
            return None  # time box reached: the markets fetched so far are a random subset of the sample
        out, blocks, cursor = [], 0, None
        while True:
            p = {"ticker": ticker, "limit": 1000, "min_ts": close_ts // 1000 - TAPE_S}
            if cursor:
                p["cursor"] = cursor
            d = api.get("/markets/trades", p)
            tr = d.get("trades") or []
            rows, b = parse_trades(tr)
            blocks += b
            out.append(rows)
            cursor = d.get("cursor")
            if not cursor or not tr:
                break
        flat = [(k, *r) for page in reversed(out) for r in page]  # pages arrive newest first
        return flat, blocks

    def store(u, res):
        if res is None:
            return
        rows, blocks = res
        c.executemany("INSERT INTO trades VALUES (?,?,?,?,?)", rows)
        c.execute("INSERT OR REPLACE INTO done VALUES ('full', ?, ?, ?)", (u[0], len(rows), blocks))
        c.commit()

    run_units(units, work, store, workers=args.workers, label="full", lim=lim)
    log.info("indexing full tapes")
    c.execute("CREATE INDEX IF NOT EXISTS ix_trades ON trades(mkt, ts_ms)")
    c.commit()


# ------------------------------------------------------------------------------------------------ analysis
CLIP = 0.001  # probabilities are clipped to Kalshi's price range before log loss and logits
EPS = 1e-9
BUCKET_EDGES = (0.0, 0.10, 0.25, 0.35, 0.65, 0.75, 0.90, 1.0 + 1e-9)
BUCKET_NAMES = ("0-10", "10-25", "25-35", "35-65", "65-75", "75-90", "90-100")
BLENDS = (0.25, 0.5, 0.75)  # weight on the market mid; the rest on the model
SCAN_FROM_S, SCAN_TO_S = 780, 120  # strategy scan: close - 13 min <= s < close - 2 min
FILL_MS = 2_000
SLIPS = (0.0, 0.01, 0.02)
R.kalshi_fee = lru_cache(maxsize=None)(kalshi_fee)  # same numbers, cached: rules.py prices the fee on every quote
fee = R.kalshi_fee


def logit(p):
    p = np.clip(np.asarray(p, float), CLIP, 1 - CLIP)
    return np.log(p / (1 - p))


def log_loss(p, y):
    p = np.clip(np.asarray(p, float), CLIP, 1 - CLIP)
    return -(y * np.log(p) + (1 - y) * np.log1p(-p))


def brier(p, y):
    return (np.asarray(p, float) - y) ** 2


def fit_logit(X: np.ndarray, y: np.ndarray, b0: np.ndarray | None = None) -> np.ndarray:
    """Logistic regression by Newton steps; X carries the intercept column."""
    b = np.zeros(X.shape[1]) if b0 is None else b0.astype(float).copy()
    for _ in range(100):
        p = expit(X @ b)
        h = (X * (p * (1 - p))[:, None]).T @ X + 1e-9 * np.eye(X.shape[1])
        step = np.linalg.solve(h, X.T @ (y - p))
        big = np.abs(step).max()
        b += step if big < 5 else step * (5 / big)
        if big < 1e-10:
            break
    return b


def tstat(x) -> float:
    x = np.asarray(x, float)
    return float(x.mean() / (x.std(ddof=1) / math.sqrt(len(x)))) if len(x) > 2 and x.std(ddof=1) > 0 else float("nan")


class Window:
    """One market's recorded index seconds and our own open/close averages, through the app's own functions."""

    def __init__(self, open_ts: int, close_ts: int, ts: np.ndarray, px: np.ndarray, lt: np.ndarray) -> None:
        lo, hi = np.searchsorted(ts, [open_ts - HISTORY_MS - 65_000, close_ts + 1_000])
        t, p, la = ts[lo:hi].tolist(), px[lo:hi].tolist(), lt[lo:hi].tolist()
        self.rows = list(zip(t, p, strict=True))
        self.close = close_ts

        def avg(end: int) -> float | None:
            a, b = np.searchsorted(ts[lo:hi], [end - 60_000, end])
            return window_average([IndexSecond(t[i], p[i], la[i]) for i in range(a, b)], end)
        self.open_avg, self.close_avg = avg(open_ts), avg(close_ts)

    def fair(self, at: int) -> dict | None:
        """The Coach's fair odds at `at`, from index seconds strictly before `at` (callers pass t - 1s)."""
        if self.open_avg is None:
            return None
        return fair_from_index(at, self.rows, self.open_avg, self.close)

    def z(self, f: dict, at: int) -> float:
        """The model's z = Phi^-1(fair_up) in closed form (no closing second observed yet: at < close - 60s)."""
        close_start = self.close - 60_000
        n, g = 60, (close_start - at) / 1000 + 1
        v = n * n * g + (n - 1) * n * (2 * n - 1) / 6
        return (n * f["price"] - n * self.open_avg) / (f["sigma"] * math.sqrt(v))


class Tape:
    """Prints split by taker side, chronological, prices in YES terms."""

    def __init__(self, ts: np.ndarray, yp: np.ndarray, ty: np.ndarray) -> None:
        y = ty == 1
        self.side = {True: (ts[y], yp[y]), False: (ts[~y], yp[~y])}
        self.n = len(ts)

    def last(self, taker_yes: bool, t, lookback: int = LOOKBACK_MS):
        """(price, age_ms) of the latest print by that taker side with t - lookback <= ts < t; NaN when none."""
        st, sp = self.side[taker_yes]
        t = np.atleast_1d(np.asarray(t, np.int64))
        if not len(st):
            return np.full(len(t), np.nan), np.full(len(t), np.nan)
        i = np.searchsorted(st, t, "left") - 1
        ii = np.clip(i, 0, None)
        ok = (i >= 0) & (st[ii] >= t - lookback)
        return np.where(ok, sp[ii], np.nan), np.where(ok, (t - st[ii]).astype(float), np.nan)

    def fill(self, side: str, s: int, limit: float) -> tuple[int, float] | None:
        """First taker-buys print for `side` in [s, s + 2s) at or under `limit` (side terms)."""
        st, sp = self.side[side == "UP"]
        i0, i1 = np.searchsorted(st, [s, s + FILL_MS], "left")
        for j in range(i0, i1):
            p = round(float(sp[j]) if side == "UP" else 1.0 - float(sp[j]), 4)
            if p <= limit + EPS:
                return int(st[j]), p
        return None


def quote_at(ya: float, yb: float) -> dict:
    """Quote dict for rules.py from print proxies: YES ask = taker-YES print, YES bid = taker-NO print (YES terms)."""
    q = {"yes_ask": None, "no_ask": None, "yes_bid": None, "no_bid": None}
    ya, yb = float(ya), float(yb)  # plain floats: kalshi_fee parses repr(price)
    if ya == ya:
        q["yes_ask"], q["no_bid"] = round(ya, 4), round(1 - ya, 4)
    if yb == yb:
        q["yes_bid"], q["no_ask"] = round(yb, 4), round(1 - yb, 4)
    return q


def old_rule_side(f: float, q: dict) -> str | None:
    """rules.py before 13d602a: no blend, no longshot floor or zone, the larger edge after the buy fee if >= 5c."""
    ya, na = q["yes_ask"], q["no_ask"]
    if not (R._ok(ya) and R._ok(na)):
        return None
    edges = {"UP": f - ya - fee(1, ya), "DOWN": (1 - f) - na - fee(1, na)}
    side = max(edges, key=lambda s: edges[s])
    return side if edges[side] >= R.MIN_EDGE - EPS else None


def simulate(win: Window, tape: Tape, result: str) -> dict:
    """Pre-registered strategy test on one window: both rules, one fill at most, exits (i) and (ii)."""
    close = win.close
    secs = np.arange(close - SCAN_FROM_S * 1000, close, 1000, dtype=np.int64)
    fair = {}
    for s in secs.tolist():
        f = win.fair(s - MODEL_LAG_MS)
        fair[s] = None if f is None else f["fair_up"]
    ya, _ = tape.last(True, secs)
    yb, _ = tape.last(False, secs)
    quotes = {s: quote_at(a, b) for s, a, b in zip(secs.tolist(), ya.tolist(), yb.tolist(), strict=True)}
    out = {}
    for rule in ("live", "old"):
        conf = R.Confirmation()
        out[rule] = None
        for s in secs.tolist():
            if s >= close - SCAN_TO_S * 1000:
                break
            f, q = fair[s], quotes[s]
            side, used = None, None
            if f is not None:
                if rule == "live":
                    d = R.entry_decision(f, q, s, close)
                    if isinstance(d, R.Setup):
                        side, used = d.side, d.fair_used
                else:
                    side = old_rule_side(f, q)
            if not conf.observe(side, s):
                continue
            ask = q["yes_ask"] if side == "UP" else q["no_ask"]
            limit = R.entry_limit(ask, R.side_fair(used, side)) if rule == "live" else ask
            conf.observe(None, s)  # a new proposal needs its own confirmation, as in the live Coach
            got = tape.fill(side, s, limit)
            if got is None:
                continue
            fill_ts, px = got
            won = (side == "UP") == (result == "yes")
            mid = R.market_mid(q)
            out[rule] = {"side": side, "decided": s, "fill_ts": fill_ts, "px": px, "ask": ask, "limit": limit,
                         "fair_up": f, "mid": mid, "won": int(won), "secs_left": (close - s) / 1000,
                         "exit": live_exit(win, tape, fair, side, px, f, fill_ts)}
            break
    return out


def live_exit(win: Window, tape: Tape, fair: dict, side: str, px: float, f_entry: float, fill_ts: int) -> dict:
    """Secondary exit: the live exit_advice each second after the fill on the held side's bid proxy."""
    close = win.close
    setup = R.Setup(side, px, f_entry, round(max(R.STOP_FLOOR, px - R.STOP_GAP), 2), close - R.TIME_RULE_MS, close,
                    entry_ts=fill_ts)
    for t in range(fill_ts // 1000 * 1000 + 1000, close, 1000):
        f = fair.get(t)
        if f is None:
            continue
        if side == "UP":
            b, _ = tape.last(False, t)
            bid = None if b[0] != b[0] else round(float(b[0]), 4)
        else:
            a, _ = tape.last(True, t)
            bid = None if a[0] != a[0] else round(1 - float(a[0]), 4)
        adv = R.exit_advice(setup, bid, f, t)
        if adv.action == "exit":
            return {"reason": adv.reason, "bid": bid, "ts": t}
    return {"reason": "settled", "bid": None, "ts": close}


def trade_pnl(tr: dict, slip: float, exit_rule: str) -> float:
    paid = min(0.99, round(tr["px"] + slip, 4))
    cost = paid + fee(1, paid)
    if exit_rule == "hold" or tr["exit"]["reason"] == "settled":
        return tr["won"] - cost
    sale = max(0.0, round(tr["exit"]["bid"] - slip, 4))
    return sale - cost - (fee(1, sale) if sale > 0 else 0.0)


def md_table(header: list[str], rows: list[list]) -> str:
    def cell(v):
        if isinstance(v, float):
            return "n/a" if v != v else f"{v:.4f}" if abs(v) < 1 else f"{v:.2f}"
        return str(v)
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(cell(v) for v in r) + " |" for r in rows]
    return "\n".join(out)


def load_index(app: sqlite3.Connection, lo: int, hi: int):
    a = np.array(app.execute("SELECT ts, price, last_trade_ts FROM index_1s WHERE ts >= ? AND ts < ? ORDER BY ts",
                             (lo, hi)).fetchall(), dtype=np.float64)
    return a[:, 0].astype(np.int64), a[:, 1], a[:, 2].astype(np.int64)


def load_tapes(c: sqlite3.Connection, table: str, keep: set[int]) -> dict[int, Tape]:
    """All prints of `table` for the markets in `keep`, one table scan (no index needed), as chronological Tapes.
    Rows of one market were inserted in time order, so a stable sort on ts keeps same-millisecond prints in order."""
    parts, cur = [], c.execute(f"SELECT mkt, ts_ms, yes_price, taker_yes FROM {table}")  # noqa: S608 - table name is a literal from the caller
    while True:
        chunk = cur.fetchmany(1_000_000)
        if not chunk:
            break
        parts.append(np.array(chunk, dtype=np.float64))
    if not parts:
        return {}
    a = np.concatenate(parts)
    a = a[np.isin(a[:, 0], np.fromiter(keep, dtype=np.float64, count=len(keep)))]
    a = a[np.lexsort((a[:, 1], a[:, 0]))]  # by market, then time; lexsort is stable
    mk, starts = np.unique(a[:, 0], return_index=True)
    ends = list(starts[1:]) + [len(a)]
    return {int(m): Tape(a[s:e, 1].astype(np.int64), a[s:e, 2], a[s:e, 3].astype(np.int64))
            for m, s, e in zip(mk, starts, ends, strict=True)}


def analyze(args, c: sqlite3.Connection) -> None:
    import pandas as pd

    t_start = time.time()
    out = Path(args.out_dir)
    rng = np.random.default_rng(args.seed + 3)
    mk = pd.read_sql("SELECT * FROM markets ORDER BY open_ts", c)
    days = sorted(mk.day.unique())
    n_hold = int(round(len(days) / 3))
    train_days, hold_days = days[: len(days) - n_hold], days[len(days) - n_hold:]
    half1 = set(days[: len(days) // 2])
    split = {d: ("train" if d in train_days else "holdout") for d in days}
    done = {(k, m) for k, m in c.execute("SELECT kind, mkt FROM done")}
    full_ok = {m for k, m in done if k == "full"}
    panel_ok = {m for k, m in done if k == "panel"}
    candles = {(m, e): (b, a) for m, e, b, a in c.execute("SELECT mkt, end_ts, yes_bid, yes_ask FROM candles")}
    full_tapes, panel_tapes = load_tapes(c, "trades", full_ok), load_tapes(c, "panel_trades", panel_ok)
    log.info("analyze: loaded %d full tapes and %d direct panels", len(full_tapes), len(panel_tapes))
    app = app_db(Path(args.app_db))
    ts, px, lt = load_index(app, int(mk.open_ts.min()) - HISTORY_MS - 120_000, int(mk.close_ts.max()) + 60_000)
    log.info("analyze: %d markets, %d days (train %s..%s, holdout %s..%s), %d full tapes, %d direct panels, "
             "%d index seconds", len(mk), len(days), train_days[0], train_days[-1], hold_days[0], hold_days[-1],
             len(full_ok), len(panel_ok), len(ts))

    panel, fid, sims, check = [], [], [], []
    for k, m in enumerate(mk.itertuples(index=False)):
        win = Window(m.open_ts, m.close_ts, ts, px, lt)
        y = 1 if m.result == "yes" else 0
        fid.append({"mkt": m.mkt, "day": m.day, "open_avg": win.open_avg, "close_avg": win.close_avg,
                    "floor_strike": m.floor_strike, "expiration_value": m.expiration_value, "y": y})
        tape = full_tapes.get(m.mkt) if m.mkt in full_ok else panel_tapes.get(m.mkt)
        if m.mkt in full_ok and m.mkt in panel_ok:  # both routes: they must give the same quote proxies
            direct = panel_tapes[m.mkt]
            taus_ms = np.array([m.close_ts - tau * 60_000 for tau in TAUS])
            for side in (True, False):
                a, _ = tape.last(side, taus_ms)
                b, _ = direct.last(side, taus_ms)
                check += [bool((x == z) or (x != x and z != z)) for x, z in zip(a, b, strict=True)]
        for tau in TAUS:
            tau_ms = m.close_ts - tau * 60_000
            f = win.fair(tau_ms - MODEL_LAG_MS)
            row = {"mkt": m.mkt, "ticker": m.ticker, "day": m.day, "split": split[m.day], "half": 1 if m.day in half1 else 2,
                   "hour": datetime.fromtimestamp(tau_ms / 1000, tz=UTC).hour, "tau": tau, "y": y,
                   "sampled": m.sampled, "fair": np.nan, "S": np.nan, "sigma": np.nan, "z": np.nan,
                   "ask": np.nan, "bid": np.nan, "ask_age": np.nan, "bid_age": np.nan, "cmid": np.nan,
                   "have_tape": tape is not None}
            if f is not None:
                row.update(fair=f["fair_up"], S=f["price"], sigma=f["sigma"], z=win.z(f, tau_ms - MODEL_LAG_MS))
            if tape is not None:
                a, aa = tape.last(True, tau_ms)
                b, ba = tape.last(False, tau_ms)
                row.update(ask=a[0], ask_age=aa[0], bid=b[0], bid_age=ba[0])
            cb = candles.get((m.mkt, tau_ms))
            if cb and cb[0] is not None and cb[1] is not None and 0 < cb[0] <= cb[1] < 1:
                row["cmid"] = (cb[0] + cb[1]) / 2
            panel.append(row)
        if m.mkt in full_ok and win.open_avg is not None:
            res = simulate(win, tape, m.result)
            for rule, tr in res.items():
                if tr is not None:
                    sims.append({"mkt": m.mkt, "ticker": m.ticker, "day": m.day, "split": split[m.day],
                                 "half": 1 if m.day in half1 else 2, "rule": rule, **{x: v for x, v in tr.items() if x != "exit"},
                                 "exit_reason": tr["exit"]["reason"], "exit_bid": tr["exit"]["bid"],
                                 "exit_ts": tr["exit"]["ts"],
                                 **{f"pnl_hold_{int(s * 100)}c": trade_pnl(tr, s, "hold") for s in SLIPS},
                                 **{f"pnl_exit_{int(s * 100)}c": trade_pnl(tr, s, "exit") for s in SLIPS}})
        if (k + 1) % 250 == 0:
            log.info("analyze: %d/%d markets, %.1f min", k + 1, len(mk), (time.time() - t_start) / 60)

    P = pd.DataFrame(panel)
    P["both"] = P.ask.notna() & P.bid.notna()
    P["single"] = P.ask.notna() ^ P.bid.notna()
    P["mid"] = np.where(P.both, (P.ask + P.bid) / 2, np.where(P.ask.notna(), P.ask, P.bid))
    zc = P.dropna(subset=["z"])
    z_err = float(np.abs(ndtr(zc.z) - zc.fair).max()) if len(zc) else 0.0
    log.info("analyze: closed-form z reproduces fair_from_index to %.2e", z_err)
    assert z_err < 1e-6, z_err
    F = pd.DataFrame(fid)
    S = pd.DataFrame(sims)
    P.to_csv(out / "panel.csv", index=False)
    S.to_csv(out / "strategy_trades.csv", index=False)
    md, js = [], {"days": days, "train_days": train_days, "holdout_days": hold_days,
                  "full_tapes": len(full_ok), "markets": len(mk)}

    # ---- data coverage
    md.append("## Data coverage\n")
    md.append(f"Markets: {len(mk)} settled KXBTC15M windows, {days[0]} to {days[-1]} ({len(days)} UTC days; "
              f"train {train_days[0]} to {train_days[-1]}, holdout {hold_days[0]} to {hold_days[-1]}). "
              f"Excluded results: {get_meta(c, 'markets_excluded')}. App cross-check: {get_meta(c, 'app_crosscheck')}.")
    nfull = c.execute("SELECT count(*), coalesce(sum(n), 0), coalesce(sum(blocks), 0) FROM done WHERE kind='full'").fetchone()
    npan = c.execute("SELECT count(*), coalesce(sum(n), 0), coalesce(sum(blocks), 0) FROM done WHERE kind='panel'").fetchone()
    md.append(f"\nFull tapes (last {TAPE_S}s of each window): {nfull[0]} markets, {nfull[1]:,} prints "
              f"({nfull[1] / max(nfull[0], 1):,.0f} per market), {nfull[2]} block trades dropped. Direct panel fetches: "
              f"{npan[0]} markets, {npan[1]:,} prints, {npan[2]} block trades dropped.")
    if check:
        md.append(f"\nRoute check on {len(full_ok & panel_ok)} markets with both a full tape and a direct panel fetch: "
                  f"{sum(check)}/{len(check)} quote proxies identical.")
    js["route_check"] = [sum(check), len(check)]
    cov = []
    for tau in TAUS:
        q = P[P.tau == tau]
        used = q[q.fair.notna() & q.mid.notna()]
        cov.append([tau, len(q), int(q.have_tape.sum()), int(q.fair.notna().sum()), int(q.both.sum()),
                    int(q.single.sum()), int((q.have_tape & q.mid.isna()).sum()), len(used),
                    float(q.ask_age.median() / 1000), float(q.bid_age.median() / 1000), int(q.cmid.notna().sum())])
    md.append("\n" + md_table(["tau (min)", "markets", "prints fetched", "model ok", "two-sided mid", "one-sided mid",
                               "no print in 30s", "used (model+mid)", "median ask-print age (s)",
                               "median bid-print age (s)", "candle mid"], cov))
    js["coverage"] = cov

    # ---- A. information
    md.append("\n## A. Information\n")
    a_rows, all_rows, combo_rows, verdict_info, js["A"] = [], [], [], {}, {}
    for tau in TAUS:
        q = P[(P.tau == tau) & P.fair.notna() & P.mid.notna()].reset_index(drop=True)
        yv = q.y.to_numpy(float)
        h = (q.split == "holdout").to_numpy()
        if h.sum() < 30 or (~h).sum() < 30:
            log.warning("tau %d: too few rows (train %d, holdout %d); skipped", tau, (~h).sum(), h.sum())
            continue
        res = {}
        for name, p in [("market mid", q.mid.to_numpy()), ("model", q.fair.to_numpy())] + \
                       [(f"blend w={w}", w * q.mid.to_numpy() + (1 - w) * q.fair.to_numpy()) for w in BLENDS]:
            res[name] = (float(brier(p[h], yv[h]).mean()), float(log_loss(p[h], yv[h]).mean()),
                         float(log_loss(p, yv).mean()), float(brier(p, yv).mean()))
        Xm = np.column_stack([np.ones(len(q)), logit(q.mid)])
        Xc = np.column_stack([Xm, logit(q.fair)])
        tr = ~h
        bm, bc = fit_logit(Xm[tr], yv[tr]), fit_logit(Xc[tr], yv[tr])
        llm = log_loss(expit(Xm[h] @ bm), yv[h])
        llc = log_loss(expit(Xc[h] @ bc), yv[h])
        llraw_all = log_loss(q.mid.to_numpy(), yv)
        llraw = llraw_all[h]
        diff, diff_raw = float(llc.mean() - llm.mean()), float(llc.mean() - llraw.mean())
        tr_groups = [np.flatnonzero(tr & (q.day == d).to_numpy()) for d in train_days]
        ho_groups = [np.flatnonzero(h & (q.day == d).to_numpy()) for d in hold_days]
        tr_groups, ho_groups = [g for g in tr_groups if len(g)], [g for g in ho_groups if len(g)]
        bd, bdr, bcoef = np.empty(args.boot), np.empty(args.boot), np.empty(args.boot)
        for b in range(args.boot):
            ti = np.concatenate([tr_groups[i] for i in rng.integers(0, len(tr_groups), len(tr_groups))])
            hi = np.concatenate([ho_groups[i] for i in rng.integers(0, len(ho_groups), len(ho_groups))])
            bmb, bcb = fit_logit(Xm[ti], yv[ti], bm), fit_logit(Xc[ti], yv[ti], bc)
            lc = log_loss(expit(Xc[hi] @ bcb), yv[hi]).mean()
            bd[b] = lc - log_loss(expit(Xm[hi] @ bmb), yv[hi]).mean()
            bdr[b] = lc - llraw_all[hi].mean()
            bcoef[b] = bcb[2]
        ci, cir, cic = np.percentile(bd, [2.5, 97.5]), np.percentile(bdr, [2.5, 97.5]), np.percentile(bcoef, [2.5, 97.5])
        bh = fit_logit(Xc[h], yv[h])
        passed = bool(ci[1] < 0 and cic[0] > 0 and diff < 0)
        verdict_info[tau] = passed
        names = ["market mid", "model"] + [f"blend w={w}" for w in BLENDS]
        a_rows.append([tau, int(h.sum())] + [res[k][1] for k in names] + [res[k][0] for k in names])
        all_rows.append([tau, len(q)] + [res[k][2] for k in names] + [res[k][3] for k in names])
        combo_rows.append([tau, int(tr.sum()), int(h.sum()), float(bm[1]), float(bc[1]), float(bc[2]),
                           f"[{cic[0]:.3f}, {cic[1]:.3f}]", float(bh[2]), diff * 1000, f"[{ci[0] * 1000:.2f}, {ci[1] * 1000:.2f}]",
                           diff_raw * 1000, f"[{cir[0] * 1000:.2f}, {cir[1] * 1000:.2f}]", "PASS" if passed else "fail"])
        js["A"][tau] = {"n_train": int(tr.sum()), "n_hold": int(h.sum()), "scores_holdout": res,
                        "market_only": bm.tolist(), "combo": bc.tolist(), "combo_holdout_refit": bh.tolist(),
                        "coef_model_ci": cic.tolist(), "ll_diff": diff, "ll_diff_ci": ci.tolist(),
                        "ll_diff_vs_raw": diff_raw, "ll_diff_vs_raw_ci": cir.tolist(), "pass": passed}
    md.append("Holdout days. Log loss (LL; lower is better; probabilities clipped to [0.001, 0.999]) and Brier. "
              "Blend w = fixed weight on the market mid, the rest on the model (nothing fitted).\n")
    hdr = ["market", "model"] + [f"blend {w}" for w in BLENDS]
    md.append(md_table(["tau", "n"] + [f"LL {x}" for x in hdr] + [f"Brier {x}" for x in hdr], a_rows))
    md.append("\nAll days (nothing here is fitted, so every day counts):\n")
    md.append(md_table(["tau", "n"] + [f"LL {x}" for x in hdr] + [f"Brier {x}" for x in hdr], all_rows))
    js["A_all_days"] = all_rows
    md.append("\nLogistic combo outcome ~ a + b1 logit(mid) + b2 logit(model), fit on train days, scored on holdout days. "
              "dLL = combo minus the market-only logistic (nested benchmark; negative = the model adds information), "
              "in thousandths of a nat per window, with the joint day-bootstrap 95% CI. Also shown against the raw mid.\n")
    md.append(md_table(["tau", "n train", "n hold", "market-only slope", "combo b1 (mid)", "combo b2 (model)",
                        "b2 95% CI", "b2 refit on holdout", "dLL x1000", "95% CI", "dLL vs raw mid x1000", "95% CI",
                        "criterion"], combo_rows))
    js["verdict_info"] = verdict_info

    # robustness: the same combo with the Kalshi top-of-book candle mid at tau instead of print proxies
    rob = []
    for tau in TAUS:
        q = P[(P.tau == tau) & P.fair.notna() & P.cmid.notna()].reset_index(drop=True)
        yv, h = q.y.to_numpy(float), (q.split == "holdout").to_numpy()
        if h.sum() < 30 or (~h).sum() < 30:
            continue
        Xm = np.column_stack([np.ones(len(q)), logit(q.cmid)])
        Xc = np.column_stack([Xm, logit(q.fair)])
        bm, bc = fit_logit(Xm[~h], yv[~h]), fit_logit(Xc[~h], yv[~h])
        d = log_loss(expit(Xc[h] @ bc), yv[h]) - log_loss(expit(Xm[h] @ bm), yv[h])
        hd = q.day[h].to_numpy()
        per_day = np.array([d[hd == dd].sum() for dd in hold_days if (hd == dd).any()])
        cnt = np.array([(hd == dd).sum() for dd in hold_days if (hd == dd).any()])
        bs = []
        for _ in range(args.boot):
            i = rng.integers(0, len(per_day), len(per_day))
            bs.append(per_day[i].sum() / cnt[i].sum())
        lo, hi = np.percentile(bs, [2.5, 97.5])
        rob.append([tau, int(h.sum()), float(log_loss(q.cmid[h], yv[h]).mean()), float(log_loss(q.fair[h], yv[h]).mean()),
                    float(bc[2]), float(d.mean() * 1000), f"[{lo * 1000:.2f}, {hi * 1000:.2f}]"])
    md.append("\nRobustness (not part of the criteria): Kalshi's own top of book at tau (1-minute candle close) instead "
              "of print proxies. CI resamples holdout days only, coefficients fixed from train.\n")
    md.append(md_table(["tau", "n hold", "LL candle mid", "LL model", "combo b2 (model)", "dLL x1000", "95% CI"], rob))
    js["A_candle"] = rob

    # calibration tables (all days, pooled over taus)
    Q = P[P.fair.notna() & P.mid.notna()]
    for by, other, label in (("mid", "fair", "market mid"), ("fair", "mid", "model")):
        rows = []
        for i, name in enumerate(BUCKET_NAMES):
            s = Q[(Q[by] >= BUCKET_EDGES[i]) & (Q[by] < BUCKET_EDGES[i + 1])]
            if len(s):
                rows.append([name, len(s), float(s[by].mean()), float(s[other].mean()), float(s.y.mean())])
        md.append(f"\nCalibration by {label} bucket (cents), all days, all taus pooled:\n")
        md.append(md_table(["bucket", "n", f"avg {label}", f"avg {'model' if by == 'mid' else 'market mid'}", "went Up"], rows))
    # calibration split early / late taus (by market mid)
    for lab, taus in (("12-6 min", (12, 10, 8, 6)), ("4-1 min", (4, 3, 2, 1))):
        rows = []
        for i, name in enumerate(BUCKET_NAMES):
            s = Q[Q.tau.isin(taus) & (Q.mid >= BUCKET_EDGES[i]) & (Q.mid < BUCKET_EDGES[i + 1])]
            if len(s):
                rows.append([name, len(s), float(s.mid.mean()), float(s.fair.mean()), float(s.y.mean())])
        md.append(f"\nBy market-mid bucket, taus {lab}:\n")
        md.append(md_table(["bucket", "n", "avg market mid", "avg model", "went Up"], rows))

    # disagreement subset
    dis = []
    for thr in (0.05, 0.10):
        for tau in TAUS + ("all",):
            s = Q[(Q.fair - Q.mid).abs() >= thr] if tau == "all" else Q[(Q.tau == tau) & ((Q.fair - Q.mid).abs() >= thr)]
            if not len(s):
                continue
            sg = np.sign(s.fair - s.mid)
            right = ((sg > 0) & (s.y == 1)) | ((sg < 0) & (s.y == 0))
            edge = sg * (s.y - s.mid)
            m_ = edge.mean()
            g_ = (edge - m_).groupby(s.mkt).sum()  # window-clustered standard error of the mean
            se = math.sqrt(float((g_ ** 2).sum())) / len(edge)
            bm_, bk_ = float(brier(s.fair, s.y).mean()), float(brier(s.mid, s.y).mean())
            dis.append([f"{int(thr * 100)}c", tau, len(s), float(right.mean()), float(m_ * 100),
                        float(m_ / se) if se > 0 else float("nan"), bm_, bk_, "market" if bk_ < bm_ else "model"])
    md.append("\nDisagreement subset (all days): 'outcome on model side' = the window went the way the model leaned relative to the mid. "
              "Edge at mid = average of sign(model - mid) x (outcome - mid) in cents, before fees and spread; t clustered "
              "by window.\n")
    md.append(md_table(["|model - mid| >=", "tau", "n", "outcome on model side", "edge at mid (c)", "t", "Brier model",
                        "Brier market", "more accurate"], dis))
    js["disagreement"] = dis

    # ---- B. index fidelity
    md.append("\n## B. Index fidelity\n")
    F = F[F.expiration_value.notna()]
    e_close = (F.close_avg - F.expiration_value).dropna()
    e_open = (F.open_avg - F.floor_strike).dropna()

    def dist(e):
        a = e.abs()
        return [len(e), float(e.mean()), float(e.median()), float(e.quantile(0.05)), float(e.quantile(0.95)),
                float(a.median()), float(a.quantile(0.9)), float(a.quantile(0.99)), float(a.max())]
    md.append(md_table(["error ($)", "n", "mean", "median", "p5", "p95", "|e| median", "|e| p90", "|e| p99", "|e| max"],
                       [["our close avg - official settlement value"] + dist(e_close),
                        ["our open avg - Kalshi floor_strike"] + dist(e_open)]))
    G = F.dropna(subset=["open_avg", "close_avg"]).copy()
    G["ours"] = (G.close_avg >= G.open_avg).astype(int)
    G["ours_vs_k"] = (G.close_avg >= G.floor_strike).astype(int)
    G["dist"] = (G.expiration_value - G.floor_strike).abs()
    G["kalshi_consistent"] = ((G.expiration_value >= G.floor_strike).astype(int) == G.y)
    rows = []
    for lab, s in (("all", G), ("|official close - strike| < $25", G[G.dist < 25]),
                   ("< $10", G[G.dist < 10]), ("< $5", G[G.dist < 5])):
        rows.append([lab, len(s), float((s.ours != s.y).mean()), int((s.ours != s.y).sum()),
                     float((s.ours_vs_k != s.y).mean())])
    md.append("\nOutcome mismatches with Kalshi's official result. 'Ours' = our close avg >= our open avg (what the model "
              "forecasts); 'our close vs Kalshi strike' isolates the close-side error.\n")
    md.append(md_table(["subset", "n", "ours != Kalshi (rate)", "count", "our close vs Kalshi strike != Kalshi"], rows))
    md.append(f"\nKalshi's result agrees with expiration_value >= floor_strike in {int(G.kalshi_consistent.sum())}/{len(G)} "
              f"windows. Windows without our open or close average (index coverage rule): "
              f"{int(F.open_avg.isna().sum())} open, {int(F.close_avg.isna().sum())} close.")
    js["B"] = {"close_err": dist(e_close), "open_err": dist(e_open), "mismatch": rows}

    # ---- C. volatility
    md.append("\n## C. Volatility: ours vs market-implied\n")
    V = Q[(Q.mid >= 0.05) & (Q.mid <= 0.95) & Q.z.notna()].copy()
    V["zm"] = ndtri(V.mid.clip(CLIP, 1 - CLIP))
    V["same_side"] = np.sign(V.zm) == np.sign(V.z)
    W = V[V.same_side & (V.zm.abs() >= 0.1) & (V.z.abs() > 1e-6)].copy()
    W["ratio"] = W.zm / W.z  # = sigma_ours / sigma_implied
    W["rv"] = W.sigma / W.S
    W["rv_t"] = pd.qcut(W.rv, 3, labels=["low", "mid", "high"]) if len(W) >= 3 else "mid"

    def vrow(lab, s):
        r = s.ratio
        return [lab, len(s), float(r.median()), float(r.quantile(0.25)), float(r.quantile(0.75)), float((r < 1).mean())]
    vt = [vrow(t, W[W.tau == t]) for t in TAUS]
    md.append(f"ratio = our sigma / the sigma that makes our model reproduce the market mid (Phi^-1(mid) / our z). "
              f"Below 1 = we assume less volatility than the market prices. Rows with mid in [5c, 95c], |Phi^-1(mid)| >= 0.1, "
              f"model and market on the same side of 50% ({len(W)} of {len(V)}; opposite sides: "
              f"{int((~V.same_side).sum())}).\n")
    md.append(md_table(["tau", "n", "median ratio", "p25", "p75", "share < 1"], vt))
    hb = [vrow(f"{h:02d}-{h + 3:02d} UTC", W[(W.hour >= h) & (W.hour < h + 4)]) for h in range(0, 24, 4)]
    md.append("\nBy hour of day (UTC, all taus):\n")
    md.append(md_table(["hours", "n", "median ratio", "p25", "p75", "share < 1"], hb))
    rb = [vrow(f"{lab} recent vol", W[W.rv_t == lab]) for lab in ("low", "mid", "high")]
    md.append("\nBy our recent realized volatility (terciles of sigma / price):\n")
    md.append(md_table(["tercile", "n", "median ratio", "p25", "p75", "share < 1"], rb))
    # what the outcomes say: the single sigma multiplier k that best fits outcomes, Phi(z / k)
    from scipy.optimize import minimize_scalar
    kf = []
    for tau in TAUS:
        s = Q[(Q.tau == tau) & Q.z.notna()]
        zz, yy = s.z.to_numpy(), s.y.to_numpy(float)
        r_ = minimize_scalar(lambda k, zz=zz, yy=yy: log_loss(ndtr(zz / k), yy).mean(), bounds=(0.3, 3.0), method="bounded")
        kf.append([tau, len(s), float(r_.x)])
    md.append("\nOutcome-fitted sigma multiplier k (Phi(z / k) with the lowest log loss, all days; above 1 = outcomes "
              "were more random than our sigma implied):\n")
    md.append(md_table(["tau", "n", "best k"], kf))
    js["C"] = {"by_tau": vt, "by_hour": hb, "by_rv": rb, "k_fit": kf}

    # ---- D. strategy
    md.append("\n## D. Tradeable P&L (full-tape sample)\n")
    n_sim = int(sum(1 for m in mk.itertuples() if m.mkt in full_ok))
    md.append(f"{n_sim} windows with full tapes. At most one fill per window; fills need a taker print for our side "
              f"within 2s at or under the limit; paid = print + slippage; exact taker fee (1 contract).\n")
    drows, js["D"] = [], {}
    for rule in ("live", "old"):
        for ex in ("hold", "exit"):
            for slip in SLIPS:
                col = f"pnl_{ex}_{int(slip * 100)}c"
                for lab in ("all", "train", "holdout", "half 1", "half 2"):
                    s = S[S.rule == rule] if len(S) else S
                    if len(s):
                        s = s if lab == "all" else s[s.split == lab] if lab in ("train", "holdout") else \
                            s[s.half == (1 if lab == "half 1" else 2)]
                    if not len(s):
                        drows.append([rule, ex, f"{int(slip * 100)}c", lab, 0] + [float("nan")] * 4)
                        continue
                    pnl = s[col].to_numpy()
                    paid = np.minimum(0.99, s.px + slip)
                    be = float((paid + paid.map(lambda p: fee(1, round(p, 4)))).mean())
                    win = float(s.won.mean()) if ex == "hold" else float((pnl > 0).mean())
                    drows.append([rule, ex, f"{int(slip * 100)}c", lab, len(s), win, be, float(pnl.mean() * 100), tstat(pnl)])
                    js["D"][f"{rule}|{ex}|{slip}|{lab}"] = [len(s), win, be, float(pnl.mean() * 100), tstat(pnl)]
    md.append(md_table(["rule", "exit", "slippage", "days", "n", "win %", "avg cost incl. fee (= breakeven win % if held)",
                        "net c/contract", "t (window-clustered)"], drows))
    if len(S):
        ex_mix = S.groupby(["rule", "exit_reason"]).size().unstack(fill_value=0)
        md.append("\nSecondary-exit reasons (count):\n")
        md.append(md_table(["rule"] + list(ex_mix.columns), [[r] + [int(v) for v in ex_mix.loc[r]] for r in ex_mix.index]))
        bys = []
        for rule in ("live", "old"):
            s = S[S.rule == rule]
            for lab, sub in (("UP", s[s.side == "UP"]), ("DOWN", s[s.side == "DOWN"]),
                             ("price < 35c", s[s.px < 0.35]), ("35-65c", s[(s.px >= 0.35) & (s.px <= 0.65)]),
                             ("price > 65c", s[s.px > 0.65])):
                if len(sub):
                    bys.append([rule, lab, len(sub), float(sub.won.mean()), float(sub.px.mean()),
                                float(sub.pnl_hold_1c.mean() * 100), tstat(sub.pnl_hold_1c)])
        md.append("\nBreakdown, hold to settlement at 1c slippage, all days:\n")
        md.append(md_table(["rule", "subset", "n", "win %", "avg print price", "net c/contract", "t"], bys))
    key = "live|hold|0.01|"
    hv = js["D"].get(key + "holdout")
    h1, h2 = js["D"].get(key + "half 1"), js["D"].get(key + "half 2")
    viable = bool(hv and hv[0] > 2 and hv[3] > 0 and hv[4] > 2 and h1 and h2 and h1[3] > 0 and h2[3] > 0)
    js["verdict_taker_viable"] = viable
    passed = [t for t, v in verdict_info.items() if v]
    md.insert(0, f"# Results (generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC)\n\n"
                 f"MODEL ADDS INFORMATION at taus: {passed or 'none'}. TAKER STRATEGY VIABLE: {viable}. "
                 f"Runtime of this analysis: {(time.time() - t_start) / 60:.1f} min.\n")
    (out / "results.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (out / "results.json").write_text(json.dumps(js, indent=1, default=str), encoding="utf-8")
    log.info("analysis written to %s (%.1f min): info pass at %s, taker viable %s", out / "results.md",
             (time.time() - t_start) / 60, passed, viable)


# ------------------------------------------------------------------------------------------------ main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=["fetch-markets", "fetch-candles", "fetch-panel", "fetch-full", "fetch-all",
                                     "analyze", "all"])
    ap.add_argument("--start", default="2026-09-29", help="first UTC day (window open date)")
    ap.add_argument("--end", default="2026-10-28", help="last UTC day, inclusive")
    ap.add_argument("--sample-frac", type=float, default=0.4, help="share of markets per day with full tapes")
    ap.add_argument("--seed", type=int, default=20261028)
    ap.add_argument("--resample", action="store_true", help="redraw the full-tape sample")
    ap.add_argument("--rate", type=float, default=4.2, help="starting requests/second (adaptive)")
    ap.add_argument("--max-rate", type=float, default=4.7, help="the trades endpoint allows ~5/s per IP")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--boot", type=int, default=2000, help="bootstrap replicates")
    ap.add_argument("--max-units", type=int, default=0, help="fetch at most this many markets per step (testing)")
    ap.add_argument("--panel-all", action="store_true", help="fetch the panel directly for sampled markets too")
    ap.add_argument("--panel-missing", action="store_true",
                    help="fetch the panel directly for sampled markets that have no full tape")
    ap.add_argument("--full-until", default="", help="UTC time (e.g. 2026-10-29T03:00) after which fetch-full "
                    "starts no new market; the tapes fetched by then are a random subset of the sample")
    ap.add_argument("--app-db", default=str(ROOT / "data" / "btcflow.db"), help="the app database (opened read-only)")
    ap.add_argument("--out-dir", default=str(ROOT / "data" / "research"))
    args = ap.parse_args()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(out / "run.log", encoding="utf-8")])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    c = research_db(out)
    steps = {"fetch-markets": [fetch_markets], "fetch-candles": [fetch_candles], "fetch-panel": [fetch_panel],
             "fetch-full": [fetch_full], "fetch-all": [fetch_markets, fetch_candles, fetch_panel, fetch_full],
             "analyze": [analyze], "all": [fetch_markets, fetch_candles, fetch_panel, fetch_full, analyze]}[args.step]
    t0 = time.time()
    for fn in steps:
        s = time.time()
        fn(args, c)
        log.info("%s done in %.1f min", fn.__name__, (time.time() - s) / 60)
    log.info("total %.1f min", (time.time() - t0) / 60)


if __name__ == "__main__":
    main()
