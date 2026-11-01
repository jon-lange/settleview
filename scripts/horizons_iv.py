"""Longer horizons (H-A) and implied volatility (H-B): does either give the Coach's model information Kalshi's price
lacks? Read-only research; nothing is ordered.

Pre-registered in docs/research/2026-10-28-longer-horizons-and-implied-vol.md. data/btcflow.db and
data/research/kalshi_trades.db are opened with mode=ro; everything this script writes goes to
data/research/horizons.db, data/research/horizons_*.csv and data/research/horizons_results.md.

Run from the repo root with the project venv:
  python scripts/horizons_iv.py fetch-kalshi    KXBTCD / KXBTC settled markets + candlesticks (<= 1.5 req/s)
  python scripts/horizons_iv.py fetch-deribit   DVOL 1-minute + historical BTC option trades (Deribit public API)
  python scripts/horizons_iv.py analyze         tests -> data/research/horizons_results.md
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sqlite3
import sys
import time
from datetime import datetime, UTC
from pathlib import Path

import httpx
import numpy as np
from scipy.optimize import minimize_scalar
from scipy.special import expit, ndtr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
import model_vs_market as mvm  # noqa: E402  (shared API pacing, Window, logistic fit, scoring helpers)
from btc_signal_engine.domain.coach.fair_value import (  # noqa: E402
    HISTORY_MS, MAX_PRICE_AGE_MS, MIN_VOL_RETURNS, PRICE_SMOOTH_S, SIGMA_FLOOR_FRAC, VOL_MINUTES, fair_from_index)

log = logging.getLogger("horizons_iv")
OUT = ROOT / "data" / "research"
DB = OUT / "horizons.db"
APP_DB = ROOT / "data" / "btcflow.db"
K15_DB = OUT / "kalshi_trades.db"
START, END = "2026-09-29", "2026-10-28"
SERIES = ("KXBTCD", "KXBTC")
HOURLY_TAUS_MIN = (50, 40, 30, 20, 15, 10, 5, 2)
DAILY_TAUS_H = (20, 16, 12, 8, 6, 4, 2, 1)
BAND = {"hourly": 0.03, "daily": 0.08}
SEC_PER_YEAR = 365 * 86_400
DERIBIT = "https://www.deribit.com/api/v2/public/"
DERIBIT_HIST = "https://history.deribit.com/api/v2/public/"
BOOT = 2000

SCHEMA = """
CREATE TABLE IF NOT EXISTS kmarkets(ticker TEXT PRIMARY KEY, event TEXT, series TEXT, open_ts INTEGER, close_ts INTEGER,
  result TEXT, strike_type TEXT, floor_strike REAL, cap_strike REAL, expiration_value REAL, volume REAL);
CREATE TABLE IF NOT EXISTS kevents(event TEXT PRIMARY KEY, series TEXT, horizon TEXT, open_ts INTEGER, close_ts INTEGER,
  spot_open REAL, n_markets INTEGER, n_selected INTEGER);
CREATE TABLE IF NOT EXISTS kcandles(ticker TEXT, period INTEGER, end_ts INTEGER, yes_bid REAL, yes_ask REAL, price REAL,
  volume REAL, PRIMARY KEY(ticker, period, end_ts));
CREATE TABLE IF NOT EXISTS kdone(event TEXT PRIMARY KEY, n_tickers INTEGER, n_candles INTEGER);
CREATE TABLE IF NOT EXISTS dvol(ts INTEGER PRIMARY KEY, open REAL, high REAL, low REAL, close REAL);
CREATE TABLE IF NOT EXISTS opt_trades(trade_id TEXT PRIMARY KEY, ts INTEGER, instrument TEXT, expiry_ts INTEGER,
  strike REAL, cp TEXT, iv REAL, index_price REAL, amount REAL);
CREATE TABLE IF NOT EXISTS opt_done(hour_ts INTEGER PRIMARY KEY, n INTEGER);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


def hdb() -> sqlite3.Connection:
    OUT.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=60)
    c.executescript(SCHEMA)
    return c


def ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=60)


def day_s(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())


def utc_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def fnum(x):
    try:
        return None if x is None or x == "" else float(x)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------------------------------------ Kalshi fetch
