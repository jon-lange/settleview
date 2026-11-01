"""Discord "call it at ~54c, cash at 80c" study (pre-registered in docs/research/coinflip-tp80.md). Read-only on
the databases.

  docker compose -f deploy/dev/compose.yaml run --rm research python scripts/quick/coinflip_tp80.py train    # all configs, both views -> experiments_results.csv,
                                                                   # TRAIN table, freeze <= 2 picks
  docker compose -f deploy/dev/compose.yaml run --rm research python scripts/quick/coinflip_tp80.py holdout  # frozen picks + references, holdout once
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, UTC
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pnw_exits as P  # noqa: E402
import pnw_exits_fixedtp as FT  # noqa: E402

X = P.X
FROZEN = X.OUT / "coinflip_tp80_picks.json"
TRAIN_OUT = X.OUT / "coinflip_tp80_train.csv"
BANDS = {"b50_55": (50, 55), "b45_55": (45, 55), "b50_60": (50, 60)}
ENTRIES = ["E0", "E1", "E2", "E3", "E4"]
EXITS = {"X1": "ftp80", "X2": "ftp80_bstop-10", "X3": "hold", "X4": "ftp80_time"}
TP = 0.80
SEED = 20261029


# ------------------------------------------------------------------------------------------- entries
def crowd_side(D, C, r, t):
    """+1/-1 side with more taker-buy dollars in [t - 60 s, t); 0 = tie / no prints / no tape."""
    if r not in C:
        return 0
    ts, yd, nd = C[r]
    a, b = np.searchsorted(ts, [t - 60_000, t], "left")
    y, n = yd[a:b].sum(), nd[a:b].sum()
    return 0 if y == n else (1 if y > n else -1)


def entries(D, C, name, lo, hi):
    """-> (k, side) per window; first minute 2..12 where the rule's side has its candle ask in [lo, hi]."""
    k_out, s_out = np.full(D.n, -1), np.zeros(D.n, int)
    open_ = D.mk.open_ts.to_numpy(np.int64)
    rng = np.random.default_rng(SEED)
    coin = np.where(rng.random(D.n) < 0.5, 1, -1)
    lo_, hi_ = lo / 100 - X.EPS, hi / 100 + X.EPS
    for k in range(X.K_FIRST, X.K_LAST + 1):
        a_up, a_dn = D.YA[:, k], 1 - D.YB[:, k]
        with np.errstate(invalid="ignore"):
            in_up, in_dn = (a_up >= lo_) & (a_up <= hi_), (a_dn >= lo_) & (a_dn <= hi_)
        t = open_ + k * 60_000
        if name in ("E0", "E0b"):
            side = np.where(in_up & in_dn, np.where(np.abs(a_up - a_dn) < X.EPS, coin, np.where(a_up < a_dn, 1, -1)),
                            np.where(in_up, 1, np.where(in_dn, -1, 0)))
        elif name in ("E1", "E2", "E3"):
            lag = {"E1": 60_000, "E2": 180_000, "E3": 300_000}[name]
            with np.errstate(invalid="ignore"):
                mv = D.btc(t) - D.btc(t - lag)
            side = np.where(mv > 0, 1, np.where(mv < 0, -1, 0))
        else:  # E4
            side = np.array([crowd_side(D, C, r, int(t[r])) for r in range(D.n)])
        ok = (k_out < 0) & (((side > 0) & in_up) | ((side < 0) & in_dn))
        if name == "E0b":
            ok &= ~np.isnan(a_up) & ~np.isnan(a_dn)
        k_out[ok], s_out[ok] = k, side[ok]
    return k_out, s_out


