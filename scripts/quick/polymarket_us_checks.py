"""Checks on the Polymarket US backfill (scripts/backfill_polymarket_us.py). Read-only on every database.

1. Coverage per span and history data quality (points per window, spacing, empty-book placeholders, gaps).
2. History display prices vs the live recorder's bid/ask (data/research/polymarket_us.db pm_quotes) for overlapping
   windows: which instant a point represents, and how far the display price sits from the ask and the mid.
3. Cross-venue settlement: Polymarket 15m priceToBeat / settlementPrice / result vs Kalshi KXBTC15M floor_strike /
   expiration_value / result (data/btcflow.db kalshi_markets), joined on window start = Kalshi open_ts.

    python scripts/dev.py script scripts/quick/polymarket_us_checks.py
"""

from __future__ import annotations

import bisect
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]


def ro(p: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)


H = ro(ROOT / "data/research/polymarket_us_history.db")
Q = ro(ROOT / "data/research/polymarket_us.db")
K = ro(ROOT / "data/btcflow.db")


def coverage() -> None:
    print("== 1. Coverage")
    for span, ok, miss, first, last, pts in H.execute("""
        SELECT span, SUM(http_status=200), SUM(http_status<>200), MIN(CASE WHEN http_status=200 THEN window_start END),
               MAX(CASE WHEN http_status=200 THEN window_start END), SUM(n_points) FROM pmh_windows GROUP BY span"""):
        import datetime as dt
        f = lambda t: dt.datetime.fromtimestamp(t, dt.UTC).strftime("%Y-%m-%d %H:%MZ")
        print(f"{span}: {ok} windows {f(first)} .. {f(last)}; 404 {miss}; points {pts}")
        # missing (404) windows between first and last
        holes = H.execute("SELECT COUNT(*) FROM pmh_windows WHERE span=? AND http_status<>200 AND window_start BETWEEN ? AND ?",
                          (span, first, last)).fetchone()[0]
        print(f"   404 inside the covered range: {holes}")
        st = Counter(r[0] for r in H.execute("SELECT status FROM pmh_windows WHERE span=? AND http_status=200", (span,)))
        print("   status:", dict(st))
        npb = H.execute("SELECT SUM(price_to_beat IS NULL), SUM(settlement_price IS NULL), SUM(result IS NULL),"
                        " SUM(result<>result_outcome) FROM pmh_windows WHERE span=? AND http_status=200", (span,)).fetchone()
        print(f"   priceToBeat null {npb[0]}, settlement null {npb[1]}, result null {npb[2]}, "
              f"computed result != market outcome {npb[3]}")
        # in-window history quality
        sec = {"5m": 300, "15m": 900, "1h": 3600}[span]
        rows = H.execute("""SELECT w.slug, w.window_start, p.ts, p.long_price, p.short_price FROM pmh_windows w
                            JOIN pmh_points p ON p.slug=w.slug WHERE w.span=? AND p.ts>=w.window_start
                            AND p.ts<w.window_end ORDER BY w.slug, p.ts""", (span,)).fetchall()
        per = defaultdict(list)
        for slug, ws, ts, lp, sp in rows:
            per[(slug, ws)].append((ts, lp, sp))
        nwin = ok
        expect = sec // 60
        npts = np.array([len(v) for v in per.values()] + [0] * (nwin - len(per)))
        gaps = Counter()
        off = Counter()
        empty = full_empty = 0
        sums = []
        for (_slug, ws), v in per.items():
            ts = [t for t, _, _ in v]
            off.update((t - ws) % 60 for t in ts)
            gaps.update(np.diff(ts).tolist())
            e = [lp == 0 and sp == 1 for _, lp, sp in v]
            empty += sum(e)
            full_empty += all(e)
            sums += [round(lp + sp, 2) for _, lp, sp in v if not (lp == 0 and sp == 1) and lp and sp]
        print(f"   in-window points/window: expected {expect}; mean {npts.mean():.2f}, min {npts.min()}, "
              f"=expected {np.mean(npts == expect):.1%}, zero {np.sum(npts == 0)}")
        print(f"   seconds offset of points within the minute: {dict(off.most_common(3))}; spacing: {dict(gaps.most_common(4))}")
        print(f"   empty-book placeholder points (long 0 / short 1): {empty} of {len(rows)}; windows all-empty {full_empty}")
        if sums:
            c = Counter(sums)
            print(f"   long+short (implied spread+1) on non-empty points: {dict(sorted(c.most_common(6)))}")


