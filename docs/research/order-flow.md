# Order flow on KXBTC15M: who buys, who sells, whales, and crowd panic

Question (user, 2026-10-29): forget chat sentiment — look at what people actually *do* on Kalshi KXBTC15M (taker
buying vs selling, size, whale trades) and whether crowd panic (the contract price moving while BTC itself has
not) reverts. Could that make a stop smarter or be a signal?

Script: `scripts/quick/order_flow.py` (read-only on every database). Results are appended below the
pre-registration, which was written and saved before any outcome was computed.

## 1. Inventory (what data exists)

**Recorded live since 2026-10-28 (`data/btcflow.db`):**

| what | table | coverage |
|---|---|---|
| Kalshi top of book (yes/no bid & ask) | `kalshi_quotes` source `ws` | 31,688 rows, ~1 per second, 46 KXBTC15M windows, 2026-10-28 21:39 → 2026-10-29 12:58 UTC; one gap 06:41 → 10:46 UTC (the Windows Update reboot). Earlier `source NULL` / `orderbook` rows: 6,451 REST snapshots 17:55 → 21:39 UTC |
| Kalshi public trade tape | — | **not recorded live.** `trades` in btcflow.db is the Coinbase BTC-USD tape (12.25M prints since 2026-09-28); `kalshi_fills` holds only our own 108 fills |
| per-second BTC index | `index_1s` | 2026-09-28 18:14 → now (2.65M seconds); same 4 h reboot gap |
| market metadata + result | `kalshi_markets` | 5,463 markets, all with `floor_strike` and `result` |

So the live record is 45 settled windows of quotes and **no Kalshi trade tape** — far too little for an
order-flow study, and it cannot answer O2/O3 at all.

**Historical research data (`data/research/kalshi_trades.db`, 2026-09-29 → 2026-10-28):**

| what | coverage |
|---|---|
| full public trade tape (ts ms, yes price, contracts, taker side) | **435 windows** (random 40% sample per day, fixed seed), 14.78M prints, the last 820 s of each window (13:40 left → close); 12k–76k prints per window |
| 1-minute candles (yes bid / ask at each minute close) | all 2,848 windows |
| result, floor_strike | all 2,848 windows |
| BTC index per second | `data/btcflow.db:index_1s`, whole period |

There are no recorded order-book snapshots before 2026-10-28, so at second resolution the bid/ask comes from the
tape, as in every earlier print-mode study: **UP ask = the latest taker-YES print, UP bid = the latest taker-NO
print (its yes price)**, each within the previous 10 s. Main sample for O1–O3: the 435 full tapes. Sample for
the PNW question: all 2,848 windows at minute resolution (candle bid/ask) plus the 435 tapes at second
resolution.

## 2. Pre-registration (written before any outcome was looked at)

**Sample and split.** The 435 full-tape windows, sorted by open time: first 60% (261 windows) = train, last 40%
(174) = test. Thresholds that need a number (O2 imbalance cut, O3 whale cut) are the train-sample quantile stated
below, computed by the script and frozen before the test rows are touched; nothing is tuned on test.

**Clock.** Scan seconds t from 12:30 to 4:00 before the close (so a 180 s follow-up ends ≥ 1 min before close).
A decision at t uses only prints with ts < t and the BTC index second < t − 1 s (`experiments.Data.btc`).
`mid(t)` = (UP bid + UP ask)/2 from the print proxy (both must exist within 10 s).

**Tradable scoring** (per contract, one trade per window = first event in the window, t clustered by window):
buy the chosen side at its proxy ask one second after the decision (prints < t + 1 s) + 1¢ slippage + exact taker
fee `ceil(0.07·P·(1−P)·1e4)/1e4`; exit either at Kalshi's result (hold) or by selling at the side's proxy bid at
t + H − 1¢ minus the fee (H = 60 or 180 s). A claim survives only if: test t ≥ the Bonferroni bar printed by
`experiments.py report` over all configs ever counted (≈ 4.2; these configs are appended to
`data/research/experiments_results.csv` with family `order_flow`), positive on train too, and ≥ 30 test trades.

**Descriptive tests** (is there any effect at all, before costs): R60 / R180 = the change in the event side's mid
over the next 60 / 180 s (positive = moves the way the hypothesis says); CAL = event side's settlement (1/0)
minus its mid at the event (is the post-event price too low/high?). All events per window are averaged within the
window first (events in a window de-duplicated: a new event needs 60 s since the last for O1, 10 s for O3), t
across windows. My own family: 21 descriptive + 20 tradable + 2 PNW = **43 tests; Bonferroni two-sided α = 0.05
→ |t| ≥ 3.25** on test for a descriptive effect to count as real.

