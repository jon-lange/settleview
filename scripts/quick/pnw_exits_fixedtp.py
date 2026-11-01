"""PNW exit study 2: fixed-price maker TP (85/90/95c/none) x patient stops (pre-registered in
docs/research/pnw-tuning.md, "Exit study 2"). Read-only on the databases.

  docker compose -f deploy/dev/compose.yaml run --rm research python scripts/quick/pnw_exits_fixedtp.py train    # 20 exits x 3 slips -> experiments_results.csv
  docker compose -f deploy/dev/compose.yaml run --rm research python scripts/quick/pnw_exits_fixedtp.py holdout  # frozen picks + ftp85_bstop-10 + live, once
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
ENTRY = P.ENTRY
FROZEN = X.OUT / "pnw_exit2_picks.json"
TRAIN_OUT = X.OUT / "pnw_exit2_train.csv"
TPS = [None, 85, 90, 95]
STOPS = ["bstop-10", "bstop0", "bstop-10c", "bstop-20", "hold"]
LIVE = "bstop-10"
USER = "ftp85_bstop-10"


def name(tp, st):
    return st if tp is None else (f"ftp{tp}" if st == "hold" else f"ftp{tp}_{st}")


def parse(e):
    tp, st = None, e
    if e.startswith("ftp"):
        a, _, b = e.partition("_")
        tp, st = int(a[3:]), (b or "hold")
    return tp, st


def stop_mask(D, st, idx, side):
    if st == "hold":
        return np.zeros((len(idx), 17), bool)
    conf = st.endswith("c")
    lim = float(st[5:-1] if conf else st[5:])
    with np.errstate(invalid="ignore"):
        m = side[:, None] * (D.btc_minutes()[idx] - D.strike[idx][:, None]) < lim
    if conf:
        prev = np.zeros_like(m)
        prev[:, 1:] = m[:, :-1]
        m = m & prev
    return m


def sim(D, k, side, e, slip):
    tp, st = parse(e)
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
    lvl = np.full(len(idx), (tp or 0) / 100)
    with np.errstate(invalid="ignore"):
        tp_hit = valid & (Ball >= lvl[:, None] - X.EPS) if tp else np.zeros(Ball.shape, bool)
        st_hit = valid & stop_mask(D, st, idx, side) & ~np.isnan(Ball)
    jt, js = X._first(tp_hit), X._first(st_hit)
    use_s = (js >= 0) & ((jt < 0) | (js <= jt))
    use_t = (jt >= 0) & ~use_s
    sale = np.maximum(0.0, np.round(Ball[i, np.clip(js, 0, 16)] - slip, 4))
    pnl = np.where(use_t, lvl - cost, np.where(use_s, sale - X.fee(np.nan_to_num(sale)) - cost, won.astype(float) - cost))
    return {"idx": idx, "pnl": pnl, "won": won, "reason": np.where(use_t, 3, np.where(use_s, 4, 0))}


def check(D, k, side):
    for e in ["hold", "bstop-10", "bstop0", "bstop-20"]:
        for slip in X.SLIPS:
            a, b = sim(D, k, side, e, slip), X.sim_minute(D, k, side, X.Exit.parse(e), slip)
            assert (a["idx"] == b["idx"]).all() and np.allclose(a["pnl"], b["pnl"], atol=1e-12) \
                and (a["reason"] == b["reason"]).all(), e
    print("check: sim == experiments.sim_minute for hold/bstop-10/bstop0/bstop-20 (0/1/2c)")


def extra(p, reason, won):
    st = reason == 4
    return {"avg_win_c": 100 * float(p[p > 0].mean()), "avg_loss_c": 100 * float(p[p < 0].mean()),
            "stopped": 100 * float(st.mean()), "stop_won": 100 * float(won[st].mean()) if st.any() else float("nan"),
            "tp_pct": 100 * float((reason == 3).mean())}


def exits():
    return [name(t, s) for t in TPS for s in STOPS]


def train():
    D = P.load()
    k, side = X.parse_entry(ENTRY)(D)
    check(D, k, side)
    run_ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ")
    rows, T, outs = [], [], {}
    for e in exits():
        for slip in X.SLIPS:
            out = sim(D, k, side, e, slip)
            for split, s in X.split_rows(D, out).items():
                rows.append({"run_ts": run_ts, "config": f"{ENTRY}|{e}", "family": "pnw_exit2", "entry": ENTRY,
                             "exit": e, "res": "minute", "slip_c": int(round(slip * 100)), "split": split,
                             "tried": int((k >= 0).sum()), **s})
            if slip == X.PRIMARY_SLIP:
                outs[e] = P.chron(D, out)
    X.append(rows)
    tr = ~D.hold[outs["hold"]["idx"]]
    for e, o in outs.items():
        T.append({"exit": e, **X.summarize(o["pnl"][tr]), **extra(o["pnl"][tr], o["reason"][tr], o["won"][tr])})
    T = pd.DataFrame(T).set_index("exit")
    T["d_live"], T["t_live"] = zip(*[P.paired(outs[e]["pnl"][tr], outs[LIVE]["pnl"][tr]) for e in T.index], strict=True)
    T.to_csv(TRAIN_OUT)
    print(f"appended {len(rows)} rows")
    cols = ["n", "win", "be", "net_c", "t", "avg_win_c", "avg_loss_c", "worst_c", "stopped", "stop_won", "tp_pct",
            "d_live", "t_live"]
    with pd.option_context("display.width", 250):
        print(T[cols].to_string(float_format=lambda v: f"{v:.2f}"))
    C = T.drop(index=[LIVE, USER]).sort_values(["net_c", "t"], ascending=False)
    picks = list(C.index[:2])
    print("FROZEN picks (train only):", picks)
    if FROZEN.exists():
        print("(already frozen; not overwriting)")
    else:
        FROZEN.write_text(json.dumps({"frozen_utc": datetime.now(UTC).isoformat(), "picks": picks}, indent=1))


def holdout():
    fz = json.loads(FROZEN.read_text())
    D = P.load()
    k, side = X.parse_entry(ENTRY)(D)
    days = sorted(D.mk.day.unique())
    hd = days[20:]
    h_half = np.where(D.mk.day.isin(set(hd[:5])), 1, np.where(D.mk.day.isin(set(hd[5:])), 2, 0))
    run = list(dict.fromkeys(fz["picks"] + [USER, LIVE]))
    outs = {e: P.chron(D, sim(D, k, side, e, X.PRIMARY_SLIP)) for e in run}
    h = D.hold[outs[LIVE]["idx"]]
    half = D.mk.half.to_numpy()[outs[LIVE]["idx"]]
    for e, o in outs.items():
        p = o["pnl"]
        s, x = X.summarize(p[h]), extra(p[h], o["reason"][h], o["won"][h])
        hh = [X.summarize(p[h & (h_half[o["idx"]] == q)])["net_c"] for q in (1, 2)]
        f15 = [X.summarize(p[half == q])["net_c"] for q in (1, 2)]
        dc, tc = P.paired(p[h], outs[LIVE]["pnl"][h])
        print(f"[{'PICK' if e in fz['picks'] else 'REQ'}] {e}: n={s['n']} win={s['win']:.1f}% be={s['be']:.1f}% "
              f"net={s['net_c']:+.2f}c t={s['t']:+.2f} avgW={x['avg_win_c']:.1f} avgL={x['avg_loss_c']:.1f} "
              f"worst={s['worst_c']:.0f} stopped={x['stopped']:.0f}% stop&won={x['stop_won']:.0f}% TP={x['tp_pct']:.0f}% "
              f"| 5d halves {hh[0]:+.2f}/{hh[1]:+.2f} 15d halves {f15[0]:+.2f}/{f15[1]:+.2f} | vs live {dc:+.2f}c (t {tc:+.2f})")


if __name__ == "__main__":
    {"train": train, "holdout": holdout}[sys.argv[1]]()
