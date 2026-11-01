# "Call it at ~54c, cash out at 80c" (Discord coin-flip + 80c take-profit)

Source: a trading Discord where people "call" a side when the market is near a coin flip ("I called down at 54%") and
cash out at about 80c ("80% cash that"). Question: does any mechanical version of "enter a side at ~50–55c, take
profit at 80c" make money on KXBTC15M after fees?

## Pre-registration (written 2026-10-29, before any number below was computed)

**Scoring:** `scripts/experiments.py` unchanged: Kalshi 1-minute candle bid/ask, entry at the chosen side's ask
(UP: yes_ask, DOWN: 1 − yes_bid) + 1c slippage (0/2c also logged), exact taker fee rounded up to $0.0001, resting
maker take-profit fills at its price with fee 0, taker exits at the held side's bid − 1c plus fee, Kalshi's official
result, one trade per window, t clustered by window. Train = first 20 UTC days (2026-09-29 .. 10-18), holdout = last
10 (10-19 .. 10-28). Driver: `scripts/quick/coinflip_tp80.py` (read-only on the databases).

**Entry timing:** decision at the candle closes 13 .. 3 minutes before close (harness minutes k = 2 .. 12). At each
minute the rule picks a side; the trade is taken at the FIRST minute where that side's candle ask is inside the band.
Bands: **[50, 55]c primary**, variants [45, 55]c and [50, 60]c (inclusive).

| entry | side chosen at each minute |
|---|---|
| E0 baseline | any side whose ask is in the band (no signal). If both are in the band, the cheaper ask; equal asks → seeded coin flip (seed 20261029) |
| E0b both sides | reference only (not pickable): at E0's minute buy BOTH sides at their asks (both must be quoted); P&L per window = average of the two legs, each with the same exit |
| E1 momentum 1m | the side BTC moved toward over the last 60 s: sign(index(t) − index(t − 60 s)), index = `D.btc` (latest second ≥ 1 s old); zero move = no side |
| E2 momentum 3m | same over 180 s |
| E3 momentum 5m | same over 300 s |
| E4 crowd | the side with more taker-BUY dollars in [t − 60 s, t): taker-YES $ = Σ count·yes_price, taker-NO $ = Σ count·(1 − yes_price) (`kalshi_trades.db` trades). Only the 435 windows with a full tape (300 train / 135 holdout); other windows: no trade. Tie / no prints = no side |

**Exits** (after the entry minute, checked at each later minute mark through the close):