def fetch_kalshi(args) -> None:
    c = hdb()
    lim = mvm.Limiter(args.rate, args.rate)
    api = mvm.Api(lim)
    lo, hi = day_s(START), day_s(END) + 86_400
    for series in SERIES:
        if c.execute("SELECT count(*) FROM kmarkets WHERE series=?", (series,)).fetchone()[0] and not args.refetch:
            log.info("%s markets already stored", series)
            continue
        cursor, n = None, 0
        while True:
            p = {"series_ticker": series, "status": "settled", "limit": 1000, "min_close_ts": lo, "max_close_ts": hi}
            if cursor:
                p["cursor"] = cursor
            d = api.get("/markets", p)
            ms = d.get("markets") or []
            rows = []
            for m in ms:
                rows.append((m["ticker"], m["event_ticker"], series, mvm.iso_ms(m["open_time"]), mvm.iso_ms(m["close_time"]),
                             m.get("result") or "", m.get("strike_type"), fnum(m.get("floor_strike")),
                             fnum(m.get("cap_strike")), fnum(m.get("expiration_value")), fnum(m.get("volume_fp"))))
            c.executemany("INSERT OR REPLACE INTO kmarkets VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            c.commit()
            n += len(ms)
            cursor = d.get("cursor")
            if n % 20000 < 1000:
                log.info("%s: %d markets", series, n)
            if not cursor or not ms:
                break
        log.info("%s: %d markets stored", series, n)

    # events: horizon by duration, spot at open from our index, strikes near the money
    app = ro(APP_DB)
    ev = c.execute("SELECT event, series, min(open_ts), max(close_ts), count(*) FROM kmarkets GROUP BY event").fetchall()
    for e, series, o, cl, n in ev:
        dur = (cl - o) / 3_600_000
        horizon = "hourly" if abs(dur - 1) < 1e-6 else "daily" if 20 <= dur <= 30 else "other"
        r = app.execute("SELECT price FROM index_1s WHERE ts < ? AND ts >= ? ORDER BY ts DESC LIMIT 1",
                        (o, o - 60_000)).fetchone()
        spot = r[0] if r else None
        nsel = 0
        if spot is not None and horizon != "other":
            b = BAND[horizon] * spot
            for (fs, cs) in c.execute("SELECT floor_strike, cap_strike FROM kmarkets WHERE event=?", (e,)):
                ks = [k for k in (fs, cs) if k is not None and k > 0]
                nsel += any(abs(k - spot) <= b for k in ks)
        c.execute("INSERT OR REPLACE INTO kevents VALUES (?,?,?,?,?,?,?,?)", (e, series, horizon, o, cl, spot, n, nsel))
    c.commit()
    log.info("events: %s", c.execute("SELECT series, horizon, count(*), sum(n_selected) FROM kevents GROUP BY 1,2").fetchall())

    have = {r[0] for r in c.execute("SELECT event FROM kdone")}
    todo = c.execute("SELECT event, horizon, open_ts, close_ts, spot_open FROM kevents WHERE horizon != 'other' "
                     "AND spot_open IS NOT NULL ORDER BY close_ts").fetchall()
    todo = [t for t in todo if t[0] not in have]
    t0 = time.time()
    for k, (e, horizon, o, cl, spot) in enumerate(todo, 1):
        b = BAND[horizon] * spot
        tick = [t for t, fs, cs, res in c.execute("SELECT ticker, floor_strike, cap_strike, result FROM kmarkets WHERE event=?",
                                                  (e,))
                if res in ("yes", "no") and any(x is not None and x > 0 and abs(x - spot) <= b for x in (fs, cs))]
        period = 1 if horizon == "hourly" else 60
        per = (cl - o) // (period * 60_000) + 2
        step = max(1, 9000 // per)
        rows = []
        for i in range(0, len(tick), step):
            part = tick[i:i + step]
            d = api.get("/markets/candlesticks", {"market_tickers": ",".join(part), "period_interval": period,
                                                  "start_ts": o // 1000, "end_ts": cl // 1000})
            for m in d.get("markets") or []:
                t = m.get("market_ticker") or m.get("ticker")
                for cs in m.get("candlesticks") or []:
                    rows.append((t, period, int(cs["end_period_ts"]) * 1000,
                                 fnum((cs.get("yes_bid") or {}).get("close_dollars")),
                                 fnum((cs.get("yes_ask") or {}).get("close_dollars")),
                                 fnum((cs.get("price") or {}).get("close_dollars")), fnum(cs.get("volume_fp"))))
        c.executemany("INSERT OR REPLACE INTO kcandles VALUES (?,?,?,?,?,?,?)", rows)
        c.execute("INSERT OR REPLACE INTO kdone VALUES (?,?,?)", (e, len(tick), len(rows)))
        c.commit()
        if k % 50 == 0 or k == len(todo):
            el = time.time() - t0
            log.info("candles: %d/%d events, %.0fs, %d requests, %d x 429, ETA %.1f min", k, len(todo), el, lim.n,
                     lim.n429, el / k * (len(todo) - k) / 60)


# ------------------------------------------------------------------------------------------------ Deribit fetch
def deribit_get(cl: httpx.Client, url: str, params: dict) -> dict:
    for attempt in range(10):
        try:
            r = cl.get(url, params=params)
        except httpx.HTTPError as e:
            log.warning("deribit %s: %s", url, e)
            time.sleep(min(30, 2 ** attempt))
            continue
        if r.status_code == 200:
            return r.json()["result"]
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(min(30, 2 ** attempt))
            continue
        raise RuntimeError(f"deribit {url} {r.status_code} {r.text[:300]}")
    raise RuntimeError(f"deribit {url} kept failing")


MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def parse_instrument(name: str):
    m = re.match(r"BTC-(\d{1,2})([A-Z]{3})(\d{2})-(\d+)-([CP])$", name)
    if not m:
        return None
    d, mon, y, k, cp = m.groups()
    exp = datetime(2000 + int(y), MONTHS[mon], int(d), 8, tzinfo=UTC)  # Deribit expiries: 08:00 UTC
    return int(exp.timestamp() * 1000), float(k), cp


def fetch_deribit(args) -> None:
    c = hdb()
    cl = httpx.Client(timeout=30)
    lo, hi = (day_s(START) - 86_400) * 1000, (day_s(END) + 2 * 86_400) * 1000
    # DVOL: pages of 1000 minutes, newest first; `continuation` is the next end_timestamp
    end, n = hi, 0
    while end > lo:
        d = deribit_get(cl, DERIBIT + "get_volatility_index_data",
                        {"currency": "BTC", "start_timestamp": lo, "end_timestamp": end, "resolution": "60"})
        rows = d.get("data") or []
        c.executemany("INSERT OR REPLACE INTO dvol VALUES (?,?,?,?,?)", [tuple(r[:5]) for r in rows])
        n += len(rows)
        nxt = d.get("continuation")
        if not rows or not nxt or nxt >= end:
            break
        end = nxt
        time.sleep(0.25)
    c.commit()
    log.info("dvol: %d minutes stored (%s)", n, c.execute("SELECT min(ts), max(ts), count(*) FROM dvol").fetchone())
    # option trades, hour by hour (history server), paging forward on has_more
    have = {r[0] for r in c.execute("SELECT hour_ts FROM opt_done")}
    hours = [h for h in range(lo, hi, 3_600_000) if h not in have and h < time.time() * 1000]
    t0 = time.time()
    for k, h in enumerate(hours, 1):
        start, got = h, []
        while True:
            d = deribit_get(cl, DERIBIT_HIST + "get_last_trades_by_currency_and_time",
                            {"currency": "BTC", "kind": "option", "start_timestamp": start,
                             "end_timestamp": h + 3_600_000 - 1, "count": 1000, "sorting": "asc"})
            tr = d.get("trades") or []
            got += tr
            time.sleep(0.2)
            if not d.get("has_more") or not tr:
                break
            start = tr[-1]["timestamp"] + (0 if tr[-1]["timestamp"] > start else 1)
            if len(got) > 50_000:
                break
        rows = []
        for t in got:
            p = parse_instrument(t["instrument_name"])
            if p is None or t.get("iv") is None:
                continue
            rows.append((str(t["trade_id"]), t["timestamp"], t["instrument_name"], p[0], p[1], p[2], float(t["iv"]),
                         float(t["index_price"]), float(t["amount"])))
        c.executemany("INSERT OR IGNORE INTO opt_trades VALUES (?,?,?,?,?,?,?,?,?)", rows)
        c.execute("INSERT OR REPLACE INTO opt_done VALUES (?,?)", (h, len(rows)))
        c.commit()
        if k % 100 == 0 or k == len(hours):
            el = time.time() - t0
            log.info("option trades: %d/%d hours, %.0fs, ETA %.1f min", k, len(hours), el, el / k * (len(hours) - k) / 60)
    log.info("option trades: %s", c.execute("SELECT count(*), min(ts), max(ts) FROM opt_trades").fetchone())


# ------------------------------------------------------------------------------------------------ model helpers
class Index:
    """index_1s in arrays; S and sigma exactly as fair_from_index computes them (checked against it in analyze)."""

    def __init__(self, app: sqlite3.Connection) -> None:
        a = np.array(app.execute("SELECT ts, price FROM index_1s ORDER BY ts").fetchall(), dtype=np.float64)
        self.ts, self.px = a[:, 0].astype(np.int64), a[:, 1]

    def s_sigma(self, at: int):
        ts, px = self.ts, self.px
        end = int(np.searchsorted(ts, at, "left"))
        if end == 0 or ts[end - 1] < at - MAX_PRICE_AGE_MS:
            return None
        lo = max(0, end - PRICE_SMOOTH_S)
        rec = px[lo:end][ts[lo:end] >= at - PRICE_SMOOTH_S * 1000]
        price = float(rec.sum() / len(rec))
        marks = at - 60_000 * np.arange(VOL_MINUTES + 1, dtype=np.int64)
        idx = np.searchsorted(ts[:end], marks, "left") - 1
        ok = idx >= 0
        if not ok.all():
            idx = idx[: int(np.argmin(ok))]
        samples = px[idx]
        if len(samples) < 2:
            return None
        rets = np.log(samples[:-1] / samples[1:])
        if len(rets) < MIN_VOL_RETURNS:
            return None
        last = float(px[end - 1])
        sigma = max(last * math.sqrt(float((rets * rets).sum()) / len(rets) / 60), last * SIGMA_FLOOR_FRAC)
        return price, sigma


def sqrt_v(at: int, close: int) -> float:
    g = (close - 60_000 - at) / 1000 + 1
    n = 60
    return math.sqrt(n * n * g + (n - 1) * n * (2 * n - 1) / 6)


class IV:
    def __init__(self, c: sqlite3.Connection) -> None:
        d = np.array(c.execute("SELECT ts, close FROM dvol ORDER BY ts").fetchall(), dtype=np.float64)
        self.dts, self.dv = (d[:, 0].astype(np.int64), d[:, 1]) if len(d) else (np.zeros(0, np.int64), np.zeros(0))
        o = np.array(c.execute("SELECT ts, expiry_ts, strike, iv, index_price FROM opt_trades ORDER BY ts").fetchall(),
                     dtype=np.float64)
        self.o = o if len(o) else np.zeros((0, 5))
        self.ots = self.o[:, 0].astype(np.int64)

    def dvol(self, at: int) -> float:
        i = int(np.searchsorted(self.dts, at - 120_000, "right")) - 1
        if i < 0 or self.dts[i] < at - 600_000:
            return float("nan")
        return float(self.dv[i])

    def short_iv(self, at: int) -> float:
        a, b = np.searchsorted(self.ots, [at - 3_600_000, at], "left")
        w = self.o[a:b]
        if not len(w):
            return float("nan")
        w = w[(w[:, 1] - at >= 2 * 3_600_000) & (np.abs(w[:, 2] / w[:, 4] - 1) <= 0.02)]
        if not len(w):
            return float("nan")
        e = w[:, 1].min()
        w = w[w[:, 1] == e]
        return float(np.median(w[:, 3])) if len(w) >= 3 else float("nan")


def iv_sigma(price, iv_pct):
    return price * np.asarray(iv_pct, float) / 100 / math.sqrt(SEC_PER_YEAR)


# ------------------------------------------------------------------------------------------------ statistics
logit, log_loss, brier, fit_logit = mvm.logit, mvm.log_loss, mvm.brier, mvm.fit_logit


def day_groups(days_arr, mask, day_list):
    return [g for g in (np.flatnonzero(mask & (days_arr == d)) for d in day_list) if len(g)]


def combo_test(mid, model, y, days_arr, hold, train_days, hold_days, rng, boot=BOOT):
    """15-minute-study criterion: nested logistic combo vs market-only, joint day bootstrap."""
    Xm = np.column_stack([np.ones(len(y)), logit(mid)])
    Xc = np.column_stack([Xm, logit(model)])
    tr = ~hold
    bm, bc = fit_logit(Xm[tr], y[tr]), fit_logit(Xc[tr], y[tr])
    diff = float(log_loss(expit(Xc[hold] @ bc), y[hold]).mean() - log_loss(expit(Xm[hold] @ bm), y[hold]).mean())
    tg, hg = day_groups(days_arr, tr, train_days), day_groups(days_arr, hold, hold_days)
    bd, bcoef = np.empty(boot), np.empty(boot)
    for b in range(boot):
        ti = np.concatenate([tg[i] for i in rng.integers(0, len(tg), len(tg))])
        hi = np.concatenate([hg[i] for i in rng.integers(0, len(hg), len(hg))])
        bmb, bcb = fit_logit(Xm[ti], y[ti], bm), fit_logit(Xc[ti], y[ti], bc)
        bd[b] = log_loss(expit(Xc[hi] @ bcb), y[hi]).mean() - log_loss(expit(Xm[hi] @ bmb), y[hi]).mean()
        bcoef[b] = bcb[2]
    ci, cic = np.percentile(bd, [2.5, 97.5]), np.percentile(bcoef, [2.5, 97.5])
    passed = bool(ci[1] < 0 and cic[0] > 0 and diff < 0)
    return {"b_mid": float(bc[1]), "b_model": float(bc[2]), "b_model_ci": cic.tolist(), "dll": diff,
            "dll_ci": ci.tolist(), "pass": passed, "market_slope": float(bm[1])}


def paired_test(p_a, p_b, y, days_arr, hold, hold_days, rng, boot=BOOT):
    """Holdout LL(a) - LL(b), holdout-day bootstrap CI."""
    d = log_loss(p_a[hold], y[hold]) - log_loss(p_b[hold], y[hold])
    hd = days_arr[hold]
    per = np.array([d[hd == x].sum() for x in hold_days if (hd == x).any()])
    cnt = np.array([(hd == x).sum() for x in hold_days if (hd == x).any()])
    bs = np.empty(boot)
    for b in range(boot):
        i = rng.integers(0, len(per), len(per))
        bs[b] = per[i].sum() / cnt[i].sum()
    lo, hi = np.percentile(bs, [2.5, 97.5])
    return float(d.mean()), [float(lo), float(hi)]


def fit_k(z_ours, s_ours, s_iv, y, mask):
    """Scalar k minimising pooled train log loss of Phi(z_ours * s_ours / (k * s_iv))."""
    zr = (z_ours * s_ours / s_iv)[mask]
    yy = y[mask]
    r = minimize_scalar(lambda k: float(log_loss(ndtr(zr / k), yy).mean()), bounds=(0.1, 10), method="bounded")
    return float(r.x)


def implied_sigma_ratio(mid, z_ours):
    """sigma_ours / sigma_market, from z_market = Phi^-1(mid) on the same mean gap: ratio = z_market / z_ours."""
    from scipy.special import ndtri
    zm = ndtri(np.clip(mid, 1e-6, 1 - 1e-6))
    ok = (np.abs(mid - 0.5) >= 0.1) & (np.sign(zm) == np.sign(z_ours)) & (z_ours != 0)
    return zm[ok] / z_ours[ok]


md_table = mvm.md_table


# ------------------------------------------------------------------------------------------------ panels
def kalshi_panel(c, idx: Index, iv: IV):
    """One row per (market, tau) for the hourly/daily KXBTCD/KXBTC markets."""
    import pandas as pd
    ev = pd.read_sql("SELECT e.* FROM kevents e JOIN kdone d USING(event) WHERE horizon != 'other'", c)
    mk = pd.read_sql("SELECT * FROM kmarkets WHERE result IN ('yes','no')", c)
    cand = {}
    for t, _per, e, b, a in c.execute("SELECT ticker, period, end_ts, yes_bid, yes_ask FROM kcandles ORDER BY ticker, end_ts"):
        cand.setdefault(t, ([], [], []))
        cand[t][0].append(e)
        cand[t][1].append(b)
        cand[t][2].append(a)
    cand = {t: (np.array(v[0], np.int64), np.array(v[1], float), np.array(v[2], float)) for t, v in cand.items()}
    rows = []
    for e in ev.itertuples(index=False):
        taus = [x * 60_000 for x in HOURLY_TAUS_MIN] if e.horizon == "hourly" else [x * 3_600_000 for x in DAILY_TAUS_H]
        ms = mk[(mk.event == e.event) & mk.ticker.isin(cand.keys())]
        for tau_ms in taus:
            t = e.close_ts - tau_ms
            at = t - 1000
            ss = idx.s_sigma(at)
            dv = iv.dvol(at)
            siv = iv.short_iv(at)
            sv = sqrt_v(at, e.close_ts)
            for m in ms.itertuples(index=False):
                ce, cb, ca = cand[m.ticker]
                if e.horizon == "hourly":
                    i = int(np.searchsorted(ce, t, "right")) - 1
                    ok = i >= 0 and ce[i] > t - 300_000
                else:
                    i = int(np.searchsorted(ce, t, "left"))
                    ok = i < len(ce) and ce[i] == t
                bid = ask = np.nan
                if ok:
                    bid, ask = cb[i], ca[i]
                two = bool(ok and bid == bid and ask == ask and 0 < bid <= ask < 1)
                r = {"series": e.series, "horizon": e.horizon, "event": e.event, "ticker": m.ticker,
                     "day": utc_day(e.close_ts), "tau": tau_ms // 60_000 if e.horizon == "hourly" else tau_ms // 3_600_000,
                     "y": 1 if m.result == "yes" else 0, "bid": bid, "ask": ask, "two": two,
                     "mid": (bid + ask) / 2 if two else np.nan, "S": np.nan, "sigma": np.nan, "fair": np.nan,
                     "z_fl": np.nan, "z_cap": np.nan, "dvol": dv, "siv": siv, "stype": m.strike_type,
                     "floor": m.floor_strike, "cap": m.cap_strike}
                if ss is not None:
                    S, sg = ss
                    zf = (60 * S - 60 * m.floor_strike) / (sg * sv) if m.floor_strike is not None else np.inf
                    zc = (60 * S - 60 * m.cap_strike) / (sg * sv) if m.cap_strike is not None else -np.inf
                    if m.strike_type == "greater":
                        fair = ndtr(zf)
                    elif m.strike_type == "less":
                        fair = 1 - ndtr(zc)
                    else:
                        fair = ndtr(zf) - ndtr(zc)
                    r.update(S=S, sigma=sg, fair=float(fair), z_fl=zf, z_cap=zc)
                rows.append(r)
    return pd.DataFrame(rows)


def fair_with_sigma(P, sig):
    """Recompute the hourly/daily fair with another sigma (z scales by sigma_ours / sig)."""
    s = P.sigma.to_numpy() / sig
    zf, zc = P.z_fl.to_numpy() * s, P.z_cap.to_numpy() * s
    st = P.stype.to_numpy()
    return np.where(st == "greater", ndtr(zf), np.where(st == "less", 1 - ndtr(zc), ndtr(zf) - ndtr(zc)))


def panel_15m(idx: Index, iv: IV):
    """KXBTC15M panel exactly as the 15-minute study builds it (print-proxy mid, candle mid, our fair and z)."""
    import pandas as pd
    k = ro(K15_DB)
    mk = pd.read_sql("SELECT mkt, ticker, open_ts, close_ts, result, day FROM markets ORDER BY open_ts", k)
    done = k.execute("SELECT kind, mkt FROM done").fetchall()
    panel_ok = {m for kd, m in done if kd == "panel"}
    full_ok = {m for kd, m in done if kd == "full"}
    taus = set(mvm.TAUS)
    last = {}  # (mkt, tau, taker_yes) -> (ts, price)

    def take(mkt, tau, ts, p, ty):
        key = (mkt, tau, ty)
        if key not in last or ts >= last[key][0]:
            last[key] = (ts, p)

    for mkt, tau, ts, p, ty in k.execute(
            "SELECT p.mkt, p.tau, p.ts_ms, p.yes_price, p.taker_yes FROM panel_trades p JOIN markets m USING(mkt) "
            "WHERE p.ts_ms >= m.close_ts - p.tau * 60000 - 30000 AND p.ts_ms < m.close_ts - p.tau * 60000"):
        take(mkt, tau, ts, p, ty)
    from_full = full_ok - panel_ok
    log.info("15m: %d direct panels, %d full tapes used (of %d full)", len(panel_ok), len(from_full), len(full_ok))
    cur = k.execute("SELECT t.mkt, t.ts_ms, t.yes_price, t.taker_yes, m.close_ts FROM trades t JOIN markets m USING(mkt) "
                    "WHERE m.close_ts - t.ts_ms > 60000 AND m.close_ts - t.ts_ms <= 750000 "
                    "AND ((m.close_ts - t.ts_ms - 1) % 60000) < 30000")
    while True:
        ch = cur.fetchmany(500_000)
        if not ch:
            break
        for mkt, ts, p, ty, cl in ch:
            if mkt not in from_full:
                continue
            tau = (cl - ts - 1) // 60000
            if tau in taus:
                take(mkt, tau, ts, p, ty)
    candles = {(m, e): (b, a) for m, e, b, a in k.execute("SELECT mkt, end_ts, yes_bid, yes_ask FROM candles")}
    app = ro(APP_DB)
    ts, px, lt = mvm.load_index(app, int(mk.open_ts.min()) - HISTORY_MS - 120_000, int(mk.close_ts.max()) + 60_000)
    rows, chk = [], []
    for m in mk.itertuples(index=False):
        win = mvm.Window(m.open_ts, m.close_ts, ts, px, lt)
        have = m.mkt in panel_ok or m.mkt in from_full
        for tau in mvm.TAUS:
            tau_ms = m.close_ts - tau * 60_000
            at = tau_ms - mvm.MODEL_LAG_MS
            f = win.fair(at)
            a = last.get((m.mkt, tau, 1))
            b = last.get((m.mkt, tau, 0))
            ask, bid = (a[1] if a else np.nan), (b[1] if b else np.nan)
            mid = (ask + bid) / 2 if a and b else ask if a else bid if b else np.nan
            cb = candles.get((m.mkt, tau_ms))
            cmid = (cb[0] + cb[1]) / 2 if cb and cb[0] is not None and cb[1] is not None and 0 < cb[0] <= cb[1] < 1 else np.nan
            r = {"mkt": m.mkt, "day": m.day, "tau": tau, "y": 1 if m.result == "yes" else 0, "have_tape": have,
                 "mid": mid if have else np.nan, "cmid": cmid, "fair": np.nan, "z": np.nan, "S": np.nan, "sigma": np.nan,
                 "dvol": iv.dvol(at), "siv": iv.short_iv(at)}
            if f is not None:
                r.update(fair=f["fair_up"], S=f["price"], sigma=f["sigma"], z=win.z(f, at))
                if len(chk) < 300:
                    chk.append(idx.s_sigma(at))
                    chk[-1] = (chk[-1], (f["price"], f["sigma"]))
            rows.append(r)
    err = max(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in chk if a is not None)
    log.info("15m: vectorised S/sigma reproduces fair_from_index to %.2e", err)
    assert err < 1e-6, err
    return pd.DataFrame(rows)


# ------------------------------------------------------------------------------------------------ analysis
def score_block(P, pcols: dict, mid_col: str, taus, train_days, hold_days, rng, label: str, md: list, js: dict,
                variants_info: dict | None = None):
    """Holdout LL/Brier tables + combo test for each prob column in pcols (name -> column)."""
    ll_rows, combo_rows, res = [], [], {}
    names = list(pcols)
    for tau in taus:
        q = P[(P.tau == tau) & P[mid_col].notna() & P.fair.notna()]
        q = q.dropna(subset=list(pcols.values())).reset_index(drop=True)
        if not len(q):
            continue
        y = q.y.to_numpy(float)
        h = (q.day.isin(hold_days)).to_numpy()
        da = q.day.to_numpy()
        if h.sum() < 30 or (~h).sum() < 30:
            ll_rows.append([tau, int(h.sum())] + [float("nan")] * (2 * len(names) + 2))
            continue
        mid = q[mid_col].to_numpy()
        row = [tau, int((~h).sum()), int(h.sum()), int(q.day[h].nunique()), float(log_loss(mid[h], y[h]).mean())]
        row += [float(log_loss(q[c].to_numpy()[h], y[h]).mean()) for c in pcols.values()]
        row += [float(brier(mid[h], y[h]).mean())] + [float(brier(q[c].to_numpy()[h], y[h]).mean()) for c in pcols.values()]
        ll_rows.append(row)
        res[tau] = {}
        for nm, col in pcols.items():
            boot = BOOT if nm in ("model", "V2") else 1000
            ct = combo_test(mid, q[col].to_numpy(), y, da, h, train_days, hold_days, rng, boot)
            res[tau][nm] = ct
            combo_rows.append([tau, nm, int(h.sum()), ct["b_mid"], ct["b_model"],
                               f"[{ct['b_model_ci'][0]:.3f}, {ct['b_model_ci'][1]:.3f}]", ct["dll"] * 1000,
                               f"[{ct['dll_ci'][0] * 1000:.2f}, {ct['dll_ci'][1] * 1000:.2f}]",
                               "PASS" if ct["pass"] else "fail"])
    md.append(f"\n#### {label}: holdout log loss and Brier\n")
    md.append(md_table(["tau", "n train", "n hold", "hold days", "LL market"] + [f"LL {n}" for n in names] +
                       ["Brier market"] + [f"Brier {n}" for n in names], ll_rows))
    md.append(f"\n#### {label}: logistic combo vs market-only (dLL x1000, negative = adds information)\n")
    md.append(md_table(["tau", "prob", "n hold", "b1 (mid)", "b2 (model)", "b2 95% CI", "dLL x1000", "95% CI", "criterion"],
                       combo_rows))
    js[label] = {"ll": ll_rows, "combo": res}
    return res


def analyze(args) -> None:
    import pandas as pd
    rng = np.random.default_rng(20261028)
    c = hdb()
    iv = IV(c)
    idx = Index(ro(APP_DB))
    days = [utc_day((day_s(START) + 86_400 * i) * 1000) for i in range(30)]
    train_days, hold_days = days[:20], days[20:]
    md, js = [], {"train_days": train_days, "hold_days": hold_days}
    log.info("index %d s, dvol %d min, option trades %d", len(idx.ts), len(iv.dts), len(iv.o))

    # ---------------- H-A
    P = kalshi_panel(c, idx, iv)
    P = P[P.day.isin(days)].reset_index(drop=True)
    P.to_csv(OUT / "horizons_panel.csv", index=False)
    # cross-check the closed form against fair_from_index on a sample of rows
    app = ro(APP_DB)
    smp = P[P.fair.notna() & (P.stype == "greater")].sample(min(200, int(P.fair.notna().sum())), random_state=1)
    ev_close = dict(c.execute("SELECT event, close_ts FROM kevents").fetchall())
    err = 0.0
    for r in smp.itertuples(index=False):
        close = ev_close[r.event]
        t = close - (r.tau * 60_000 if r.horizon == "hourly" else r.tau * 3_600_000)
        at = t - 1000
        rows = app.execute("SELECT ts, price FROM index_1s WHERE ts >= ? AND ts < ? ORDER BY ts", (at - HISTORY_MS, at)).fetchall()
        f = fair_from_index(at, rows, r.floor, close)
        err = max(err, abs(f["fair_up"] - r.fair))
    log.info("H-A: closed form reproduces fair_from_index to %.2e on %d rows", err, len(smp))
    assert err < 1e-6, err
    js["HA_fair_check"] = err

    md.append("## Results\n")
    md.append("### Data coverage\n")
    cov = []
    for (s, hz), g in P.groupby(["series", "horizon"]):
        evs = g.event.nunique()
        cov.append([s, hz, evs, g.ticker.nunique(), int(g.two.sum()), int((g.two & g.mid.between(0.05, 0.95)).sum()),
                    int(g.fair.notna().sum()), int(g.dvol.notna().sum()), int(g.siv.notna().sum()),
                    int(g[g.day.isin(hold_days)].event.nunique())])
    md.append(md_table(["series", "horizon", "events", "markets", "two-sided rows", "in-play rows", "model ok",
                        "DVOL ok", "short IV ok", "holdout events"], cov))
    ev_all = pd.read_sql("SELECT series, horizon, count(*) n FROM kevents GROUP BY 1, 2", c)
    md.append("\nEvents found by horizon (before date filter): " + "; ".join(
        f"{r.series} {r.horizon} {r.n}" for r in ev_all.itertuples()))
    js["HA_coverage"] = cov

    for s in SERIES:
        for hz in ("hourly", "daily"):
            Q = P[(P.series == s) & (P.horizon == hz)].copy()
            if not len(Q):
                continue
            taus = HOURLY_TAUS_MIN if hz == "hourly" else DAILY_TAUS_H
            unit = "min" if hz == "hourly" else "h"
            Q["inplay"] = Q.two & Q.mid.between(0.05, 0.95)
            Q["mid_ip"] = np.where(Q.inplay, Q.mid, np.nan)
            Q["mid_all"] = np.where(Q.two, Q.mid, np.nan)
            for w in (0.25, 0.5, 0.75):
                Q[f"blend{w}"] = w * Q.mid + (1 - w) * Q.fair
            md.append(f"\n### H-A {s} {hz} (tau in {unit})\n")
            score_block(Q, {"model": "fair", "blend 0.25": "blend0.25", "blend 0.5": "blend0.5", "blend 0.75": "blend0.75"},
                        "mid_ip", taus, train_days, hold_days, rng, f"{s} {hz} in-play", md, js)
            # secondary: all two-sided rows, model only
            score_block(Q, {"model": "fair"}, "mid_all", taus, train_days, hold_days, rng, f"{s} {hz} all two-sided", md, js)
            # sigma ratio vs market-implied (KXBTCD only; in-play)
            if s == "KXBTCD":
                rr = []
                for tau in taus:
                    g = Q[(Q.tau == tau) & Q.inplay & Q.fair.notna()]
                    r1 = implied_sigma_ratio(g.mid.to_numpy(), g.z_fl.to_numpy())
                    dr = (iv_sigma(g.S.to_numpy(), g.dvol.to_numpy()) / g.sigma.to_numpy())
                    ok = (np.abs(g.mid.to_numpy() - 0.5) >= 0.1) & np.isfinite(dr)
                    r2 = implied_sigma_ratio(g.mid.to_numpy()[ok], (g.z_fl.to_numpy() / dr)[ok]) if ok.any() else []
                    rr.append([tau, len(r1), float(np.median(r1)) if len(r1) else float("nan"),
                               float(np.median(r2)) if len(r2) else float("nan")])
                md.append(f"\nMedian sigma ratio vs the market-implied sigma ({s} {hz}, in-play rows with |mid - 0.5| >= 0.1): "
                          "ours / market, and raw DVOL / market.\n")
                md.append(md_table(["tau", "n", "ours / market", "DVOL / market"], rr))
                js[f"{s} {hz} sigma ratio"] = rr
            # H-A with implied vol (secondary)
            Qd = Q[Q.dvol.notna() & Q.fair.notna()].copy()
            if len(Qd) > 200:
                s_d = iv_sigma(Qd.S.to_numpy(), Qd.dvol.to_numpy())
                Qd["V1"] = fair_with_sigma(Qd, s_d)
                trm = (~Qd.day.isin(hold_days) & Qd.inplay).to_numpy()
                yy = Qd.y.to_numpy(float)

                def ll_k(k, Qd=Qd, trm=trm, s_d=s_d, yy=yy):
                    return float(log_loss(fair_with_sigma(Qd[trm], k * s_d[trm]), yy[trm]).mean())
                kk = float(minimize_scalar(ll_k, bounds=(0.1, 10), method="bounded").x)
                Qd["V2"] = fair_with_sigma(Qd, kk * s_d)
                sv3 = np.sqrt((Qd.sigma.to_numpy() ** 2 + (kk * s_d) ** 2) / 2)
                Qd["V3"] = fair_with_sigma(Qd, sv3)
                md.append(f"\nH-A redo with implied vol ({s} {hz}): k fitted on train in-play rows = {kk:.3f}.\n")
                js[f"{s} {hz} k"] = kk
                score_block(Qd, {"model": "fair", "V1": "V1", "V2": "V2", "V3": "V3"}, "mid_ip", taus, train_days,
                            hold_days, rng, f"{s} {hz} in-play, IV variants", md, js)
                pr = []
                for tau in taus:
                    q = Qd[(Qd.tau == tau) & Qd.inplay].reset_index(drop=True)
                    hh = q.day.isin(hold_days).to_numpy()
                    if hh.sum() < 30:
                        continue
                    d, ci = paired_test(q.V2.to_numpy(), q.fair.to_numpy(), q.y.to_numpy(float), q.day.to_numpy(), hh,
                                        hold_days, rng)
                    pr.append([tau, int(hh.sum()), d * 1000, f"[{ci[0] * 1000:.2f}, {ci[1] * 1000:.2f}]",
                               "PASS" if ci[1] < 0 else "fail"])
                md.append(f"\nV2 fair minus our fair, holdout LL x1000 ({s} {hz}, in-play; negative = IV better):\n")
                md.append(md_table(["tau", "n hold", "dLL x1000", "95% CI", "IV beats ours"], pr))
                js[f"{s} {hz} V2 vs ours"] = pr

    # ---------------- H-B on the 15-minute panel
    F = panel_15m(idx, iv)
    F.to_csv(OUT / "horizons_panel_15m.csv", index=False)
    F = F[F.fair.notna()].reset_index(drop=True)
    s_d = iv_sigma(F.S.to_numpy(), F.dvol.to_numpy())
    s_s = iv_sigma(F.S.to_numpy(), F.siv.to_numpy())
    z, so = F.z.to_numpy(), F.sigma.to_numpy()
    F["V1"] = ndtr(z * so / s_d)
    yy = F.y.to_numpy(float)
    trm = (~F.day.isin(hold_days)).to_numpy() & np.isfinite(s_d)
    k15 = fit_k(z, so, s_d, yy, trm)
    F["V2"] = ndtr(z * so / (k15 * s_d))
    F["V3"] = ndtr(z * so / np.sqrt((so ** 2 + (k15 * s_d) ** 2) / 2))
    F["V4"] = ndtr(z * so / s_s)
    js["k15"] = k15
    md.append("\n### H-B: implied vol on the 15-minute panel\n")
    n_mid = int(F.mid.notna().sum())
    md.append(f"Rows with our fair: {len(F)} ({F.mkt.nunique()} windows x 8 taus); with a print-proxy mid: {n_mid}; "
              f"with a candle mid: {int(F.cmid.notna().sum())}; with DVOL: {int(np.isfinite(s_d).sum())}; with short-dated "
              f"option IV (V4): {int(np.isfinite(s_s).sum())}. k (V2 scale on DVOL), fitted on train days: {k15:.3f}.\n")
    rat = []
    for tau in mvm.TAUS:
        g = F[(F.tau == tau)]
        m = g.mid.notna().to_numpy()
        r_ours = implied_sigma_ratio(g.mid.to_numpy()[m], g.z.to_numpy()[m])
        dr = (iv_sigma(g.S.to_numpy(), g.dvol.to_numpy()) / g.sigma.to_numpy())[m]
        r_d = implied_sigma_ratio(g.mid.to_numpy()[m], g.z.to_numpy()[m] / dr)
        sr = (iv_sigma(g.S.to_numpy(), g.siv.to_numpy()) / g.sigma.to_numpy())[m]
        ok = np.isfinite(sr)
        r_s = implied_sigma_ratio(g.mid.to_numpy()[m][ok], (g.z.to_numpy()[m] / sr)[ok])
        rat.append([tau, len(r_ours), float(np.median(r_ours)), float(np.median(r_d)),
                    float(np.median(r_s)) if len(r_s) else float("nan"),
                    float(np.nanmedian(iv_sigma(1, g.dvol.to_numpy()) / (g.sigma / g.S).to_numpy()))])
    md.append("Median sigma ratios vs the market-implied sigma (print mid, |mid - 0.5| >= 0.1): ours / market, raw DVOL / "
              "market, raw short-dated IV / market; and raw DVOL / ours.\n")
    md.append(md_table(["tau", "n", "ours / market", "DVOL / market", "short IV / market", "DVOL / ours"], rat))
    js["15m sigma ratios"] = rat
    pc = {"model": "fair", "V1": "V1", "V2": "V2", "V3": "V3"}
    score_block(F, pc, "mid", mvm.TAUS, train_days, hold_days, rng, "15m print mid", md, js)
    F4 = F[np.isfinite(s_s)]
    score_block(F4, {"model": "fair", "V4": "V4"}, "mid", mvm.TAUS, train_days, hold_days, rng,
                "15m print mid, rows with short-dated IV", md, js)
    score_block(F, {"model": "fair", "V2": "V2"}, "cmid", mvm.TAUS, train_days, hold_days, rng,
                "15m candle mid (robustness)", md, js)
    pr = []
    for tau in mvm.TAUS:
        q = F[(F.tau == tau) & F.mid.notna()].reset_index(drop=True)
        hh = q.day.isin(hold_days).to_numpy()
        yv, da = q.y.to_numpy(float), q.day.to_numpy()
        row = [tau, int(hh.sum())]
        for col in ("V1", "V2", "V3"):
            qq = np.isfinite(q[col].to_numpy())
            d, ci = paired_test(q[col].to_numpy()[qq], q.fair.to_numpy()[qq], yv[qq], da[qq], hh[qq], hold_days, rng)
            row += [d * 1000, f"[{ci[0] * 1000:.2f}, {ci[1] * 1000:.2f}]"]
            if col == "V2":
                row.append("PASS" if ci[1] < 0 else "fail")
        q4 = np.isfinite(q.V4.to_numpy())
        d, ci = paired_test(q.V4.to_numpy()[q4], q.fair.to_numpy()[q4], yv[q4], da[q4], hh[q4], hold_days, rng)
        row += [d * 1000, f"[{ci[0] * 1000:.2f}, {ci[1] * 1000:.2f}]"]
        pr.append(row)
    md.append("\nIV fair minus our fair, holdout LL x1000 (print-mid rows; negative = IV sigma better). "
              "Criterion IV BEATS OUR SIGMA is on V2.\n")
    md.append(md_table(["tau", "n hold", "V1", "95% CI", "V2", "95% CI", "V2 criterion", "V3", "95% CI", "V4", "95% CI"], pr))
    js["15m IV vs ours"] = pr

    (OUT / "horizons_results.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (OUT / "horizons_results.json").write_text(json.dumps(js, indent=1, default=str), encoding="utf-8")
    log.info("analyze done")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fetch-kalshi", "fetch-deribit", "analyze"])
    ap.add_argument("--rate", type=float, default=1.5)
    ap.add_argument("--refetch", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(OUT / "horizons.log")])
    {"fetch-kalshi": fetch_kalshi, "fetch-deribit": fetch_deribit, "analyze": analyze}[args.cmd](args)


if __name__ == "__main__":
    main()