# ------------------------------------------------------------------------------------------- view M
def sim_m(D, k, side, ex, slip):
    if ex != "ftp80_time":
        return FT.sim(D, k, side, ex, slip)
    idx = np.flatnonzero(k >= 0)
    kk, sd = k[idx], side[idx]
    px = np.where(sd > 0, D.YA[idx, kk], 1 - D.YB[idx, kk])
    paid = np.minimum(0.99, np.round(px + slip, 4))
    cost = paid + X.fee(paid)
    Ball = np.where(sd[:, None] > 0, D.YB[idx], 1 - D.YA[idx])
    cols = np.arange(17)
    valid = (cols[None, :] > kk[:, None]) & (cols[None, :] <= X.K_TIME)
    won = (sd > 0) == (D.y[idx] == 1)
    with np.errstate(invalid="ignore"):
        jt = X._first(valid & (Ball >= TP - X.EPS))
    tb = Ball[:, X.K_TIME]
    use_t = jt >= 0
    late = ~use_t & ~np.isnan(tb)
    sale = np.maximum(0.0, np.round(np.nan_to_num(tb) - slip, 4))
    pnl = np.where(use_t, TP - cost, np.where(late, sale - X.fee(sale) - cost, won.astype(float) - cost))
    return {"idx": idx, "pnl": pnl, "won": won, "reason": np.where(use_t, 3, np.where(late, 2, 0))}


