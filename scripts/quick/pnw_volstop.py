"""PNW exit study 4: volatility-scaled VS(z) / probability P(p) / hybrid H(z) index stops vs live `bstop-10`
(pre-registered in docs/research/pnw-tuning.md, "Exit study 4"). Read-only on the databases.

  python scripts/dev.py script scripts/quick/pnw_volstop.py train     # checks, both views, append to
                                                                  # experiments_results.csv, TRAIN tables, freeze picks
  python scripts/dev.py script scripts/quick/pnw_volstop.py holdout   # frozen picks + bstop-10 + hold, holdout once

View S (primary, all windows): stop checked every second on the 1 s-lagged index; sale at the held side's minute
candle-close bid at the first minute mark >= trigger + 1 s. View T (435 full tapes): sale at the tape-proxy bid like
experiments.PrintSim.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, UTC
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pnw_exits as P  # noqa: E402

X = P.X
sys.path.insert(0, str(X.ROOT))
from btc_signal_engine.domain.coach import fair_value as FV  # noqa: E402

ENTRY = P.ENTRY
FROZEN = X.OUT / "pnw_volstop_picks.json"
LIVE = "bstop-10"
ZS, PS_ = (0.25, 0.5, 0.75, 1.0, 1.5), (0.35, 0.25, 0.15, 0.10)
RULES = [f"VS{z}" for z in ZS] + [f"P{p}" for p in PS_] + [f"H{z}" for z in ZS]
SIG_WIN_MS = 15 * 60_000
LAT_MS = 1_000


def family(e):
    return e[0] if e[0] in "VPH" and e not in ("hold",) else "ref"


# ------------------------------------------------------------------------------------------- features
class Feat:
    """Per-second features for one window: dist = BTC - K (1 s lag), sig_rem ($), p_up (domain model, strike K)."""

    def __init__(self, D):
        self.D = D
        self.ts, self.px = D.idx_ts, D.idx_px
        d2 = np.diff(self.px) ** 2
        self.cs2 = np.concatenate([[0.0], np.cumsum(d2)])
        self.cspx = np.concatenate([[0.0], np.cumsum(self.px)])
        self.cache = {}

    def sigma1s(self, s):
        ts = self.ts
        e = np.searchsorted(ts, s - 1_000, "left")  # rows with ts < s - 1 s
        b = np.searchsorted(ts, s - 1_000 - SIG_WIN_MS, "left")
        nd = e - b - 1  # diffs between consecutive rows inside [b, e)
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(nd >= 10, np.sqrt((self.cs2[np.maximum(e - 1, 0)] - self.cs2[b]) / np.maximum(nd, 1)),
                            np.nan)

    def p_up(self, at, K, close):
        """Vectorised fair_value.fair_from_index(at, rows, K, close)['fair_up'] (complete index assumed; verified)."""
        ts, px = self.ts, self.px
        end = np.searchsorted(ts, at, "left")
        ok = (end > 0) & (ts[np.maximum(end - 1, 0)] >= at - FV.MAX_PRICE_AGE_MS)
        b10 = np.maximum(np.searchsorted(ts, at - FV.PRICE_SMOOTH_S * 1000, "left"), end - FV.PRICE_SMOOTH_S)
        price = (self.cspx[end] - self.cspx[b10]) / np.maximum(end - b10, 1)
        samp = np.stack([px[np.maximum(np.searchsorted(ts, at - k * 60_000, "left") - 1, 0)]
                         for k in range(FV.VOL_MINUTES + 1)], axis=1)
        rets = np.log(samp[:, :-1] / samp[:, 1:])
        last = px[np.maximum(end - 1, 0)]
        sigma = np.maximum(last * np.sqrt((rets ** 2).mean(1) / 60), last * FV.SIGMA_FLOOR_FRAC)
        cstart = close - FV.CLOSE_SECONDS * 1000
        lo = np.searchsorted(ts, cstart, "left")
        m = np.where(at > cstart, np.minimum(end - lo, FV.CLOSE_SECONDS), 0)
        A = np.where(m > 0, self.cspx[lo + m] - self.cspx[lo], 0.0)
        n = FV.CLOSE_SECONDS - m
        fu = np.maximum(at, cstart + m * 1000)
        g = (fu - at) / 1000 + 1
        v = n * n * g + (n - 1) * n * (2 * n - 1) / 6
        sd = sigma * np.sqrt(v)
        gap = A + n * price - FV.CLOSE_SECONDS * K
        from scipy.special import ndtr
        with np.errstate(invalid="ignore", divide="ignore"):
            p = np.where(n == 0, (A >= FV.CLOSE_SECONDS * K - 1e-9).astype(float), ndtr(gap / sd))
        return np.where(ok, p, np.nan)

    def window(self, r):
        if r in self.cache:
            return self.cache[r]
        D = self.D
        o, c = int(D.mk.open_ts.iat[r]), int(D.mk.close_ts.iat[r])
        secs = np.arange(o, c + 1_000, 1_000, dtype=np.int64)
        K = float(D.strike[r])
        dist = D.btc(secs) - K
        sig_rem = self.sigma1s(secs) * np.sqrt((c - secs) / 1000)
        pu = self.p_up(secs - 1_000, K, c)
        self.cache[r] = (secs, dist, sig_rem, pu)
        return self.cache[r]

    def verify(self, rows, n=300, seed=7):
        """Assert the vectorised p_up equals the domain function on random (window, second) samples."""
        D, rng = self.D, np.random.default_rng(seed)
        worst = 0.0
        for r in rng.choice(rows, size=min(n, len(rows)), replace=False):
            o, c = int(D.mk.open_ts.iat[r]), int(D.mk.close_ts.iat[r])
            secs, _, _, pu = self.window(r)
            j = int(rng.integers(6 * 60, len(secs)))  # include closing-minute seconds sometimes
            if rng.random() < 0.3:
                j = int(rng.integers(len(secs) - 62, len(secs)))
            at = int(secs[j]) - 1_000
            lo, hi = np.searchsorted(self.ts, [o - X.HISTORY_MS - 65_000, c + 1_000])
            rws = list(zip(self.ts[lo:hi].tolist(), self.px[lo:hi].tolist(), strict=True))
            f = FV.fair_from_index(at, rws, float(D.strike[r]), c)
            ref = np.nan if f is None else f["fair_up"]
            assert (np.isnan(ref) and np.isnan(pu[j])) or abs(ref - pu[j]) < 1e-6, (r, j, ref, pu[j])
            if not np.isnan(ref):
                worst = max(worst, abs(ref - pu[j]))
        print(f"verify: vectorised P(up) == domain fair_from_index on {n} samples (max |diff| {worst:.1e}; cumsum rounding)")


def fire_mask(F, r, side, secs_sub, e):
    secs, dist, sig_rem, pu = F.window(r)
    i = np.searchsorted(secs, secs_sub)
    d, sr, p = side * dist[i], sig_rem[i], (pu[i] if side > 0 else 1 - pu[i])
    with np.errstate(invalid="ignore"):
        if e == "hold":
            return np.zeros(len(i), bool)
        if e == LIVE:
            return d < -10
        if e.startswith("VS"):
            return d <= -float(e[2:]) * sr
        if e.startswith("H"):
            return (d <= -float(e[1:]) * sr) & (d < -10)
        if e.startswith("P"):
            return p < float(e[1:]) - 1e-12
    raise ValueError(e)


# ------------------------------------------------------------------------------------------- view S
def sim_sec(D, F, k, side, e, slip, marks_only=False):
    """marks_only=True: check only at minute marks, sell at that mark (machinery check vs sim_minute)."""
    res = {"idx": [], "pnl": [], "won": [], "reason": [], "t_fire": []}
    for r in np.flatnonzero(k >= 0):
        s_, kk = int(side[r]), int(k[r])
        o = int(D.mk.open_ts.iat[r])
        px = D.YA[r, kk] if s_ > 0 else 1 - D.YB[r, kk]
        paid = min(0.99, round(float(px) + slip, 4))
        cost = paid + float(X.fee(paid))
        won = (s_ > 0) == (D.y[r] == 1)
        bid = D.YB[r] if s_ > 0 else 1 - D.YA[r]
        if marks_only:
            secs = o + np.arange(kk + 1, X.K_CLOSE + 1, dtype=np.int64) * 60_000
            jm = (secs - o) // 60_000
        else:
            secs = np.arange(o + kk * 60_000 + 1_000, o + X.K_CLOSE * 60_000 + 1_000, 1_000, dtype=np.int64)
            jm = -((o - (secs + LAT_MS)) // 60_000)  # ceil: first mark >= trigger + latency
        okj = jm <= X.K_CLOSE
        b = np.where(okj, bid[np.clip(jm, 0, 16)], np.nan)
        fire = fire_mask(F, r, s_, secs, e) & ~np.isnan(b)
        hit = np.flatnonzero(fire)
        if len(hit):
            sale = max(0.0, round(float(b[hit[0]]) - slip, 4))
            reason, pnl, tf = 4, sale - float(X.fee(sale)) - cost, float(secs[hit[0]])
        else:
            reason, pnl, tf = 0, float(won) - cost, -1.0
        for key, v in (("idx", r), ("pnl", pnl), ("won", won), ("reason", reason), ("t_fire", tf)):
            res[key].append(v)
    return {key: np.asarray(v, dtype=(int if key == "idx" else float if key in ("pnl", "t_fire") else None))
            for key, v in res.items()}


# ------------------------------------------------------------------------------------------- view T
def sim_tape(D, F, PS, k, side, e, slip, latency_ms=LAT_MS):
    res = {"idx": [], "pnl": [], "won": [], "reason": [], "t_fire": []}
    for r in sorted(D.tapes):
        if k[r] < 0:
            continue
        p = PS.path(r, int(k[r]), int(side[r]))
        if p is None:
            continue
        px, sts, spx, close, fill_ts = p
        paid = min(0.99, round(px + slip, 4))
        cost = paid + float(X.fee(paid))
        won = (side[r] > 0) == (D.y[r] == 1)
        t_st, m = np.inf, None
        if e != "hold":
            secs = np.arange((fill_ts // 1000 + 1) * 1000, close, 1000, dtype=np.int64)
            bad = np.flatnonzero(fire_mask(F, r, int(side[r]), secs, e))
            if len(bad):
                t_st = float(secs[bad[0]])
                m = int(np.searchsorted(sts, t_st + latency_ms, "left"))
                m = m if m < len(sts) else None
        if m is not None:
            sale = max(0.0, round(float(spx[m]) - slip, 4))
            reason, pnl = 4, sale - float(X.fee(sale)) - cost
        else:
            reason, pnl = 0, float(won) - cost
        for key, v in (("idx", r), ("pnl", pnl), ("won", won), ("reason", reason),
                       ("t_fire", t_st if m is not None else -1.0)):
            res[key].append(v)
    return {key: np.asarray(v, dtype=(int if key == "idx" else float if key in ("pnl", "t_fire") else None))
            for key, v in res.items()}


def check(D, F, PS, k, side):
    for e in ("hold", LIVE):
        for slip in X.SLIPS:
            a, b = sim_sec(D, F, k, side, e, slip, marks_only=True), X.sim_minute(D, k, side, X.Exit.parse(e), slip)
            assert (a["idx"] == b["idx"]).all() and np.allclose(a["pnl"], b["pnl"], atol=1e-12) \
                and (a["reason"] == b["reason"]).all(), ("minute", e)
            a, b = sim_tape(D, F, PS, k, side, e, slip), PS.run(k, side, X.Exit.parse(e), slip)
            assert (a["idx"] == b["idx"]).all() and np.allclose(a["pnl"], b["pnl"], atol=1e-12) \
                and (a["reason"] == b["reason"]).all(), ("tape", e)
    print("check: sec sim (marks only) == experiments.sim_minute and tape sim == PrintSim.run for hold/bstop-10 (0/1/2c)")


# ------------------------------------------------------------------------------------------- reporting
def ledger(o, ref, m):
    rf, of = ref["reason"][m] == 4, o["reason"][m] == 4
    won = ref["won"][m].astype(bool)
    d = 100 * (o["pnl"][m] - ref["pnl"][m])
    saved, extra, new = rf & ~of & won, rf & ~of & ~won, of & ~rf
    later = rf & of & (o["t_fire"][m] > ref["t_fire"][m])
    earlier = rf & of & (o["t_fire"][m] < ref["t_fire"][m])
    return {"wsaw_n": int(saved.sum()), "wsaw_c": float(d[saved].sum()), "xloss_n": int(extra.sum()),
            "xloss_c": float(d[extra].sum()), "new_n": int(new.sum()), "new_c": float(d[new].sum()),
            "later_n": int(later.sum()), "later_c": float(d[later].sum()),
            "earlier_n": int(earlier.sum()), "earlier_c": float(d[earlier].sum())}


def row(o, ref, m):
    p = o["pnl"][m]
    st = o["reason"][m] == 4
    s = X.summarize(p)
    dc, tc = P.paired(p, ref["pnl"][m])
    return {**s, "avg_loss_c": 100 * float(p[p < 0].mean()), "fired": 100 * float(st.mean()),
            "fired_won": 100 * float(o["won"][m][st].mean()) if st.any() else float("nan"),
            "d_live": dc, "t_live": tc, **ledger(o, ref, m)}


COLS = ["n", "win", "be", "net_c", "t", "avg_loss_c", "worst_c", "fired", "fired_won", "d_live", "t_live",
        "wsaw_n", "wsaw_c", "xloss_n", "xloss_c", "new_n", "new_c", "later_n", "later_c", "earlier_n", "earlier_c"]


def setup():
    D = X.load(X.OUT / "kalshi_trades.db", X.ROOT / "data" / "btcflow.db", tapes=True)
    k, side = X.parse_entry(ENTRY)(D)
    F = Feat(D)
    return D, F, X.PrintSim(D), k, side


def views(D, F, PS):
    return {"S": (lambda k, s, e, sl: sim_sec(D, F, k, s, e, sl), "pnw_vs_sec", "@sec"),
            "T": (lambda k, s, e, sl: sim_tape(D, F, PS, k, s, e, sl), "pnw_vs_tape", "@tape")}


def train():
    D, F, PS, k, side = setup()
    F.verify(np.flatnonzero(k >= 0))
    check(D, F, PS, k, side)
    run_ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ")
    rows, outs = [], {"S": {}, "T": {}}
    for v, (fn, fam, suf) in views(D, F, PS).items():
        for e in RULES + [LIVE, "hold"]:
            for slip in X.SLIPS:
                out = fn(k, side, e, slip)
                for split, s in X.split_rows(D, out).items():
                    rows.append({"run_ts": run_ts, "config": f"{ENTRY}|{e}{suf}", "family": fam, "entry": ENTRY,
                                 "exit": e, "res": "minute", "slip_c": int(round(slip * 100)), "split": split,
                                 "tried": int((k >= 0).sum()), **s})
                if slip == X.PRIMARY_SLIP:
                    outs[v][e] = out
    X.append(rows)
    print(f"appended {len(rows)} rows")
    tabs = {}
    for v, O in outs.items():
        m = ~D.hold[O[LIVE]["idx"]]
        T = pd.DataFrame({e: row(o, O[LIVE], m) for e, o in O.items()}).T
        dh = {e: P.paired(o["pnl"][m], O["hold"]["pnl"][m]) for e, o in O.items()}
        T["d_hold"], T["t_hold"] = [dh[e][0] for e in T.index], [dh[e][1] for e in T.index]
        tabs[v] = T
        T.to_csv(X.OUT / f"pnw_volstop_train_{v}.csv")
        with pd.option_context("display.width", 350, "display.max_rows", 100, "display.max_columns", 40):
            print(f"\nTRAIN view {v}, 1c:")
            print(T[COLS + ["d_hold", "t_hold"]].to_string(float_format=lambda x: f"{x:.2f}"))
    S = tabs["S"].loc[RULES].sort_values(["net_c", "t"], ascending=False)
    picks = []
    for e, r in S.iterrows():
        if r.d_live <= 0:
            continue
        if not picks or family(e) != family(picks[0]):
            picks.append(e)
        if len(picks) == 2:
            break
    print("\nFROZEN picks (train only, view S):", picks)
    if FROZEN.exists():
        print("(already frozen; not overwriting)")
    else:
        FROZEN.write_text(json.dumps({"frozen_utc": datetime.now(UTC).isoformat(), "picks": picks},
                                     indent=1))


def holdout():
    fz = json.loads(FROZEN.read_text())
    D, F, PS, k, side = setup()
    days = sorted(D.mk.day.unique())
    hd = days[20:]
    h_half = np.where(D.mk.day.isin(set(hd[:5])), 1, np.where(D.mk.day.isin(set(hd[5:])), 2, 0))
    for v, (fn, _, _) in views(D, F, PS).items():
        O = {e: P.chron(D, fn(k, side, e, X.PRIMARY_SLIP)) for e in fz["picks"] + [LIVE, "hold"]}
        idx = O[LIVE]["idx"]
        h = D.hold[idx]
        half = D.mk.half.to_numpy()[idx]
        print(f"\nHOLDOUT view {v}:")
        for e, o in O.items():
            r = row(o, O[LIVE], h)
            p = o["pnl"]
            hh = [X.summarize(p[h & (h_half[idx] == q)]).get("net_c", float("nan")) for q in (1, 2)]
            f15 = [X.summarize(p[half == q]).get("net_c", float("nan")) for q in (1, 2)]
            dh, th = P.paired(p[h], O["hold"]["pnl"][h])
            print(f" [{'PICK' if e in fz['picks'] else 'REF'}] {e}: n={r['n']} win={r['win']:.1f}% be={r['be']:.1f}% "
                  f"net={r['net_c']:+.2f}c t={r['t']:+.2f} avgL={r['avg_loss_c']:.1f} worst={r['worst_c']:.0f} "
                  f"fired={r['fired']:.0f}% fired&won={r['fired_won']:.0f}% | vs live {r['d_live']:+.2f}c "
                  f"(t {r['t_live']:+.2f}) vs hold {dh:+.2f}c (t {th:+.2f}) | saved {r['wsaw_n']} ({r['wsaw_c']:+.0f}c) "
                  f"extra {r['xloss_n']} ({r['xloss_c']:+.0f}c) new {r['new_n']} ({r['new_c']:+.0f}c) "
                  f"later {r['later_n']} ({r['later_c']:+.0f}c) earlier {r['earlier_n']} ({r['earlier_c']:+.0f}c) "
                  f"| 5d halves {hh[0]:+.2f}/{hh[1]:+.2f} 15d halves {f15[0]:+.2f}/{f15[1]:+.2f}")


if __name__ == "__main__":
    {"train": train, "holdout": holdout}[sys.argv[1]]()
