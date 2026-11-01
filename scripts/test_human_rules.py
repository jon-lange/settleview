"""Test popular human trading rules for KXBTC15M on Kalshi's own trade tape (read-only research).

Pre-registered in docs/research/2026-10-28-human-strategies.md (rules H1-H17, criteria, execution model).
Reads data/research/kalshi_trades.db (built by scripts/model_vs_market.py) and the per-second BTC index in
data/btcflow.db, both opened read-only (mode=ro). Orders nothing. Writes only under data/research/:
  human_rules_entries.csv   one row per rule entry
  human_rules_results.md    the results tables pasted into the doc

Run from the repo root:  .venv/Scripts/python.exe scripts/test_human_rules.py
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from datetime import datetime, UTC
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.special import ndtr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from btc_signal_engine.domain.settlement.fees import kalshi_fee  # noqa: E402

fee = lru_cache(maxsize=None)(kalshi_fee)
LOOK = 30_000  # quote proxies: latest print of each taker side in [s - 30 s, s)
FILL_MS = 2_000  # entries fill at the first taker-buy print for the side in [s, s + 2 s)
EPS = 1e-9
SLIPS = (0.0, 0.01)
TAPE_S = 820  # full tapes cover the last 13m40s of each window
T_CRIT_P = 2 * (1 - ndtr(2.0))  # two-sided p of t = 2 (0.0455)
MIN_N = 30

RULES = {
    "H1": "Late favourite 90-98c at 3/2/1 min left",
    "H2": "Last-minute favourite 85-95c at 1 min left",
    "H3": "Late favourite 90-98c + BTC far from strike (2/1 min left)",
    "H4": "BTC momentum +-0.4% at 3/5 min elapsed",
    "H5": "BTC lag momentum +-0.15%, min 5-9 elapsed, ask <= 90c",
    "H6": "Favourite 75-90c at 8/6/4 min left (longshot bias)",
    "H7": "Cheap early longshot 3-20c at 12/10 min left",
    "H8": "Contract momentum: mid +10c in 60 s",
    "H9": "Fade: buy side whose mid fell 10c in 60 s",
    "H10": "Cross 50: mid <50c 30 s ago, now >=55c",
    "H11": "Volume spike (3x, >=100 contracts): follow taker flow",
    "H12": "Rolling 5-min low (mean reversion), 20-80c",
    "H13": "Round-number (25/50/75c) support bounce",
    "H14": "Pair both sides when YES ask + NO ask <= 98c",
    "H15": "Late favourite 90-98c, spread <= 2c (scan last 3 min)",
    "H16": "Late favourite 90-98c, no spread filter (scan last 3 min)",
    "H17": "First minutes: early leader 55-75c (min 1:50-3:00 elapsed)",
}


class Tape:
    """Prints of one market, split by taker side, chronological, prices in YES terms."""

    def __init__(self, ts, yp, cnt, ty) -> None:
        y = ty == 1
        self.side = {True: (ts[y], yp[y], cnt[y]), False: (ts[~y], yp[~y], cnt[~y])}
        self.ts, self.cnt = ts, cnt

    def last(self, taker_yes: bool, t):
        st, sp, _ = self.side[taker_yes]
        t = np.atleast_1d(np.asarray(t, np.int64))
        if not len(st):
            return np.full(len(t), np.nan), np.full(len(t), np.nan)
        i = np.searchsorted(st, t, "left") - 1
        ii = np.clip(i, 0, None)
        ok = (i >= 0) & (st[ii] >= t - LOOK)
        return np.where(ok, sp[ii], np.nan), np.where(ok, (t - st[ii]).astype(float), np.nan)

    def fill(self, side: str, s: int, limit: float):
        """First taker-buys print for `side` in [s, s + 2 s) at or under `limit` (side terms) -> price or None."""
        st, sp, _ = self.side[side == "UP"]
        i0, i1 = np.searchsorted(st, [s, s + FILL_MS], "left")
        for j in range(i0, i1):
            p = round(float(sp[j]) if side == "UP" else 1.0 - float(sp[j]), 4)
            if p <= limit + EPS:
                return p
        return None

    def vol(self, taker_yes: bool | None, a, b):
        """Contracts printed in [a, b) (vectorised over a, b), all prints or one taker side."""
        if taker_yes is None:
            st, c = self.ts, self.cnt
        else:
            st, _, c = self.side[taker_yes]
        cs = np.concatenate([[0.0], np.cumsum(c)])
        return cs[np.searchsorted(st, b, "left")] - cs[np.searchsorted(st, a, "left")]


def load_tapes(c: sqlite3.Connection, table: str, keep: set[int]) -> dict[int, Tape]:
    cols = "mkt, ts_ms, yes_price, count, taker_yes"
    parts, cur = [], c.execute(f"SELECT {cols} FROM {table}")  # noqa: S608 - table name is a literal from the caller
    while True:
        chunk = cur.fetchmany(1_000_000)
        if not chunk:
            break
        a = np.array(chunk, dtype=np.float64)
        parts.append(a[np.isin(a[:, 0], np.fromiter(keep, np.float64, len(keep)))])
    if not parts:
        return {}
    a = np.concatenate(parts)
    a = a[np.lexsort((a[:, 1], a[:, 0]))]  # stable: same-ms prints keep insertion order
    mk, starts = np.unique(a[:, 0], return_index=True)
    ends = list(starts[1:]) + [len(a)]
    return {int(m): Tape(a[s:e, 1].astype(np.int64), a[s:e, 2], a[s:e, 3], a[s:e, 4].astype(np.int64))
            for m, s, e in zip(mk, starts, ends, strict=True)}


class Index:
    def __init__(self, app: sqlite3.Connection, lo: int, hi: int) -> None:
        a = np.array(app.execute("SELECT ts, price FROM index_1s WHERE ts >= ? AND ts < ? ORDER BY ts",
                                 (lo, hi)).fetchall(), dtype=np.float64)
        self.ts, self.px = a[:, 0].astype(np.int64), a[:, 1]

    def at(self, s: int) -> float | None:
        """Latest index price with ts < s - 1 s, if it is no older than 10 s."""
        i = int(np.searchsorted(self.ts, s - 1_000, "left")) - 1
        if i < 0 or self.ts[i] < s - 11_000:
            return None
        return float(self.px[i])


def entry(tape: Tape, side: str, s: int, ask: float, cap: float = 0.99, plus: float = 0.01):
    """Fill at the first taker print within 2 s at or under min(seen ask + 1c, cap)."""
    return tape.fill(side, s, min(round(ask + plus, 4), cap))


def net(px: float, won: bool, slip: float) -> tuple[float, float]:
    paid = min(0.99, round(px + slip, 4))
    cost = paid + fee(1, paid)
    return (1.0 if won else 0.0) - cost, cost


# ------------------------------------------------------------------------------------------ fixed-time rules
def quotes(tape: Tape, s: int):
    ya, aa = tape.last(True, s)
    yb, ba = tape.last(False, s)
    ya, yb, aa, ba = float(ya[0]), float(yb[0]), float(aa[0]), float(ba[0])
    up_ask = ya if ya == ya else None
    dn_ask = round(1 - yb, 4) if yb == yb else None
    return up_ask, dn_ask, ya, yb, aa, ba


def fixed_rules(m, tape: Tape, idx: Index) -> list[dict]:
    close, strike = int(m.close_ts), float(m.floor_strike) if m.floor_strike == m.floor_strike else None
    out = []

    def scan(rule, taus, pick):
        for tau in taus:
            s = close - tau * 60_000 - 2_000
            up, dn, *_ = quotes(tape, s)
            got = pick(s, tau, up, dn)
            if got is None:
                continue
            side, ask, cap = got
            px = entry(tape, side, s, ask, cap)
            if px is not None:
                out.append({"rule": rule, "side": side, "px": px, "decided": s, "secs_left": (close - s) / 1000})
                return

    def favourite(lo, hi):
        def pick(s, tau, up, dn):
            c = [(a, sd) for a, sd in ((up, "UP"), (dn, "DOWN")) if a is not None and lo - EPS <= a <= hi + EPS]
            if not c:
                return None
            a, sd = max(c)
            return sd, a, hi
        return pick

    scan("H1", (3, 2, 1), favourite(0.90, 0.98))
    scan("H2", (1,), favourite(0.85, 0.95))
    scan("H6", (8, 6, 4), favourite(0.75, 0.90))
    scan("H7", (12, 10), favourite(0.03, 0.20))

    if strike is not None:
        def far(s, tau, up, dn):
            got = favourite(0.90, 0.98)(s, tau, up, dn)
            px = idx.at(s)
            if got is None or px is None:
                return None
            need = (close - s) / 1000 - 10  # dollars: a line through the published examples
            d = px - strike
            if (got[0] == "UP" and d >= need) or (got[0] == "DOWN" and -d >= need):
                return got
            return None
        scan("H3", (2, 1), far)

        def mom(th, max_ask):
            def pick(s, tau, up, dn):
                px = idx.at(s)
                if px is None:
                    return None
                r = px / strike - 1
                if r >= th and up is not None and up <= max_ask + EPS:
                    return "UP", up, max_ask
                if r <= -th and dn is not None and dn <= max_ask + EPS:
                    return "DOWN", dn, max_ask
                return None
            return pick
        scan("H4", (12, 10), mom(0.004, 0.99))
        scan("H5", (10, 8, 6), mom(0.0015, 0.90))
    return out


# ------------------------------------------------------------------------------------------ scanning rules
def scan_rules(m, tape: Tape) -> list[dict]:
    close = int(m.close_ts)
    t0 = close - TAPE_S * 1000
    secs = np.arange(t0, close, 1000, dtype=np.int64)
    ya, aa = tape.last(True, secs)
    yb, ba = tape.last(False, secs)
    mid = np.where(~np.isnan(ya) & ~np.isnan(yb), (ya + yb) / 2, np.where(np.isnan(ya), yb, ya))
    up_ask, dn_ask = ya, 1 - yb
    rel = (close - secs) / 1000  # seconds to close
    out = []

    def lag(arr, n):
        r = np.full_like(arr, np.nan)
        r[n:] = arr[:-n]
        return r

    def run(rule, cand_up, cand_dn, lo_s, hi_s, cap=0.99, plus=0.01, prefer=None):
        """Candidates at seconds with lo_s >= secs-to-close > hi_s; first fill wins."""
        rng = (rel <= lo_s) & (rel > hi_s)
        cu = np.nan_to_num(cand_up, nan=0).astype(bool) & rng
        cd = np.nan_to_num(cand_dn, nan=0).astype(bool) & rng
        for k in np.flatnonzero(cu | cd):
            sides = [sd for sd, ok in (("UP", cu[k]), ("DOWN", cd[k])) if ok]
            if len(sides) == 2:
                sides = [prefer(k)] if prefer else sides[:1]
            sd = sides[0]
            ask = up_ask[k] if sd == "UP" else dn_ask[k]
            if ask != ask:
                continue
            px = tape.fill(sd, int(secs[k]), min(round(float(ask) + plus, 4), cap))
            if px is not None:
                out.append({"rule": rule, "side": sd, "px": px, "decided": int(secs[k]), "secs_left": float(rel[k])})
                return

    d60 = mid - lag(mid, 60)
    with np.errstate(invalid="ignore"):
        # H8 momentum / H9 fade (12m10s .. 2m before close)
        run("H8", (d60 >= 0.10 - EPS) & (up_ask <= 0.90 + EPS), (d60 <= -0.10 + EPS) & (dn_ask <= 0.90 + EPS), 730, 120)
        run("H9", (d60 <= -0.10 + EPS) & (up_ask >= 0.10 - EPS), (d60 >= 0.10 - EPS) & (dn_ask >= 0.10 - EPS), 730, 120)
        # H10 cross 50
        m30 = lag(mid, 30)
        run("H10", (m30 < 0.50) & (mid >= 0.55 - EPS), (m30 > 0.50) & (mid <= 0.45 + EPS), 760, 120)
        # H11 volume spike
        v30 = tape.vol(None, secs - 30_000, secs)
        base = tape.vol(None, secs - 330_000, secs - 30_000) / 10
        vy, vn = tape.vol(True, secs - 30_000, secs), tape.vol(False, secs - 30_000, secs)
        spike = (v30 >= 3 * base) & (v30 >= 100) & (base > 0)
        run("H11", spike & (vy > vn) & (up_ask >= 0.10 - EPS) & (up_ask <= 0.90 + EPS),
            spike & (vn > vy) & (dn_ask >= 0.10 - EPS) & (dn_ask <= 0.90 + EPS), 490, 120)
        # H12 rolling 5-min low of each side's taker-buy prints
        def bucket_min(taker_yes):
            st, sp, _ = tape.side[taker_yes]
            v = sp if taker_yes else 1 - sp
            b = np.full(len(secs), np.inf)
            k = ((st - t0) // 1000).astype(np.int64)
            ok = (k >= 0) & (k < len(secs))
            np.minimum.at(b, k[ok], v[ok])
            return b  # b[k] = min side price over prints in [secs[k], secs[k] + 1 s)
        def roll_min_before(b):  # min over [s - 300 s, s) = buckets k-300 .. k-1
            r = pd.Series(b).rolling(300, min_periods=300).min().to_numpy()
            return lag(r, 1)
        lo_up, lo_dn = roll_min_before(bucket_min(True)), roll_min_before(bucket_min(False))
        band = lambda a: (a >= 0.20 - EPS) & (a <= 0.80 + EPS)  # noqa: E731
        run("H12", (up_ask <= lo_up + EPS) & band(up_ask), (dn_ask <= lo_dn + EPS) & band(dn_ask), 510, 120)
        # H13 round-number support bounce (side mid)
        mu = pd.Series(mid)
        mn_up = lag(mu.rolling(60, min_periods=30).min().to_numpy(), 1)
        mx_up = lag(mu.rolling(60, min_periods=30).max().to_numpy(), 1)
        cu = np.zeros(len(secs), bool)
        cd = np.zeros(len(secs), bool)
        for L in (0.25, 0.50, 0.75):
            cu |= (mn_up >= L - 0.03 - EPS) & (mn_up <= L + 0.01 + EPS) & (mid >= L + 0.04 - EPS) & (mid <= L + 0.08 + EPS)
            mn_dn, md = 1 - mx_up, 1 - mid  # DOWN mid min over the window = 1 - UP mid max
            cd |= (mn_dn >= L - 0.03 - EPS) & (mn_dn <= L + 0.01 + EPS) & (md >= L + 0.04 - EPS) & (md <= L + 0.08 + EPS)
        run("H13", cu, cd, 730, 120)
        # H15 / H16 late favourite scan
        fav_up = (up_ask >= 0.90 - EPS) & (up_ask <= 0.98 + EPS)
        fav_dn = (dn_ask >= 0.90 - EPS) & (dn_ask <= 0.98 + EPS)
        tight = (aa <= 5_000) & (ba <= 5_000) & (ya - yb <= 0.02 + EPS)
        run("H15", fav_up & tight, fav_dn & tight, 180, 10, cap=0.98)
        run("H16", fav_up, fav_dn, 180, 10, cap=0.98)
        # H17 early leader
        lead_up = (up_ask >= 0.55 - EPS) & (up_ask <= 0.75 + EPS)
        lead_dn = (dn_ask >= 0.55 - EPS) & (dn_ask <= 0.75 + EPS)
        run("H17", lead_up, lead_dn, 790, 720, cap=0.75, prefer=lambda k: "UP" if mid[k] >= 0.5 else "DOWN")
        # H14 pair both sides
        pair = (aa <= 5_000) & (ba <= 5_000) & (up_ask + dn_ask <= 0.98 + EPS)
        rng = (rel <= 790) & (rel > 10)
        for k in np.flatnonzero(np.nan_to_num(pair, nan=0).astype(bool) & rng):
            s = int(secs[k])
            pu = tape.fill("UP", s, round(float(up_ask[k]), 4))
            pd_ = tape.fill("DOWN", s, round(float(dn_ask[k]), 4))
            if pu is None and pd_ is None:
                continue
            out.append({"rule": "H14", "side": "PAIR", "px": pu, "px2": pd_, "decided": s, "secs_left": float(rel[k])})
            break
    return out


# ------------------------------------------------------------------------------------------ scoring
def score(e: dict, result: str) -> dict:
    up = result == "yes"
    if e["rule"] != "H14":
        won = (e["side"] == "UP") == up
        r = {"won": int(won)}
        for s in SLIPS:
            r[f"pnl_{int(s * 100)}c"], r[f"cost_{int(s * 100)}c"] = net(e["px"], won, s)
        return r
    legs = [(p, sd) for p, sd in ((e["px"], "UP"), (e.get("px2"), "DOWN")) if p is not None]
    r = {"legs": len(legs)}
    for s in SLIPS:
        tot, cost = 0.0, 0.0
        for p, sd in legs:
            n_, c_ = net(p, (sd == "UP") == up, s)
            tot, cost = tot + n_, cost + c_
        r[f"pnl_{int(s * 100)}c"], r[f"cost_{int(s * 100)}c"] = tot, cost
    r["won"] = int(r["pnl_1c"] > 0)
    return r


def stats(x: pd.DataFrame, col: str = "pnl_1c") -> dict:
    v = x[col].to_numpy(float)
    n = len(v)
    if n == 0:
        return {"n": 0, "win": np.nan, "be": np.nan, "net": np.nan, "t": np.nan}
    sd = v.std(ddof=1) if n > 1 else np.nan
    t = v.mean() / (sd / math.sqrt(n)) if n > 2 and sd > 0 else np.nan
    return {"n": n, "win": 100 * x.won.mean(), "be": 100 * x[col.replace("pnl", "cost")].mean(),
            "net": 100 * v.mean(), "t": t}


def holm(p: np.ndarray) -> np.ndarray:
    p = np.asarray(p, float)
    order = np.argsort(np.where(np.isnan(p), 2, p))
    m = int((~np.isnan(p)).sum())
    adj = np.full(len(p), np.nan)
    run = 0.0
    for rank, i in enumerate(order):
        if np.isnan(p[i]):
            continue
        run = max(run, min(1.0, (m - rank) * p[i]))
        adj[i] = run
    return adj


def fmt(v, d=1):
    return "n/a" if v is None or v != v else f"{v:.{d}f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "research" / "kalshi_trades.db"))
    ap.add_argument("--app-db", default=str(ROOT / "data" / "btcflow.db"))
    ap.add_argument("--out-dir", default=str(ROOT / "data" / "research"))
    args = ap.parse_args()
    c = sqlite3.connect(f"file:{Path(args.db).as_posix()}?mode=ro", uri=True)
    app = sqlite3.connect(f"file:{Path(args.app_db).as_posix()}?mode=ro", uri=True)
    mk = pd.read_sql("SELECT * FROM markets ORDER BY open_ts", c)
    days = sorted(mk.day.unique())
    n_hold = int(round(len(days) / 3))
    hold = set(days[len(days) - n_hold:])
    half1 = set(days[: len(days) // 2])
    done = c.execute("SELECT kind, mkt FROM done").fetchall()
    full_ok = {m for k, m in done if k == "full"}
    panel_ok = {m for k, m in done if k == "panel"} - full_ok
    full = load_tapes(c, "trades", full_ok)
    panel = load_tapes(c, "panel_trades", panel_ok)
    idx = Index(app, int(mk.open_ts.min()) - 120_000, int(mk.close_ts.max()) + 60_000)
    print(f"markets {len(mk)}, days {days[0]}..{days[-1]} (holdout {min(hold)}..{max(hold)}), "
          f"full tapes {len(full)}, panel-only {len(panel)}, index seconds {len(idx.ts)}", flush=True)

    rows = []
    for k, m in enumerate(mk.itertuples(index=False)):
        tape = full.get(m.mkt) or panel.get(m.mkt)
        if tape is None:
            continue
        ents = fixed_rules(m, tape, idx)
        if m.mkt in full:
            ents += scan_rules(m, tape)
        for e in ents:
            rows.append({"mkt": m.mkt, "ticker": m.ticker, "day": m.day, "holdout": m.day in hold,
                         "half": 1 if m.day in half1 else 2, "full_tape": m.mkt in full, "result": m.result,
                         **e, **score(e, m.result)})
        if (k + 1) % 500 == 0:
            print(f"  {k + 1}/{len(mk)} markets", flush=True)
    E = pd.DataFrame(rows)
    out = Path(args.out_dir)
    E.to_csv(out / "human_rules_entries.csv", index=False)

    # ---- results table
    res = []
    for r, name in RULES.items():
        x = E[E.rule == r] if len(E) else E
        h = stats(x[x.holdout]) if len(x) else stats(x)
        res.append({"rule": r, "name": name, "markets": "all" if r in ("H1", "H2", "H3", "H4", "H5", "H6", "H7") else "full-tape sample",
                    "hold": h, "hold0": stats(x[x.holdout], "pnl_0c") if len(x) else stats(x),
                    "train": stats(x[~x.holdout]) if len(x) else stats(x), "all": stats(x) if len(x) else stats(x),
                    "h1": stats(x[x.half == 1]) if len(x) else stats(x), "h2": stats(x[x.half == 2]) if len(x) else stats(x)})
    t = np.array([q["hold"]["t"] for q in res], float)
    p = np.where(np.isnan(t), np.nan, 2 * (1 - ndtr(np.abs(t))))
    padj = holm(p)
    md = ["| rule | n | win % | break-even % | net c/contract (1c slip) | net at 0c slip | t | Holm p | H1 net | H2 net | verdict |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for q, pa in zip(res, padj, strict=True):
        h = q["hold"]
        ok = (h["n"] >= MIN_N and h["net"] > 0 and h["t"] > 2 and pa < T_CRIT_P
              and q["h1"]["net"] > 0 and q["h2"]["net"] > 0)
        q["verdict"] = "SURVIVES" if ok else ("FAILS (n < 30)" if h["n"] < MIN_N else "FAILS")
        md.append(f"| {q['rule']} {q['name']} | {h['n']} | {fmt(h['win'])} | {fmt(h['be'])} | {fmt(h['net'], 2)} | "
                  f"{fmt(q['hold0']['net'], 2)} | {fmt(h['t'], 2)} | {fmt(pa, 3)} | {fmt(q['h1']['net'], 2)} | "
                  f"{fmt(q['h2']['net'], 2)} | {q['verdict']} |")
    md2 = ["| rule | train n | train win % | train BE % | train net c | train t | all n | all net c | all t |",
           "|---|---|---|---|---|---|---|---|---|"]
    for q in res:
        a, b = q["train"], q["all"]
        md2.append(f"| {q['rule']} | {a['n']} | {fmt(a['win'])} | {fmt(a['be'])} | {fmt(a['net'], 2)} | {fmt(a['t'], 2)} | "
                   f"{b['n']} | {fmt(b['net'], 2)} | {fmt(b['t'], 2)} |")
    extra = []
    if len(E):
        pr = E[E.rule == "H14"]
        if len(pr):
            extra.append(f"H14 pairs: {int((pr.legs == 2).sum())} both legs filled, {int((pr.legs == 1).sum())} one leg only "
                         f"(held naked to settlement).")

    # ---- descriptive calibration (not part of the tested family): UP mid at tau vs outcome, all markets
    cal = []
    for tau in (12, 6, 3, 1):
        recs = []
        for m in mk.itertuples(index=False):
            tape = full.get(m.mkt) or panel.get(m.mkt)
            if tape is None:
                continue
            s = int(m.close_ts) - tau * 60_000 - 2_000
            up, dn, ya, yb, *_ = quotes(tape, s)
            if up is None or dn is None:
                continue
            recs.append(((ya + yb) / 2, 1 if m.result == "yes" else 0))
        if not recs:
            continue
        a = np.array(recs)
        bins = [0, 0.1, 0.25, 0.4, 0.6, 0.75, 0.9, 1.0001]
        cells = []
        for lo, hi in zip(bins[:-1], bins[1:], strict=True):
            sel = (a[:, 0] >= lo) & (a[:, 0] < hi)
            if sel.sum():
                cells.append(f"{int(lo * 100)}-{int(min(hi, 1) * 100)}c: n={int(sel.sum())}, mid {100 * a[sel, 0].mean():.1f}%, "
                             f"UP won {100 * a[sel, 1].mean():.1f}%")
        cal.append(f"- **{tau} min left** ({len(a)} markets): " + "; ".join(cells))

    span = f"{days[0]} to {days[-1]}"
    text = (f"## Results\n\nPeriod {span} ({len(days)} UTC days; holdout {min(hold)} to {max(hold)}). "
            f"{len(mk)} settled markets; {len(full)} have full tapes (the random sample the scanning rules H8-H17 run on); "
            f"the fixed-time rules H1-H7 run on every market with prints at the check times. Generated "
            f"{datetime.now(UTC):%Y-%m-%d %H:%M} UTC by `scripts/test_human_rules.py`.\n\n"
            f"### Holdout (pre-registered test)\n\nNet and break-even are per contract (H14: per pair). t is window-clustered; "
            f"Holm p is adjusted over all {len(res)} rules; H1/H2 net = first/last 15 days of the full period at 1c slippage.\n\n"
            + "\n".join(md) + "\n\n" + ("\n".join(extra) + "\n\n" if extra else "")
            + "### Training two-thirds and full period (information only)\n\n" + "\n".join(md2)
            + "\n\n### Calibration of the market mid (descriptive, all periods, not a tested rule)\n\n" + "\n".join(cal) + "\n")
    (out / "human_rules_results.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
