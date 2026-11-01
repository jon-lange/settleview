"""Order flow on KXBTC15M: panic (O1), taker imbalance (O2), whales (O3), and PNW panic-vs-index-stop.

Pre-registered in docs/research/order-flow.md (section 2) before any outcome was computed. Read-only on every
database (SQLite mode=ro). Appends the 20 tradable configs to data/research/experiments_results.csv
(family "order_flow"; res "minute" is the harness's counting label, the actual resolution is print/second).

  .venv/Scripts/python.exe scripts/quick/order_flow.py            # everything, prints markdown tables

Sample: the 435 full-tape windows in data/research/kalshi_trades.db (last 820 s of each window), split
chronologically 60% train / 40% test. Bid/ask proxy from the tape: UP ask = latest taker-YES print, UP bid =
latest taker-NO print (yes price), each within 10 s, prints with ts < t only.
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, UTC
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import experiments as X  # noqa: E402

SLIP = 0.01
MAXAGE = 10_000
SCAN0, SCAN1 = 750_000, 240_000  # scan from 12:30 to 4:00 before the close
BTC_JUST = 15.0
FAM = "order_flow"
fee = lambda p: float(X.fee(p))  # noqa: E731


# ------------------------------------------------------------------------------------------- data
def load():
    D = X.load(X.OUT / "kalshi_trades.db", ROOT / "data" / "btcflow.db", tapes=False)
    c = X.ro(X.OUT / "kalshi_trades.db")
    full = {m for (m,) in c.execute("SELECT mkt FROM done WHERE kind='full'")}
    row = {int(m): i for i, m in enumerate(D.mk.mkt)}
    keep = np.array(sorted(m for m in full if m in row), float)
    parts, cur = [], c.execute("SELECT mkt, ts_ms, yes_price, count, taker_yes FROM trades")
    while True:
        ch = cur.fetchmany(2_000_000)
        if not ch:
            break
        a = np.array(ch, dtype=np.float64)
        parts.append(a[np.isin(a[:, 0], keep)])
    a = np.concatenate(parts)
    a = a[np.lexsort((a[:, 1], a[:, 0]))]
    ids, st = np.unique(a[:, 0], return_index=True)
    en = list(st[1:]) + [len(a)]
    T = {}
    for m, s, e in zip(ids, st, en, strict=True):
        b = a[s:e]
        y = b[:, 4] == 1
        T[row[int(m)]] = dict(yts=b[y, 1].astype(np.int64), ypx=b[y, 2], ycn=b[y, 3],
                              nts=b[~y, 1].astype(np.int64), npx=b[~y, 2], ncn=b[~y, 3])
    rows = sorted(T, key=lambda r: int(D.mk.open_ts.iat[r]))
    ntr = int(round(0.6 * len(rows)))
    test = {r: (i >= ntr) for i, r in enumerate(rows)}
    print(f"tapes: {len(rows)} windows, {len(a):,} prints; train {ntr}, test {len(rows) - ntr}; "
          f"{D.mk.day.iat[rows[0]]} .. {D.mk.day.iat[rows[ntr - 1]]} | {D.mk.day.iat[rows[ntr]]} .. "
          f"{D.mk.day.iat[rows[-1]]}", flush=True)
    return D, T, rows, test


def last(ts, px, t):
    t = np.atleast_1d(np.asarray(t, np.int64))
    if not len(ts):
        return np.full(len(t), np.nan)
    i = np.searchsorted(ts, t, "left") - 1
    ii = np.clip(i, 0, None)
    return np.where((i >= 0) & (ts[ii] >= t - MAXAGE), px[ii], np.nan)


class W:
    """Per-window proxy quotes."""

    def __init__(self, D, T, r):
        self.r, self.tp = r, T[r]
        self.close = int(D.mk.close_ts.iat[r])
        self.open = int(D.mk.open_ts.iat[r])
        self.y = int(D.y[r])
        self.strike = float(D.strike[r])

    def ask(self, t):
        return last(self.tp["yts"], self.tp["ypx"], t)

    def bid(self, t):
        return last(self.tp["nts"], self.tp["npx"], t)

    def mid(self, t):
        return (self.ask(t) + self.bid(t)) / 2

    def side_mid(self, t, s):
        m = self.mid(t)
        return m if s > 0 else 1 - m


def trade(w: W, t: int, s: int, H):
    """Buy side s at its proxy ask at t + 1 s (+1c, fee); hold, or sell at its proxy bid at t + H (-1c, fee)."""
    a = w.ask(t + 1000)[0] if s > 0 else 1 - w.bid(t + 1000)[0]
    if a != a:
        return None
    paid = min(0.99, round(a + SLIP, 4))
    cost = paid + fee(paid)
    won = float((s > 0) == (w.y == 1))
    if H is not None:
        b = w.bid(t + H)[0] if s > 0 else 1 - w.ask(t + H)[0]
        if b == b:
            sale = max(0.0, round(b - SLIP, 4))
            return sale - fee(sale) - cost
    return won - cost


def tstat(v):
    v = np.asarray([x for x in v if x == x], float)
    n = len(v)
    if n < 3 or v.std(ddof=1) == 0:
        return n, float(v.mean()) if n else float("nan"), float("nan")
    return n, float(v.mean()), float(v.mean() / (v.std(ddof=1) / math.sqrt(n)))


def desc(events, rows, test):
    """events: dict r -> list of (R60, R180, CAL); window-averaged, then t across windows, per split."""
    out = {}
    for split in ("train", "test"):
        per = [np.nanmean(np.array(events[r]), axis=0) for r in rows if test[r] == (split == "test") and events.get(r)]
        per = np.array(per) if per else np.zeros((0, 3))
        out[split] = [tstat(per[:, j] * 100) for j in range(3)] + [sum(len(events[r]) for r in rows
                                                                       if test[r] == (split == "test")
                                                                       and events.get(r))]
    return out


def fmt_desc(name, d):
    s = []
    for split in ("train", "test"):
        (n, m1, t1), (_, m2, t2), (_, m3, t3), ne = d[split]
        s.append(f"| {name} | {split} | {n} / {ne} | {m1:+.2f} ({t1:+.2f}) | {m2:+.2f} ({t2:+.2f}) | "
                 f"{m3:+.2f} ({t3:+.2f}) |")
    return s


DESC_HDR = ["| test | split | windows / events | R60 ¢ (t) | R180 ¢ (t) | CAL ¢ (t) |", "|---|---|---|---|---|---|"]


def score(name, firsts, rows, test, run_ts, csv_rows, md):
    """firsts: dict r -> (t, side) for the first event; returns markdown row per split, appends csv rows."""
    for H, hn in ((None, "hold"), (60_000, "sell60"), (180_000, "sell180")):
        if name[1] is not None and hn not in name[1]:
            continue
        res = {}
        for split in ("train", "test"):
            pnl = []
            for r in rows:
                if test[r] != (split == "test") or r not in firsts:
                    continue
                p = trade(firsts[r][2], firsts[r][0], firsts[r][1], H)
                if p is not None:
                    pnl.append(p)
            st = X.summarize(np.array(pnl))
            res[split] = st
            csv_rows.append({"run_ts": run_ts, "config": f"{name[0]}|{hn}", "family": FAM, "entry": name[0],
                             "exit": hn, "res": "minute", "slip_c": 1,
                             "split": "holdout" if split == "test" else "train",
                             "tried": sum(1 for r in rows if test[r] == (split == "test") and r in firsts), **st})
        tr, te = res["train"], res["test"]
        f = lambda s: (f"{s['n']} | {s.get('win', float('nan')):.1f} | {s.get('be', float('nan')):.1f} | "  # noqa: E731
                       f"{s.get('net_c', float('nan')):+.2f} | {s.get('t', float('nan')):+.2f}") if s["n"] else "0 | | | |"
        md.append(f"| {name[0]} | {hn} | {f(tr)} | {f(te)} |")


TR_HDR = ["| config | exit | train n | win % | BE % | net ¢ | t | test n | win % | BE % | net ¢ | t |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]


# ------------------------------------------------------------------------------------------- O1
def o1(D, T, rows, test, run_ts, csv_rows, md):
    md += ["### O1 — panic (contract moves ≥ X¢ in 60 s, BTC does not)", ""]
    dmd = list(DESC_HDR)
    tmd = list(TR_HDR)
    ctl = list(DESC_HDR)
    for X_ in (8, 12):
        x = X_ / 100
        ev, evj, firsts = {}, {}, {}
        for r in rows:
            w = W(D, T, r)
            secs = np.arange(w.close - SCAN0, w.close - SCAN1 + 1, 1000, dtype=np.int64)
            m, m0 = w.mid(secs), w.mid(secs - 60_000)
            b, b0 = D.btc(secs), D.btc(secs - 60_000)
            dm, db = m - m0, b - b0
            with np.errstate(invalid="ignore"):
                dn = (dm <= -x + X.EPS) & (db > -BTC_JUST)
                up = (dm >= x - X.EPS) & (db < BTC_JUST)
                jdn = (dm <= -x + X.EPS) & (db <= -BTC_JUST)
                jup = (dm >= x - X.EPS) & (db >= BTC_JUST)
            for store, lo, hi in ((ev, dn, up), (evj, jdn, jup)):
                last_t = -10**15
                for j in np.flatnonzero(lo | hi):
                    t = int(secs[j])
                    if t < last_t + 60_000:
                        continue
                    last_t = t
                    s = 1 if lo[j] else -1  # the panicked (fallen) side
                    m_t = w.side_mid(t, s)[0]
                    vals = (w.side_mid(t + 60_000, s)[0] - m_t, w.side_mid(t + 180_000, s)[0] - m_t,
                            float((s > 0) == (w.y == 1)) - m_t)
                    store.setdefault(r, []).append(vals)
                    if store is ev and r not in firsts:
                        firsts[r] = (t, s, w)
        dmd += fmt_desc(f"O1 X={X_}¢ unjustified (panicked side)", desc(ev, rows, test))
        ctl += fmt_desc(f"control X={X_}¢ justified (fallen side)", desc(evj, rows, test))
        score((f"of_o1_panic_fade_x{X_}", None), firsts, rows, test, run_ts, csv_rows, tmd)
    md += ["Descriptive (positive = the panicked side recovers / settles above its post-panic mid):", ""] + dmd
    md += ["", "Control — the same size move when BTC did move ≥ $15 the same way:", ""] + ctl
    md += ["", "Tradable: buy the panicked side (fade), first event per window:", ""] + tmd + [""]


# ------------------------------------------------------------------------------------------- O2
def ols(x, y):
    x, y = np.asarray(x, float), np.asarray(y, float)
    ok = ~np.isnan(x) & ~np.isnan(y)
    x, y = x[ok], y[ok]
    n = len(x)
    if n < 5:
        return n, float("nan"), float("nan")
    xc = x - x.mean()
    b = (xc * (y - y.mean())).sum() / (xc ** 2).sum()
    e = y - y.mean() - b * xc
    se = math.sqrt((e ** 2).sum() / (n - 2) / (xc ** 2).sum())
    return n, b, b / se


def o2(D, T, rows, test, run_ts, csv_rows, md):
    md += ["### O2 — taker order-flow imbalance over the last 60 s", ""]
    dmd = ["| τ | split | n | slope R60 ¢/unit imb (t) | slope R180 (t) | slope CAL (t) | |imb| cut (train P80) |",
           "|---|---|---|---|---|---|---|"]
    tmd = list(TR_HDR)
    for tau in (10, 7, 4):
        recs = {}
        for r in rows:
            w = W(D, T, r)
            t = w.close - tau * 60_000
            tp = w.tp
            Y = tp["ycn"][(tp["yts"] >= t - 60_000) & (tp["yts"] < t)].sum()
            N = tp["ncn"][(tp["nts"] >= t - 60_000) & (tp["nts"] < t)].sum()
            if Y + N <= 0:
                continue
            imb = (Y - N) / (Y + N)
            m_t = w.mid(t)[0]
            recs[r] = (imb, 100 * (w.mid(t + 60_000)[0] - m_t), 100 * (w.mid(t + 180_000)[0] - m_t),
                       100 * (w.y - m_t), t, w)
        cut = float(np.quantile([abs(v[0]) for r, v in recs.items() if not test[r]], 0.8))
        for split in ("train", "test"):
            R = [recs[r] for r in rows if r in recs and test[r] == (split == "test")]
            im = [v[0] for v in R]
            cells = []
            for j in (1, 2, 3):
                n, b, tt = ols(im, [v[j] for v in R])
                cells.append(f"{b:+.2f} ({tt:+.2f})")
            dmd.append(f"| {tau} min | {split} | {n} | " + " | ".join(cells) + f" | {cut:.3f} |")
        for mode in ("follow", "fade"):
            firsts = {}
            for r, v in recs.items():
                if abs(v[0]) >= cut and v[0] != 0:
                    s = int(np.sign(v[0])) * (1 if mode == "follow" else -1)
                    firsts[r] = (v[4], s, v[5])
            score((f"of_o2_imb_{mode}_tau{tau}", ("hold",)), firsts, rows, test, run_ts, csv_rows, tmd)
    md += ["OLS of the UP mid's change / settlement residual on imb ∈ [−1, 1] (one obs per window):", ""] + dmd
    md += ["", "Tradable (|imb| ≥ train P80, hold to settlement):", ""] + tmd + [""]


# ------------------------------------------------------------------------------------------- O3
def orders(w: W):
    """Taker orders: prints aggregated by (ms, side). Returns ts, side (+1 YES / -1 NO), notional $."""
    out = []
    for key, s in (("y", 1), ("n", -1)):
        ts, px, cn = w.tp[key + "ts"], w.tp[key + "px"], w.tp[key + "cn"]
        if not len(ts):
            continue
        u, st = np.unique(ts, return_index=True)
        notional = cn * (px if s > 0 else 1 - px)
        out.append((u, np.full(len(u), s), np.add.reduceat(notional, st), np.add.reduceat(cn, st)))
    ts = np.concatenate([o[0] for o in out])
    o = np.argsort(ts, kind="stable")
    return ts[o], np.concatenate([o_[1] for o_ in out])[o], np.concatenate([o_[2] for o_ in out])[o], \
        np.concatenate([o_[3] for o_ in out])[o]


def o3(D, T, rows, test, run_ts, csv_rows, md):
    md += ["### O3 — whales (top 1% by notional, train-set cut)", ""]
    O, B = {}, {}
    for r in rows:
        w = W(D, T, r)
        ts, sd, nt, cn = orders(w)
        sc = (ts >= w.close - SCAN0) & (ts < w.close - SCAN1)
        O[r] = (w, ts[sc], sd[sc], nt[sc], cn[sc])
        q = (ts - w.close) // 10_000
        end = w.close + (q + 1) * 10_000
        df = pd.DataFrame({"end": end, "v": sd * nt})
        g = df.groupby("end").v.sum()
        e = g.index.to_numpy(np.int64)
        k = (e >= w.close - SCAN0) & (e <= w.close - SCAN1)
        B[r] = (e[k], g.to_numpy()[k])
    tr_rows = [r for r in rows if not test[r]]
    cut1 = float(np.quantile(np.concatenate([O[r][3] for r in tr_rows]), 0.99))
    cut2 = float(np.quantile(np.abs(np.concatenate([B[r][1] for r in tr_rows])), 0.99))
    allo = np.concatenate([O[r][3] for r in rows])
    allc = np.concatenate([O[r][4] for r in rows])
    yes_share = np.mean(np.concatenate([O[r][2] for r in rows]) > 0)
    md += [f"Taker orders in the scan range (12:30–4:00 left), all 435 windows: {len(allo):,} orders, "
           f"{100 * yes_share:.1f}% buy YES; notional median ${np.median(allo):.2f}, P90 ${np.quantile(allo, .9):.2f}, "
           f"P99 ${np.quantile(allo, .99):.0f}, max ${allo.max():,.0f}; contracts median {np.median(allc):.1f}, "
           f"P99 {np.quantile(allc, .99):.0f}. **Train cuts:** single order ≥ ${cut1:,.0f}; 10-s net ≥ ${cut2:,.0f}.",
           ""]
    dmd = list(DESC_HDR)
    tmd = list(TR_HDR)
    for kind in ("single", "cluster"):
        ev, firsts = {}, {}
        for r in rows:
            w = O[r][0]
            if kind == "single":
                _, ts, sd, nt, _c = O[r]
                hit = np.flatnonzero(nt >= cut1)
                cand = [(int(ts[j]) + 1, int(sd[j])) for j in hit]
            else:
                e, v = B[r]
                hit = np.flatnonzero(np.abs(v) >= cut2)
                cand = [(int(e[j]), int(np.sign(v[j]))) for j in hit]
            last_t = -10**15
            for t, s in cand:
                if t < last_t + 10_000:
                    continue
                last_t = t
                m_t = w.side_mid(t + 1000, s)[0]
                ev.setdefault(r, []).append((w.side_mid(t + 61_000, s)[0] - m_t, w.side_mid(t + 181_000, s)[0] - m_t,
                                             float((s > 0) == (w.y == 1)) - m_t))
                if r not in firsts:
                    firsts[r] = (t, s, w)
        dmd += fmt_desc(f"O3 {kind} (whale side)", desc(ev, rows, test))
        for mode in ("follow", "fade"):
            f2 = {r: (t, s if mode == "follow" else -s, w) for r, (t, s, w) in firsts.items()}
            score((f"of_o3_{kind}_{mode}", ("hold", "sell180")), f2, rows, test, run_ts, csv_rows, tmd)
    md += ["Descriptive (positive = the whale's side keeps rising / settles above its mid 1 s after the whale):",
           ""] + dmd + ["", "Tradable (first whale per window; follow = buy the whale's side):", ""] + tmd + [""]


# ------------------------------------------------------------------------------------------- PNW
def pnw(D, T, md):
    md += ["### PNW positions: contract-price panic against the position (step 3)", ""]
    k, side = X.ENTRIES["pnw_tune"](D)
    Bd = None
    open_ = D.mk.open_ts.to_numpy(np.int64)
    btcm = D.btc_minutes()
    YB, YA = D.YB, D.YA
    days = sorted(D.mk.day.unique())
    half1 = set(days[:15])
    recs = []
    for r in np.flatnonzero(k >= 0):
        s = int(side[r])
        sb = YB[r] if s > 0 else 1 - YA[r]
        sm = (YB[r] + YA[r]) / 2 if s > 0 else 1 - (YB[r] + YA[r]) / 2
        b0 = sb[k[r]]
        if b0 != b0:
            continue
        for kk in range(int(k[r]) + 1, 15):
            cush = s * (btcm[r, kk] - D.strike[r])
            if sb[kk] == sb[kk] and sb[kk] <= b0 - 0.08 + X.EPS and cush > 0:
                won = float((s > 0) == (D.y[r] == 1))
                recs.append(dict(r=r, kk=kk, bid=sb[kk], mid=sm[kk], won=won, cush=cush,
                                 hold_minus_sell=won - (sb[kk] - fee(sb[kk])), h1=D.mk.day.iat[r] in half1))
                break
    n_entries = int((k >= 0).sum())
    df = pd.DataFrame(recs)

    def line(name, d):
        n, m, t = tstat(100 * (d.won - d.mid))
        _, m2, t2 = tstat(100 * d.hold_minus_sell)
        return (f"| {name} | {len(d)} | {100 * d.won.mean():.1f} | {100 * d.bid.mean():.1f} | {100 * d.mid.mean():.1f} | "
                f"{m:+.1f} ({t:+.2f}) | {m2:+.1f} ({t2:+.2f}) |")
    hdr = ["| subset | n | won % | mean bid ¢ | mean mid ¢ | won − mid ¢ (t) | hold − sell-at-bid net of fee ¢ (t) |",
           "|---|---|---|---|---|---|---|"]
    md += [f"Minute resolution, all 2,848 windows: {n_entries} PNW entries; {len(df)} had a panic "
           f"(side bid ≤ entry bid − 8¢ at a later minute mark, ≥ 1 min left, BTC still past the strike).", ""] + hdr
    md.append(line("all", df))
    md.append(line("BTC cushion ≥ $20 at panic", df[df.cush >= 20]))
    md.append(line("BTC cushion < $20 at panic", df[df.cush < 20]))
    md.append(line("days 1–15", df[df.h1]))
    md.append(line("days 16–30", df[~df.h1]))
    # second resolution on the tapes
    recs2 = []
    for r in np.flatnonzero(k >= 0):
        if r not in T:
            continue
        w = W(D, T, r)
        s = int(side[r])
        te = int(open_[r] + k[r] * 60_000)
        secs = np.arange(te, w.close - 60_000 + 1, 1000, dtype=np.int64)
        sb = w.bid(secs) if s > 0 else 1 - w.ask(secs)
        sm = w.mid(secs) if s > 0 else 1 - w.mid(secs)
        b0 = sb[0]
        if b0 != b0:
            continue
        with np.errstate(invalid="ignore"):
            cush = s * (D.btc(secs) - w.strike)
            hit = np.flatnonzero((sb <= b0 - 0.08 + X.EPS) & (cush > 0))
        if len(hit):
            j = hit[0]
            won = float((s > 0) == (w.y == 1))
            recs2.append(dict(bid=sb[j], mid=sm[j], won=won, cush=cush[j],
                              hold_minus_sell=won - (sb[j] - fee(sb[j]))))
    d2 = pd.DataFrame(recs2)
    n_tape = sum(1 for r in np.flatnonzero(k >= 0) if r in T)
    md += ["", f"Second resolution on the full tapes ({n_tape} PNW entries; proxy bid = latest taker-NO/YES print):",
           ""] + hdr
    if len(d2):
        md.append(line("all (tapes)", d2))
        md.append(line("cushion ≥ $20 (tapes)", d2[d2.cush >= 20]))
        md.append(line("cushion < $20 (tapes)", d2[d2.cush < 20]))
    md.append("")
    return Bd


def main():
    D, T, rows, test = load()
    run_ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ")
    md, csv_rows = [], []
    o1(D, T, rows, test, run_ts, csv_rows, md)
    o2(D, T, rows, test, run_ts, csv_rows, md)
    o3(D, T, rows, test, run_ts, csv_rows, md)
    pnw(D, T, md)
    X.append(csv_rows)
    raw = pd.read_csv(X.RESULTS)
    nconf = raw[raw.res == "minute"].config.nunique()
    from scipy.stats import norm
    bar = float(norm.isf(0.05 / nconf))
    md.append(f"Configs counted in experiments_results.csv after this run: {nconf:,} → one-sided Bonferroni bar "
              f"t ≥ {bar:.2f} (this run added {len({r['config'] for r in csv_rows})} configs).")
    out = "\n".join(md)
    (X.OUT / "order_flow_results.md").write_text(out, encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
