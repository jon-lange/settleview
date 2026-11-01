"""PNW band study 5: entry price band under a fixed 85c TP (pre-registered in docs/research/pnw-tuning.md,
"Band study 5"). Read-only on the databases.

  .venv/Scripts/python.exe scripts/quick/pnw_band_tp85.py train    # 8 bands x 2 exits x 3 slips -> CSV; train only
  .venv/Scripts/python.exe scripts/quick/pnw_band_tp85.py holdout  # frozen picks + live [65,80], once
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, UTC
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pnw_exits as P  # noqa: E402
import pnw_exits_fixedtp as F  # noqa: E402

X = P.X
BANDS = [(55, 65), (60, 70), (65, 75), (70, 80), (60, 75), (60, 80), (65, 80), (55, 80)]
LIVE_B, UNION_B = (65, 80), (55, 80)
TP, RIDE = "ftp85_bstop-10", "bstop-10"
FROZEN = X.OUT / "pnw_band5_picks.json"
TRAIN_OUT = X.OUT / "pnw_band5_train.csv"


def entry(b):
    return f"pnw_tune:cushion=50,start=9,end=7,lo={b[0]},hi={b[1]}"


def full(D, out):
    """Per-window pnl (0 where no trade) and a traded mask."""
    v, m = np.zeros(D.n), np.zeros(D.n, bool)
    v[out["idx"]], m[out["idx"]] = out["pnl"], True
    return v, m


def upaired(D, a, b, sel):
    va, ma = full(D, a)
    vb, mb = full(D, b)
    u = (ma | mb) & sel
    d = (va - vb)[u]
    return 100 * d.mean(), float(d.mean() / (d.std(ddof=1) / math.sqrt(len(d)))) if d.std() > 0 else float("nan"), int(u.sum())


def stats(o, m):
    p, r = o["pnl"][m], o["reason"][m]
    s = X.summarize(p)
    return {**s, "tp_pct": 100 * float((r == 3).mean()), "stop_pct": 100 * float((r == 4).mean()),
            "avg_win_c": 100 * float(p[p > 0].mean()), "avg_loss_c": 100 * float(p[p < 0].mean())}


def run_all(D, slip):
    outs = {}
    for b in BANDS:
        k, side = X.parse_entry(entry(b))(D)
        for e in (TP, RIDE):
            outs[(b, e)] = P.chron(D, F.sim(D, k, side, e, slip))
    return outs


def train():
    D = P.load()
    k, side = X.parse_entry(entry(LIVE_B))(D)
    F.check(D, k, side)
    run_ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ")
    rows = []
    for slip in X.SLIPS:
        for (b, e), out in run_all(D, slip).items():
            for split, s in X.split_rows(D, out).items():
                rows.append({"run_ts": run_ts, "config": f"{entry(b)}|{e}", "family": "pnw_band5", "entry": entry(b),
                             "exit": e, "res": "minute", "slip_c": int(round(slip * 100)), "split": split,
                             "tried": int((out["idx"] >= 0).sum()), **s})
    X.append(rows)
    print(f"appended {len(rows)} rows ({len(BANDS) * 2 * len(X.SLIPS)} configs x splits)")
    outs = run_all(D, X.PRIMARY_SLIP)
    tr_sel = ~D.hold
    T = []
    for (b, e), o in outs.items():
        m = ~D.hold[o["idx"]]
        dc, tc, nu = upaired(D, o, outs[(LIVE_B, TP)], tr_sel)
        T.append({"band": f"{b[0]}-{b[1]}", "exit": e, **stats(o, m), "per_day": m.sum() / 20,
                  "d_live_c_per_win": dc, "t_live": tc, "n_union": nu})
    T = pd.DataFrame(T)
    T.to_csv(TRAIN_OUT, index=False)
    cols = ["band", "exit", "n", "per_day", "win", "be", "net_c", "t", "tp_pct", "stop_pct", "avg_win_c",
            "avg_loss_c", "d_live_c_per_win", "t_live", "n_union"]
    with pd.option_context("display.width", 250):
        print(T[cols].to_string(index=False, float_format=lambda v: f"{v:.2f}"))
    # per-price breakdown, union band, train
    print("\nPer-price (train, union 55-80 entry, by entry ask bucket):")
    pb = []
    for e in (TP, RIDE):
        o = outs[(UNION_B, e)]
        idx = o["idx"]
        kk, ss = X.parse_entry(entry(UNION_B))(D)
        ask = np.where(ss[idx] > 0, D.YA[idx, kk[idx]], 1 - D.YB[idx, kk[idx]])
        ac = np.round(100 * ask).astype(int)
        m = ~D.hold[idx]
        for lo in range(55, 81, 2):
            sel = m & (ac >= lo) & (ac <= lo + 1)
            if sel.sum() == 0:
                continue
            s = stats(o, sel)
            pb.append({"exit": e, "bucket": f"{lo}-{min(lo + 1, 80)}", "n": s["n"], "win": s["win"], "be": s["be"],
                       "net_c": s["net_c"], "t": s["t"], "tp_pct": s["tp_pct"], "stop_pct": s["stop_pct"]})
    PB = pd.DataFrame(pb)
    PB.to_csv(X.OUT / "pnw_band5_perprice_train.csv", index=False)
    with pd.option_context("display.width", 250):
        print(PB.to_string(index=False, float_format=lambda v: f"{v:.2f}"))
    live_net = T[(T.band == "65-80") & (T.exit == TP)].net_c.iloc[0]
    C = T[(T.exit == TP) & ~T.band.isin(["65-80", "55-80"])]
    picks = []
    c1 = C[C.n >= 60].sort_values(["net_c", "t"], ascending=False)
    if len(c1) and c1.net_c.iloc[0] > live_net:
        picks.append(c1.band.iloc[0])
    c2 = C[~C.band.isin(picks)].sort_values("d_live_c_per_win", ascending=False)
    if len(c2) and c2.d_live_c_per_win.iloc[0] > 0 and c2.net_c.iloc[0] > live_net:
        picks.append(c2.band.iloc[0])
    print("FROZEN picks (train only):", picks, f"(live train net {live_net:+.2f}c)")
    if FROZEN.exists():
        print("(already frozen; not overwriting)")
    else:
        FROZEN.write_text(json.dumps({"frozen_utc": datetime.now(UTC).isoformat(), "picks": picks}, indent=1))


def holdout():
    fz = json.loads(FROZEN.read_text())
    D = P.load()
    days = sorted(D.mk.day.unique())
    hd = days[20:]
    h_half = np.where(D.mk.day.isin(set(hd[:5])), 1, np.where(D.mk.day.isin(set(hd[5:])), 2, 0))
    run = [tuple(int(x) for x in p.split("-")) for p in fz["picks"]] + [LIVE_B]
    outs = {}
    for b in run:
        k, side = X.parse_entry(entry(b))(D)
        for e in (TP, RIDE):
            outs[(b, e)] = P.chron(D, F.sim(D, k, side, e, X.PRIMARY_SLIP))
    for (b, e), o in outs.items():
        h = D.hold[o["idx"]]
        half = D.mk.half.to_numpy()[o["idx"]]
        s = stats(o, h)
        hh = [X.summarize(o["pnl"][h & (h_half[o["idx"]] == q)]).get("net_c", float("nan")) for q in (1, 2)]
        f15 = [X.summarize(o["pnl"][half == q])["net_c"] for q in (1, 2)]
        dc, tc, nu = upaired(D, o, outs[(LIVE_B, TP)], D.hold)
        dr, trr, _ = upaired(D, o, outs[(LIVE_B, RIDE)], D.hold)
        tag = "LIVE" if b == LIVE_B else "PICK"
        print(f"[{tag}] {b[0]}-{b[1]} {e}: n={s['n']} win={s['win']:.1f}% be={s['be']:.1f}% net={s['net_c']:+.2f}c "
              f"t={s['t']:+.2f} TP={s['tp_pct']:.0f}% stop={s['stop_pct']:.0f}% avgW={s['avg_win_c']:.1f} "
              f"avgL={s['avg_loss_c']:.1f} | 5d {hh[0]:+.2f}/{hh[1]:+.2f} 15d {f15[0]:+.2f}/{f15[1]:+.2f} | "
              f"vs live+TP85 {dc:+.2f}c/win (t {tc:+.2f}, union n={nu}) | vs live+RIDE {dr:+.2f} (t {trr:+.2f})")


if __name__ == "__main__":
    {"train": train, "holdout": holdout}[sys.argv[1]]()