| exit | rule |
|---|---|
| X1 TP80 | resting maker sell of the whole position at 80c (fills when the held side's candle bid ≥ 80c, no fee); else hold to settlement |
| X2 TP80 + bstop-10 | X1, plus a taker sale at the bid once the index is $10 past the strike on the wrong side (same minute as a TP → stop counted first, conservative) |
| X3 hold | no TP, no stop: settle |
| X4 TP80 + time | X1 checked through the candle 1:00 before close; if not hit, sell at that candle's bid (taker) |

So 5 tradable entries x 3 bands x 4 exits = 60 configs, plus E0b x 3 x 4 = 12 reference configs. Views:

* **View M (primary):** minute marks, all 2,848 windows.
* **View T (robustness):** the 435 full-tape windows at print resolution: entry fills at the first taker-buy print
  of that side within 2 s at ≤ candle ask + 1c (as `PrintSim.path`); the TP fills at the first bid print ≥ 80c
  (catches intra-minute touches that minute marks miss); the index stop is checked every second (`index_1s`) and
  sells at the first bid print ≥ 1 s later; the time exit sells at the first bid print at/after 1:00 left + 1 s.

Every view-M config and every view-T config (suffix `@tape`) is appended to `data/research/experiments_results.csv`
(families `coinflip_tp80`, `coinflip_tp80_tape`) at 0/1/2c and counted in the Bonferroni line.

**Train report (1c, train days only):** n, win % vs break-even % (harness definition: avg loss / (avg win + avg
loss)), % that hit 80c, net c/contract, avg loss, worst, t.

**Freeze (train only, at most 2):** the 2 tradable view-M configs with the highest train t among those with train
n ≥ 60. They run on the holdout once, together with two references that are always reported: E0 [50,55] X1 (the
literal Discord rule with no signal) and E0b [50,55] X1.

**Pass bar (holdout):** n ≥ 30; holdout t above the one-sided 5% Bonferroni line over every config in
`experiments_results.csv` after this run (norm.isf(0.05 / n_configs)); net > 0 in both 5-day holdout halves and both
15-day halves of the whole sample; and train net > 0. Passing = CANDIDATE only (needs new days + shadow lab).

**Optional-stopping check (pre-stated expectation):** if the price is a calibrated (martingale) probability, a
take-profit cannot change expected value; it only trades many small wins for fewer, larger losses. Prediction:
for E0 [50,55], X1 − X3 ≈ 0 (paired, within noise), and the share that touches 80c ≈ (entry price − share that
wins without ever touching 80c) / 0.80, i.e. roughly 60–66% at a ~52c entry. A real signal must show up as X3
(hold) being positive too, not only as a high TP hit rate.

## Results (run 2026-10-29)

Checks: view M `hold` equals `experiments.sim_minute`; view T `hold`/`bstop-10` equal `PrintSim.run` at 0/1/2c.
2,160 rows appended (72 configs x 2 views); configs counted 3,860 → **Bonferroni bar 4.21**. Full train table:
`data/research/coinflip_tp80_train.csv`.

### Train (first 20 days, 1c), primary band [50,55]c, view M (all windows)

| entry | exit | n | win % | BE % | hit 80c % | net c | avg loss c | worst c | t |
|---|---|---|---|---|---|---|---|---|---|
| E0 no signal | X1 TP80 | 1,045 | 59.0 | 69.6 | 59.0 | −8.39 | −55.5 | −58 | −6.90 |
| E0 | X2 TP80+bstop-10 | 1,045 | 34.7 | 51.0 | 34.5 | −7.99 | −25.1 | −58 | −10.27 |
| E0 | X3 hold | 1,045 | 52.3 | 55.6 | – | −3.28 | −55.6 | −58 | −2.12 |
| E0 | X4 TP80+time | 1,045 | 57.1 | 68.1 | 53.2 | −7.99 | −49.7 | −58 | −6.95 |
| E1 mom 1m | X1 | 777 | 57.5 | 69.1 | 57.5 | −9.23 | −55.2 | −58 | −6.51 |
| E1 | X3 | 777 | 49.4 | 55.3 | – | −5.84 | −55.2 | −58 | −3.25 |
| E2 mom 3m | X1 | 816 | 59.8 | 69.3 | 59.8 | −7.59 | −55.4 | −58 | −5.53 |
| E2 | X3 | 816 | 52.3 | 55.4 | – | −3.11 | −55.4 | −58 | −1.78 |
| E3 mom 5m | X1 | 826 | 62.6 | 69.3 | 62.6 | −5.33 | −55.3 | −58 | −3.96 |
| E3 | X3 | 826 | 55.7 | 55.4 | – | +0.29 | −55.4 | −58 | +0.17 |
| E4 crowd (tapes) | X1 | 123 | 57.7 | 69.4 | 57.7 | −9.35 | −55.6 | −58 | −2.61 |
| E4 | X3 | 123 | 52.0 | 55.5 | – | −3.50 | −55.6 | −58 | −0.77 |
| E0b both sides | X1 | 1,045 | 13.5 | 33.1 | 100 | −7.85 | −13.3 | −14 | −18.6 |
| E0b | X3 | 1,045 | 0 | – | – | −3.25 | −3.3 | −4 | – |

All 60 tradable view-M configs: every TP80 version (X1/X2/X4) loses 4.8–9.8c/contract (t −3.8 to −11.7) in every
band; the stop (X2) and time exit (X4) don't rescue it. Only hold (X3) gets near zero (−5.8 to +0.3c). View T (435
tapes, train n 107–211, catches intra-minute 80c touches): TP80 versions −0.2 to −6.5c except E0 [45,55] X1 +0.03c
(t 0.01) and E1 [50,60] X1 +0.50c (t 0.18) — noise; hold versions −5.7 to +3.3c (t ≤ 0.88).