**O1 — panic (contract moves, BTC doesn't).** At second t, Δ = mid(t) − mid(t − 60 s), ΔB = BTC(t) − BTC(t − 60 s).
An *unjustified* move: Δ ≤ −X with ΔB > −$15 (UP fell, BTC did not fall ≥ $15), or Δ ≥ +X with ΔB < +$15 (DOWN
fell). The *panicked side* is the side whose price fell. X = 8¢ and 12¢. Hypothesis: the panicked side reverts
(R60/R180 > 0) and its post-panic price is too low (CAL > 0). Tradable (fade = buy the panicked side): X × {hold,
sell at +60 s, sell at +180 s} = 6 configs. Control (reported, not tested): *justified* moves (BTC moved ≥ $15
the same way).

**O2 — order-flow imbalance.** At τ = 10, 7, 4 min left: imb = (Y − N)/(Y + N), Y/N = taker-YES / taker-NO
contracts in the previous 60 s (no event if Y + N = 0). Descriptive: OLS slope of the UP mid's R60 / R180 and of
CAL(UP) on imb (one obs per window per τ). Tradable: when |imb| ≥ the train 80th percentile of |imb| at that τ,
buy the side the flow points to (follow) or the other side (fade), hold: 3 τ × 2 = 6 configs.

**O3 — whales.** A taker order = prints with the same ms timestamp and taker side; notional ($) = Σ contracts ×
price paid (YES: yes price, NO: 1 − yes price). (a) *single*: order notional ≥ the train 99th percentile of
order notional; (b) *cluster*: non-overlapping 10-s buckets (aligned to the close), |YES − NO notional| ≥ the
train 99th percentile of that bucket statistic; event time = the order's ms + 1 / the bucket end. Whale direction
= side bought (net). Descriptive R60/R180/CAL of the whale side measured from the mid 1 s after the event
(after its own price impact). Tradable: {single, cluster} × {follow, fade} × {hold, sell at +180 s} = 8 configs.

**PNW panic (step 3, descriptive).** Live PNW entry (`experiments.pnw_tune` defaults: 9–7 min left, BTC ≥ $50 past
the strike, side ask 65–80¢). A *panic against the position*: at a later minute mark (≥ 1 min left; minute
resolution, all 2,848 windows) the side's bid ≤ entry-minute bid − 8¢ while BTC (index) is still past the strike
on the position's side. First such mark per trade. Report n, win %, mean bid and mid at the panic, and
won − bid (hold vs sell-at-bid before fee) with window-clustered t, split by BTC cushion at the panic
(≥ $20 / < $20) and by chronological halves. Same on the 435 tapes at second resolution (proxy bid). If the
panicked position wins at about its bid/mid, contract-price panic carries no extra information and an index-based
stop that ignores it is the right design; if it wins clearly more than its bid, a price-based stop would be
selling cheap (holding wins); if less, the panic is informative and the index stop is late.

## 3. Results

Run 2026-10-29, `scripts/quick/order_flow.py` (tables also in `data/research/order_flow_results.md`). Train =
261 windows 2026-09-29 → 10-16, test = 174 windows 10-16 → 10-28. The script was run twice (the first run crashed
on printing after appending its 20 CSV rows); the rerun's rows supersede, the config count is unchanged.

### Verdicts

- **What people do.** 4.88M taker orders (prints grouped by ms and side) in 435 windows between 12:30 and 4:00
  left: 51.2% buy YES, so the flow is close to balanced. Median order $4.85, P90 $79, P99 ~$800, largest $83.6k.
  Whales are not rare: the top 1% (≥ $789) shows up ~30 times per window.
- **O1 panic: KILLED.** A contract move of ≥ 8–12¢ in 60 s with no matching BTC move does *not* revert. The
  panicked side keeps drifting down a little (test R60 −1.3¢, t −2.3 at 8¢; −2.7¢, t −2.9 at 12¢) and settles at
  or below its post-panic mid (CAL −1.4 / −3.1¢, not significant). Moves BTC did justify drift the same way (test
  R60 −1.8¢, t −3.9: the only test row past |t| 3.25, and train is only −0.7¢, t −1.8). That is a 1–2¢ continuation,
  well below the ~6¢ round trip (spread + 2 fees + slippage), the same as H8. Fade trades: hold test +1.0¢
  (t 0.26) at 8¢ but train −5.4¢; −1.8¢ at 12¢; every sell-after-60/180-s version loses 5–8¢ (t −2 to −8).
- **O2 taker imbalance: KILLED.** No slope is stable across train and test (τ = 7 min flips sign: R180 train
  −12.8 t −2.5, test +1.9; CAL train −18.9, test +31.5 t 2.35). Tradable configs scatter around zero, best test
  t 2.18 on n = 30 (follow, τ 7), which lost −11.8¢ on train; its mirror lost on test. Noise.
- **O3 whales: KILLED.** The whale's side moves +0.2¢ to +0.4¢ over the next 1–3 min (t ≤ 1.4) and its settlement
  matches the post-whale price (CAL −0.2 to +0.2¢). Following or fading the first whale loses about the cost
  (hold −5.1 / −0.9¢ test; 10-s clusters −0.0 / −3.1¢). Big takers are not informed beyond the price — or the
  price absorbs what they know within a second.
- **None of the 20 tradable configs is near the Bonferroni bar of 4.19** (3,627 configs counted).
- **PNW panic (step 3): the contract-price panic carries no reliable information either way.** Minute resolution,
  283 of 687 PNW entries saw their side's bid fall ≥ 8¢ with BTC still past the strike. They won 56.2% vs a
  60.0¢ bid / 60.5¢ mid at the panic: −4.3¢ vs mid (t −1.5); holding vs selling at the bid (net of fee) −2.2¢
  (t −0.7). Halves disagree (days 1–15 +3.0¢, days 16–30 −6.8¢, t −1.7). At second resolution on the tapes (72
  panics in 105 entries) it is the other sign: won 72.2% vs 64.7¢ bid, hold − sell +9.2¢ (t 1.75), noisier because
  a single print can drop the proxy bid. Read together: a panicked PNW position is worth roughly its price — selling
  into the panic neither saves money nor clearly gives it away, so a price-based stop has nothing to add. That
  supports keeping the stop on the BTC index (`bstop-10`, H-PNW-X), not on the contract bid.

### What a Coach "whale watch" / "panic" indicator can honestly show

Descriptive only: "big taker buying YES ($X in the last 10 s)", "price fell 10¢ in a minute while BTC moved $Y".
It must not be labelled a signal or suggest fading/following: on 30 days of tapes it predicts neither the next
minutes nor settlement beyond the price, and the panic tends to continue slightly, not revert.

### Tables

### O1 — panic (contract moves ≥ X¢ in 60 s, BTC does not)

Descriptive (positive = the panicked side recovers / settles above its post-panic mid):

| test | split | windows / events | R60 ¢ (t) | R180 ¢ (t) | CAL ¢ (t) |
|---|---|---|---|---|---|
| O1 X=8¢ unjustified (panicked side) | train | 230 / 892 | -0.78 (-1.64) | -2.07 (-2.25) | -3.63 (-2.28) |
| O1 X=8¢ unjustified (panicked side) | test | 151 / 618 | -1.34 (-2.27) | -2.19 (-2.02) | -1.36 (-0.69) |
| O1 X=12¢ unjustified (panicked side) | train | 155 / 389 | -0.68 (-0.95) | -1.91 (-1.49) | -3.14 (-1.39) |
| O1 X=12¢ unjustified (panicked side) | test | 124 / 278 | -2.66 (-2.94) | -2.52 (-1.23) | -3.08 (-1.00) |

Control — the same size move when BTC did move ≥ $15 the same way:

| test | split | windows / events | R60 ¢ (t) | R180 ¢ (t) | CAL ¢ (t) |
|---|---|---|---|---|---|
| control X=8¢ justified (fallen side) | train | 256 / 1346 | -0.44 (-1.33) | -0.75 (-1.30) | -1.36 (-1.30) |
| control X=8¢ justified (fallen side) | test | 173 / 966 | -1.05 (-2.81) | -2.34 (-3.33) | -2.98 (-2.90) |
| control X=12¢ justified (fallen side) | train | 248 / 1013 | -0.71 (-1.81) | -1.60 (-2.34) | -3.86 (-3.26) |
| control X=12¢ justified (fallen side) | test | 168 / 715 | -1.83 (-3.89) | -2.62 (-3.07) | -2.97 (-2.25) |

Tradable: buy the panicked side (fade), first event per window:

| config | exit | train n | win % | BE % | net ¢ | t | test n | win % | BE % | net ¢ | t |
|---|---|---|---|---|---|---|---|---|---|---|---|
| of_o1_panic_fade_x8 | hold | 230 | 35.2 | 41.2 | -5.37 | -1.80 | 151 | 43.0 | 41.9 | +1.01 | +0.26 |
| of_o1_panic_fade_x8 | sell60 | 230 | 23.5 | 54.7 | -5.59 | -8.21 | 151 | 21.9 | 53.6 | -6.35 | -6.85 |
| of_o1_panic_fade_x8 | sell180 | 230 | 35.7 | 54.3 | -5.74 | -4.69 | 151 | 39.1 | 59.9 | -6.46 | -4.26 |
| of_o1_panic_fade_x12 | hold | 155 | 36.1 | 41.7 | -4.80 | -1.36 | 124 | 37.9 | 39.8 | -1.77 | -0.41 |
| of_o1_panic_fade_x12 | sell60 | 155 | 24.5 | 59.2 | -6.17 | -7.41 | 124 | 23.4 | 62.4 | -7.56 | -7.54 |
| of_o1_panic_fade_x12 | sell180 | 155 | 34.2 | 53.0 | -6.46 | -4.02 | 124 | 41.1 | 52.4 | -4.76 | -2.10 |

### O2 — taker order-flow imbalance over the last 60 s

OLS of the UP mid's change / settlement residual on imb ∈ [−1, 1] (one obs per window):

| τ | split | n | slope R60 ¢/unit imb (t) | slope R180 (t) | slope CAL (t) | |imb| cut (train P80) |
|---|---|---|---|---|---|---|
| 10 min | train | 261 | +0.69 (+0.29) | +0.92 (+0.21) | -5.19 (-0.52) | 0.355 |
| 10 min | test | 174 | -0.54 (-0.19) | -1.65 (-0.35) | +2.53 (+0.23) | 0.355 |
| 7 min | train | 261 | -3.79 (-1.37) | -12.84 (-2.49) | -18.85 (-1.85) | 0.291 |
| 7 min | test | 174 | +1.65 (+0.47) | +1.90 (+0.27) | +31.50 (+2.35) | 0.291 |
| 4 min | train | 261 | -0.87 (-0.22) | +1.23 (+0.12) | +10.77 (+1.00) | 0.233 |
| 4 min | test | 174 | -4.07 (-0.66) | -9.39 (-0.48) | -17.74 (-1.17) | 0.233 |

Tradable (|imb| ≥ train P80, hold to settlement):

| config | exit | train n | win % | BE % | net ¢ | t | test n | win % | BE % | net ¢ | t |
|---|---|---|---|---|---|---|---|---|---|---|---|
| of_o2_imb_follow_tau10 | hold | 53 | 18.9 | 28.0 | -7.52 | -1.57 | 43 | 25.6 | 25.8 | -0.16 | -0.03 |
| of_o2_imb_fade_tau10 | hold | 53 | 81.1 | 78.3 | +2.37 | +0.50 | 43 | 74.4 | 80.0 | -4.90 | -0.80 |
| of_o2_imb_follow_tau7 | hold | 53 | 13.2 | 28.6 | -11.78 | -2.76 | 30 | 50.0 | 27.3 | +17.33 | +2.18 |
| of_o2_imb_fade_tau7 | hold | 53 | 86.8 | 77.6 | +7.14 | +1.68 | 30 | 50.0 | 79.1 | -22.46 | -2.82 |
| of_o2_imb_follow_tau4 | hold | 53 | 62.3 | 63.8 | -0.70 | -0.16 | 27 | 74.1 | 77.7 | -1.50 | -0.32 |
| of_o2_imb_fade_tau4 | hold | 53 | 37.7 | 44.8 | -3.11 | -0.70 | 27 | 25.9 | 31.5 | -2.19 | -0.47 |

### O3 — whales (top 1% by notional, train-set cut)

Taker orders in the scan range (12:30–4:00 left), all 435 windows: 4,876,627 orders, 51.2% buy YES; notional median $4.85, P90 $78.84, P99 $806, max $83,639; contracts median 15.4, P99 1534. **Train cuts:** single order ≥ $789; 10-s net ≥ $30,270.

Descriptive (positive = the whale's side keeps rising / settles above its mid 1 s after the whale):

| test | split | windows / events | R60 ¢ (t) | R180 ¢ (t) | CAL ¢ (t) |
|---|---|---|---|---|---|
| O3 single (whale side) | train | 261 / 8391 | -0.06 (-0.41) | +0.44 (+1.25) | +0.22 (+0.22) |
| O3 single (whale side) | test | 174 / 5758 | +0.24 (+1.38) | +0.35 (+0.70) | -0.16 (-0.13) |
| O3 cluster (whale side) | train | 76 / 136 | -0.01 (-0.01) | +0.91 (+0.48) | -0.10 (-0.03) |
| O3 cluster (whale side) | test | 84 / 177 | -0.71 (-1.16) | +0.15 (+0.10) | -0.24 (-0.09) |

Tradable (first whale per window; follow = buy the whale's side):

| config | exit | train n | win % | BE % | net ¢ | t | test n | win % | BE % | net ¢ | t |
|---|---|---|---|---|---|---|---|---|---|---|---|
| of_o3_single_follow | hold | 261 | 57.5 | 59.2 | -1.49 | -0.53 | 174 | 51.7 | 57.5 | -5.08 | -1.42 |
| of_o3_single_follow | sell180 | 261 | 36.8 | 57.3 | -5.46 | -5.16 | 174 | 46.0 | 61.2 | -4.38 | -3.23 |
| of_o3_single_fade | hold | 261 | 42.5 | 47.8 | -4.48 | -1.60 | 174 | 48.3 | 49.3 | -0.94 | -0.26 |
| of_o3_single_fade | sell180 | 261 | 34.9 | 57.2 | -5.94 | -5.66 | 174 | 32.8 | 56.3 | -7.15 | -5.24 |
| of_o3_cluster_follow | hold | 76 | 90.8 | 92.0 | -1.10 | -0.34 | 84 | 92.9 | 92.9 | -0.02 | -0.01 |
| of_o3_cluster_follow | sell180 | 76 | 48.7 | 54.4 | -0.77 | -0.45 | 84 | 45.2 | 57.2 | -1.45 | -1.01 |
| of_o3_cluster_fade | hold | 76 | 9.2 | 11.7 | -2.21 | -0.69 | 84 | 7.1 | 10.6 | -3.12 | -1.11 |
| of_o3_cluster_fade | sell180 | 76 | 6.6 | 18.9 | -5.15 | -3.01 | 84 | 10.7 | 26.2 | -4.25 | -3.02 |

### PNW positions: contract-price panic against the position (step 3)

Minute resolution, all 2,848 windows: 687 PNW entries; 283 had a panic (side bid ≤ entry bid − 8¢ at a later minute mark, ≥ 1 min left, BTC still past the strike).

| subset | n | won % | mean bid ¢ | mean mid ¢ | won − mid ¢ (t) | hold − sell-at-bid net of fee ¢ (t) |
|---|---|---|---|---|---|---|
| all | 283 | 56.2 | 60.0 | 60.5 | -4.3 (-1.47) | -2.2 (-0.73) |
| BTC cushion ≥ $20 at panic | 149 | 59.7 | 62.6 | 63.1 | -3.3 (-0.83) | -1.2 (-0.30) |
| BTC cushion < $20 at panic | 134 | 52.2 | 57.1 | 57.7 | -5.4 (-1.25) | -3.2 (-0.74) |
| days 1–15 | 134 | 60.4 | 59.1 | 59.6 | +0.8 (+0.19) | +3.0 (+0.71) |
| days 16–30 | 149 | 52.3 | 60.8 | 61.3 | -8.9 (-2.21) | -6.8 (-1.68) |

Second resolution on the full tapes (105 PNW entries; proxy bid = latest taker-NO/YES print):

| subset | n | won % | mean bid ¢ | mean mid ¢ | won − mid ¢ (t) | hold − sell-at-bid net of fee ¢ (t) |
|---|---|---|---|---|---|---|
| all (tapes) | 72 | 72.2 | 64.7 | 65.3 | +6.9 (+1.32) | +9.2 (+1.75) |
| cushion ≥ $20 (tapes) | 70 | 71.4 | 64.5 | 65.2 | +6.2 (+1.17) | +8.5 (+1.58) |
| cushion < $20 (tapes) | 2 | 100.0 | 69.0 | 70.0 | +30.0 (+nan) | +32.5 (+nan) |

Configs counted in experiments_results.csv after this run: 3,627 → one-sided Bonferroni bar t ≥ 4.19 (this run added 20 configs).
