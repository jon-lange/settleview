"""Reusable experiment harness for KXBTC15M strategies (read-only research; orders nothing).

A strategy = an ENTRY rule (which window minute and which side) + an EXIT rule (hold / stop / take-profit /
trailing / time). Every strategy is scored with the same honesty rules as docs/research/2026-10-28-model-vs-market.md:

  * Entries only at prices that were really there. Minute resolution: Kalshi's 1-minute candle close at the
    decision minute; UP pays yes_ask, DOWN pays 1 - yes_bid, plus slippage 0/1/2c (1c primary), capped at 99c.
  * Exits sell at the held side's bid (UP: yes_bid, DOWN: 1 - yes_ask) minus the same slippage.
  * Exact Kalshi taker fee 0.07*C*P*(1-P) rounded up to $0.0001 on every taker leg; settlement pays 1/0, no fee.
  * Stops / take-profits / trailing stops are checked at each later minute candle close (minute resolution) and,
    on windows with a full trade tape, at every print (print resolution; see PRINT MODE below).
  * Outcome = Kalshi's official `result`. Train = first 20 UTC days, holdout = last 10.
  * Entries need more than 2 minutes left (live MIN_LEFT_MS): decision minutes are 13..3 minutes before close.
  * One trade per window per strategy, so t = mean / (sd / sqrt(n)) is clustered by window.

PRINT MODE (windows with a full tape only): the side and decision minute come from the same candle data; the
entry fills at the first taker-buy print for that side in [t, t + 2 s) at or under the candle ask + 1c (no fill =
no trade), paying that print + slippage. The held side's bid is then each opposite-taker print (a taker-NO print
at YES price p means someone sold YES into a p bid); every print is a trigger check, and the sale is at that
print's price minus slippage. The time exit sells at the latest bid print in [close - 90 s, close - 60 s).

Databases are opened with mode=ro. Writes only under data/research/:
  experiments_results.csv   one row per (config, resolution, slippage, split), appended
  experiments_fair.npz      cache of the model's fair odds at each decision minute
  experiments_report.md     tables for docs/research/2026-10-28-experiments.md (written by `report`)

CLI (repo root):
  python scripts/dev.py experiments run --entry forced:model:8 --exit sl10_tp15
  python scripts/dev.py experiments run --entry taker:edge=3,blend=0.25,floor=0 --exit hold
  python scripts/dev.py experiments grid            # families 1-3, every combination
  python scripts/dev.py experiments report          # tables + multiple-testing correction

Adding a strategy: write `def my_rule(D, **params) -> (k, side)` returning, for every window, the decision
minute k (minutes since the open, 2..12; -1 = no trade) and side (+1 UP, -1 DOWN), register it in ENTRIES,
and call it as --entry my_rule:a=1,b=2.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, UTC
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.special import ndtr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from model_vs_market import HISTORY_MS, Window, load_index  # noqa: E402  (same fair-value path as the study)

EPS = 1e-9
SLIPS = (0.0, 0.01, 0.02)
PRIMARY_SLIP = 0.01
K_FIRST, K_LAST = 2, 12  # decision minutes since open: 13 .. 3 minutes left (> 2 min, as live)
K_TIME = 14  # the candle ending 1 minute before close
K_CLOSE = 15  # the candle ending at the close (last quote before trading stops)
FILL_MS = 2_000
SEED = 20261028
OUT = ROOT / "data" / "research"
RESULTS = OUT / "experiments_results.csv"


def fee(p):
    """Exact Kalshi taker fee for 1 contract at price p (vectorised; matches fees.kalshi_fee on a 0.01c grid)."""
    p = np.asarray(p, float)
    return np.ceil(np.round(0.07 * p * (1 - p) * 1e4, 6)) / 1e4


# ============================================================================================ data
@dataclass
class Data:
    mk: pd.DataFrame  # markets in open_ts order, with split / half
    YB: np.ndarray  # (n, 17) candle-close yes_bid by minute k since open (NaN = no bid)
    YA: np.ndarray  # (n, 17) yes_ask (NaN = no ask)
    F: np.ndarray  # (n, 17) model fair_up at open + k min - 1 s (NaN where unavailable)
    y: np.ndarray  # 1 = Kalshi result yes
    hold: np.ndarray  # bool: holdout day
    tapes: dict  # row index -> (yes_ts, yes_px, no_ts, no_px)  full tapes only
    strike: np.ndarray | None = None  # Kalshi floor_strike
    open_avg: np.ndarray | None = None  # our own open average (index mean over the 60 s before the open)
    idx_ts: np.ndarray | None = None  # per-second Coinbase index (data/btcflow.db index_1s), ms
    idx_px: np.ndarray | None = None
    cb_ts: np.ndarray | None = None  # Coinbase 1-minute candles from data/btcflow.db (start ts, ms)
    cb_typ: np.ndarray | None = None  # typical price (h + l + c) / 3
    cb_vol: np.ndarray | None = None

    def btc_minutes(self) -> np.ndarray:
        """(n, 17) index price usable at open + j minutes (cached)."""
        if not hasattr(self, "_btc_min"):
            o = self.mk.open_ts.to_numpy(np.int64)
            self._btc_min = np.stack([self.btc(o + j * 60_000) for j in range(17)], axis=1)
        return self._btc_min

    def bars1m(self):
        """1-minute index OHLC bars (end_ms, o, h, l, c); a bar ending at m uses seconds with ts < m - 1 s.
        Empty minutes carry the previous close."""
        if not hasattr(self, "_bars"):
            m = (self.idx_ts + 1_000) // 60_000  # minute bucket of the shifted second
            df = pd.DataFrame({"m": m, "p": self.idx_px})
            g = df.groupby("m").p
            b = pd.DataFrame({"o": g.first(), "h": g.max(), "l": g.min(), "c": g.last()})
            full = np.arange(int(b.index.min()), int(b.index.max()) + 1)
            b = b.reindex(full)
            b["c"] = b.c.ffill()
            for col in ("o", "h", "l"):
                b[col] = b[col].fillna(b.c)
            self._bars = ((full + 1) * 60_000, b.o.to_numpy(), b.h.to_numpy(), b.l.to_numpy(), b.c.to_numpy())
        return self._bars

    def indicator(self, name: str, t) -> np.ndarray:
        """14-period Wilder RSI / ADX on 1-minute index bars, value of the last bar ending at or before t. A bar
        ending at m is built from index seconds with ts < m - 1 s (same 1-second lag as every other input)."""
        if not hasattr(self, "_ind"):
            end, o_, h_, l_, c_ = self.bars1m()
            idx = pd.RangeIndex(len(end))
            c, h, l = pd.Series(c_, idx), pd.Series(h_, idx), pd.Series(l_, idx)
            w = dict(alpha=1 / 14, adjust=False)
            dlt = c.diff()
            rsi = 100 - 100 / (1 + dlt.clip(lower=0).ewm(**w).mean() / (-dlt.clip(upper=0)).ewm(**w).mean())
            up, dn = h.diff(), -l.diff()
            pdm = np.where((up > dn) & (up > 0), up, 0.0)
            mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
            tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
            atr = tr.ewm(**w).mean()
            pdi = 100 * pd.Series(pdm, index=idx).ewm(**w).mean() / atr
            mdi = 100 * pd.Series(mdm, index=idx).ewm(**w).mean() / atr
            adx = (100 * (pdi - mdi).abs() / (pdi + mdi)).ewm(**w).mean()
            self._ind = (end, {"rsi": rsi.to_numpy(), "adx": adx.to_numpy()})
        end, vals = self._ind
        j = np.searchsorted(end, np.asarray(t, np.int64), "right") - 1
        return np.where(j >= 0, vals[name][np.clip(j, 0, None)], np.nan)

    def idx_mean(self, a, b) -> np.ndarray:
        """Mean index price over seconds with a <= ts < b (vectorised; NaN when empty)."""
        if not hasattr(self, "_idx_cs"):
            self._idx_cs = np.concatenate([[0.0], np.cumsum(self.idx_px)])
        i0, i1 = np.searchsorted(self.idx_ts, a, "left"), np.searchsorted(self.idx_ts, b, "left")
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(i1 > i0, (self._idx_cs[i1] - self._idx_cs[i0]) / (i1 - i0), np.nan)

    def btc(self, t) -> np.ndarray:
        """Index price a decision at t may use: latest second with ts < t - 1 s, no older than 10 s (else NaN)."""
        t = np.asarray(t, np.int64)
        i = np.searchsorted(self.idx_ts, t - 1_000, "left") - 1
        ii = np.clip(i, 0, None)
        return np.where((i >= 0) & (self.idx_ts[ii] >= t - 11_000), self.idx_px[ii], np.nan)

    @property
    def n(self) -> int:
        return len(self.mk)

    def ask(self, side: np.ndarray, k: np.ndarray) -> np.ndarray:
        """Price to buy `side` at minute k (row-wise)."""
        i = np.arange(self.n)
        kk = np.clip(k, 0, 16)
        return np.where(side > 0, self.YA[i, kk], 1 - self.YB[i, kk])

    def bids(self, side: np.ndarray) -> np.ndarray:
        """(n, 17) price at which the held side can be sold, by minute."""
        return np.where(side[:, None] > 0, self.YB, 1 - self.YA)

    def mid(self) -> np.ndarray:
        return (self.YB + self.YA) / 2


def ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=60)


def compute_fair(mk: pd.DataFrame, ts, px, lt):
    F = np.full((len(mk), 17), np.nan)
    OA = np.full(len(mk), np.nan)
    for i, m in enumerate(mk.itertuples(index=False)):
        win = Window(int(m.open_ts), int(m.close_ts), ts, px, lt)
        OA[i] = np.nan if win.open_avg is None else win.open_avg
        for k in range(K_FIRST, K_LAST + 2):  # 13 .. 2 minutes left (k = 13 for rules allowing exactly 2 min)
            f = win.fair(int(m.open_ts) + k * 60_000 - 1_000)
            if f is not None:
                F[i, k] = f["fair_up"]
        if (i + 1) % 500 == 0:
            print(f"  fair: {i + 1}/{len(mk)} windows", flush=True)
    return F, OA


def load_full_tapes(c: sqlite3.Connection, rows_by_mkt: dict[int, int]) -> dict:
    done = {m for (m,) in c.execute("SELECT mkt FROM done WHERE kind='full'")}  # read before the tape rows
    keep = np.fromiter((m for m in done if m in rows_by_mkt), np.float64)
    if not len(keep):
        return {}
    parts, cur = [], c.execute("SELECT mkt, ts_ms, yes_price, taker_yes FROM trades")
    while True:
        chunk = cur.fetchmany(2_000_000)
        if not chunk:
            break
        a = np.array(chunk, dtype=np.float64)
        parts.append(a[np.isin(a[:, 0], keep)])
    a = np.concatenate(parts)
    a = a[np.lexsort((a[:, 1], a[:, 0]))]
    mk_ids, starts = np.unique(a[:, 0], return_index=True)
    ends = list(starts[1:]) + [len(a)]
    out = {}
    for m, s, e in zip(mk_ids, starts, ends, strict=True):
        b = a[s:e]
        y = b[:, 3] == 1
        out[rows_by_mkt[int(m)]] = (b[y, 1].astype(np.int64), b[y, 2], b[~y, 1].astype(np.int64), b[~y, 2])
    return out


def load(db: Path, app_db: Path, tapes: bool = True, refresh_fair: bool = False) -> Data:
    t0 = time.time()
    c = ro(db)
    mk = pd.read_sql("SELECT mkt, ticker, open_ts, close_ts, result, day, floor_strike FROM markets "
                     "WHERE result IN ('yes','no') ORDER BY open_ts", c)
    days = sorted(mk.day.unique())
    hold_days = set(days[20:])  # 30 days: train = first 20, holdout = last 10
    mk["split"] = np.where(mk.day.isin(hold_days), "holdout", "train")
    mk["half"] = np.where(mk.day.isin(set(days[:15])), 1, 2)
    n = len(mk)
    row = {int(m): i for i, m in enumerate(mk.mkt)}
    cd = pd.read_sql("SELECT mkt, end_ts, yes_bid, yes_ask FROM candles", c)
    cd = cd[cd.mkt.isin(row)]
    open_ = mk.set_index("mkt").open_ts
    k = ((cd.end_ts.to_numpy() - open_.loc[cd.mkt].to_numpy()) // 60_000).astype(int)
    ok = (k >= 0) & (k <= 16)
    YB, YA = np.full((n, 17), np.nan), np.full((n, 17), np.nan)
    r = cd.mkt.map(row).to_numpy()[ok]
    yb, ya = cd.yes_bid.to_numpy(float)[ok], cd.yes_ask.to_numpy(float)[ok]
    YB[r, k[ok]] = np.where((yb > 0) & (yb < 1), yb, np.nan)
    YA[r, k[ok]] = np.where((ya > 0) & (ya < 1), ya, np.nan)
    bad = YB > YA + EPS  # crossed candle: treat as no quote
    YB[bad], YA[bad] = np.nan, np.nan

    cache = OUT / "experiments_fair.npz"
    F = OA = None
    ts, px, lt = load_index(ro(app_db), int(mk.open_ts.min()) - HISTORY_MS - 120_000, int(mk.close_ts.max()) + 60_000)
    if cache.exists() and not refresh_fair:
        z = np.load(cache)
        if len(z["mkt"]) == n and (z["mkt"] == mk.mkt.to_numpy()).all() and "OA" in z.files:
            F, OA = z["F"], z["OA"]
    if F is None:
        print("computing model fair values (cached afterwards)...", flush=True)
        F, OA = compute_fair(mk, ts, px, lt)
        np.savez(cache, mkt=mk.mkt.to_numpy(), F=F, OA=OA)
    cb = np.array(ro(app_db).execute("SELECT ts, high, low, close, volume FROM candles WHERE granularity = 60 "
                                     "ORDER BY ts").fetchall(), dtype=np.float64)
    T = load_full_tapes(c, row) if tapes else {}
    print(f"loaded {n} windows ({len(days)} days, holdout {min(hold_days)}..{max(hold_days)}), "
          f"{len(T)} full tapes, {time.time() - t0:.1f}s", flush=True)
    return Data(mk, YB, YA, F, (mk.result == "yes").to_numpy().astype(int), (mk.split == "holdout").to_numpy(), T,
                mk.floor_strike.to_numpy(float), OA, ts, px,
                cb[:, 0].astype(np.int64), cb[:, 1:4].mean(axis=1), cb[:, 4])


# ============================================================================================ entry rules
ENTRIES: dict = {}


def entry_rule(name: str):
    """Register an entry rule: @entry_rule("my_rule") on `def my_rule(D, **params) -> (k, side)`.
    It is then runnable as  experiments.py run --entry my_rule:param=value,...  (values: numbers or words)."""
    def deco(fn):
        ENTRIES[name] = fn
        return fn
    return deco


def _none(n):
    return np.full(n, -1), np.zeros(n, int)


def forced(D: Data, rule: str, tau: int):
    """Family 1: a position in every window at `tau` minutes before close; side chosen by `rule`."""
    n, k0 = D.n, 15 - int(tau)
    assert K_FIRST <= k0 <= K_LAST, "entries need > 2 min left"
    yb, ya = D.YB[:, k0], D.YA[:, k0]
    quoted = ~np.isnan(yb) & ~np.isnan(ya)
    mid = (yb + ya) / 2
    coin = np.where(np.random.default_rng(SEED + int(tau)).random(n) < 0.5, 1, -1)
    if rule == "model":  # higher raw (unblended, pre-fee) fair - ask edge
        f = D.F[:, k0]
        e_up, e_dn = f - ya, (1 - f) - (1 - yb)
        side = np.where(e_up > e_dn, 1, np.where(e_up < e_dn, -1, coin))
        ok = quoted & ~np.isnan(f)
    elif rule in ("favourite", "underdog"):
        side = np.where(mid > 0.5 + EPS, 1, np.where(mid < 0.5 - EPS, -1, coin))
        side = side if rule == "favourite" else -side
        ok = quoted
    elif rule == "momentum":  # side whose mid rose most over the prior minute (UP rose -> UP)
        prev = (D.YB[:, k0 - 1] + D.YA[:, k0 - 1]) / 2
        d = mid - prev
        side = np.where(d > EPS, 1, np.where(d < -EPS, -1, coin))
        ok = quoted & ~np.isnan(prev)
    elif rule == "coin":
        side, ok = coin, quoted
    else:
        raise ValueError(rule)
    return np.where(ok, k0, -1), np.where(ok, side, 0)


@entry_rule("taker")
def taker(D: Data, edge: float = 5, blend: float = 0.5, floor: int = 1):
    """Family 2: the Coach's model-taker entry at minute resolution (first qualifying minute, 13 .. 3 min left).

    fair_used = (1-blend)*fair + blend*mid; edge(side) = fair_used(side) - ask - fee(ask); longshot floor on:
    asks < 25c never bought, 25-35c need edge + 3c. Same arithmetic as rules.entry_decision (checked in `selftest`),
    without the 10-second confirmation (not observable at minute resolution) and with the engine gate off."""
    me, w = float(edge) / 100, float(blend)
    k_out, s_out = _none(D.n)
    todo = np.ones(D.n, bool)
    for k in range(K_FIRST, K_LAST + 1):
        yb, ya, f = D.YB[:, k], D.YA[:, k], D.F[:, k]
        used = (1 - w) * f + w * (yb + ya) / 2
        a_up, a_dn = ya, 1 - yb
        e_up, e_dn = used - a_up - fee(a_up), (1 - used) - a_dn - fee(a_dn)

        def need(a):
            if not int(floor):
                return np.full_like(a, me)
            return np.where(a < 0.25 - EPS, np.inf, np.where(a < 0.35 - EPS, me + 0.03, me))
        el_up = e_up >= need(a_up) - EPS
        el_dn = e_dn >= need(a_dn) - EPS
        el_up &= ~np.isnan(e_up)
        el_dn &= ~np.isnan(e_dn)
        hit = todo & (el_up | el_dn)
        side = np.where(el_up & el_dn, np.where(e_up >= e_dn, 1, -1), np.where(el_up, 1, -1))
        k_out[hit], s_out[hit] = k, side[hit]
        todo &= ~hit
    return k_out, s_out


@entry_rule("preceptress_strong_momentum")
def preceptress_strong_momentum(D: Data, edge: float = 12, ref: str = "strike", move: float = 0.12, crowd: int = 1):
    """github.com/preceptress/btc-15-minute-prediction-model, "BTC 15-minute Strong Momentum", README filters as
    written, checked once per minute (candle closes), 12 .. 2 minutes left; first qualifying minute, held to settlement:
      ask 20-85c and spread (yes_ask - yes_bid) <= 5c; |BTC / ref - 1| >= move %, with the window move and the latest
      3-minute BTC move both in the trade's direction; candle age <= 120 s (always true on history);
      edge = model fair(side) - ask >= edge % (absolute, in probability points); crowding: skip a side if 8+ of the
      last 10 signals (this rule's earlier entries, across windows, time order) were that side.
    ref = "strike" (Kalshi floor_strike) or "own" (our open average). BTC = index_1s, latest second < t - 1 s."""
    e_min, mv = float(edge) / 100, float(move) / 100
    ref_px = D.strike if ref == "strike" else D.open_avg
    open_ = D.mk.open_ts.to_numpy(np.int64)
    ks = range(3, 14)  # 12 .. 2 minutes left
    S = np.zeros((D.n, 17), int)
    for k in ks:
        t = open_ + k * 60_000
        p, p3 = D.btc(t), D.btc(t - 180_000)
        with np.errstate(invalid="ignore"):
            wm, m3 = p / ref_px - 1, p / p3 - 1
            yb, ya, f = D.YB[:, k], D.YA[:, k], D.F[:, k]
            tight = (ya - yb) <= 0.05 + EPS
            ok = {}
            for sd, a, fs, dirn in ((1, ya, f, (wm >= mv - EPS) & (m3 > 0)),
                                    (-1, 1 - yb, 1 - f, (wm <= -mv + EPS) & (m3 < 0))):
                ok[sd] = dirn & tight & (a >= 0.20 - EPS) & (a <= 0.85 + EPS) & (fs - a >= e_min - EPS)
        S[:, k] = np.where(ok[1], 1, np.where(ok[-1], -1, 0))  # the direction filters make the sides exclusive
    k_out, s_out = _none(D.n)
    hist: list[int] = []
    for i in np.argsort(open_, kind="stable"):  # crowding needs time order
        for k in ks:
            sd = int(S[i, k])
            if sd == 0:
                continue
            if int(crowd) and len(hist) >= 10 and sum(h == sd for h in hist[-10:]) >= 8:
                continue
            k_out[i], s_out[i] = k, sd
            hist.append(sd)
            break
    return k_out, s_out


@entry_rule("h5_lag_momentum")
def h5_lag_momentum(D: Data, move: float = 0.15, max_ask: float = 90):
    """H5 from docs/research/2026-10-28-human-strategies.md at minute resolution: at 10, 8, 6 min left (first that
    qualifies), BTC index vs Kalshi floor_strike >= +move % -> UP, <= -move % -> DOWN, side ask <= max_ask c."""
    k_out, s_out = _none(D.n)
    open_ = D.mk.open_ts.to_numpy(np.int64)
    for k in (5, 7, 9):
        with np.errstate(invalid="ignore"):
            r = D.btc(open_ + k * 60_000) / D.strike - 1
            up = (r >= move / 100 - EPS) & (D.YA[:, k] <= max_ask / 100 + EPS)
            dn = (r <= -move / 100 + EPS) & (1 - D.YB[:, k] <= max_ask / 100 + EPS)
        hit = (k_out < 0) & (up | dn)
        k_out[hit], s_out[hit] = k, np.where(up, 1, -1)[hit]
    return k_out, s_out


def _proxy_asks(D: Data, r: int, secs: np.ndarray):
    """Per-second ask proxies from a full tape (as in the earlier studies): UP ask = latest taker-YES print in
    [s - 30 s, s); DOWN ask = 1 - latest taker-NO print in [s - 30 s, s). NaN when none."""
    yts, ypx, nts, npx = D.tapes[r]

    def last(ts, px):
        i = np.searchsorted(ts, secs, "left") - 1
        ii = np.clip(i, 0, None)
        ok = (i >= 0) & (ts[ii] >= secs - 30_000) if len(ts) else np.zeros(len(secs), bool)
        return np.where(ok, px[ii], np.nan) if len(ts) else np.full(len(secs), np.nan)
    return last(yts, ypx), 1 - last(nts, npx)


@entry_rule("first_touch_fade_underdog")
def first_touch_fade_underdog(D: Data, L: float = 20, buy: str = "fade"):
    """Reddit "odds of touching": at the FIRST minute (13 .. 2 min left) a side's ask touches L cents or less,
    buy the other side (buy="fade", the favourite) or the touched side (buy="touched") at its ask then; hold.
    A window whose side was already at or below L at the first candle (14 min left) is skipped: its first touch
    came before the scan. Touch = the side's candle ask <= L."""
    lv = float(L) / 100
    k_out, s_out = _none(D.n)
    done = np.zeros(D.n, bool)
    for k in range(1, 14):
        a_up, a_dn = D.YA[:, k], 1 - D.YB[:, k]
        with np.errstate(invalid="ignore"):
            t_up, t_dn = a_up <= lv + EPS, a_dn <= lv + EPS
        hit = ~done & (t_up ^ t_dn)
        if k >= 2:
            touched = np.where(t_up, 1, -1)
            side = -touched if buy == "fade" else touched
            ok = hit & ~np.isnan(np.where(side > 0, D.YA[:, k], D.YB[:, k]))
            k_out[ok], s_out[ok] = k, side[ok]
        done |= hit | (t_up & t_dn)
    return k_out, s_out


def _first_touch_tape(D: Data, r: int, L: float = 20, buy: str = "fade"):
    """Per-second version on a full tape: scan each second 13 .. 2 min left on the print-proxy asks."""
    lv = float(L) / 100
    close = int(D.mk.close_ts.iat[r])
    secs = np.arange(close - 810_000, close - 120_000 + 1, 1000, dtype=np.int64)  # from the tape's first 30 s
    a_up, a_dn = _proxy_asks(D, r, secs)
    with np.errstate(invalid="ignore"):
        t_up, t_dn = a_up <= lv + EPS, a_dn <= lv + EPS
    hit = np.flatnonzero(t_up ^ t_dn)
    if not len(hit) or secs[hit[0]] < close - 780_000:
        return None  # never touched, or touched before the scan began
    j = hit[0]
    touched = 1 if t_up[j] else -1
    side = -touched if buy == "fade" else touched
    ask = a_up[j] if side > 0 else a_dn[j]
    return None if ask != ask else (int(secs[j]), side, float(ask))


first_touch_fade_underdog.tape_scan = _first_touch_tape


@entry_rule("early_momentum_vwap")
def early_momentum_vwap(D: Data, lo: float = 20, hi: float = 85):
    """Reddit early entry: at 3, 4, 5 or 6 minutes elapsed (first that qualifies), buy UP if BTC > trailing 60-min
    VWAP AND the 5- and 15-minute moving averages both slope up AND BTC > Kalshi floor_strike; DOWN mirror;
    the side's ask must be lo..hi cents; hold to settlement.
    BTC = index_1s (latest second < t - 1 s). VWAP = Coinbase 1-minute candles (typical price x volume) that closed
    in the last 60 minutes; where fewer than 45 such candles exist (the last ~6 h of the period) a time-weighted
    index average is used instead (reported). MA slope = MA(t) - MA(t - 60 s) over per-second index prices."""
    open_ = D.mk.open_ts.to_numpy(np.int64)
    k_out, s_out = _none(D.n)
    cs_v = np.concatenate([[0.0], np.cumsum(D.cb_vol)])
    cs_pv = np.concatenate([[0.0], np.cumsum(D.cb_vol * D.cb_typ)])
    D.vwap_fallback = 0
    for k in (3, 4, 5, 6):
        t = open_ + k * 60_000
        p = D.btc(t)
        i0 = np.searchsorted(D.cb_ts, t - 3_600_000, "left")
        i1 = np.searchsorted(D.cb_ts, t - 60_000, "right")  # candles that closed by t
        vol = cs_v[i1] - cs_v[i0]
        with np.errstate(invalid="ignore", divide="ignore"):
            vwap = np.where(((i1 - i0) >= 45) & (vol > 0), (cs_pv[i1] - cs_pv[i0]) / vol, np.nan)
            twap = D.idx_mean(t - 3_600_000, t - 1_000)
            fb = np.isnan(vwap)
            ref = np.where(fb, twap, vwap)
            m5 = D.idx_mean(t - 301_000, t - 1_000) - D.idx_mean(t - 361_000, t - 61_000)
            m15 = D.idx_mean(t - 901_000, t - 1_000) - D.idx_mean(t - 961_000, t - 61_000)
            up = (p > ref) & (m5 > 0) & (m15 > 0) & (p > D.strike)
            dn = (p < ref) & (m5 < 0) & (m15 < 0) & (p < D.strike)
            a_up, a_dn = D.YA[:, k], 1 - D.YB[:, k]
            up &= (a_up >= lo / 100 - EPS) & (a_up <= hi / 100 + EPS)
            dn &= (a_dn >= lo / 100 - EPS) & (a_dn <= hi / 100 + EPS)
        hit = (k_out < 0) & (up | dn)
        D.vwap_fallback += int((hit & fb).sum())
        k_out[hit], s_out[hit] = k, np.where(up, 1, -1)[hit]
    return k_out, s_out


def calm_flags(D: Data, t: np.ndarray, min_hist_days: float = 1.0) -> np.ndarray:
    """+1 calm / -1 not calm / 0 unknown at decision times t (ms). Realized vol = sqrt(sum of squared 1-minute log
    returns over the prior 60 minutes) of the index (each minute mark takes the latest second < mark - 1 s).
    Calm = RV(t) below the median of RV over the trailing 7 days (minute marks in (t - 7 d, t]); no lookahead.
    Unknown while fewer than `min_hist_days` of RV history exist (the first day of the period)."""
    if not hasattr(D, "_rv"):
        m0 = (int(D.idx_ts.min()) // 60_000 + 1) * 60_000
        marks = np.arange(m0, int(D.idx_ts.max()) + 60_000, 60_000, dtype=np.int64)
        px = D.btc(marks)
        r2 = pd.Series(np.diff(np.log(px), prepend=np.nan) ** 2)
        rv = np.sqrt(r2.rolling(60, min_periods=50).sum()).to_numpy()
        med = pd.Series(rv).rolling(7 * 1440, min_periods=int(min_hist_days * 1440)).median().to_numpy()
        D._rv = (marks, rv, med)
    marks, rv, med = D._rv
    j = np.clip(np.searchsorted(marks, t, "right") - 1, 0, len(marks) - 1)
    a, b = rv[j], med[j]
    return np.where(np.isnan(a) | np.isnan(b), 0, np.where(a < b, 1, -1))


@entry_rule("calm_regime_tp20")
def calm_regime_tp20(D: Data, rule: str = "model", tau: float = 8, regime: str = "calm"):
    """Reddit "My Full Strategy" ("shines in calmer waters"): a forced entry (Family 1 `rule` at `tau` min left),
    taken only when the 60-min realized vol is below (regime=calm) or at/above (regime=busy) its trailing 7-day
    median at the decision time. Run with exits hold and sl15_tp20 (TP +20c / stop -15c)."""
    k, side = forced(D, rule, int(tau))
    f = calm_flags(D, D.mk.open_ts.to_numpy(np.int64) + (15 - int(tau)) * 60_000)
    keep = (k >= 0) & (f == (1 if regime == "calm" else -1))
    return np.where(keep, k, -1), np.where(keep, side, 0)


@entry_rule("pnw_cushion_tp20")
def pnw_cushion_tp20(D: Data, variant: str = "v1", cushion: float = 50, lo: float = 65, hi: float = 80):
    """PNW Coast Off-Grid (YouTube walkthrough; r/KalshiBTCUporDown15 "+20c TP" author). At 9, 8, 7 min left (first
    qualifying minute): BTC at least $cushion past Kalshi floor_strike -> that side is the favourite; buy it if its
    ask is lo..hi cents. Exit `mtp20_bstop20` (resting maker TP at entry + 20c; taker sell at the bid once BTC is back
    within $20 of the strike), or `hold` (v4).
    v2 = v1 + calm (60-min RV below its trailing 7-day median) + ADX(14, 1-min) in 20..35;
    v3 = v2 + RSI(14, 1-min) lean: RSI < 30 -> UP setups only, RSI > 70 -> DOWN setups only; v4 = v1 entry."""
    open_ = D.mk.open_ts.to_numpy(np.int64)
    k_out, s_out = _none(D.n)
    for k in (6, 7, 8):
        t = open_ + k * 60_000
        with np.errstate(invalid="ignore"):
            d = D.btc(t) - D.strike
            side = np.where(d >= cushion - EPS, 1, np.where(d <= -cushion + EPS, -1, 0))
            ask = np.where(side > 0, D.YA[:, k], 1 - D.YB[:, k])
            ok = (side != 0) & (ask >= lo / 100 - EPS) & (ask <= hi / 100 + EPS)
            if variant in ("v2", "v3"):
                adx = D.indicator("adx", t)
                ok &= (calm_flags(D, t) == 1) & (adx >= 20) & (adx <= 35)
            if variant == "v3":
                rsi = D.indicator("rsi", t)
                ok &= ~((rsi < 30) & (side < 0)) & ~((rsi > 70) & (side > 0))
        hit = (k_out < 0) & ok
        k_out[hit], s_out[hit] = k, side[hit]
    return k_out, s_out


@entry_rule("pnw_tune")
def pnw_tune(D: Data, cushion: float = 50, start: float = 9, end: float = 7, lo: float = 65, hi: float = 80):
    """PNW cushion entry with every threshold exposed (docs/research/pnw-tuning.md, pre-registered grid). At the
    candle closes `start` .. `end` minutes before close (first qualifying minute): BTC index (latest second < t - 1 s)
    at least $cushion past Kalshi floor_strike -> that side; buy it if its ask is lo..hi cents. Defaults = live
    PnwParams (= pnw_cushion_tp20 v1). Pair with exits mtp{x}_bstop{y} / mtp{x} / bstop{y} / hold."""
    open_ = D.mk.open_ts.to_numpy(np.int64)
    k_out, s_out = _none(D.n)
    ks = range(15 - int(start), 15 - int(end) + 1)
    assert ks[0] >= K_FIRST and ks[-1] <= K_LAST, "entries need > 2 min left"
    for k in ks:
        t = open_ + k * 60_000
        with np.errstate(invalid="ignore"):
            d = D.btc(t) - D.strike
            side = np.where(d >= cushion - EPS, 1, np.where(d <= -cushion + EPS, -1, 0))
            ask = np.where(side > 0, D.YA[:, k], 1 - D.YB[:, k])
            ok = (side != 0) & (ask >= lo / 100 - EPS) & (ask <= hi / 100 + EPS)
        hit = (k_out < 0) & ok
        k_out[hit], s_out[hit] = k, side[hit]
    return k_out, s_out


@entry_rule("pnw_underdog_rebound")
def pnw_underdog_rebound(D: Data, lo: float = 33, hi: float = 45):
    """PNW v5 ("underdog rebound"): in the first 3 minutes of trading (14, 13, 12 min left; the 15-min mark has no
    candle), if RSI(14, 1-min) < 30 (lean UP) or > 70 (lean DOWN) and the lean side's ask is lo..hi cents, buy it.
    Exits: mtp20 (resting maker TP +20c, else hold) or hold."""
    open_ = D.mk.open_ts.to_numpy(np.int64)
    k_out, s_out = _none(D.n)
    for k in (1, 2, 3):
        t = open_ + k * 60_000
        rsi = D.indicator("rsi", t)
        with np.errstate(invalid="ignore"):
            lean = np.where(rsi < 30, 1, np.where(rsi > 70, -1, 0))
            ask = np.where(lean > 0, D.YA[:, k], 1 - D.YB[:, k])
            ok = (lean != 0) & (ask >= lo / 100 - EPS) & (ask <= hi / 100 + EPS)
        hit = (k_out < 0) & ok
        k_out[hit], s_out[hit] = k, lean[hit]
    return k_out, s_out


def day_levels(D: Data) -> dict:
    """Per UTC day d: range high/low = day d-1's index high/low (needs >= 20 h of seconds that day); swing high = the
    most recent earlier daily high (days d-2 .. d-11) above the range high; swing low likewise below the range low.
    Only data from days before d is used."""
    if hasattr(D, "_levels"):
        return D._levels
    day = (D.idx_ts // 86_400_000)
    df = pd.DataFrame({"d": day, "p": D.idx_px}).groupby("d").p.agg(["max", "min", "count"])
    out = {}
    for d in range(int(df.index.min()) + 1, int(df.index.max()) + 2):
        prev = d - 1
        if prev not in df.index or df.loc[prev, "count"] < 20 * 3600:
            continue
        rh, rl = float(df.loc[prev, "max"]), float(df.loc[prev, "min"])
        sh = sl = None
        for q in range(d - 2, d - 12, -1):
            if q not in df.index:
                continue
            if sh is None and df.loc[q, "max"] > rh:
                sh = float(df.loc[q, "max"])
            if sl is None and df.loc[q, "min"] < rl:
                sl = float(df.loc[q, "min"])
        out[d] = {"lower": [x for x in (rl, sl) if x is not None], "upper": [x for x in (rh, sh) if x is not None]}
    D._levels = out
    return out


def _touch(lo, hi, lv: dict, near: float):
    """(+1 with the lower level touched | -1 with the upper level | 0) for one bar's low/high."""
    for L in lv["lower"]:
        if lo <= L * (1 + near):
            return 1, L
    for U in lv["upper"]:
        if hi >= U * (1 - near):
            return -1, U
    return 0, None


def _confirm_seq(bars, lv_of, near: float, max_age: int):
    """Three-bar reversal over a bar sequence [(o, h, l, c), ...]: a bar touches/breaks a level, a later bar closes
    green (lower level; red for upper), then a later bar trades above that bar's high (below its low).
    -> list of (bar index of the confirming bar, side, level)."""
    hits, st = [], {1: None, -1: None}  # per side: [touch_i, level, sneaky extreme or None]
    for i, (o, h, l, c) in enumerate(bars):
        lv = lv_of(i)
        for sd in (1, -1):
            cur = st[sd]
            if cur is not None and i - cur[0] > max_age:
                st[sd] = cur = None
            if cur is not None and cur[2] is not None and ((sd > 0 and h > cur[2]) or (sd < 0 and l < cur[2])):
                hits.append((i, sd, cur[1]))
                st[sd] = None
                continue
            if cur is not None and i > cur[0] and ((sd > 0 and c > o) or (sd < 0 and c < o)):
                cur[2] = h if sd > 0 else l  # the most recent "sneaky" bar
        if lv is not None:
            sd, L = _touch(l, h, lv, near)
            if sd != 0:
                st[sd] = [i, L, None]
    return hits


@entry_rule("level_reversal_pivot")
def level_reversal_pivot(D: Data, variant: str = "v1", near: float = 0.10):
    """YouTube "sneaky pivot" day-trading reversal translated to BTC 15m. Levels: day_levels() (previous UTC day's
    range high/low, plus swing high/low from up to 10 days before). near = touch tolerance in %.
    v1: at 13 min left, BTC within near% of a lower level, or traded below it in the prior 15 min -> UP; the mirror
        for upper levels -> DOWN (both -> skip). Hold.
    v2: 1-minute index bars inside the window (bars ending 1..13 min after the open): touch, a green (red) "sneaky"
        bar, then a bar trading above (below) its high (low) -> buy UP (DOWN) at the end of that bar, >= 2 min left.
    v3: the same sequence on 15-minute index bars aligned to the windows (lookback 8 bars); the next window takes the
        trade at 13 min left.
    Sets D.level_ref (the touched level) for the `lstop` exit."""
    nr = float(near) / 100
    lv_all = day_levels(D)
    open_ = D.mk.open_ts.to_numpy(np.int64)
    days = open_ // 86_400_000
    k_out, s_out = _none(D.n)
    D.level_ref = np.full(D.n, np.nan)
    if variant == "v1":
        t = open_ + 2 * 60_000
        p = D.btc(t)
        lo15 = np.array([np.nan] * D.n)
        hi15 = np.array([np.nan] * D.n)
        i0 = np.searchsorted(D.idx_ts, t - 900_000, "left")
        i1 = np.searchsorted(D.idx_ts, t - 1_000, "left")
        for i in range(D.n):
            if i1[i] > i0[i]:
                seg = D.idx_px[i0[i]:i1[i]]
                lo15[i], hi15[i] = seg.min(), seg.max()
        for i in range(D.n):
            lv = lv_all.get(int(days[i]))
            if lv is None or p[i] != p[i]:
                continue
            up = [L for L in lv["lower"] if abs(p[i] / L - 1) <= nr or lo15[i] < L]
            dn = [U for U in lv["upper"] if abs(p[i] / U - 1) <= nr or hi15[i] > U]
            if bool(up) != bool(dn):
                k_out[i], s_out[i] = 2, (1 if up else -1)
                D.level_ref[i] = (up or dn)[0]
    elif variant == "v2":
        end, o, h, l, c = D.bars1m()
        for i in range(D.n):
            lv = lv_all.get(int(days[i]))
            if lv is None:
                continue
            j0 = np.searchsorted(end, open_[i] + 60_000, "left")
            js = [j0 + q for q in range(13) if j0 + q < len(end) and end[j0 + q] == open_[i] + (q + 1) * 60_000]
            if len(js) < 13:
                continue
            bars = [(o[j], h[j], l[j], c[j]) for j in js]
            hits = _confirm_seq(bars, lambda q, lv=lv: lv, nr, 13)
            if hits:
                q, sd, L = hits[0]
                k_out[i], s_out[i], D.level_ref[i] = q + 1, sd, L  # bar q ends q + 1 minutes after the open
    else:  # v3
        bars = []
        for i in range(D.n):
            a, b = np.searchsorted(D.idx_ts, [open_[i] - 1_000, D.mk.close_ts.iat[i] - 1_000], "left")
            seg = D.idx_px[a:b]
            bars.append((seg[0], seg.max(), seg.min(), seg[-1]) if len(seg) else (np.nan,) * 4)
        consecutive = np.diff(open_, prepend=open_[0] - 900_000) == 900_000
        hits = _confirm_seq(bars, lambda q: lv_all.get(int(days[q])) if bars[q][0] == bars[q][0] else None, nr, 8)
        for q, sd, L in hits:
            if q + 1 < D.n and consecutive[q + 1]:
                k_out[q + 1], s_out[q + 1], D.level_ref[q + 1] = 2, sd, L
    ok = k_out >= 0
    ask = np.where(s_out > 0, D.YA[np.arange(D.n), np.clip(k_out, 0, 16)], 1 - D.YB[np.arange(D.n), np.clip(k_out, 0, 16)])
    ok &= ~np.isnan(ask)
    return np.where(ok, k_out, -1), np.where(ok, s_out, 0)


@entry_rule("h6_favourite_75_90")
def h6_favourite_75_90(D: Data, lo: float = 75, hi: float = 90):
    """H6 from the human-rules study at minute resolution: at 8, 6, 4 min left (first that qualifies), buy the side
    whose ask is lo..hi cents; hold."""
    k_out, s_out = _none(D.n)
    for k in (7, 9, 11):
        a_up, a_dn = D.YA[:, k], 1 - D.YB[:, k]
        with np.errstate(invalid="ignore"):
            up = (a_up >= lo / 100 - EPS) & (a_up <= hi / 100 + EPS)
            dn = (a_dn >= lo / 100 - EPS) & (a_dn <= hi / 100 + EPS)
        hit = (k_out < 0) & (up | dn)
        k_out[hit], s_out[hit] = k, np.where(up & (~dn | (a_up >= a_dn)), 1, -1)[hit]
    return k_out, s_out


@entry_rule("h_panic")
def h_panic(D: Data, max_ask: float = 35, left: float = 4, resid: float = 3):
    """H_PANIC (pre-registered 2026-10-30, written before any result was seen). Late "panic" pricing near the strike.

    Rule: at each decision minute with <= `left` minutes remaining (first qualifying minute wins), if BTC is within
    0.1% of Kalshi floor_strike (|D.btc(t) - strike| / strike <= 0.001, index as of t - 1 s) and either side's
    executable candle ask is <= `max_ask` cents, buy that side (the cheaper one if both qualify); hold to settlement.
    The harness has a fair-value input (D.F, the random-walk model's fair_up at t - 1 s), so the residual filter is
    ON: the bought side's fair - ask - taker fee(ask) must exceed `resid` cents (ask = candle ask, before slippage).
    Minutes: left=4 -> 4 and 3 min left (k = 11, 12; the harness's > 2 min entry floor excludes exactly 2);
    left=2 -> exactly 2 min left (k = 13, the last minute with a fair value; NOTE this is below the live app's
    > 2 min entry floor, so it is not executable as the app stands).
    Variants (all four run and counted): max_ask in {35, 25} x left in {4, 2}; exit hold only.
    Pass bar (harness standard): holdout net clears the Bonferroni correction `report` prints, positive in both
    chronological halves, >= 30 holdout trades. Anything less is KILLED (clearly negative) or inconclusive."""
    ks = {4: (11, 12), 2: (13,)}[int(left)]
    open_ = D.mk.open_ts.to_numpy(np.int64)
    k_out, s_out = _none(D.n)
    for k in ks:
        with np.errstate(invalid="ignore"):
            near = np.abs(D.btc(open_ + k * 60_000) - D.strike) <= 0.001 * D.strike
            a_up, a_dn = D.YA[:, k], 1 - D.YB[:, k]
            f = D.F[:, k]
            r_up, r_dn = f - a_up - fee(a_up), (1 - f) - a_dn - fee(a_dn)
            up = (a_up <= max_ask / 100 + EPS) & (r_up > resid / 100 + EPS)
            dn = (a_dn <= max_ask / 100 + EPS) & (r_dn > resid / 100 + EPS)
        hit = (k_out < 0) & near & (up | dn)
        k_out[hit], s_out[hit] = k, np.where(up & (~dn | (a_up <= a_dn)), 1, -1)[hit]
    return k_out, s_out


@entry_rule("late_residual")
def late_residual(D: Data, left: float = 4, edge: float = 4, cap: float = 70):
    """LATE_RESIDUAL (pre-registered 2026-10-30, written before any result was seen). The public "Scout/Trader"
    late-residual-edge rule, read as an exact rule:

    At ONE fixed decision minute with `left` minutes remaining (left=4 -> k = 11; left=3 -> k = 12), take the model
    probability of each side from the harness's fair input D.F (our random-walk fair_up at t - 1 s; DOWN = 1 - fair_up),
    and the fee-adjusted edge of each side = p_side - ask_side - fee(ask_side), ask = executable candle ask before
    slippage (UP: yes_ask, DOWN: 1 - yes_bid). Pick the side with the larger edge (tie -> UP); buy it only if that
    edge >= `edge` cents AND its ask <= `cap` cents. One trade per window, held to settlement. No quote or no fair
    value at that minute = no trade. 2 min left is excluded: the harness and the live app require > 2 min left.
    Variants (all eight run and counted): left in {4, 3} x edge in {4, 5} x cap in {70, 65}; exit hold only.
    Pass bar (harness standard): holdout net clears the Bonferroni correction `report` prints, positive in both
    chronological halves, >= 30 holdout trades. Anything less is KILLED (clearly negative) or inconclusive."""
    k = {4: 11, 3: 12}[int(left)]
    a_up, a_dn, f = D.YA[:, k], 1 - D.YB[:, k], D.F[:, k]
    with np.errstate(invalid="ignore"):
        e_up, e_dn = f - a_up - fee(a_up), (1 - f) - a_dn - fee(a_dn)
        side = np.where(np.isnan(e_dn) | (e_up >= e_dn), 1, -1)
        e = np.where(side > 0, e_up, e_dn)
        a = np.where(side > 0, a_up, a_dn)
        ok = ~np.isnan(e) & ~np.isnan(a) & (e >= edge / 100 - EPS) & (a <= cap / 100 + EPS)
    return np.where(ok, k, -1), np.where(ok, side, 0)


NAMED = [  # named candidate rules run by `grid --families named` (held to settlement, as their sources specify)
    "preceptress_strong_momentum:edge=12,ref=strike", "preceptress_strong_momentum:edge=12,ref=own",
    "preceptress_strong_momentum:edge=3,ref=strike", "preceptress_strong_momentum:edge=3,ref=own",
    "h5_lag_momentum",  # comparison: the human-rules study's H5, re-run here at minute resolution
] + [f"first_touch_fade_underdog:L={lv},buy={b}" for b in ("fade", "touched") for lv in (10, 15, 20, 25, 30)] + [
    "early_momentum_vwap"] + [
    (f"calm_regime_tp20:rule={r},tau={t},regime={g}", ["hold", "sl15_tp20"])
    for r in ("model", "favourite", "momentum", "coin") for t in (12, 8, 4) for g in ("calm", "busy")] + [
    ("pnw_cushion_tp20:variant=v1", ["mtp20_bstop20"]), ("pnw_cushion_tp20:variant=v2", ["mtp20_bstop20"]),
    ("pnw_cushion_tp20:variant=v3", ["mtp20_bstop20"]), ("pnw_cushion_tp20:variant=v4", ["hold"]),
    ("pnw_underdog_rebound", ["mtp20", "hold"]), "h6_favourite_75_90",
    "level_reversal_pivot:variant=v1", ("level_reversal_pivot:variant=v2", ["hold", "lstop25"]),
    ("level_reversal_pivot:variant=v3", ["hold", "lstop25"])]


def parse_entry(spec: str):
    """'forced:model:8' or 'taker:edge=3,blend=0.25,floor=0' or 'my_rule:a=1'."""
    name, _, rest = spec.partition(":")
    if name == "forced":
        rule, tau = rest.split(":")
        return lambda D: forced(D, rule, int(tau))
    kw = dict(p.split("=") for p in rest.split(",") if p) if rest else {}
    fn = ENTRIES[name]

    def val(v):
        try:
            return float(v)
        except ValueError:
            return v
    params = {k: val(v) for k, v in kw.items()}
    f = (lambda D: fn(D, **params))
    if hasattr(fn, "tape_scan"):  # rule also defines a per-second decision on full tapes (view "print_scan")
        f.tape_scan = lambda D, r: fn.tape_scan(D, r, **params)
    return f


# ============================================================================================ exit rules
@dataclass(frozen=True)
class Exit:
    stop: float | None = None  # sell when bid <= entry - stop
    tp: float | None = None  # sell when bid >= entry + tp
    trail: float | None = None  # sell when bid <= max(entry, highest bid so far) - trail
    time_exit: bool = False  # sell at the bid 1 minute before close
    maker_tp: float | None = None  # resting sell at entry + x (cap 99c): fills at that price, no fee, no slippage,
    #                                once the held side's bid reaches it
    btc_stop: float | None = None  # sell at the bid (taker) once BTC is back within $x of the strike on our side
    level_stop: float | None = None  # sell at the bid once BTC is $x beyond the entry rule's level (D.level_ref)

    @staticmethod
    def parse(s: str) -> Exit:
        kw = {}
        for p in s.split("_"):
            if p == "hold":
                continue
            if p == "time1m":
                kw["time_exit"] = True
            elif p.startswith("sl"):
                kw["stop"] = int(p[2:]) / 100
            elif p.startswith("tp"):
                kw["tp"] = int(p[2:]) / 100
            elif p.startswith("trail"):
                kw["trail"] = int(p[5:]) / 100
            elif p.startswith("mtp"):
                kw["maker_tp"] = int(p[3:]) / 100
            elif p.startswith("bstop"):
                kw["btc_stop"] = float(p[5:])
            elif p.startswith("lstop"):
                kw["level_stop"] = float(p[5:])
            else:
                raise ValueError(s)
        return Exit(**kw)


EXITS = (["hold"] + [f"sl{x}" for x in (5, 10, 15, 20, 30)] + [f"tp{x}" for x in (5, 10, 15)]
         + [f"sl{a}_tp{b}" for a in (5, 10, 15, 20, 30) for b in (5, 10, 15)] + ["trail10", "time1m"]
         + ["tp20", "sl10_tp20", "sl15_tp20", "sl20_tp20"])  # added 2026-10-29: Reddit "My Full Strategy" +20c TP


def _first(mask: np.ndarray) -> np.ndarray:
    """Column of the first True per row, or -1."""
    j = mask.argmax(axis=1)
    return np.where(mask.any(axis=1), j, -1)


def trigger_cols(B: np.ndarray, px: np.ndarray, ex: Exit) -> np.ndarray:
    """B: (n, m) bid path (NaN = no check possible), px: entry ask. -> first triggering column per row or -1."""
    with np.errstate(invalid="ignore"):
        hit = np.zeros(B.shape, bool)
        if ex.stop is not None:
            hit |= (px - ex.stop)[:, None] + EPS >= B
        if ex.tp is not None:
            hit |= (px + ex.tp)[:, None] - EPS <= B
        if ex.trail is not None:
            hw = np.fmax.accumulate(np.where(np.isnan(B), -np.inf, B), axis=1)
            hw = np.maximum(hw, px[:, None])
            hit |= hw - ex.trail + EPS >= B
    return _first(hit & ~np.isnan(B))


# ============================================================================================ simulation
def sim_minute(D: Data, k: np.ndarray, side: np.ndarray, ex: Exit, slip: float, rows=None):
    """Minute resolution. -> dict of arrays over traded windows (index, pnl, won, reason)."""
    idx = np.flatnonzero(k >= 0) if rows is None else np.asarray([r for r in rows if k[r] >= 0], int)
    k, side = k[idx], side[idx]
    i = np.arange(len(idx))
    px = np.where(side > 0, D.YA[idx, k], 1 - D.YB[idx, k])
    paid = np.minimum(0.99, np.round(px + slip, 4))
    cost = paid + fee(paid)
    Ball = np.where(side[:, None] > 0, D.YB[idx], 1 - D.YA[idx])
    last = K_TIME if ex.time_exit else K_CLOSE
    cols = np.arange(17)
    B = np.where((cols[None, :] > k[:, None]) & (cols[None, :] <= last), Ball, np.nan)
    won = (side > 0) == (D.y[idx] == 1)
    if ex.maker_tp is not None or ex.btc_stop is not None or ex.level_stop is not None:  # 3 TP, 4 stop, 0 settled
        valid = (cols[None, :] > k[:, None]) & (cols[None, :] <= last)
        with np.errstate(invalid="ignore"):
            lvl = np.minimum(0.99, px + (ex.maker_tp or 0))
            tp_hit = valid & (Ball >= lvl[:, None] - EPS) if ex.maker_tp is not None else np.zeros(B.shape, bool)
            if ex.btc_stop is not None or ex.level_stop is not None:
                ref, lim_ = ((D.strike, ex.btc_stop) if ex.btc_stop is not None else (D.level_ref, -ex.level_stop))
                dist = side[:, None] * (D.btc_minutes()[idx] - ref[idx][:, None])
                st_hit = valid & (dist < lim_) & ~np.isnan(Ball)
            else:
                st_hit = np.zeros(B.shape, bool)
        jt, js = _first(tp_hit), _first(st_hit)
        use_s = (js >= 0) & ((jt < 0) | (js <= jt))  # same minute: assume the stop came first (conservative)
        use_t = (jt >= 0) & ~use_s
        sale = np.maximum(0.0, np.round(Ball[i, np.clip(js, 0, 16)] - slip, 4))
        pnl = np.where(use_t, lvl - cost, np.where(use_s, sale - fee(np.nan_to_num(sale)) - cost, won.astype(float) - cost))
        return {"idx": idx, "pnl": pnl, "won": won, "reason": np.where(use_t, 3, np.where(use_s, 4, 0))}
    j = trigger_cols(B, px, ex)
    with np.errstate(invalid="ignore"):
        at_tp = (ex.tp is not None) & (B[i, np.clip(j, 0, 16)] >= px + (ex.tp or 0) - EPS)
    reason = np.where(j >= 0, np.where(at_tp, 3, 4), 0)  # 3 = take-profit, 4 = stop/trail, 2 = time, 0 = settled
    if ex.time_exit:
        tb = B[i, K_TIME]
        late = (j < 0) & ~np.isnan(tb)
        j = np.where(late, K_TIME, j)
        reason = np.where(late, 2, reason)
    sale = np.where(j >= 0, np.maximum(0.0, np.round(B[i, np.clip(j, 0, 16)] - slip, 4)), np.nan)
    pnl = np.where(j >= 0, sale - fee(np.nan_to_num(sale)) - cost, won.astype(float) - cost)
    return {"idx": idx, "pnl": pnl, "won": won, "reason": reason}


class PrintSim:
    """Print resolution on full-tape windows, memoised per (window, minute, side)."""

    def __init__(self, D: Data):
        self.D, self.cache = D, {}

    def path(self, r: int, k: int, s: int):
        key = (r, k, s)
        if key in self.cache:
            return self.cache[key]
        D = self.D
        yts, ypx, nts, npx = D.tapes[r]
        t = int(D.mk.open_ts.iat[r]) + k * 60_000
        close = int(D.mk.close_ts.iat[r])
        ask = D.YA[r, k] if s > 0 else 1 - D.YB[r, k]
        lim = min(0.99, round(float(ask) + 0.01, 4))
        bts, bpx = (yts, ypx) if s > 0 else (nts, 1 - npx)  # prints of takers buying the side we buy
        i0, i1 = np.searchsorted(bts, [t, t + FILL_MS], "left")
        fill = None
        for jj in range(i0, i1):
            p = round(float(bpx[jj]), 4)
            if p <= lim + EPS:
                fill = (int(bts[jj]), p)
                break
        out = None
        if fill is not None:
            sts, spx = (nts, npx) if s > 0 else (yts, 1 - ypx)  # opposite takers hit our side's bid
            a, b = np.searchsorted(sts, [fill[0] + 1, close], "left")
            out = (fill[1], sts[a:b], np.round(spx[a:b], 4), close, fill[0])
        self.cache[key] = out
        return out

    def run(self, k: np.ndarray, side: np.ndarray, ex: Exit, slip: float, latency_ms: int = 1_000):
        """latency_ms: the sale is the first bid print at least this long after the trigger print (1 s primary:
        the triggering print itself consumed that bid, so selling into it is optimistic; 0 = sell at the trigger)."""
        D = self.D
        rows = [r for r in sorted(D.tapes) if k[r] >= 0]
        res = {"idx": [], "pnl": [], "won": [], "reason": [], "tried": len(rows)}
        for r in rows:
            p = self.path(r, int(k[r]), int(side[r]))
            if p is None:
                continue
            px, sts, spx, close, fill_ts = p
            paid = min(0.99, round(px + slip, 4))
            cost = paid + float(fee(paid))
            won = (side[r] > 0) == (D.y[r] == 1)
            if ex.maker_tp is not None or ex.btc_stop is not None or ex.level_stop is not None:
                lvl = min(0.99, px + (ex.maker_tp or 0))
                t_tp = np.inf
                if ex.maker_tp is not None:
                    h = np.flatnonzero(spx >= lvl - EPS)
                    t_tp = float(sts[h[0]]) if len(h) else np.inf
                t_st, m = np.inf, None
                if ex.btc_stop is not None or ex.level_stop is not None:
                    ref, lim_ = ((D.strike[r], ex.btc_stop) if ex.btc_stop is not None
                                 else (D.level_ref[r], -ex.level_stop))
                    secs = np.arange((fill_ts // 1000 + 1) * 1000, close, 1000, dtype=np.int64)
                    with np.errstate(invalid="ignore"):
                        bad = np.flatnonzero(side[r] * (D.btc(secs) - ref) < lim_)
                    if len(bad):
                        t_st = float(secs[bad[0]])
                        m = int(np.searchsorted(sts, t_st + latency_ms, "left"))
                        m = m if m < len(sts) else None
                if t_tp < t_st:
                    reason, pnl = 3, lvl - cost
                elif m is not None:
                    sale = max(0.0, round(float(spx[m]) - slip, 4))
                    reason, pnl = 4, sale - float(fee(sale)) - cost
                else:
                    reason, pnl = 0, float(won) - cost
                res["idx"].append(r)
                res["pnl"].append(pnl)
                res["won"].append(won)
                res["reason"].append(reason)
                continue
            if ex.time_exit:
                cut = np.searchsorted(sts, close - 60_000, "left")
                spx_c = spx[:cut]
            else:
                cut, spx_c = len(sts), spx
            j = int(trigger_cols(spx_c[None, :], np.array([px]), ex)[0]) if len(spx_c) else -1
            reason, sale = 0, None
            if j >= 0:
                m = j if latency_ms <= 0 else int(np.searchsorted(sts, sts[j] + latency_ms, "left"))
                if m < len(sts):
                    is_tp = ex.tp is not None and spx_c[j] >= px + ex.tp - EPS
                    reason, sale = (3 if is_tp else 4), float(spx[m])
            elif ex.time_exit:
                m = int(np.searchsorted(sts, close - 60_000 + latency_ms, "left"))
                if m < len(sts):
                    reason, sale = 2, float(spx[m])
            if sale is None:
                pnl = float(won) - cost
            else:
                sale = max(0.0, round(sale - slip, 4))
                pnl = sale - float(fee(sale)) - cost
            res["idx"].append(r)
            res["pnl"].append(pnl)
            res["won"].append(won)
            res["reason"].append(reason)
        res["idx"] = np.asarray(res["idx"], dtype=int)
        res["pnl"] = np.asarray(res["pnl"], dtype=float)
        return {key: (np.asarray(v) if isinstance(v, list) else v) for key, v in res.items()}


# ============================================================================================ statistics
def summarize(pnl: np.ndarray) -> dict:
    """pnl per contract in dollars, chronological (one trade per window)."""
    n = len(pnl)
    if n == 0:
        return {"n": 0}
    sd = float(pnl.std(ddof=1)) if n > 1 else float("nan")
    w = pnl > 0
    aw = float(pnl[w].mean()) if w.any() else float("nan")
    al = float(-pnl[~w].mean()) if (~w).any() else float("nan")
    be = al / (aw + al) if w.any() and (~w).any() else float("nan")
    cum = np.cumsum(pnl)
    dd = float(np.max(np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:] - cum)) if n else 0.0
    return {"n": n, "win": 100 * float(w.mean()), "be": 100 * be, "net_c": 100 * float(pnl.mean()),
            "sd_c": 100 * sd, "t": float(pnl.mean() / (sd / math.sqrt(n))) if n > 2 and sd > 0 else float("nan"),
            "worst_c": 100 * float(pnl.min()), "maxdd_c": 100 * dd}


def split_rows(D: Data, out: dict) -> dict:
    idx, pnl = out["idx"], out["pnl"]
    o = np.argsort(D.mk.open_ts.to_numpy()[idx], kind="stable")
    idx, pnl = idx[o], pnl[o]
    h = D.hold[idx]
    half = D.mk.half.to_numpy()[idx]
    res = {"train": summarize(pnl[~h]), "holdout": summarize(pnl[h]), "all": summarize(pnl),
           "half1": summarize(pnl[half == 1]), "half2": summarize(pnl[half == 2])}
    reasons = np.asarray(out["reason"])
    res["all"]["exit_share"] = float((reasons > 0).mean()) if len(reasons) else float("nan")
    if len(reasons):
        reasons = reasons[o]
        for name, m in (("train", ~h), ("holdout", h), ("all", np.ones(len(h), bool)),
                        ("half1", half == 1), ("half2", half == 2)):
            if m.any():
                rr = reasons[m]
                res[name].update(share_tp=float((rr == 3).mean()), share_stop=float((rr == 4).mean()),
                                 share_settle=float((rr == 0).mean()))
    return res


def evaluate(D: Data, family: str, entry_name: str, entry_fn, exits, prints: PrintSim | None, run_ts: str,
             slips=SLIPS) -> list[dict]:
    k, side = entry_fn(D)
    rows = []
    tape_rows = sorted(D.tapes)
    for ex_name in exits:
        ex = Exit.parse(ex_name)
        for slip in slips:
            views = [("minute", sim_minute(D, k, side, ex, slip))]
            if prints is not None and tape_rows:
                views.append(("minute_tape", sim_minute(D, k, side, ex, slip, rows=tape_rows)))
                views.append(("print", prints.run(k, side, ex, slip, 1_000)))
                views.append(("print_instant", prints.run(k, side, ex, slip, 0)))
            if prints is not None and hasattr(entry_fn, "tape_scan") and ex_name == "hold":
                views.append(("print_scan", tape_scan_hold(D, entry_fn.tape_scan, slip)))
            for res_name, out in views:
                for split, st in split_rows(D, out).items():
                    rows.append({"run_ts": run_ts, "config": f"{entry_name}|{ex_name}", "family": family,
                                 "entry": entry_name, "exit": ex_name, "res": res_name, "slip_c": int(round(slip * 100)),
                                 "split": split, "tried": out.get("tried", int((k >= 0).sum()) if res_name == "minute"
                                                                   else int(sum(k[r] >= 0 for r in tape_rows))),
                                 **st})
    return rows


def tape_scan_hold(D: Data, scan, slip: float) -> dict:
    """Per-second decisions on full tapes: fill at the first taker-buy print for the side in [t, t + 2 s) at or
    under the seen ask + 1c (no fill = no trade), pay print + slippage + fee, hold to settlement."""
    res = {"idx": [], "pnl": [], "won": [], "reason": [], "tried": 0}
    for r in sorted(D.tapes):
        d = scan(D, r)
        if d is None:
            continue
        res["tried"] += 1
        t, side, ask = d
        yts, ypx, nts, npx = D.tapes[r]
        bts, bpx = (yts, ypx) if side > 0 else (nts, 1 - npx)
        lim = min(0.99, round(ask + 0.01, 4))
        i0, i1 = np.searchsorted(bts, [t, t + FILL_MS], "left")
        px = next((round(float(bpx[j]), 4) for j in range(i0, i1) if round(float(bpx[j]), 4) <= lim + EPS), None)
        if px is None:
            continue
        paid = min(0.99, round(px + slip, 4))
        won = (side > 0) == (D.y[r] == 1)
        res["idx"].append(r)
        res["pnl"].append(float(won) - paid - float(fee(paid)))
        res["won"].append(won)
        res["reason"].append(0)
    res["idx"] = np.asarray(res["idx"], dtype=int)
    res["pnl"] = np.asarray(res["pnl"], dtype=float)
    return res


def append(rows: list[dict]) -> None:
    """Append to the results CSV; if new columns appear, rewrite the file with the union of columns."""
    df = pd.DataFrame(rows)
    if RESULTS.exists():
        head = list(pd.read_csv(RESULTS, nrows=0).columns)
        if set(df.columns) - set(head):
            old = pd.read_csv(RESULTS)
            pd.concat([old, df], ignore_index=True).to_csv(RESULTS, index=False)
            return
        df.reindex(columns=head).to_csv(RESULTS, mode="a", header=False, index=False)
    else:
        df.to_csv(RESULTS, index=False)


# ============================================================================================ grid
def family1():
    for tau in (12, 8, 4):
        for rule in ("model", "favourite", "underdog", "momentum", "coin"):
            yield "F1", f"forced:{rule}:{tau}", (lambda D, r=rule, t=tau: forced(D, r, t))


def family2():
    for e in (0, 1, 2, 3, 5, 8):
        for w in (0, 0.25, 0.5, 0.75):
            for fl in (1, 0):
                yield "F2", f"taker:edge={e},blend={w},floor={fl}", (lambda D, e=e, w=w, fl=fl: taker(D, e, w, fl))


def selftest(D: Data) -> None:
    """taker() must agree with rules.entry_decision at the live parameters, minute by minute."""
    from btc_signal_engine.domain.coach import rules as R
    rng = np.random.default_rng(1)
    bad = tot = 0
    for r in rng.choice(D.n, 300, replace=False):
        for kk in range(K_FIRST, K_LAST + 1):
            yb, ya, f = D.YB[r, kk], D.YA[r, kk], D.F[r, kk]
            if np.isnan(yb) or np.isnan(ya) or np.isnan(f):
                continue
            q = {"yes_ask": round(float(ya), 4), "no_ask": round(1 - float(yb), 4), "yes_bid": round(float(yb), 4),
                 "no_bid": round(1 - float(ya), 4)}
            d = R.entry_decision(float(f), q, 0, (15 - kk) * 60_000)
            want = d.side if isinstance(d, R.Setup) else None
            sub = Data(D.mk.iloc[[r]], D.YB[[r]], D.YA[[r]], D.F[[r]], D.y[[r]], D.hold[[r]], {})
            sub.YB, sub.YA, sub.F = (np.where(np.arange(17) == kk, M, np.nan) for M in (sub.YB, sub.YA, sub.F))
            kx, sx = taker(sub, 5, 0.5, 1)
            got = None if kx[0] < 0 else ("UP" if sx[0] > 0 else "DOWN")
            tot += 1
            bad += got != want
    print(f"selftest: taker(5, 0.5, floor) vs rules.entry_decision: {tot - bad}/{tot} decisions agree", flush=True)


def cmd_grid(args) -> None:
    t0 = time.time()
    D = load(Path(args.db), Path(args.app_db), tapes=not args.no_prints)
    selftest(D)
    P = None if args.no_prints else PrintSim(D)
    run_ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ")
    def named():  # an entry is "spec" (held to settlement) or ("spec", [exits])
        for e in NAMED:
            spec, exits = (e, ["hold"]) if isinstance(e, str) else e
            yield "named", spec, parse_entry(spec), exits
    fams = {"1": lambda: ((f, n, fn, EXITS) for f, n, fn in family1()),
            "2": lambda: ((f, n, fn, EXITS) for f, n, fn in family2()), "named": named}
    for f in args.families.split(","):
        for fam, name, fn, exits in fams[f]():
            append(evaluate(D, fam, name, fn, exits, P, run_ts))
            print(f"  {name}: {time.time() - t0:.0f}s", flush=True)
    print(f"grid done in {time.time() - t0:.0f}s -> {RESULTS}", flush=True)


def cmd_run(args) -> None:
    t0 = time.time()
    D = load(Path(args.db), Path(args.app_db), tapes=not args.no_prints)
    P = None if args.no_prints else PrintSim(D)
    run_ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ")
    rows = evaluate(D, args.family, args.entry, parse_entry(args.entry), args.exit.split(","), P, run_ts)
    append(rows)
    df = pd.DataFrame(rows)
    show = df[(df.slip_c == 1) & df.split.isin(["train", "holdout"])]
    print(show[["config", "res", "split", "n", "win", "be", "net_c", "t", "sd_c", "worst_c", "maxdd_c"]]
          .to_string(index=False, float_format=lambda v: f"{v:.2f}"))
    print(f"{time.time() - t0:.0f}s; appended {len(rows)} rows to {RESULTS}")


# ============================================================================================ report
def fmt(v, d=1):
    return "n/a" if v is None or v != v else f"{v:.{d}f}"


def latest(df: pd.DataFrame) -> pd.DataFrame:
    """Most recent row per (config, res, slip, split): reruns supersede, but every config ever tried is counted."""
    return df.sort_values("run_ts").groupby(["config", "res", "slip_c", "split"], as_index=False).last()


def cmd_report(args) -> None:
    raw = pd.read_csv(RESULTS)
    n_configs = raw[raw.res == "minute"].config.nunique()
    df = latest(raw)
    P = df[(df.res == "minute") & (df.slip_c == 1)]
    piv = P.pivot_table(index=["family", "entry", "exit", "config"], columns="split",
                        values=["n", "win", "be", "net_c", "t", "sd_c", "worst_c", "maxdd_c"]).reset_index()
    piv.columns = ["_".join(c).strip("_") if isinstance(c, tuple) else c for c in piv.columns]
    md = []

    def row(r, extra=""):
        return (f"| {r.entry}{extra} | {int(r.n_train)} | {fmt(r.win_train)} | {fmt(r.be_train)} | {fmt(r.net_c_train, 2)} | "
                f"{fmt(r.t_train, 2)} | {int(r.n_holdout)} | {fmt(r.win_holdout)} | {fmt(r.be_holdout)} | "
                f"{fmt(r.net_c_holdout, 2)} | {fmt(r.t_holdout, 2)} |")
    hdr = ("| entry | train n | win % | BE % | net c | t | holdout n | win % | BE % | net c | t |\n"
           "|---|---|---|---|---|---|---|---|---|---|---|")

    # ---- Family 1, hold to settlement, and the best exit per entry by train t
    f1 = piv[piv.family == "F1"]
    md += ["### Family 1: forced entry, hold to settlement (minute resolution, 1c slippage)", "", hdr]
    md += [row(r) for r in f1[f1.exit == "hold"].itertuples()]
    md += ["", "Slippage sensitivity (all 30 days, hold): net c/contract at 0 / 1 / 2c slippage.", "",
           "| entry | 0c | 1c | 2c |", "|---|---|---|---|"]
    A = df[(df.res == "minute") & (df.split == "all") & (df.exit == "hold")]
    for e in f1[f1.exit == "hold"].entry:
        v = [A[(A.entry == e) & (A.slip_c == s)].net_c.iloc[0] for s in (0, 1, 2)]
        md.append(f"| {e} | {fmt(v[0], 2)} | {fmt(v[1], 2)} | {fmt(v[2], 2)} |")

    # ---- Family 2
    f2 = piv[piv.family == "F2"]
    md += ["", "### Family 2: model taker aggressiveness, hold to settlement (minute resolution, 1c slippage)", "", hdr]
    md += [row(r) for r in f2[f2.exit == "hold"].sort_values(["entry"]).itertuples()]

    # ---- Named rules: one row per config (train / holdout / halves), full-tape views, calm vs busy
    nm = piv[piv.family == "named"]
    if len(nm):
        def g(r, c):
            v = getattr(r, c, float("nan"))
            return v
        md += ["", "### Named candidate rules (minute resolution, 1c slippage)", "",
               "| config | train n | train net c | train t | holdout n | win % | BE % | holdout net c | holdout t | "
               "1st-half n / net c | 2nd-half n / net c | exit mix (all days) TP / stop / settle |",
               "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        mix = df[(df.res == "minute") & (df.slip_c == 1) & (df.split == "all")].set_index("config")
        plain = nm[~nm.entry.str.startswith("calm_regime")]
        for r in plain.sort_values("config").itertuples():
            md.append(f"| {r.config} | {int(g(r, 'n_train') or 0)} | {fmt(g(r, 'net_c_train'), 2)} | {fmt(g(r, 't_train'), 2)} | "
                      f"{int(g(r, 'n_holdout') or 0)} | {fmt(g(r, 'win_holdout'))} | {fmt(g(r, 'be_holdout'))} | "
                      f"{fmt(g(r, 'net_c_holdout'), 2)} | {fmt(g(r, 't_holdout'), 2)} | "
                      f"{int(g(r, 'n_half1') or 0)} / {fmt(g(r, 'net_c_half1'), 2)} | "
                      f"{int(g(r, 'n_half2') or 0)} / {fmt(g(r, 'net_c_half2'), 2)} | "
                      + ("n/a" if "share_tp" not in mix or r.config not in mix.index or mix.loc[r.config, "share_tp"] != mix.loc[r.config, "share_tp"]
                         else f"{100 * mix.loc[r.config, 'share_tp']:.0f}% / {100 * mix.loc[r.config, 'share_stop']:.0f}% / "
                              f"{100 * mix.loc[r.config, 'share_settle']:.0f}%") + " |")
        cm = nm[nm.entry.str.startswith("calm_regime")].copy()
        if len(cm):
            cm["base"] = cm.entry.str.replace(r",regime=\w+", "", regex=True)
            cm["reg"] = cm.entry.str.extract(r"regime=(\w+)")[0]
            md += ["", "**Calm vs busy regime, same forced entries** (calm = 60-min realized vol below its trailing "
                   "7-day median at the decision; minute resolution, 1c):", "",
                   "| forced entry | exit | calm train n / net c / t | calm holdout n / net c / t | "
                   "busy train n / net c / t | busy holdout n / net c / t |", "|---|---|---|---|---|---|"]
            for (b, ex), grp in cm.groupby(["base", "exit"]):
                cells = []
                for reg in ("calm", "busy"):
                    x = grp[grp.reg == reg]
                    for sp in ("train", "holdout"):
                        if len(x):
                            r = x.iloc[0]
                            cells.append(f"{int(r[f'n_{sp}'])} / {fmt(r[f'net_c_{sp}'], 2)} / {fmt(r[f't_{sp}'], 2)}")
                        else:
                            cells.append("n/a")
                md.append(f"| {b.replace('calm_regime_tp20:', '')} | {ex} | " + " | ".join(cells) + " |")
        V = df[(df.family == "named") & (df.slip_c == 1) & df.split.isin(["all", "holdout"])
               & df.res.isin(["minute_tape", "print", "print_scan"]) & ~df.entry.str.startswith("calm_regime")]
        if len(V):
            md += ["", "Named rules on full-tape windows only, hold to settlement (`minute_tape` = the minute "
                   "decision on these windows; `print` = the same decision filled at the tape; `print_scan` = decided "
                   "each second on the tape's print-proxy quotes and filled at the tape):", "",
                   "| config | resolution | all n / net c / t | holdout n / net c / t |", "|---|---|---|---|"]
            for (cfg, res_), grp in V.groupby(["config", "res"]):
                cells = []
                for sp in ("all", "holdout"):
                    x = grp[grp.split == sp]
                    r = x.iloc[0] if len(x) else None
                    cells.append("n/a" if r is None or r.n != r.n or r.n == 0 else
                                 f"{int(r.n)} / {fmt(r.net_c, 2)} / {fmt(r.t, 2)}")
                md.append(f"| {cfg} | {res_} | {cells[0]} | {cells[1]} |")
        md.append("")

    # ---- Family 3: exits on representative entries
    reps = ["forced:model:8", "forced:favourite:8", "forced:coin:8", "forced:underdog:12",
            "taker:edge=5,blend=0.5,floor=1", "taker:edge=0,blend=0,floor=0"]
    md += ["", "### Family 3: exits on the same entries (minute resolution, 1c slippage)", ""]
    for e in reps:
        x = piv[piv.entry == e]
        if not len(x):
            continue
        md += [f"**{e}**", "", hdr.replace("| entry |", "| exit |")]
        order = {k: i for i, k in enumerate(EXITS)}
        for r in sorted(x.itertuples(), key=lambda r: order.get(r.exit, 99)):
            md.append(row(r).replace(f"| {r.entry} |", f"| {r.exit} |", 1))
        md.append("")

    # ---- average effect of each exit over all entries (train+holdout)
    Aall = df[(df.res == "minute") & (df.slip_c == 1) & (df.split == "all") & df.family.isin(["F1", "F2"])]
    base = Aall[Aall.exit == "hold"].set_index("entry")
    eff = []
    for ex in EXITS:
        x = Aall[Aall.exit == ex].set_index("entry")
        common = x.index.intersection(base.index)
        d = x.loc[common]
        b = base.loc[common]
        eff.append((ex, len(common), (d.net_c - b.net_c).mean(), (d.net_c - b.net_c).min(), (d.net_c - b.net_c).max(),
                    (d.sd_c / b.sd_c).mean(), (d.worst_c).mean(), (d.maxdd_c / b.maxdd_c).median(),
                    d.exit_share.mean() if "exit_share" in d else np.nan))
    md += ["### Exit effect averaged over all 63 entry rules (all 30 days, minute, 1c)", "",
           "Net change = exit's net c/contract minus hold's on the same entries. sd ratio = exit sd / hold sd.",
           "", "| exit | entries | mean net change c | min | max | mean sd ratio | mean worst trade c | median maxDD ratio | share exited early |",
           "|---|---|---|---|---|---|---|---|---|"]
    for e in eff:
        md.append(f"| {e[0]} | {e[1]} | {fmt(e[2], 2)} | {fmt(e[3], 2)} | {fmt(e[4], 2)} | {fmt(e[5], 2)} | "
                  f"{fmt(e[6], 1)} | {fmt(e[7], 2)} | {fmt(100 * e[8], 0)}% |")

    # ---- stop-loss distribution shape on the same entries
    md += ["", "### Stop-loss effect: P&L distribution on the same entries (all 30 days, minute, 1c, c/contract)", ""]
    for e in ["forced:model:8", "forced:coin:8", "taker:edge=5,blend=0.5,floor=1"]:
        md += [f"**{e}**", "", "| exit | n | mean | sd | worst window | max drawdown | win % |", "|---|---|---|---|---|---|---|"]
        for ex in ["hold", "sl5", "sl10", "sl15", "sl20", "sl30", "trail10", "time1m", "tp20", "sl15_tp20"]:
            x = Aall[(Aall.entry == e) & (Aall.exit == ex)]
            if len(x):
                r = x.iloc[0]
                md.append(f"| {ex} | {int(r.n)} | {fmt(r.net_c, 2)} | {fmt(r.sd_c, 1)} | {fmt(r.worst_c, 1)} | "
                          f"{fmt(r.maxdd_c, 0)} | {fmt(r.win)} |")
        md.append("")

    # ---- resolution check
    R2 = df[(df.slip_c == 1) & (df.split == "all") & df.res.isin(["minute_tape", "print", "print_instant"])]
    if len(R2) and R2.res.nunique() == 3:
        w = R2.pivot_table(index=["family", "entry", "exit"], columns="res",
                           values=["net_c", "n", "exit_share", "worst_c", "sd_c"]).reset_index()
        w.columns = ["_".join(c).strip("_") for c in w.columns]
        w["d"] = w.net_c_print - w.net_c_minute_tape
        w["d0"] = w.net_c_print_instant - w.net_c_minute_tape
        w = w[w.n_print >= 100]  # small-n configs (strict taker settings) swamp the averages with noise
        w["kind"] = w.exit.map(lambda s: "hold" if s == "hold" else "time" if s == "time1m" else "trail" if "trail" in s
                               else "stop+tp" if "sl" in s and "tp" in s else "stop" if "sl" in s else "tp")
        md += ["### Minute vs per-print resolution (full-tape windows, all days, 1c)", "",
               f"{int(w.n_minute_tape.max())} windows with full tapes at most; configs with >= 100 print-mode trades only. Same entry decisions; print resolution "
               "fills the entry at the tape and checks every bid print. d = print net - minute net on the same windows "
               "(print: sale at the first bid print >= 1 s after the trigger; instant: sale at the trigger print).",
               "", "| exit kind | configs | mean n print / minute | mean net minute c | mean d (1 s) c | median d (1 s) | "
               "mean d (instant) c | mean worst trade minute / print c | early-exit share minute / print |",
               "|---|---|---|---|---|---|---|---|---|"]
        for kind, g in w.groupby("kind"):
            md.append(f"| {kind} | {len(g)} | {fmt(g.n_print.mean(), 0)} / {fmt(g.n_minute_tape.mean(), 0)} | "
                      f"{fmt(g.net_c_minute_tape.mean(), 2)} | {fmt(g.d.mean(), 2)} | {fmt(g.d.median(), 2)} | "
                      f"{fmt(g.d0.mean(), 2)} | {fmt(g.worst_c_minute_tape.mean(), 1)} / {fmt(g.worst_c_print.mean(), 1)} | "
                      f"{fmt(100 * g.exit_share_minute_tape.mean(), 0)}% / {fmt(100 * g.exit_share_print.mean(), 0)}% |")
        ss = w[w.exit.isin(["hold", "sl5", "sl10", "sl20", "tp10", "sl10_tp10", "trail10", "time1m"])
               & w.entry.isin(["forced:model:8", "forced:coin:8", "forced:favourite:8"])]
        md += ["", "Selected rows (net c/contract, all days, tape windows): minute | print 1 s | print instant", "",
               "| entry | exit | n | minute | print (1 s) | print (instant) |", "|---|---|---|---|---|---|"]
        for r in ss.itertuples():
            md.append(f"| {r.entry} | {r.exit} | {int(r.n_print)} | {fmt(r.net_c_minute_tape, 2)} | {fmt(r.net_c_print, 2)} | "
                      f"{fmt(r.net_c_print_instant, 2)} |")
        # holdout of the print view for the best minute configs is in the CSV (res=print, split=holdout)

    # ---- multiple testing
    H = piv.dropna(subset=["t_holdout"])
    H = H[H.n_holdout >= 30]
    from scipy.stats import norm
    t_crit = float(norm.isf(0.05 / n_configs))
    md += ["", "### Best configurations and the multiple-testing correction", "",
           f"Configurations tried (distinct entry x exit at the primary spec, every run counted): **{n_configs}**. "
           f"Bonferroni at family-wise 5%, one-sided: holdout t must exceed **{t_crit:.2f}** "
           f"(p < 0.05/{n_configs} = {0.05 / n_configs:.1e}).", ""]
    best_h = H.sort_values("t_holdout", ascending=False).head(10)
    md += ["Top 10 by holdout t (selected ON the holdout, so only the Bonferroni line applies):", "",
           "| config | train net c | train t | holdout n | holdout net c | holdout t | one-sided p | Bonferroni p |",
           "|---|---|---|---|---|---|---|---|"]
    for r in best_h.itertuples():
        p = float(1 - ndtr(r.t_holdout))
        md.append(f"| {r.config} | {fmt(r.net_c_train, 2)} | {fmt(r.t_train, 2)} | {int(r.n_holdout)} | "
                  f"{fmt(r.net_c_holdout, 2)} | {fmt(r.t_holdout, 2)} | {p:.2g} | {min(1, p * n_configs):.2g} |")
    T = piv.dropna(subset=["t_train"])
    T = T[T.n_train >= 60]
    md += ["", "Train-selected (the honest procedure: pick on the first 20 days, test once on the last 10). "
           "Top 5 by train t per family; correction = Bonferroni over the 10 holdout tests shown:", "",
           "| family | config | train n | train net c | train t | holdout n | holdout net c | holdout t | Bonferroni p (x10) |",
           "|---|---|---|---|---|---|---|---|---|"]
    for fam in ("F1", "F2"):
        for r in T[T.family == fam].sort_values("t_train", ascending=False).head(5).itertuples():
            p = float(1 - ndtr(r.t_holdout)) if r.t_holdout == r.t_holdout else float("nan")
            md.append(f"| {fam} | {r.config} | {int(r.n_train)} | {fmt(r.net_c_train, 2)} | {fmt(r.t_train, 2)} | "
                      f"{int(r.n_holdout)} | {fmt(r.net_c_holdout, 2)} | {fmt(r.t_holdout, 2)} | {min(1, 10 * p):.2g} |")
    cands = H[(H.t_holdout > t_crit) & (H.net_c_holdout > 0)]
    md += ["", f"**Candidates (holdout survives Bonferroni over {n_configs}):** "
           + (", ".join(cands.config) if len(cands) else "none.")]
    dist = H.t_holdout
    md += ["", f"Holdout t over all {len(H)} configs with n >= 30: mean {dist.mean():.2f}, sd {dist.std():.2f}, "
           f"{int((dist > 0).sum())} of {len(H)} > 0, max {dist.max():.2f}, min {dist.min():.2f}. "
           f"Holdout net c: median {H.net_c_holdout.median():.2f}, {int((H.net_c_holdout > 0).sum())} of {len(H)} > 0."]
    (OUT / "experiments_report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "grid", "report", "selftest"])
    ap.add_argument("--entry", help="e.g. forced:model:8 | taker:edge=3,blend=0.25,floor=0")
    ap.add_argument("--exit", default="hold", help="comma list, e.g. hold,sl10,sl10_tp15,trail10,time1m")
    ap.add_argument("--family", default="adhoc")
    ap.add_argument("--families", default="1,2,named")
    ap.add_argument("--no-prints", action="store_true", help="skip the per-print resolution run")
    ap.add_argument("--db", default=str(OUT / "kalshi_trades.db"))
    ap.add_argument("--app-db", default=str(ROOT / "data" / "btcflow.db"))
    args = ap.parse_args()
    if args.cmd == "grid":
        cmd_grid(args)
    elif args.cmd == "run":
        cmd_run(args)
    elif args.cmd == "report":
        cmd_report(args)
    else:
        selftest(load(Path(args.db), Path(args.app_db), tapes=False))


if __name__ == "__main__":
    main()