def vs_recorder() -> None:
    print("\n== 2. History vs live recorder bid/ask")
    qs = defaultdict(list)
    for slug, ts_ms, bid, ask in Q.execute("SELECT slug, ts_ms, bid, ask FROM pm_quotes ORDER BY slug, ts_ms"):
        qs[slug].append((ts_ms / 1000, bid, ask))
    pts = H.execute("""SELECT p.slug, p.ts, p.long_price, p.short_price, w.window_start, w.window_end FROM pmh_points p
                       JOIN pmh_windows w ON w.slug=p.slug WHERE p.ts>=w.window_start AND p.ts<w.window_end""").fetchall()
    pts = [p for p in pts if p[0] in qs]
    print(f"overlapping windows {len({p[0] for p in pts})}, points {len(pts)}")
    if not pts:
        return

    def quote_at(slug, t):
        v = qs[slug]
        i = bisect.bisect_right([x[0] for x in v], t) - 1
        if i < 0 or t - v[i][0] > 3:
            return None
        return v[i]

    best = None
    for o in range(-60, 61, 5):
        n = ma = mb = 0
        for slug, ts, lp, sp, *_ in pts:
            q = quote_at(slug, ts + o)
            if not q or q[1] is None or q[2] is None:
                continue
            n += 1
            ma += abs(lp - q[2]) < 1e-9
            mb += abs(sp - (1 - q[1])) < 1e-9
        if n:
            print(f"   offset {o:+3d}s: n={n} long==ask {ma / n:.1%}  short==1-bid {mb / n:.1%}")
            if best is None or ma / n > best[1]:
                best = (o, ma / n)
    o = best[0]
    da, dm, spr = [], [], []
    for slug, ts, lp, _sp, *_ in pts:
        q = quote_at(slug, ts + o)
        if not q or q[1] is None or q[2] is None:
            continue
        da.append(lp - q[2])
        dm.append(lp - (q[1] + q[2]) / 2)
        spr.append(q[2] - q[1])
    da, dm, spr = map(np.array, (da, dm, spr))
    print(f"   at best offset {o:+d}s: long - ask: mean {da.mean()*100:+.2f}c, |.| median {np.median(abs(da))*100:.1f}c, "
          f"p90 {np.quantile(abs(da), .9)*100:.1f}c, exact {np.mean(abs(da) < 1e-9):.1%}, within 1c {np.mean(abs(da) < .0101):.1%}")
    print(f"   long - mid: mean {dm.mean()*100:+.2f}c, |.| median {np.median(abs(dm))*100:.1f}c; recorder spread median "
          f"{np.median(spr)*100:.1f}c mean {spr.mean()*100:.2f}c")
    # how much the ask moves inside a minute (staleness of a 1-minute point)
    mv = []
    for _slug, v in qs.items():
        a = [(t, ask) for t, _, ask in v if ask is not None]
        for i in range(len(a)):
            j = bisect.bisect_left([x[0] for x in a], a[i][0] + 30)
            if j < len(a) and a[j][0] - a[i][0] < 33:
                mv.append(abs(a[j][1] - a[i][1]))
    mv = np.array(mv)
    print(f"   recorder ask change over 30 s: median {np.median(mv)*100:.1f}c, p90 {np.quantile(mv, .9)*100:.1f}c")


def kalshi() -> None:
    print("\n== 3. Polymarket 15m vs Kalshi KXBTC15M")
    km = {o // 1000: (t, fs, ev, r) for t, o, fs, ev, r in K.execute(
        "SELECT ticker, open_ts, floor_strike, expiration_value, result FROM kalshi_markets WHERE ticker LIKE 'KXBTC15M%'")}
    pw = H.execute("""SELECT w.slug, w.window_start, w.price_to_beat, p.settlement_price, w.settlement_price, w.result,
                      w.result_outcome FROM pmh_windows w LEFT JOIN pmh_windows p ON p.span=w.span AND
                      p.window_end=w.window_start WHERE w.span='15m' AND w.http_status=200 ORDER BY w.window_start""").fetchall()
    n = ptb_eq = sp_eq = res_eq = out_eq = ptb_n = 0
    mism = []
    for slug, ws, ptb, prev_sp, sp, res, out in pw:
        if ws not in km:
            continue
        t, fs, ev, kr = km[ws]
        if kr not in ("yes", "no") or sp is None:
            continue
        n += 1
        strike = ptb if ptb is not None else prev_sp
        if strike is not None and fs is not None:
            ptb_n += 1
            ptb_eq += abs(strike - fs) < 0.005
        sp_eq += ev is not None and abs(sp - ev) < 0.005
        kres = "up" if kr == "yes" else "down"
        res = None if strike is None else ("up" if sp >= strike else "down")
        res_eq += res == kres
        out_eq += out == kres
        if res != kres or out != kres or ev is None or abs(sp - ev) >= 0.005 or (strike is not None and fs is not None
                                                                                and abs(strike - fs) >= 0.005):
            mism.append((slug, t, strike, fs, sp, ev, res, out, kr))
    print(f"n={n}; strike equal {ptb_eq}/{ptb_n}; settlement equal {sp_eq}/{n}; computed result agrees {res_eq}/{n}; "
          f"market outcome agrees {out_eq}/{n}")
    ptb_null = sum(1 for r in pw if r[2] is None and r[1] in km)
    print(f"Polymarket priceToBeat null on {ptb_null} overlapping windows (strike taken from previous window's settlement)")
    ties = sum(1 for r in pw if r[1] in km and r[4] is not None and (r[2] or r[3]) is not None and abs(r[4] - (r[2] or r[3])) < 0.005)
    print(f"exact ties (settlement == strike) in overlap: {ties}")
    print(f"mismatches: {len(mism)}")
    for m in mism[:40]:
        print("  ", m)


if __name__ == "__main__":
    coverage()
    vs_recorder()
    kalshi()