# ------------------------------------------------------------------------------------------- view T
def sim_t(D, PS, k, side, ex, slip, latency_ms=1_000):
    res = {"idx": [], "pnl": [], "won": [], "reason": []}
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
        tp_on, stop_on, time_on = ex.startswith("ftp80"), "bstop" in ex, ex.endswith("time")
        horizon = close - 60_000 if time_on else close
        t_tp = np.inf
        if tp_on:
            h = np.flatnonzero((spx >= TP - X.EPS) & (sts < horizon))
            t_tp = float(sts[h[0]]) if len(h) else np.inf
        t_st, m = np.inf, None
        if stop_on:
            secs = np.arange((fill_ts // 1000 + 1) * 1000, close, 1000, dtype=np.int64)
            with np.errstate(invalid="ignore"):
                bad = np.flatnonzero(side[r] * (D.btc(secs) - D.strike[r]) < -10)
            if len(bad):
                t_st = float(secs[bad[0]])
                m = int(np.searchsorted(sts, t_st + latency_ms, "left"))
                m = m if m < len(sts) else None
        if tp_on and t_tp < t_st:
            reason, pnl = 3, TP - cost
        elif m is not None:
            sale = max(0.0, round(float(spx[m]) - slip, 4))
            reason, pnl = 4, sale - float(X.fee(sale)) - cost
        elif time_on and (mm := int(np.searchsorted(sts, close - 60_000 + latency_ms, "left"))) < len(sts):
            sale = max(0.0, round(float(spx[mm]) - slip, 4))
            reason, pnl = 2, sale - float(X.fee(sale)) - cost
        else:
            reason, pnl = 0, float(won) - cost
        for key, v in (("idx", r), ("pnl", pnl), ("won", won), ("reason", reason)):
            res[key].append(v)
    return {"idx": np.asarray(res["idx"], int), "pnl": np.asarray(res["pnl"], float),
            "won": np.asarray(res["won"], bool), "reason": np.asarray(res["reason"], int)}


def both(fn, k, side):
    """E0b: average of an UP leg and a DOWN leg at the same minute (windows where both legs exist)."""
    a, b = fn(k, np.where(k >= 0, 1, 0)), fn(k, np.where(k >= 0, -1, 0))
    common = np.intersect1d(a["idx"], b["idx"])
    ia, ib = np.searchsorted(a["idx"], common), np.searchsorted(b["idx"], common)
    return {"idx": common, "pnl": (a["pnl"][ia] + b["pnl"][ib]) / 2, "won": a["won"][ia] | b["won"][ib],
            "reason": np.maximum(a["reason"][ia], b["reason"][ib])}


# ------------------------------------------------------------------------------------------- setup / checks
def setup():
    D = X.load(X.OUT / "kalshi_trades.db", X.ROOT / "data" / "btcflow.db", tapes=True)
    c = X.ro(X.OUT / "kalshi_trades.db")
    rows = {int(m): r for r, m in enumerate(D.mk.mkt) if r in D.tapes}
    ids = ",".join(map(str, rows))
    t = pd.read_sql(f"SELECT mkt, ts_ms, yes_price, count, taker_yes FROM trades WHERE mkt IN ({ids})", c)  # noqa: S608 - ids are ints from the dataset
    t = t.sort_values(["mkt", "ts_ms"])
    C = {}
    for m, g in t.groupby("mkt"):
        y = g.taker_yes.to_numpy() == 1
        p, n = g.yes_price.to_numpy(float), g["count"].to_numpy(float)
        C[rows[int(m)]] = (g.ts_ms.to_numpy(np.int64), np.where(y, n * p, 0.0), np.where(~y, n * (1 - p), 0.0))
    return D, X.PrintSim(D), C


def check(D, PS, k, side):
    for slip in X.SLIPS:
        for e in ("hold", "bstop-10"):
            a, b = sim_t(D, PS, k, side, e, slip), PS.run(k, side, X.Exit.parse(e), slip)
            assert (a["idx"] == b["idx"]).all() and np.allclose(a["pnl"], b["pnl"], atol=1e-12), ("tape", e)
        a, b = sim_m(D, k, side, "hold", slip), X.sim_minute(D, k, side, X.Exit.parse("hold"), slip)
        assert np.allclose(a["pnl"], b["pnl"], atol=1e-12)
    print("check: view M hold == sim_minute; view T hold/bstop-10 == PrintSim.run (0/1/2c)")


def configs():
    for en in ENTRIES + ["E0b"]:
        for bn in BANDS:
            for xn in EXITS:
                yield en, bn, xn


def run_cfg(D, PS, C, en, bn, xn, slip, view, cache):
    key = (en, bn)
    if key not in cache:
        cache[key] = entries(D, C, en, *BANDS[bn])
    k, side = cache[key]
    ex = EXITS[xn]
    fn = (lambda kk, ss: sim_m(D, kk, ss, ex, slip)) if view == "M" else (lambda kk, ss: sim_t(D, PS, kk, ss, ex, slip))
    out = both(fn, k, side) if en == "E0b" else fn(k, side)
    return P.chron(D, out), int((k >= 0).sum())


def touched80(D, out, k, side):
    """Did the held side's candle bid reach 80c at any later minute mark (regardless of exit)?"""
    idx = out["idx"]
    Ball = np.where(side[idx][:, None] > 0, D.YB[idx], 1 - D.YA[idx])
    cols = np.arange(17)
    v = (cols[None, :] > k[idx][:, None]) & (cols[None, :] <= X.K_CLOSE)
    with np.errstate(invalid="ignore"):
        return (v & (Ball >= TP - X.EPS)).any(axis=1)


def stats(o, m):
    p, rs = o["pnl"][m], o["reason"][m]
    s = X.summarize(p)
    if s["n"] == 0:
        return s
    s.update(avg_loss_c=100 * float(p[p < 0].mean()) if (p < 0).any() else float("nan"),
             tp_pct=100 * float((rs == 3).mean()), stop_pct=100 * float((rs == 4).mean()))
    return s


COLS = ["n", "win", "be", "tp_pct", "net_c", "avg_loss_c", "worst_c", "t"]


def train():
    D, PS, C = setup()
    k0, s0 = entries(D, C, "E0", 50, 55)
    check(D, PS, k0, s0)
    run_ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ")
    rows, T, cache = [], [], {}
    for view in ("M", "T"):
        for en, bn, xn in configs():
            cfg = f"coinflip:{en},{bn}|{xn}" + ("@tape" if view == "T" else "")
            for slip in X.SLIPS:
                out, tried = run_cfg(D, PS, C, en, bn, xn, slip, view, cache)
                for split, s in X.split_rows(D, out).items():
                    rows.append({"run_ts": run_ts, "config": cfg, "family": "coinflip_tp80" + ("_tape" if view == "T" else ""),
                                 "entry": f"coinflip:{en},{bn}", "exit": EXITS[xn], "res": "minute",
                                 "slip_c": int(round(slip * 100)), "split": split, "tried": tried, **s})
                if slip == X.PRIMARY_SLIP:
                    m = ~D.hold[out["idx"]]
                    T.append({"view": view, "entry": en, "band": bn, "exit": xn, **stats(out, m)})
    X.append(rows)
    print(f"appended {len(rows)} rows")
    T = pd.DataFrame(T)
    T.to_csv(TRAIN_OUT, index=False)
    with pd.option_context("display.width", 250, "display.max_rows", 300):
        for v in ("M", "T"):
            print(f"\nTRAIN view {v} (1c):")
            print(T[T.view == v].set_index(["entry", "band", "exit"])[COLS].to_string(float_format=lambda x: f"{x:.2f}"))
    # optional-stopping check on E0 [50,55], train, view M
    o1, _ = run_cfg(D, PS, C, "E0", "b50_55", "X1", 0.01, "M", cache)
    o3, _ = run_cfg(D, PS, C, "E0", "b50_55", "X3", 0.01, "M", cache)
    m = ~D.hold[o1["idx"]]
    dc, tc = P.paired(o1["pnl"][m], o3["pnl"][m])
    idx = o1["idx"][m]
    px = D.ask(s0, k0)[idx]
    tch = touched80(D, {"idx": idx}, k0, s0)
    won = o3["won"][m]
    print(f"\nOPTIONAL STOPPING (E0 b50_55, train, minute): X1 - X3 = {dc:+.2f}c (paired t {tc:+.2f}); "
          f"mean ask {100 * px.mean():.1f}c; settled win {100 * won.mean():.1f}%; touched 80c {100 * tch.mean():.1f}%; "
          f"won without touching {100 * (won & ~tch).mean():.1f}%; martingale prediction for touch "
          f"{100 * (px.mean() - (won & ~tch).mean()) / TP:.1f}%")
    M = T[(T.view == "M") & (T.entry != "E0b") & (T.n >= 60)].sort_values("t", ascending=False)
    picks = [f"{r.entry},{r.band}|{r.exit}" for r in M.head(2).itertuples()]
    print("FROZEN picks (train only):", picks, "train net>0:", list(M.head(2).net_c > 0))
    n_cfg = pd.read_csv(X.RESULTS, usecols=["config", "res"]).query("res == 'minute'").config.nunique()
    print(f"configs counted: {n_cfg} -> Bonferroni bar {norm.isf(0.05 / n_cfg):.2f}")
    if FROZEN.exists():
        print("(already frozen; not overwriting)")
    else:
        FROZEN.write_text(json.dumps({"frozen_utc": datetime.now(UTC).isoformat(), "picks": picks,
                                      "train_net_pos": [bool(x) for x in M.head(2).net_c > 0]}, indent=1))


def holdout():
    fz = json.loads(FROZEN.read_text())
    D, PS, C = setup()
    days = sorted(D.mk.day.unique())
    hd = days[20:]
    n_cfg = pd.read_csv(X.RESULTS, usecols=["config", "res"]).query("res == 'minute'").config.nunique()
    bar = norm.isf(0.05 / n_cfg)
    print(f"configs counted: {n_cfg} -> Bonferroni bar {bar:.2f}")
    run = list(dict.fromkeys(fz["picks"] + ["E0,b50_55|X1", "E0b,b50_55|X1", "E0,b50_55|X3"]))
    cache = {}
    for view in ("M", "T"):
        print(f"\nHOLDOUT view {view} (1c):")
        for c in run:
            en_bn, xn = c.split("|")
            en, bn = en_bn.split(",")
            o, _ = run_cfg(D, PS, C, en, bn, xn, 0.01, view, cache)
            h = D.hold[o["idx"]]
            day = D.mk.day.to_numpy()[o["idx"]]
            half = D.mk.half.to_numpy()[o["idx"]]
            s = stats(o, h)
            if s["n"] == 0:
                print(f" {c}: no trades")
                continue
            hh = [X.summarize(o["pnl"][h & np.isin(day, q)]).get("net_c", np.nan) for q in (hd[:5], hd[5:])]
            f15 = [X.summarize(o["pnl"][half == q]).get("net_c", np.nan) for q in (1, 2)]
            tag = "PICK" if c in fz["picks"] else "REF"
            print(f" [{tag}] {c}: n={s['n']} win={s['win']:.1f}% be={s['be']:.1f}% TP={s['tp_pct']:.0f}% "
                  f"net={s['net_c']:+.2f}c avgL={s['avg_loss_c']:.1f} worst={s['worst_c']:.0f} t={s['t']:+.2f} "
                  f"(bar {bar:.2f}) | 5d halves {hh[0]:+.2f}/{hh[1]:+.2f} | 15d halves {f15[0]:+.2f}/{f15[1]:+.2f}")


if __name__ == "__main__":
    {"train": train, "holdout": holdout}[sys.argv[1]]()