**Frozen picks (train only, top 2 view-M train t, n ≥ 60):** E3 [50,55] X3 (train +0.29c, t 0.17) and E3 [45,55] X3
(train −0.37c, so it could not become a candidate anyway). Neither is a "cash at 80" rule: hold beat every TP.

### Holdout (last 10 days, run once, 1c)

| config | view | n | win % | BE % | hit 80c % | net c | t | 5-day halves | 15-day halves |
|---|---|---|---|---|---|---|---|---|---|
| PICK E3 [50,55] hold | M | 443 | 53.3 | 55.4 | – | −2.09 | −0.88 | −4.13 / −0.14 | −0.29 / −0.78 |
| PICK E3 [45,55] hold | M | 562 | 53.2 | 53.3 | – | −0.09 | −0.04 | −1.62 / +1.39 | −1.04 / +0.47 |
| REF E0 [50,55] TP80 (the Discord rule) | M | 547 | 56.9 | 69.4 | 57 | **−10.00** | −5.90 | −9.23 / −10.75 | −8.93 / −8.95 |
| REF E0 [50,55] hold | M | 547 | 49.7 | 55.5 | – | −5.76 | −2.69 | | |
| REF E0b [50,55] TP80 | M | 547 | 12.2 | 33.1 | 100 | −8.35 | −14.9 | | |
| PICK E3 [50,55] hold | T | 63 | 52.4 | 55.3 | – | −2.93 | −0.46 | | |
| PICK E3 [45,55] hold | T | 80 | 53.8 | 53.0 | – | +0.73 | +0.13 | −5.00 / +5.19 | −6.97 / −0.23 |
| REF E0 [50,55] TP80 | T | 79 | 63.3 | 69.6 | 63 | −5.02 | −1.15 | | |

**Verdict: KILLED.** Nothing approaches the 4.21 bar; both picks are negative in at least one half; the literal rule
(first side to reach 50–55c, resting sell at 80c) loses ~10c per contract on the holdout (t −5.9), ~8c on train.

### Optional stopping, checked on E0 [50,55] (train, view M)

Mean entry ask 52.9c; settled win 52.3%; the held side's bid touched 80c in 59.0%; essentially every winner touched
80c first (0.0% won without touching). **X1 − X3 = −5.11c (paired t −6.67).**

Why: Kalshi's price is close to a fair probability, i.e. a martingale. Stopping at a target cannot change the
expected value of what you hold; it only moves outcomes around (win rate 52% → 59%, but break-even 56% → 70%, because
each win is now ~+25c instead of ~+45c while each loss is still ~−55c). The best a TP can do is break even with
holding, and in practice it does worse: when the 80c sale happens the contract is often already worth more (of the
contracts that reached 80c, 88.6% went on to win, so they were worth ~89c on average and were sold for 80c: 8.6c x
59% ≈ 5.1c lost). Prices jump in the last minutes, so "reaching 80c" usually means "already at 85–99c". The martingale
prediction for the touch rate with no overshoot would be (52.9 − 0) / 80 = 66%; the observed 59% is that gap. On the
tapes (finer resolution) the gap is smaller (TP80 −3.6c vs hold +1.4c on 161 train trades) but still the wrong sign.

The no-signal both-sides check gives the cost of a coin-flip entry: E0b hold −3.25c ≈ E0 hold −3.28c, i.e. picking
"the side that reached 52c first" carries no information; it pays half the spread + 1c slippage + ~1.75c fee.

### Plain explanation (shareable)

At 50–55c the market is saying "coin flip". Calling a side there and cashing at 80c *feels* like it works because
you cash out more often than you lose (about 57–63% of the time) — but each cash-out earns ~25c while each loss costs
~55c, so you need to win ~70% to break even, and nobody's "call" gets there. On 2,848 Bitcoin 15-minute markets, the
mechanical version lost ~8–10c per contract after fees; adding momentum (1/3/5 min) or "follow the crowd's buying"
didn't fix it, and a stop or a time exit made no difference. Taking profit at 80c is actually worse than just holding
(about −5c per contract), because by the time the price hits 80c it is often already worth more. What people remember
in Discord is the 80% cash-outs; the 0c losses are quieter.
