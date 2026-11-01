"""PNW exit study: "let trades ride" (pre-registered in docs/research/pnw-tuning.md). Read-only on the databases.

  docker compose -f deploy/dev/compose.yaml run --rm research python scripts/quick/pnw_exits.py train     # 28 exits x 3 slips -> experiments_results.csv;
                                                                # prints TRAIN-only tables; freezes <= 3 picks
  docker compose -f deploy/dev/compose.yaml run --rm research python scripts/quick/pnw_exits.py holdout   # frozen picks + hold + current, holdout once

Entry is fixed (live PNW). Exits are simulated by `sim`, a generic copy of experiments.sim_minute's maker-TP / stop
branch with an arbitrary stop mask (checked against sim_minute before anything is scored).
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, UTC
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import experiments as X  # noqa: E402

ENTRY = "pnw_tune:cushion=50,start=9,end=7,lo=65,hi=80"
FROZEN = X.OUT / "pnw_exit_picks.json"
TRAIN_OUT = X.OUT / "pnw_exit_train.csv"
GROUPS = {"index": ["bstop0", "bstop-10"], "gated": ["gstop20n3", "gstop20n2", "gstop20n1"],
          "bid": ["bidle40", "bidle30", "bidle20"], "dollar": ["dl20", "dl40"], "model": ["medge3", "mfair35"]}
BASE = ["hold", "mtp20", "bstop20", "mtp20_bstop20"]
CURRENT = "mtp20_bstop20"


def all_exits():
    out = list(BASE)
    for g in GROUPS.values():
        for s in g:
            out += [s, f"mtp20_{s}"]
    return out


def group_of(name):
    s = name.replace("mtp20_", "")
    for g, v in GROUPS.items():
        if s in v:
            return g
    return "base"


def stop_mask(D, name, idx, side, Ball, valid, paid):
    """Boolean (n, 17): minute marks at which the stop part of exit `name` triggers (before the `valid` filter)."""
    s = name.replace("mtp20_", "").replace("mtp20", "")
    n = len(idx)
    if s in ("", "hold"):
        return np.zeros((n, 17), bool)
    cols = np.arange(17)[None, :]
    with np.errstate(invalid="ignore"):
        if s.startswith("bstop"):
            dist = side[:, None] * (D.btc_minutes()[idx] - D.strike[idx][:, None])
            return dist < float(s[5:])
        if s.startswith("gstop"):
            gap, N = s[5:].split("n")
            dist = side[:, None] * (D.btc_minutes()[idx] - D.strike[idx][:, None])
            return (dist < float(gap)) & (cols >= X.K_CLOSE - int(N))
        if s.startswith("bidle"):
            return Ball <= int(s[5:]) / 100 + X.EPS
        if s.startswith("dl"):
            return Ball <= ((1 - int(s[2:]) / 100) * paid)[:, None] + X.EPS
        fs = np.where(side[:, None] > 0, D.F[idx], 1 - D.F[idx])
        if s.startswith("medge"):
            return fs < Ball - int(s[5:]) / 100
        if s.startswith("mfair"):
            return fs < int(s[5:]) / 100
    raise ValueError(name)


def sim(D, k, side, name, slip):
    idx = np.flatnonzero(k >= 0)
    k, side = k[idx], side[idx]
    i = np.arange(len(idx))
    px = np.where(side > 0, D.YA[idx, k], 1 - D.YB[idx, k])
    paid = np.minimum(0.99, np.round(px + slip, 4))
    cost = paid + X.fee(paid)
    Ball = np.where(side[:, None] > 0, D.YB[idx], 1 - D.YA[idx])
    cols = np.arange(17)
    valid = (cols[None, :] > k[:, None]) & (cols[None, :] <= X.K_CLOSE)
    won = (side > 0) == (D.y[idx] == 1)
    tp = 0.20 if name.startswith("mtp20") else None
    with np.errstate(invalid="ignore"):
        lvl = np.minimum(0.99, px + (tp or 0))
        tp_hit = valid & (Ball >= lvl[:, None] - X.EPS) if tp is not None else np.zeros(Ball.shape, bool)
        st_hit = valid & stop_mask(D, name, idx, side, Ball, valid, paid) & ~np.isnan(Ball)
    jt, js = X._first(tp_hit), X._first(st_hit)
    use_s = (js >= 0) & ((jt < 0) | (js <= jt))
    use_t = (jt >= 0) & ~use_s
    sale = np.maximum(0.0, np.round(Ball[i, np.clip(js, 0, 16)] - slip, 4))
    pnl = np.where(use_t, lvl - cost, np.where(use_s, sale - X.fee(np.nan_to_num(sale)) - cost, won.astype(float) - cost))
    return {"idx": idx, "pnl": pnl, "won": won, "reason": np.where(use_t, 3, np.where(use_s, 4, 0)), "exit_k": js}


def load():
    return X.load(X.OUT / "kalshi_trades.db", ROOT / "data" / "btcflow.db", tapes=False)


def check(D, k, side):
    for e in ["hold", "mtp20", "bstop20", "mtp20_bstop20", "bstop0", "bstop-10"]:
        for slip in X.SLIPS:
            a, b = sim(D, k, side, e, slip), X.sim_minute(D, k, side, X.Exit.parse(e), slip)
            assert (a["idx"] == b["idx"]).all() and np.allclose(a["pnl"], b["pnl"], atol=1e-12) \
                and (a["reason"] == b["reason"]).all(), e
    print("check: generic sim == experiments.sim_minute for hold/mtp20/bstop20/mtp20_bstop20/bstop0/bstop-10 (0/1/2c)")


def chron(D, out):
    o = np.argsort(D.mk.open_ts.to_numpy()[out["idx"]], kind="stable")
    return {key: (v[o] if isinstance(v, np.ndarray) and len(v) == len(o) else v) for key, v in out.items()}


def extra(pnl, reason, won):
    lose = pnl < 0
    st = reason == 4
    return {"avg_loss_c": 100 * float(pnl[lose].mean()) if lose.any() else float("nan"),
            "fired": 100 * float(st.mean()), "fired_won": 100 * float(won[st].mean()) if st.any() else float("nan"),
            "share_tp": 100 * float((reason == 3).mean())}


def paired(a, b):
    d = a - b
    return 100 * d.mean(), float(d.mean() / (d.std(ddof=1) / math.sqrt(len(d)))) if d.std() > 0 else float("nan")


def dist(pnl, label):
    q = np.percentile(100 * pnl, [5, 25, 50, 75, 95])
    print(f"  {label}: n={len(pnl)} mean {100 * pnl.mean():+.2f}c sd {100 * pnl.std(ddof=1):.1f}c | p5 {q[0]:+.0f} "
          f"p25 {q[1]:+.0f} p50 {q[2]:+.0f} p75 {q[3]:+.0f} p95 {q[4]:+.0f} | worst {100 * pnl.min():+.0f}c | "
          f"<= -50c: {100 * (pnl <= -0.5).mean():.1f}% | max DD {X.summarize(pnl)['maxdd_c'] / 100:.2f} $/contract | "
          f"$10 stake (x{10 / 0.73:.1f}): worst ${10 / 0.73 * pnl.min():+.2f}, max DD ${10 / 0.73 * X.summarize(pnl)['maxdd_c'] / 100:.2f}")


def train():
    D = load()
    k, side = X.parse_entry(ENTRY)(D)
    check(D, k, side)
    run_ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ")
    rows, T, outs = [], [], {}
    for e in all_exits():
        for slip in X.SLIPS:
            out = sim(D, k, side, e, slip)
            for split, st in X.split_rows(D, out).items():
                rows.append({"run_ts": run_ts, "config": f"{ENTRY}|{e}", "family": "pnw_exit", "entry": ENTRY,
                             "exit": e, "res": "minute", "slip_c": int(round(slip * 100)), "split": split,
                             "tried": int((k >= 0).sum()), **st})
            if slip == X.PRIMARY_SLIP:
                outs[e] = chron(D, out)
    X.append(rows)
    for e, o in outs.items():
        tr = ~D.hold[o["idx"]]
        s = X.summarize(o["pnl"][tr])
        T.append({"exit": e, "group": group_of(e), **s, **extra(o["pnl"][tr], o["reason"][tr], o["won"][tr])})
    T = pd.DataFrame(T).set_index("exit")
    tr = ~D.hold[outs["hold"]["idx"]]
    for ref in ("hold", CURRENT):
        T[f"d_{ref}"], T[f"t_{ref}"] = zip(*[paired(outs[e]["pnl"][tr], outs[ref]["pnl"][tr]) for e in T.index], strict=True)
    T.to_csv(TRAIN_OUT)
    print(f"appended {len(rows)} rows ({len(outs)} configs x 3 slips x 5 splits)")
    cols = ["n", "win", "be", "net_c", "t", "avg_loss_c", "worst_c", "maxdd_c", "fired", "fired_won", "share_tp",
            "d_hold", "t_hold", "d_" + CURRENT, "t_" + CURRENT]
    with pd.option_context("display.width", 250):
        print("\nTRAIN (first 20 days, 1c):")
        print(T[cols].to_string(float_format=lambda v: f"{v:.2f}"))
    print("\nTRAIN per-trade P&L distribution:")
    for e in ("hold", CURRENT, "mtp20", "bstop20"):
        dist(outs[e]["pnl"][tr], e)
    picks = []
    C = T[T.group != "base"].sort_values(["net_c", "t"], ascending=False)
    used = set()
    for e, r in C.iterrows():
        if r.group in used:
            continue
        picks.append(e)
        used.add(r.group)
        if len(picks) == 3:
            break
    print("\nFROZEN picks (train only):", picks)
    if FROZEN.exists():
        print(f"(already frozen in {FROZEN}; not overwriting)")
    else:
        FROZEN.write_text(json.dumps({"frozen_utc": datetime.now(UTC).isoformat(), "picks": picks}, indent=1))


def holdout():
    fz = json.loads(FROZEN.read_text())
    D = load()
    k, side = X.parse_entry(ENTRY)(D)
    days = sorted(D.mk.day.unique())
    hd = days[20:]
    h_half = np.where(D.mk.day.isin(set(hd[:5])), 1, np.where(D.mk.day.isin(set(hd[5:])), 2, 0))
    outs = {e: chron(D, sim(D, k, side, e, X.PRIMARY_SLIP)) for e in fz["picks"] + ["hold", CURRENT]}
    h = D.hold[outs["hold"]["idx"]]
    half = D.mk.half.to_numpy()[outs["hold"]["idx"]]
    for e, o in outs.items():
        p = o["pnl"]
        s, x = X.summarize(p[h]), extra(p[h], o["reason"][h], o["won"][h])
        hh = [X.summarize(p[h & (h_half[o["idx"]] == q)])["net_c"] for q in (1, 2)]
        f15 = [X.summarize(p[half == q])["net_c"] for q in (1, 2)]
        dh, th = paired(p[h], outs["hold"]["pnl"][h])
        dc, tc = paired(p[h], outs[CURRENT]["pnl"][h])
        print(f"\n[{'PICK' if e in fz['picks'] else 'BASE'}] {e}: holdout n={s['n']} win={s['win']:.1f}% be={s['be']:.1f}% "
              f"net={s['net_c']:+.2f}c t={s['t']:+.2f} avg loss={x['avg_loss_c']:.1f}c worst={s['worst_c']:.0f}c "
              f"maxDD={s['maxdd_c']:.0f}c fired={x['fired']:.0f}% fired&won={x['fired_won']:.0f}% TP={x['share_tp']:.0f}%")
        print(f"  5-day halves {hh[0]:+.2f} / {hh[1]:+.2f}; 15-day halves {f15[0]:+.2f} / {f15[1]:+.2f}; "
              f"vs hold {dh:+.2f}c (t {th:+.2f}); vs current {dc:+.2f}c (t {tc:+.2f})")
    print("\nHOLDOUT per-trade P&L distribution:")
    for e in ("hold", CURRENT):
        dist(outs[e]["pnl"][h], e)


if __name__ == "__main__":
    {"train": train, "holdout": holdout}[sys.argv[1]]()
