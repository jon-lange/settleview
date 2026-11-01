# Parameter experiments: forced positions, aggressiveness, stop-losses and named candidate rules (2026-10-28)

**Question (from the user):** "experiment and adjust different parameters, try being more aggressive, add stop loss, do analysis of forcing the engine to choose a position and see how it does."

Later requests added named rules from GitHub, Reddit and YouTube; all are tested here under the same rules.

**Short answer.** None of the 1,966 configurations tried makes money after fees and spread on the 10 held-out days at a level that survives the multiple-testing correction.

- **Best holdout t:** 1.01, against a bar of 4.05.
- **Holdout net:** only 3 of 1,840 configurations with at least 30 holdout trades are above zero.
- **Forced positions:** forcing a position every window loses roughly the cost of crossing the spread plus the fee, whichever side is picked.
- **More aggressive model taker:** trades up to ten times as often at about the same loss per contract, so it loses more in total.
- **Stops and take-profits:** they narrow the spread of outcomes but do not raise the average; on average they lower it.
- **Named rules:** every one is negative or too small to judge.

**No CANDIDATES.**

This is research on recorded data only. Nothing was ordered, the app and its databases were opened read-only, and nothing in the live app was changed.

- Harness: `scripts/experiments.py`.
- Raw results: `data/research/experiments_results.csv`, one row per configuration × resolution × slippage × split, with every run appended.
- Generated tables (reproduced below): `data/research/experiments_report.md`.

## Setup

- **Data.** All 2,848 settled KXBTC15M windows, 2026-09-29 to 2026-10-28 (30 UTC days), from `data/research/kalshi_trades.db`. Outcome = Kalshi's official `result`. **Train** = the first 20 days (1,896 windows); **holdout** = the last 10 (952). The halves are the first and last 15 days.
- **Prices (minute resolution, every window).** Kalshi's 1-minute candles: the candle ending at minute *k* after the open gives the top of book at that moment. Buying UP pays `yes_ask`; buying DOWN pays `1 − yes_bid`. Selling sells at the held side's bid (`yes_bid` for UP, `1 − yes_ask` for DOWN). Slippage of 0, 1 or 2¢ is added to every buy and subtracted from every sale; **1¢ is the primary figure**. The buy price is capped at 99¢.
- **Fees.** The exact taker fee is 0.07·C·P·(1−P), rounded up to $0.0001, on every buy and every early sale. The vectorised fee matches `fees.kalshi_fee` at every price on a 0.01¢ grid. Settlement pays $1 or $0 with no fee. The one maker leg tested, PNW's resting take-profit, pays no fee and no slippage.
- **Model.** `fair_from_index` (`btc_signal_engine/domain/coach/fair_value.py`) runs at decision time − 1 s through the same `Window` code as the model-vs-market study. It is available in 99.2% of windows.
- **BTC inputs** come from `index_1s` in `data/btcflow.db`, using the latest second strictly before *t* − 1 s. The 1-minute bars for RSI, ADX and the pivot rule are built from those seconds with the same 1-second lag. The VWAP uses the app's Coinbase 1-minute candles, which carry volume.
- **Time-left rule.** Families 1 and 2 decide at 13 to 3 minutes before close, as live (more than 2 minutes left). Named rules whose sources say "2 to 12 minutes" may enter at exactly 2 minutes left.
- **Family 1, forced entry.** One position in every quoted window at 12, 8 or 4 minutes before close. The side comes from one of five rules:
  - (a) **model**: the side with the higher raw edge, fair minus ask, unblended and before fees;
  - (b) **favourite**: the side whose mid is above 50¢;
  - (c) **underdog**: the opposite of (b);
  - (d) **momentum**: the side whose mid rose over the previous minute;
  - (e) **coin**: a seeded coin flip.

  Ties go to the seeded coin; momentum ties are 2–5% of windows.
- **Family 2, model-taker aggressiveness.** The Coach's entry rule at each minute from 13 to 3 minutes left; the first minute that qualifies is the entry. It blends the model toward the market, `fair_used = (1−w)·fair + w·mid`, sets `edge = fair_used(side) − ask − fee`, and enters the side with the larger edge if it clears the minimum. The grid is:
  - minimum edge in {0, 1, 2, 3, 5, 8}¢;
  - market weight *w* in {0, 0.25, 0.5, 0.75};
  - longshot floor on or off.

  The live setting is 5¢ / 0.5 / on. The harness's rule agrees with `rules.entry_decision` on all 3,298 sampled decisions. It leaves out the 10-second confirmation, which minute data cannot observe, and the engine gate.
- **Family 3, exits**, applied to every family 1 and family 2 entry. There are 30 exit rules:
  - hold to settlement;
  - a stop at −5, −10, −15, −20 or −30¢ from the entry ask;
  - a take-profit at +5, +10, +15 or +20¢;
  - stop and take-profit pairs: all 15 combinations of the stops with +5, +10 and +15¢, plus +20¢ with stops of −10, −15 and −20¢;
  - a 10¢ trailing stop;
  - a time exit, selling at the bid 1 minute before close.

  At minute resolution, triggers are checked at each later candle close.
- **Per-print resolution.** This uses the 435 windows with a full trade tape, a random sample. The side and minute come from the same candle decision. The entry fills at the first taker-buy print for that side within 2 s, at or under the candle ask + 1¢; with no fill there is no trade. Each later opposite-taker print is a bid observation and a trigger check. The sale is at the first bid print at least 1 s after the trigger (primary), since the triggering print used up that bid; the instant version is also reported.

  For first-touch only, a `print_scan` view also decides each second from the tape's print-proxy quotes. For PNW, the BTC-distance stop is checked each second.
- **Statistics.** Each configuration takes at most one trade per window, so t = mean / (sd/√n) is clustered by window.
  - **Win %** is the share of trades with positive net P&L.
  - **Break-even %** (BE) is |avg loss| / (avg win + |avg loss|), the win rate the observed sizes need for zero net. For a hold to settlement it equals the average all-in cost, so **win % against BE is "outcome rate vs price paid"**.
  - **Net** is in cents per contract after fees.
  - **Max drawdown** is the largest peak-to-trough fall of cumulative P&L in window order, for one contract per trade.
- **Configurations counted: 1,966.** The count is read from the results CSV, so every run ever made counts:

  | group | configurations |
  |---|---|
  | Family 1: 15 entries × 30 exits | 450 |
  | Family 2: 48 entries × 30 exits | 1,440 |
  | Named rules, with their exits | 76 |

  Slippage levels and the per-print views are robustness views of the same configurations, not extra choices.
- **Multiple-testing correction: Bonferroni, one-sided.** A holdout result is a CANDIDATE only if its t exceeds Φ⁻¹(1 − 0.05/1,966) = **4.05**. That keeps the chance of any false CANDIDATE under 5%, even when the best is chosen by looking at the holdout. The configurations are strongly correlated, since the same entries appear with many exits, so Bonferroni is conservative here. It makes no difference: the best holdout t is 1.01, below even an uncorrected 1.64. The honest procedure is also reported: choose on train, then test once on the holdout.

### How minute resolution biases the exit results

Checking triggers only at minute closes has two opposite effects:

- It misses dips and spikes inside the minute, so fewer stops and take-profits fire.
- When a stop does fire, it sells at a bid that may have gapped past the level.

The per-print check on the 435 tape windows measures both (table "Minute vs per-print resolution" below). On the **mean** the effect is small: per print, stops net about +0.3¢ more than at minute resolution, take-profits about −0.05¢, and time exits and trailing stops within 0.1¢. On the **tails** it is large: the average worst trade under a stop is −73¢ at minute resolution against −40¢ per print, and −62¢ against −30¢ for the trailing stop.

So minute resolution **exaggerates stop tail losses** and leaves the average almost unchanged. No result changes sign.

The exception is PNW's maker take-profit plus BTC stop. On the tape windows it nets +1.98¢ at minute resolution but −1.48¢ per print, because the per-second BTC stop fires on brief returns toward the strike that a minute check misses. Minute resolution flatters that rule by about 3.5¢.

## Plain-English summary

- **Forced positions.** Picking a side every window costs roughly the half-spread (0.3–0.5¢) plus the fee (0.8–1.5¢) plus slippage (1¢), about 2–3¢ a contract, and the loss is about that size for every side rule.
  - The model's choice is no better than a coin flip at 12 minutes (−4.3¢ vs −4.4¢ over all days) and only a little better at 8 minutes (−1.8¢ vs −3.7¢, not significant on the holdout).
  - The model picks the favourite in only 44–47% of windows.
  - Favourite and underdog lose about the same, because the market's mid is well calibrated: at 8 minutes left the favourite's mid averages 75.1% and it wins 75.3%.
  - The least bad is momentum at 12 minutes, at −0.7¢ over all days and +0.5¢ on the holdout (t 0.34). That is noise.
- **Being more aggressive.** Lowering the minimum edge and the market weight raises the trade count, from 297 windows at the live setting to 2,825 at 0¢ / 0, without improving the loss per contract (−1¢ to −4¢), so total losses grow roughly in line with volume. Stricter settings trade rarely and lose more per contract (−5¢ to −16¢): when the model disagrees with the market most, the market is right. The longshot floor raises the win rate but not the net.
- **Stops.** Across all 63 entry rules, every stop lowers the average P&L slightly, by 0.35–0.75¢ a contract, while cutting the sd to 63–91% of hold's.
  - **Max drawdown rises**, by a median 1.1–1.6× over 30 days. The loss per trade grows a little and the win rate falls a lot (from 49% to 30% at −5¢ on forced model entries), so the losing streaks get longer.
  - The worst single trade barely improves at minute resolution, because stops gap through, but improves to about −40¢ per print.
  - Take-profits are worse than stops (−1.6¢ to −2.0¢, and −1.3¢ for +20¢), and stop + take-profit pairs are worse again (−1.1¢ to −2.0¢). Each early exit pays a second fee and the spread, and a take-profit caps the winners that pay for the losers.
  - The one place stops look better is the live taker setting, at −6.4¢ with a −5¢ stop against −8.0¢ for hold, on 297 trades. That is well within noise.
- **Named rules.** All are negative on the holdout, or have too few trades to say anything.
  - **The README's 12% edge filter** fires about once every 1.5 days (18–19 windows in 30 days), because the model rarely disagrees with the market by 12 points.
  - **The "calm regime"** makes no consistent difference.
  - **First-touch fading** shows that the market already prices the touch: in both directions the win rate is 1–4 points below the break-even rate.
  - **The pivot level rules** that look positive (v3 +5.5¢ on 24 holdout trades; v1 +11.8¢ on the 47 tape windows only) have t < 1.9 and fail the correction by a wide margin.

## Named candidate rules

Each rule is one registered function in `scripts/experiments.py`, built as written in its source wherever the source was specific. Every number is at minute resolution with 1¢ slippage, and the full tables are below. "Win % vs BE %" is the outcome rate against the price paid.

**`preceptress_strong_momentum`** (github.com/preceptress/btc-15-minute-prediction-model, "Strong Momentum"). The README's filters, checked once per minute from 12 to 2 minutes left:

- the ask is 20–85¢ and the spread is 5¢ or less;
- BTC has moved at least 0.12% from the reference, and the window move and the last 3-minute move agree;
- the model's edge is at least 12 points (absolute, fair minus ask);
- crowding filter: skip a side if 8 of the last 10 entries took it.

The candle-age check is always true on history.

| variant | trades in 30 days | holdout n | holdout net c | holdout t | train net c |
|---|---|---|---|---|---|
| 12% edge, Kalshi strike | 18 | 5 | −39.2 | — | — |
| 12% edge, our open average | 19 | 7 | −4.3 | — | — |
| 3% edge, Kalshi strike | 126 | 34 | −22.85 | −2.64 | −0.31 |
| 3% edge, our open average | 140 | 40 | −18.87 | −2.37 | −0.98 |

At the 12% edge the model almost never disagrees with Kalshi by that much, so neither variant has enough trades to judge. The 3% variants lose badly on the holdout and roughly break even on train. The crowding filter removes 13–25 entries of the 3% variants.

For comparison, **H5** (BTC lag momentum, ±0.15% at 10, 8 and 6 minutes left, ask ≤ 90¢) was re-run here at minute resolution:

- train +0.36¢ (t 0.15);
- holdout −6.75¢ (n 81, t −1.57);
- the human-rules study's print-based interim figure was −5.65¢ (n 49).

Both momentum rules buy favourites that are already priced, and both lose on the holdout.

**`first_touch_fade_underdog`** (Reddit, "odds of touching"). At the first minute from 13 to 2 minutes left when a side's ask touches L¢ or less, for L in {10, 15, 20, 25, 30}, the rule buys the other side, the favourite. The mirror buys the touched side. Windows whose side was already at or below L at the first candle are skipped, because their first touch came earlier. The touch is measured on the ask.

All 10 configurations lose on the holdout:

- **fade:** −1.1¢ to −4.3¢;
- **touched:** −0.9¢ to −1.8¢.

In every case the win rate sits 1–4 points below the break-even rate. For example, at L = 20 the touched side wins 15.5% against a break-even of 16.7%, and the favourite wins 84.5% against 87.9%. The earlier quick check on 292 tapes ("touched sides win 3–4 points below their price") counted every window that ever touched L, a biased selection. Measured properly at the first touch, the touched side wins about 1 point below its price, which is the fee and the spread.

The per-second tape scan on 435 windows agrees: at L = 20 the touched side nets +0.19¢ over all days (n 380, t 0.09). The market already prices the touch.

**`early_momentum_vwap`** (Reddit). At 3 to 6 minutes elapsed (first qualifying minute), the rule enters on the side where all four conditions agree:

- BTC is above (or below) the trailing 60-minute VWAP;
- the 5-minute moving average slopes the same way;
- the 15-minute moving average slopes the same way;
- the window move (BTC vs Kalshi strike) agrees.

The ask must be 20–85¢, and the trade is held. The VWAP is built from the app's Coinbase 1-minute candles with volume. A time-weighted index average replaces it where fewer than 45 candles exist, which affects 20 of 1,961 entries in the last ~6 hours of the period.

Results:

- train −1.54¢ (t −1.24);
- holdout −5.49¢ (n 655, t −3.00);
- both halves negative (−2.17¢, −3.54¢);
- holdout win rate 64.3% against a break-even of 70.0%.

**`calm_regime_tp20`** (Reddit "My Full Strategy": "shines in calmer waters"). This applies the family 1 forced entries (model, favourite, momentum, coin at 12, 8 and 4 minutes left) only in calm windows: 60-minute realized volatility below its trailing 7-day median at the decision, with no lookahead. The opposite ("busy") is reported alongside. Each runs with hold and with the +20¢ take-profit / −15¢ stop. The first day has no volatility history and is skipped.

Calm against busy shows no consistent pattern across the 24 pairs. The only positive holdout among all 1,966 configurations is busy-regime momentum at 12 minutes with hold: +2.17¢ (t 1.01), with train at −1.97¢. That is noise. The +20¢ / −15¢ exit is worse than hold in 19 of the 24 holdout comparisons.

**`pnw_cushion_tp20`** (PNW Coast Off-Grid YouTube walkthrough). At 9, 8 or 7 minutes left, if BTC is at least $50 past Kalshi's strike, the rule buys that favourite when its ask is 65–80¢. The exits are a resting maker take-profit at +20¢ (no fee) and a BTC stop that sells at the bid once BTC comes back within $20 of the strike.

| variant | trades in 30 days | train net c (t) | holdout n | holdout net c (t) | ends: TP / stop / settle |
|---|---|---|---|---|---|
| v1, base rule | 687 | −2.68 (−2.34) | 222 | −1.97 (−1.21) | 56% / 44% / 0% |
| v2, + calm and ADX 20–35 | 75 | −2.20 | 28 | +2.24 (0.56) | — |
| v3, + RSI lean | 72 | — | 28 | +2.24 (0.56) | — |
| v4, v1 held to settlement | 687 | −1.65 | 222 | −3.54 (−1.21) | — |
| v5 "underdog rebound", hold | 157 | — | — | −6.67 | — |
| v5 "underdog rebound", +20¢ TP | 157 | — | — | −9.45 | — |

- In v1, no trade reaches settlement: BTC either reaches the take-profit or comes back toward the strike first.
- v3's RSI lean removes only 3 trades from v2, so their holdout trades are identical.
- Per print on the tape windows, v1's exit nets 3.5¢ worse than at minute resolution (+1.98¢ → −1.48¢), because the per-second BTC stop catches brief returns toward the strike.
- Against **H6** (the favourite at 75–90¢ at 8, 6 or 4 minutes left, run here at minute resolution: train −3.15¢, holdout −3.50¢, n 579, t −2.16), PNW v1 loses a little less per contract, but the difference is well within noise.

**`level_reversal_pivot`** (YouTube "sneaky pivot"). Levels are the previous UTC day's range high and low from `index_1s`, plus the most recent earlier daily high above it and daily low below it, looking back up to 10 days. The first day lacks a full prior day and is skipped. Touch means within 0.10%.

- **v1, touch at open:** at 13 minutes left, BTC near a lower level, or through it in the prior 15 minutes, means buy UP. The upper-level mirror buys DOWN; it is applied symmetrically, including "traded through".
- **v2:** three 1-minute bars inside the window: a touch, a green "sneaky" bar, then a break of its high (mirrored for upper levels).
- **v3:** the same sequence on 15-minute bars across windows; the next window takes the trade at 13 minutes left.

| variant | qualifying windows | holdout net c | holdout t | holdout win % vs BE % | notes |
|---|---|---|---|---|---|
| v1 | 849 | −2.81 | −1.14 | 50.6 vs 53.8 | — |
| v2 | 101 | −4.19 | — | 73.3 vs 78.8 | −3.28¢ with the $25 level stop |
| v3 | 49 | +5.50 (n 24) | 0.63 | 54.2 vs 47.1 | train +5.92¢ (t 0.61); +5.45¢ with the stop |

The question was whether Kalshi already prices the bounce. For v1 and v2 it does: they win less often than the price paid implies. v3 is the only pivot variant that is positive in train, holdout and both halves. It rests on 49 trades over a month with t ≈ 0.6, about 6 t-units short of the correction bar, so this is not a CANDIDATE.

## Caveats

- **Minute resolution.** Triggers are checked once a minute, which exaggerates stop tails (worst trade about −73¢ against −40¢ per print) and leaves the mean nearly unchanged (±0.3¢). The per-print check covers 435 random windows, 15% of the total.
- **Execution assumptions.** The candle top of book says nothing about depth beyond one contract. The 10-second confirmation and the engine gate are not modelled.
- **Sample and period.** One month (30 days; a 10-day holdout of 952 windows) of one market regime, BTC at roughly $80–86k. A rule that is flat here could behave differently in another regime. Some named rules have too few trades to test at all: 18–19 for the README's 12% filter and 49 for pivot v3.
- **Interpretation choices.** Where a source was vague, the choices made are written in each rule's docstring in `scripts/experiments.py`: the touch is measured on the ask, the PNW maker TP fills when the bid reaches it, the pivot mirror is symmetric, and "signal" in the crowding filter means an entry the rule actually took.

## Runtime

- Loading the data takes about 20 s, including 435 full tapes, and the fair-value cache builds in under 10 s.
- The whole grid (1,966 configurations × 3 slippages × minute, tape and per-print views) runs in 133 s.
- `report` takes a few seconds.
- A single `run` of one rule takes about 10–20 s.

## How to test a new idea in one function

A rule is one decorated function in `scripts/experiments.py`. It receives the loaded data `D` and returns, for every window, the decision minute `k` (minutes since the open; −1 means no trade) and the side (+1 UP, −1 DOWN).

`D` provides:

| field | contents |
|---|---|
| `D.YB`, `D.YA` | candle-close yes bid and ask, shape `(windows, 17)` by minute |
| `D.F` | model fair_up at each minute |
| `D.btc(t)` | the BTC index a decision at `t` may use |
| `D.strike` | Kalshi's floor_strike |
| `D.open_avg` | our own open average |
| `D.mk` | open and close times, day and split |

The harness then does the rest: prices, slippage, fees, the exits, train and holdout, both halves, per-print fills on the tape windows, and the CSV row. This is the whole H5 rule (the file version only adds float tolerances):

```python
@entry_rule("h5_lag_momentum")
def h5_lag_momentum(D, move=0.15, max_ask=90):
    k_out, s_out = _none(D.n)
    open_ = D.mk.open_ts.to_numpy(np.int64)
    for k in (5, 7, 9):                                   # 10, 8, 6 minutes left
        r = D.btc(open_ + k * 60_000) / D.strike - 1
        up = (r >= move / 100) & (D.YA[:, k] <= max_ask / 100)
        dn = (r <= -move / 100) & (1 - D.YB[:, k] <= max_ask / 100)
        hit = (k_out < 0) & (up | dn)
        k_out[hit], s_out[hit] = k, np.where(up, 1, -1)[hit]
    return k_out, s_out
```

Run it, with any exits and parameters, from the repo root:

```
python scripts/dev.py experiments run --entry h5_lag_momentum:move=0.2,max_ask=85 --exit hold,sl10,sl10_tp15,trail10,time1m
python scripts/dev.py experiments report     # re-tabulates; the multiple-testing count includes every config ever run
```

One `run` takes about 10–15 s, mostly loading the data. The full grid, all 1,966 configurations at both resolutions, takes about 2 minutes.

Every configuration you run is appended to the CSV and raises the Bonferroni bar in `report`. Trying 20 variants of an idea and keeping the best is exactly the kind of search the correction charges for.

## Results (generated by `scripts/experiments.py report`)

All tables: minute resolution and 1c slippage unless stated; c = cents per contract after fees; BE = break-even win rate.

### Family 1: forced entry, hold to settlement (minute resolution, 1c slippage)

| entry | train n | win % | BE % | net c | t | holdout n | win % | BE % | net c | t |
|---|---|---|---|---|---|---|---|---|---|---|
| forced:coin:12 | 1896 | 47.5 | 53.6 | -5.01 | -4.83 | 952 | 50.0 | 53.7 | -3.10 | -2.08 |
| forced:coin:4 | 1896 | 49.2 | 54.6 | -2.47 | -3.17 | 952 | 49.5 | 54.6 | -2.28 | -2.11 |
| forced:coin:8 | 1896 | 49.3 | 53.9 | -3.07 | -3.30 | 952 | 47.3 | 54.7 | -5.01 | -3.73 |
| forced:favourite:12 | 1896 | 66.7 | 69.7 | -2.75 | -2.65 | 952 | 65.5 | 69.7 | -3.90 | -2.62 |
| forced:favourite:4 | 1896 | 83.1 | 86.7 | -3.04 | -3.89 | 952 | 83.8 | 86.1 | -1.94 | -1.80 |
| forced:favourite:8 | 1896 | 75.9 | 77.8 | -1.63 | -1.75 | 952 | 73.9 | 78.1 | -3.72 | -2.77 |
| forced:model:12 | 1883 | 47.8 | 52.8 | -4.08 | -3.92 | 942 | 47.7 | 53.2 | -4.63 | -3.09 |
| forced:model:4 | 1883 | 50.3 | 57.1 | -3.10 | -3.97 | 942 | 47.1 | 51.5 | -1.98 | -1.82 |
| forced:model:8 | 1883 | 50.2 | 53.0 | -1.89 | -2.03 | 942 | 47.0 | 49.5 | -1.70 | -1.26 |
| forced:momentum:12 | 1896 | 61.3 | 62.9 | -1.32 | -1.27 | 952 | 62.4 | 61.8 | 0.50 | 0.34 |
| forced:momentum:4 | 1896 | 69.1 | 72.2 | -1.66 | -2.13 | 952 | 71.1 | 72.3 | -0.63 | -0.58 |
| forced:momentum:8 | 1896 | 62.1 | 67.3 | -3.70 | -3.99 | 952 | 61.2 | 65.3 | -2.92 | -2.18 |
| forced:underdog:12 | 1896 | 33.3 | 36.7 | -3.19 | -3.07 | 952 | 34.5 | 36.6 | -2.02 | -1.36 |
| forced:underdog:4 | 1896 | 16.9 | 18.0 | -0.94 | -1.21 | 952 | 16.2 | 18.6 | -1.94 | -1.79 |
| forced:underdog:8 | 1896 | 24.1 | 28.1 | -3.57 | -3.85 | 952 | 26.1 | 27.7 | -1.43 | -1.06 |

Slippage sensitivity (all 30 days, hold): net c/contract at 0 / 1 / 2c slippage.

| entry | 0c | 1c | 2c |
|---|---|---|---|
| forced:coin:12 | -3.37 | -4.37 | -5.37 |
| forced:coin:4 | -1.48 | -2.41 | -3.29 |
| forced:coin:8 | -2.72 | -3.72 | -4.71 |
| forced:favourite:12 | -2.16 | -3.14 | -4.11 |
| forced:favourite:4 | -1.87 | -2.67 | -3.40 |
| forced:favourite:8 | -1.37 | -2.33 | -3.27 |
| forced:model:12 | -3.26 | -4.26 | -5.26 |
| forced:model:4 | -1.85 | -2.73 | -3.56 |
| forced:model:8 | -0.83 | -1.83 | -2.82 |
| forced:momentum:12 | 0.28 | -0.71 | -1.69 |
| forced:momentum:4 | -0.49 | -1.31 | -2.08 |
| forced:momentum:8 | -2.46 | -3.44 | -4.41 |
| forced:underdog:12 | -1.78 | -2.80 | -3.82 |
| forced:underdog:4 | -0.23 | -1.27 | -2.32 |
| forced:underdog:8 | -1.82 | -2.85 | -3.89 |

### Family 2: model taker aggressiveness, hold to settlement (minute resolution, 1c slippage)

| entry | train n | win % | BE % | net c | t | holdout n | win % | BE % | net c | t |
|---|---|---|---|---|---|---|---|---|---|---|
| taker:edge=0,blend=0,floor=0 | 1883 | 48.1 | 51.9 | -3.26 | -3.08 | 942 | 47.9 | 51.2 | -2.86 | -1.86 |
| taker:edge=0,blend=0,floor=1 | 1844 | 54.9 | 58.6 | -3.15 | -2.96 | 909 | 55.2 | 58.9 | -3.17 | -2.04 |
| taker:edge=0,blend=0.25,floor=0 | 1883 | 47.3 | 51.9 | -3.79 | -3.62 | 942 | 48.0 | 50.8 | -2.38 | -1.57 |
| taker:edge=0,blend=0.25,floor=1 | 1801 | 57.7 | 60.8 | -2.55 | -2.40 | 891 | 57.5 | 60.9 | -2.95 | -1.91 |
| taker:edge=0,blend=0.5,floor=0 | 1878 | 46.9 | 51.3 | -3.38 | -3.35 | 941 | 47.4 | 49.8 | -1.96 | -1.33 |
| taker:edge=0,blend=0.5,floor=1 | 1657 | 63.0 | 64.9 | -1.59 | -1.48 | 815 | 61.8 | 64.6 | -2.28 | -1.45 |
| taker:edge=0,blend=0.75,floor=0 | 1688 | 47.6 | 50.3 | -1.60 | -1.73 | 866 | 43.1 | 46.6 | -2.17 | -1.62 |
| taker:edge=0,blend=0.75,floor=1 | 1118 | 75.8 | 75.9 | -0.07 | -0.07 | 499 | 72.1 | 76.2 | -3.22 | -1.81 |
| taker:edge=1,blend=0,floor=0 | 1879 | 47.3 | 51.6 | -3.56 | -3.42 | 941 | 47.9 | 50.5 | -2.15 | -1.42 |
| taker:edge=1,blend=0,floor=1 | 1744 | 55.6 | 59.1 | -3.02 | -2.77 | 841 | 54.6 | 58.4 | -3.36 | -2.06 |
| taker:edge=1,blend=0.25,floor=0 | 1852 | 47.1 | 51.1 | -3.16 | -3.09 | 934 | 47.3 | 49.6 | -1.82 | -1.22 |
| taker:edge=1,blend=0.25,floor=1 | 1569 | 58.6 | 60.5 | -1.57 | -1.37 | 773 | 57.3 | 60.1 | -2.42 | -1.44 |
| taker:edge=1,blend=0.5,floor=0 | 1687 | 46.1 | 49.3 | -2.33 | -2.28 | 861 | 44.7 | 47.2 | -1.88 | -1.25 |
| taker:edge=1,blend=0.5,floor=1 | 1181 | 62.4 | 63.7 | -1.05 | -0.82 | 564 | 59.2 | 63.4 | -3.58 | -1.85 |
| taker:edge=1,blend=0.75,floor=0 | 769 | 34.5 | 39.4 | -3.46 | -2.38 | 390 | 33.1 | 34.1 | -0.72 | -0.36 |
| taker:edge=1,blend=0.75,floor=1 | 339 | 61.9 | 65.5 | -3.24 | -1.27 | 144 | 61.8 | 67.1 | -4.58 | -1.21 |
| taker:edge=2,blend=0,floor=0 | 1837 | 47.1 | 50.8 | -2.96 | -2.87 | 928 | 47.4 | 49.4 | -1.61 | -1.07 |
| taker:edge=2,blend=0,floor=1 | 1573 | 57.3 | 58.8 | -1.31 | -1.13 | 775 | 56.0 | 58.5 | -2.25 | -1.32 |
| taker:edge=2,blend=0.25,floor=0 | 1716 | 45.2 | 49.7 | -3.40 | -3.28 | 878 | 45.7 | 47.1 | -1.12 | -0.74 |
| taker:edge=2,blend=0.25,floor=1 | 1308 | 57.8 | 60.7 | -2.42 | -1.94 | 637 | 56.7 | 59.7 | -2.67 | -1.45 |
| taker:edge=2,blend=0.5,floor=0 | 1294 | 41.7 | 44.8 | -2.34 | -2.00 | 659 | 39.2 | 42.8 | -2.72 | -1.66 |
| taker:edge=2,blend=0.5,floor=1 | 749 | 61.4 | 62.7 | -1.13 | -0.68 | 346 | 59.0 | 62.5 | -3.10 | -1.25 |
| taker:edge=2,blend=0.75,floor=0 | 330 | 28.8 | 37.6 | -6.61 | -2.98 | 168 | 26.2 | 33.4 | -5.36 | -1.79 |
| taker:edge=2,blend=0.75,floor=1 | 144 | 50.0 | 61.6 | -10.69 | -2.66 | 62 | 51.6 | 60.1 | -8.03 | -1.28 |
| taker:edge=3,blend=0,floor=0 | 1721 | 45.7 | 49.5 | -2.96 | -2.81 | 886 | 46.2 | 47.5 | -1.08 | -0.71 |
| taker:edge=3,blend=0,floor=1 | 1370 | 56.5 | 59.0 | -2.13 | -1.72 | 686 | 55.5 | 58.0 | -2.14 | -1.20 |
| taker:edge=3,blend=0.25,floor=0 | 1488 | 44.2 | 47.5 | -2.48 | -2.26 | 765 | 42.9 | 44.6 | -1.36 | -0.85 |
| taker:edge=3,blend=0.25,floor=1 | 1017 | 59.6 | 60.3 | -0.62 | -0.43 | 491 | 56.2 | 59.7 | -3.12 | -1.48 |
| taker:edge=3,blend=0.5,floor=0 | 900 | 39.1 | 41.7 | -1.98 | -1.39 | 438 | 37.2 | 39.4 | -1.63 | -0.80 |
| taker:edge=3,blend=0.5,floor=1 | 470 | 59.6 | 61.6 | -1.79 | -0.82 | 205 | 56.1 | 63.1 | -6.09 | -1.90 |
| taker:edge=3,blend=0.75,floor=0 | 148 | 29.7 | 42.1 | -9.73 | -2.80 | 65 | 26.2 | 39.6 | -9.42 | -2.07 |
| taker:edge=3,blend=0.75,floor=1 | 79 | 46.8 | 59.8 | -12.88 | -2.23 | 34 | 44.1 | 56.9 | -11.06 | -1.42 |
| taker:edge=5,blend=0,floor=0 | 1390 | 43.7 | 46.5 | -2.14 | -1.85 | 719 | 41.6 | 44.0 | -1.95 | -1.17 |
| taker:edge=5,blend=0,floor=1 | 961 | 57.8 | 58.2 | -0.36 | -0.24 | 463 | 54.2 | 57.7 | -3.05 | -1.41 |
| taker:edge=5,blend=0.25,floor=0 | 971 | 40.0 | 42.4 | -1.89 | -1.37 | 473 | 38.1 | 40.6 | -1.98 | -1.00 |
| taker:edge=5,blend=0.25,floor=1 | 544 | 58.8 | 59.6 | -0.74 | -0.37 | 244 | 56.1 | 60.3 | -3.73 | -1.25 |
| taker:edge=5,blend=0.5,floor=0 | 380 | 35.5 | 41.8 | -4.93 | -2.23 | 194 | 32.5 | 38.8 | -4.92 | -1.63 |
| taker:edge=5,blend=0.5,floor=1 | 204 | 52.9 | 60.5 | -7.07 | -2.06 | 93 | 49.5 | 60.4 | -10.15 | -2.01 |
| taker:edge=5,blend=0.75,floor=0 | 37 | 27.0 | 45.3 | -16.36 | -2.23 | 18 | 27.8 | 41.9 | -13.13 | -1.23 |
| taker:edge=5,blend=0.75,floor=1 | 23 | 34.8 | 57.7 | -22.05 | -2.21 | 13 | 30.8 | 47.5 | -16.53 | -1.23 |
| taker:edge=8,blend=0,floor=0 | 815 | 38.9 | 42.0 | -2.42 | -1.58 | 398 | 37.7 | 39.5 | -1.44 | -0.67 |
| taker:edge=8,blend=0,floor=1 | 467 | 56.7 | 57.6 | -0.83 | -0.37 | 203 | 54.7 | 59.5 | -4.28 | -1.30 |
| taker:edge=8,blend=0.25,floor=0 | 416 | 36.8 | 42.2 | -4.31 | -2.01 | 205 | 33.2 | 40.1 | -5.33 | -1.81 |
| taker:edge=8,blend=0.25,floor=1 | 233 | 54.1 | 59.2 | -4.85 | -1.50 | 107 | 48.6 | 59.7 | -10.12 | -2.18 |
| taker:edge=8,blend=0.5,floor=0 | 124 | 35.5 | 45.8 | -8.80 | -2.13 | 49 | 32.7 | 45.3 | -10.16 | -1.69 |
| taker:edge=8,blend=0.5,floor=1 | 81 | 46.9 | 58.5 | -11.41 | -2.01 | 33 | 42.4 | 56.0 | -11.93 | -1.50 |
| taker:edge=8,blend=0.75,floor=0 | 3 | 33.3 | 32.4 | 0.64 | 0.03 | 5 | 40.0 | 41.6 | -1.47 | -0.06 |
| taker:edge=8,blend=0.75,floor=1 | 1 | 100.0 | n/a | 48.25 | n/a | 4 | 50.0 | 45.5 | 4.52 | 0.15 |

### Named candidate rules (minute resolution, 1c slippage)

| config | train n | train net c | train t | holdout n | win % | BE % | holdout net c | holdout t | 1st-half n / net c | 2nd-half n / net c | exit mix (all days) TP / stop / settle |
|---|---|---|---|---|---|---|---|---|---|---|---|
| early_momentum_vwap|hold | 1306 | -1.54 | -1.24 | 655 | 64.3 | 70.0 | -5.49 | -3.00 | 975 / -2.17 | 986 / -3.54 | 0% / 0% / 100% |
| first_touch_fade_underdog:L=10,buy=fade|hold | 1466 | -1.50 | -2.30 | 736 | 93.8 | 94.9 | -1.12 | -1.26 | 1116 / -1.24 | 1086 / -1.51 | 0% / 0% / 100% |
| first_touch_fade_underdog:L=10,buy=touched|hold | 1466 | -1.46 | -2.23 | 736 | 6.2 | 8.1 | -1.82 | -2.05 | 1116 / -1.73 | 1086 / -1.43 | 0% / 0% / 100% |
| first_touch_fade_underdog:L=15,buy=fade|hold | 1613 | -1.49 | -1.99 | 822 | 89.2 | 91.9 | -2.68 | -2.49 | 1228 / -1.20 | 1207 / -2.59 | 0% / 0% / 100% |
| first_touch_fade_underdog:L=15,buy=touched|hold | 1613 | -2.47 | -3.30 | 822 | 10.8 | 12.0 | -1.17 | -1.09 | 1228 / -2.75 | 1207 / -1.29 | 0% / 0% / 100% |
| first_touch_fade_underdog:L=20,buy=fade|hold | 1706 | -1.86 | -2.20 | 867 | 84.5 | 87.9 | -3.32 | -2.72 | 1298 / -1.57 | 1275 / -3.15 | 0% / 0% / 100% |
| first_touch_fade_underdog:L=20,buy=touched|hold | 1706 | -2.77 | -3.27 | 867 | 15.5 | 16.7 | -1.22 | -1.00 | 1298 / -3.05 | 1275 / -1.42 | 0% / 0% / 100% |
| first_touch_fade_underdog:L=25,buy=fade|hold | 1729 | -2.26 | -2.41 | 866 | 79.7 | 83.8 | -4.12 | -3.02 | 1316 / -1.94 | 1279 / -3.85 | 0% / 0% / 100% |
| first_touch_fade_underdog:L=25,buy=touched|hold | 1729 | -2.85 | -3.03 | 866 | 20.3 | 21.3 | -0.92 | -0.68 | 1316 / -3.16 | 1279 / -1.23 | 0% / 0% / 100% |
| first_touch_fade_underdog:L=30,buy=fade|hold | 1636 | -3.44 | -3.26 | 801 | 75.0 | 79.4 | -4.28 | -2.81 | 1250 / -3.62 | 1187 / -3.82 | 0% / 0% / 100% |
| first_touch_fade_underdog:L=30,buy=touched|hold | 1636 | -2.06 | -1.95 | 801 | 25.0 | 26.1 | -1.16 | -0.76 | 1250 / -1.88 | 1187 / -1.64 | 0% / 0% / 100% |
| h5_lag_momentum|hold | 192 | 0.36 | 0.15 | 81 | 81.5 | 88.3 | -6.75 | -1.57 | 135 / -0.40 | 138 / -3.07 | 0% / 0% / 100% |
| h6_favourite_75_90|hold | 1238 | -3.15 | -2.86 | 579 | 81.0 | 84.5 | -3.50 | -2.16 | 929 / -2.76 | 888 / -3.79 | 0% / 0% / 100% |
| level_reversal_pivot:variant=v1|hold | 493 | -2.98 | -1.41 | 356 | 50.6 | 53.8 | -2.81 | -1.14 | 391 / -2.59 | 458 / -3.17 | 0% / 0% / 100% |
| level_reversal_pivot:variant=v2|hold | 56 | -7.33 | -1.55 | 45 | 73.3 | 78.8 | -4.19 | -0.72 | 38 / -6.68 | 63 / -5.48 | 0% / 0% / 100% |
| level_reversal_pivot:variant=v2|lstop25 | 56 | -6.81 | -1.48 | 45 | 71.1 | 75.9 | -3.28 | -0.60 | 38 / -5.91 | 63 / -4.83 | 0% / 5% / 95% |
| level_reversal_pivot:variant=v3|hold | 25 | 5.92 | 0.61 | 24 | 54.2 | 47.1 | 5.50 | 0.63 | 19 / 2.31 | 30 / 7.87 | 0% / 0% / 100% |
| level_reversal_pivot:variant=v3|lstop25 | 25 | 8.36 | 0.98 | 24 | 54.2 | 46.9 | 5.45 | 0.64 | 19 / 5.51 | 30 / 7.84 | 0% / 12% / 88% |
| pnw_cushion_tp20:variant=v1|mtp20_bstop20 | 465 | -2.68 | -2.34 | 222 | 58.1 | 62.4 | -1.97 | -1.21 | 343 / -2.78 | 344 / -2.13 | 56% / 44% / 0% |
| pnw_cushion_tp20:variant=v2|mtp20_bstop20 | 47 | -2.20 | -0.64 | 28 | 67.9 | 62.7 | 2.24 | 0.56 | 39 / -3.49 | 36 / 2.65 | 56% / 44% / 0% |
| pnw_cushion_tp20:variant=v3|mtp20_bstop20 | 44 | -0.55 | -0.16 | 28 | 67.9 | 62.7 | 2.24 | 0.56 | 37 / -1.05 | 35 / 2.22 | 57% / 43% / 0% |
| pnw_cushion_tp20:variant=v4|hold | 465 | -1.65 | -0.85 | 222 | 74.3 | 77.9 | -3.54 | -1.21 | 343 / -0.19 | 344 / -4.33 | 0% / 0% / 100% |
| pnw_underdog_rebound|hold | 102 | -10.85 | -2.39 | 55 | 34.5 | 41.3 | -6.67 | -1.03 | 78 / -12.34 | 79 / -6.47 | 0% / 0% / 100% |
| pnw_underdog_rebound|mtp20 | 102 | -10.71 | -3.67 | 55 | 54.5 | 70.6 | -9.45 | -2.36 | 78 / -12.19 | 79 / -8.37 | 53% / 0% / 47% |
| preceptress_strong_momentum:edge=12,ref=own|hold | 12 | -7.00 | -0.54 | 7 | 71.4 | 75.4 | -4.31 | -0.21 | 10 / -11.69 | 9 / 0.31 | 0% / 0% / 100% |
| preceptress_strong_momentum:edge=12,ref=strike|hold | 13 | -4.56 | -0.37 | 5 | 40.0 | 76.8 | -39.19 | -1.50 | 11 / -8.39 | 7 / -23.28 | 0% / 0% / 100% |
| preceptress_strong_momentum:edge=3,ref=own|hold | 100 | -0.98 | -0.25 | 40 | 62.5 | 81.0 | -18.87 | -2.37 | 70 / -0.68 | 70 / -11.50 | 0% / 0% / 100% |
| preceptress_strong_momentum:edge=3,ref=strike|hold | 92 | -0.31 | -0.08 | 34 | 58.8 | 81.5 | -22.85 | -2.64 | 66 / 1.57 | 60 / -15.15 | 0% / 0% / 100% |

**Calm vs busy regime, same forced entries** (calm = 60-min realized vol below its trailing 7-day median at the decision; minute resolution, 1c):

| forced entry | exit | calm train n / net c / t | calm holdout n / net c / t | busy train n / net c / t | busy holdout n / net c / t |
|---|---|---|---|---|---|
| rule=coin,tau=12 | hold | 937 / -5.26 / -3.53 | 493 / -2.03 / -0.98 | 862 / -4.67 / -3.06 | 459 / -4.25 / -1.98 |
| rule=coin,tau=12 | sl15_tp20 | 937 / -6.20 / -8.00 | 493 / -5.21 / -4.76 | 862 / -5.64 / -6.96 | 459 / -7.81 / -7.15 |
| rule=coin,tau=4 | hold | 936 / -3.75 / -3.27 | 494 / -2.81 / -1.81 | 863 / -1.23 / -1.10 | 458 / -1.71 / -1.13 |
| rule=coin,tau=4 | sl15_tp20 | 936 / -3.41 / -4.21 | 494 / -3.85 / -3.52 | 863 / -1.80 / -2.30 | 458 / -1.62 / -1.47 |
| rule=coin,tau=8 | hold | 941 / -2.97 / -2.21 | 495 / -5.10 / -2.70 | 858 / -3.12 / -2.30 | 457 / -4.91 / -2.56 |
| rule=coin,tau=8 | sl15_tp20 | 941 / -4.51 / -5.62 | 495 / -5.27 / -4.64 | 858 / -3.34 / -4.13 | 457 / -5.10 / -4.48 |
| rule=favourite,tau=12 | hold | 937 / -3.00 / -2.01 | 493 / -2.50 / -1.21 | 862 / -2.23 / -1.46 | 459 / -5.40 / -2.52 |
| rule=favourite,tau=12 | sl15_tp20 | 937 / -4.08 / -5.01 | 493 / -5.69 / -5.13 | 862 / -4.64 / -5.57 | 459 / -4.98 / -4.37 |
| rule=favourite,tau=4 | hold | 936 / -2.69 / -2.35 | 494 / -1.02 / -0.66 | 863 / -3.74 / -3.34 | 458 / -2.94 / -1.94 |
| rule=favourite,tau=4 | sl15_tp20 | 936 / -3.04 / -3.71 | 494 / -2.86 / -2.64 | 863 / -2.64 / -3.33 | 458 / -2.30 / -2.09 |
| rule=favourite,tau=8 | hold | 941 / -1.86 / -1.38 | 495 / -3.11 / -1.65 | 858 / -1.55 / -1.15 | 457 / -4.39 / -2.29 |
| rule=favourite,tau=8 | sl15_tp20 | 941 / -2.85 / -3.45 | 495 / -4.09 / -3.64 | 858 / -1.83 / -2.25 | 457 / -4.11 / -3.56 |
| rule=model,tau=12 | hold | 925 / -4.61 / -3.08 | 486 / -1.53 / -0.73 | 861 / -3.93 / -2.58 | 456 / -7.93 / -3.69 |
| rule=model,tau=12 | sl15_tp20 | 925 / -5.38 / -6.78 | 486 / -5.09 / -4.52 | 861 / -5.73 / -7.10 | 456 / -6.11 / -5.46 |
| rule=model,tau=4 | hold | 924 / -5.14 / -4.48 | 487 / -2.82 / -1.81 | 862 / -1.42 / -1.26 | 455 / -1.08 / -0.71 |
| rule=model,tau=4 | sl15_tp20 | 924 / -4.50 / -5.62 | 487 / -3.48 / -3.20 | 862 / -1.40 / -1.76 | 455 / -1.91 / -1.64 |
| rule=model,tau=8 | hold | 929 / -1.21 / -0.89 | 489 / -1.51 / -0.80 | 857 / -2.51 / -1.85 | 453 / -1.91 / -0.99 |
| rule=model,tau=8 | sl15_tp20 | 929 / -2.82 / -3.44 | 489 / -3.60 / -3.16 | 857 / -4.10 / -5.12 | 453 / -2.78 / -2.49 |
| rule=momentum,tau=12 | hold | 937 / -0.84 / -0.56 | 493 / -1.04 / -0.51 | 862 / -1.97 / -1.29 | 459 / 2.17 / 1.01 |
| rule=momentum,tau=12 | sl15_tp20 | 937 / -4.87 / -6.18 | 493 / -4.84 / -4.36 | 862 / -4.52 / -5.49 | 459 / -4.48 / -3.96 |
| rule=momentum,tau=4 | hold | 936 / -2.22 / -1.93 | 494 / -0.45 / -0.29 | 863 / -1.24 / -1.11 | 458 / -0.82 / -0.54 |
| rule=momentum,tau=4 | sl15_tp20 | 936 / -2.57 / -3.16 | 494 / -1.05 / -0.98 | 863 / -2.44 / -3.11 | 458 / -0.86 / -0.76 |
| rule=momentum,tau=8 | hold | 941 / -2.76 / -2.05 | 495 / -1.73 / -0.92 | 858 / -4.52 / -3.34 | 457 / -4.22 / -2.21 |
| rule=momentum,tau=8 | sl15_tp20 | 941 / -3.45 / -4.24 | 495 / -4.13 / -3.73 | 858 / -3.38 / -4.20 | 457 / -4.56 / -4.03 |

Named rules on full-tape windows only, hold to settlement (`minute_tape` = the minute decision on these windows; `print` = the same decision filled at the tape; `print_scan` = decided each second on the tape's print-proxy quotes and filled at the tape):

| config | resolution | all n / net c / t | holdout n / net c / t |
|---|---|---|---|
| early_momentum_vwap|hold | minute_tape | 285 / -2.24 / -0.83 | 95 / -3.77 / -0.77 |
| early_momentum_vwap|hold | print | 285 / -2.22 / -0.82 | 95 / -3.72 / -0.76 |
| first_touch_fade_underdog:L=10,buy=fade|hold | minute_tape | 349 / -0.34 / -0.28 | 119 / 0.27 / 0.14 |
| first_touch_fade_underdog:L=10,buy=fade|hold | print | 345 / -0.35 / -0.28 | 117 / 0.28 / 0.14 |
| first_touch_fade_underdog:L=10,buy=fade|hold | print_scan | 364 / -1.80 / -1.17 | 122 / -2.86 / -1.02 |
| first_touch_fade_underdog:L=10,buy=touched|hold | minute_tape | 349 / -2.62 / -2.11 | 119 / -3.22 / -1.62 |
| first_touch_fade_underdog:L=10,buy=touched|hold | print | 349 / -2.60 / -2.10 | 119 / -3.21 / -1.62 |
| first_touch_fade_underdog:L=10,buy=touched|hold | print_scan | 364 / -1.46 / -0.95 | 122 / -0.41 / -0.15 |
| first_touch_fade_underdog:L=15,buy=fade|hold | minute_tape | 373 / -2.19 / -1.37 | 125 / -4.66 / -1.57 |
| first_touch_fade_underdog:L=15,buy=fade|hold | print | 371 / -2.21 / -1.37 | 124 / -4.69 / -1.56 |
| first_touch_fade_underdog:L=15,buy=fade|hold | print_scan | 383 / -4.42 / -2.34 | 125 / -6.71 / -1.93 |
| first_touch_fade_underdog:L=15,buy=touched|hold | minute_tape | 373 / -1.73 / -1.08 | 125 / 0.84 / 0.28 |
| first_touch_fade_underdog:L=15,buy=touched|hold | print | 373 / -1.69 / -1.06 | 125 / 0.85 / 0.29 |
| first_touch_fade_underdog:L=15,buy=touched|hold | print_scan | 383 / -0.22 / -0.12 | 125 / 2.07 / 0.59 |
| first_touch_fade_underdog:L=20,buy=fade|hold | minute_tape | 399 / -4.00 / -2.18 | 130 / -5.32 / -1.64 |
| first_touch_fade_underdog:L=20,buy=fade|hold | print | 397 / -4.02 / -2.18 | 129 / -5.36 / -1.63 |
| first_touch_fade_underdog:L=20,buy=fade|hold | print_scan | 380 / -5.27 / -2.49 | 121 / -6.04 / -1.59 |
| first_touch_fade_underdog:L=20,buy=touched|hold | minute_tape | 399 / -0.57 / -0.31 | 130 / 0.90 / 0.28 |
| first_touch_fade_underdog:L=20,buy=touched|hold | print | 399 / -0.54 / -0.29 | 130 / 0.92 / 0.28 |
| first_touch_fade_underdog:L=20,buy=touched|hold | print_scan | 380 / 0.19 / 0.09 | 121 / 0.93 / 0.24 |
| first_touch_fade_underdog:L=25,buy=fade|hold | minute_tape | 407 / -4.42 / -2.20 | 128 / -5.65 / -1.54 |
| first_touch_fade_underdog:L=25,buy=fade|hold | print | 407 / -4.39 / -2.19 | 128 / -5.62 / -1.53 |
| first_touch_fade_underdog:L=25,buy=fade|hold | print_scan | 353 / -2.66 / -1.18 | 109 / -0.31 / -0.08 |
| first_touch_fade_underdog:L=25,buy=touched|hold | minute_tape | 407 / -0.64 / -0.32 | 128 / 0.63 / 0.17 |
| first_touch_fade_underdog:L=25,buy=touched|hold | print | 407 / -0.61 / -0.31 | 128 / 0.68 / 0.19 |
| first_touch_fade_underdog:L=25,buy=touched|hold | print_scan | 355 / -2.38 / -1.05 | 109 / -5.17 / -1.32 |
| first_touch_fade_underdog:L=30,buy=fade|hold | minute_tape | 376 / -4.06 / -1.83 | 114 / -2.36 / -0.60 |
| first_touch_fade_underdog:L=30,buy=fade|hold | print | 376 / -4.01 / -1.81 | 114 / -2.33 / -0.59 |
| first_touch_fade_underdog:L=30,buy=fade|hold | print_scan | 291 / -3.04 / -1.15 | 90 / 5.25 / 1.24 |
| first_touch_fade_underdog:L=30,buy=touched|hold | minute_tape | 376 / -1.42 / -0.64 | 114 / -3.05 / -0.77 |
| first_touch_fade_underdog:L=30,buy=touched|hold | print | 376 / -1.39 / -0.63 | 114 / -3.00 / -0.76 |
| first_touch_fade_underdog:L=30,buy=touched|hold | print_scan | 292 / -2.43 / -0.92 | 90 / -10.96 / -2.59 |
| h5_lag_momentum|hold | minute_tape | 38 / -6.40 / -1.05 | 13 / -4.78 / -0.47 |
| h5_lag_momentum|hold | print | 38 / -6.40 / -1.05 | 13 / -4.78 / -0.47 |
| h6_favourite_75_90|hold | minute_tape | 272 / -4.81 / -1.98 | 80 / -7.74 / -1.65 |
| h6_favourite_75_90|hold | print | 272 / -4.78 / -1.97 | 80 / -7.69 / -1.63 |
| level_reversal_pivot:variant=v1|hold | minute_tape | 129 / 1.95 / 0.48 | 47 / 11.79 / 1.80 |
| level_reversal_pivot:variant=v1|hold | print | 129 / 1.99 / 0.49 | 47 / 11.83 / 1.81 |
| level_reversal_pivot:variant=v2|hold | minute_tape | 15 / -5.74 / -0.67 | 9 / -2.01 / -0.17 |
| level_reversal_pivot:variant=v2|hold | print | 14 / -6.22 / -0.67 | 8 / -2.38 / -0.17 |
| level_reversal_pivot:variant=v2|lstop25 | minute_tape | 15 / -5.37 / -0.67 | 9 / -2.53 / -0.22 |
| level_reversal_pivot:variant=v2|lstop25 | print | 14 / -5.12 / -0.62 | 8 / -2.82 / -0.21 |
| level_reversal_pivot:variant=v3|hold | minute_tape | 10 / -1.08 / -0.08 | 4 / 7.17 / 0.27 |
| level_reversal_pivot:variant=v3|hold | print | 10 / -0.98 / -0.07 | 4 / 7.42 / 0.28 |
| level_reversal_pivot:variant=v3|lstop25 | minute_tape | 10 / -1.08 / -0.08 | 4 / 7.17 / 0.27 |
| level_reversal_pivot:variant=v3|lstop25 | print | 10 / -0.98 / -0.07 | 4 / 7.42 / 0.28 |
| pnw_cushion_tp20:variant=v1|mtp20_bstop20 | minute_tape | 105 / 1.98 / 0.89 | 29 / 6.65 / 1.80 |
| pnw_cushion_tp20:variant=v1|mtp20_bstop20 | print | 105 / -1.48 / -0.75 | 29 / 0.65 / 0.17 |
| pnw_cushion_tp20:variant=v2|mtp20_bstop20 | minute_tape | 14 / 4.46 / 0.77 | 4 / 4.40 / 0.34 |
| pnw_cushion_tp20:variant=v2|mtp20_bstop20 | print | 14 / -3.58 / -0.60 | 4 / -4.79 / -0.36 |
| pnw_cushion_tp20:variant=v3|mtp20_bstop20 | minute_tape | 14 / 4.46 / 0.77 | 4 / 4.40 / 0.34 |
| pnw_cushion_tp20:variant=v3|mtp20_bstop20 | print | 14 / -3.58 / -0.60 | 4 / -4.79 / -0.36 |
| pnw_cushion_tp20:variant=v4|hold | minute_tape | 105 / 3.02 / 0.79 | 29 / 3.86 / 0.54 |
| pnw_cushion_tp20:variant=v4|hold | print | 105 / 3.09 / 0.81 | 29 / 4.00 / 0.55 |
| pnw_underdog_rebound|hold | minute_tape | 26 / -12.07 / -1.32 | 12 / 7.22 / 0.49 |
| pnw_underdog_rebound|hold | print | 6 / -9.69 / -0.46 | 2 / 6.29 / n/a |
| pnw_underdog_rebound|mtp20 | minute_tape | 26 / -13.04 / -2.14 | 12 / -7.52 / -0.85 |
| pnw_underdog_rebound|mtp20 | print | 6 / -3.02 / -0.23 | 2 / -11.71 / n/a |
| preceptress_strong_momentum:edge=12,ref=own|hold | minute_tape | 8 / -30.39 / -1.55 | 3 / -47.46 / -1.35 |
| preceptress_strong_momentum:edge=12,ref=own|hold | print | 8 / -30.39 / -1.55 | 3 / -47.46 / -1.35 |
| preceptress_strong_momentum:edge=12,ref=strike|hold | minute_tape | 8 / -30.39 / -1.55 | 3 / -47.46 / -1.35 |
| preceptress_strong_momentum:edge=12,ref=strike|hold | print | 8 / -30.39 / -1.55 | 3 / -47.46 / -1.35 |
| preceptress_strong_momentum:edge=3,ref=own|hold | minute_tape | 24 / -10.04 / -1.03 | 5 / -41.11 / -1.54 |
| preceptress_strong_momentum:edge=3,ref=own|hold | print | 24 / -10.04 / -1.03 | 5 / -41.11 / -1.54 |
| preceptress_strong_momentum:edge=3,ref=strike|hold | minute_tape | 19 / -12.38 / -1.11 | 4 / -30.15 / -0.96 |
| preceptress_strong_momentum:edge=3,ref=strike|hold | print | 19 / -12.38 / -1.11 | 4 / -30.15 / -0.96 |


### Family 3: exits on the same entries (minute resolution, 1c slippage)

**forced:model:8**

| exit | train n | win % | BE % | net c | t | holdout n | win % | BE % | net c | t |
|---|---|---|---|---|---|---|---|---|---|---|
| hold | 1883 | 50.2 | 53.0 | -1.89 | -2.03 | 942 | 47.0 | 49.5 | -1.70 | -1.26 |
| sl5 | 1883 | 31.0 | 38.2 | -3.28 | -5.38 | 942 | 27.7 | 34.9 | -3.39 | -3.92 |
| sl10 | 1883 | 36.7 | 42.4 | -2.84 | -4.12 | 942 | 32.9 | 38.5 | -2.94 | -2.98 |
| sl15 | 1883 | 40.8 | 45.5 | -2.57 | -3.39 | 942 | 37.6 | 41.4 | -2.14 | -1.98 |
| sl20 | 1883 | 42.6 | 47.9 | -3.07 | -3.85 | 942 | 40.6 | 43.2 | -1.60 | -1.39 |
| sl30 | 1883 | 46.5 | 50.3 | -2.42 | -2.81 | 942 | 45.1 | 46.2 | -0.68 | -0.54 |
| tp5 | 1883 | 68.1 | 77.4 | -3.70 | -6.73 | 942 | 66.3 | 77.0 | -4.19 | -5.30 |
| tp10 | 1883 | 65.0 | 72.8 | -3.61 | -5.76 | 942 | 61.8 | 70.2 | -3.86 | -4.26 |
| tp15 | 1883 | 60.8 | 67.5 | -3.38 | -4.92 | 942 | 58.9 | 64.6 | -2.88 | -2.89 |
| sl5_tp5 | 1883 | 48.3 | 68.5 | -4.53 | -14.51 | 942 | 47.0 | 64.8 | -4.08 | -9.08 |
| sl5_tp10 | 1883 | 43.8 | 60.3 | -4.35 | -12.24 | 942 | 42.1 | 57.4 | -4.07 | -8.12 |
| sl5_tp15 | 1883 | 40.0 | 53.4 | -3.99 | -10.07 | 942 | 38.3 | 50.0 | -3.59 | -6.36 |
| sl10_tp5 | 1883 | 54.6 | 72.1 | -4.58 | -12.91 | 942 | 53.6 | 69.3 | -4.08 | -8.11 |
| sl10_tp10 | 1883 | 50.6 | 64.8 | -4.39 | -10.71 | 942 | 48.0 | 61.6 | -4.15 | -7.24 |
| sl10_tp15 | 1883 | 46.7 | 58.2 | -3.97 | -8.69 | 942 | 44.3 | 53.8 | -3.36 | -5.10 |
| sl15_tp5 | 1883 | 59.1 | 74.1 | -4.49 | -11.21 | 942 | 57.6 | 72.3 | -4.29 | -7.63 |
| sl15_tp10 | 1883 | 55.0 | 67.4 | -4.32 | -9.39 | 942 | 52.1 | 64.4 | -4.19 | -6.51 |
| sl15_tp15 | 1883 | 51.1 | 61.1 | -3.85 | -7.47 | 942 | 49.0 | 56.9 | -3.07 | -4.18 |
| sl20_tp5 | 1883 | 61.1 | 75.8 | -4.76 | -11.03 | 942 | 60.7 | 73.0 | -3.91 | -6.42 |
| sl20_tp10 | 1883 | 57.3 | 69.6 | -4.62 | -9.39 | 942 | 55.3 | 65.9 | -3.91 | -5.62 |
| sl20_tp15 | 1883 | 53.4 | 63.4 | -4.13 | -7.54 | 942 | 52.2 | 58.9 | -2.78 | -3.51 |
| sl30_tp5 | 1883 | 65.3 | 76.6 | -4.04 | -8.50 | 942 | 64.5 | 74.7 | -3.60 | -5.26 |
| sl30_tp10 | 1883 | 61.7 | 71.1 | -3.94 | -7.22 | 942 | 59.7 | 67.5 | -3.21 | -4.07 |
| sl30_tp15 | 1883 | 57.4 | 65.4 | -3.66 | -6.02 | 942 | 56.8 | 61.5 | -2.16 | -2.44 |
| trail10 | 1883 | 36.6 | 44.2 | -2.93 | -5.29 | 942 | 34.0 | 42.9 | -3.49 | -4.43 |
| time1m | 1883 | 47.9 | 54.2 | -3.50 | -4.36 | 942 | 44.7 | 50.3 | -3.30 | -2.77 |
| tp20 | 1883 | 58.0 | 63.7 | -3.09 | -4.19 | 942 | 55.3 | 61.1 | -3.15 | -2.94 |
| sl10_tp20 | 1883 | 43.9 | 53.5 | -3.64 | -7.29 | 942 | 40.9 | 50.0 | -3.53 | -4.90 |
| sl15_tp20 | 1883 | 48.4 | 56.6 | -3.47 | -6.20 | 942 | 45.8 | 53.3 | -3.20 | -4.01 |
| sl20_tp20 | 1883 | 50.5 | 59.1 | -3.89 | -6.53 | 942 | 48.8 | 54.9 | -2.77 | -3.22 |

**forced:favourite:8**

| exit | train n | win % | BE % | net c | t | holdout n | win % | BE % | net c | t |
|---|---|---|---|---|---|---|---|---|---|---|
| hold | 1896 | 75.9 | 77.8 | -1.63 | -1.75 | 952 | 73.9 | 78.1 | -3.72 | -2.77 |
| sl5 | 1896 | 47.3 | 54.7 | -2.94 | -5.43 | 952 | 44.3 | 54.9 | -4.00 | -5.49 |
| sl10 | 1896 | 54.8 | 60.6 | -2.64 | -4.36 | 952 | 52.4 | 60.4 | -3.48 | -4.25 |
| sl15 | 1896 | 60.5 | 64.9 | -2.27 | -3.42 | 952 | 57.8 | 65.1 | -3.74 | -4.04 |
| sl20 | 1896 | 64.1 | 67.8 | -2.16 | -3.04 | 952 | 61.4 | 67.9 | -3.67 | -3.66 |
| sl30 | 1896 | 69.1 | 71.8 | -1.83 | -2.35 | 952 | 68.2 | 72.3 | -2.83 | -2.52 |
| tp5 | 1896 | 85.2 | 90.4 | -3.73 | -5.87 | 952 | 84.0 | 90.8 | -4.95 | -5.31 |
| tp10 | 1896 | 83.6 | 87.5 | -3.14 | -4.35 | 952 | 81.5 | 87.4 | -4.85 | -4.55 |
| tp15 | 1896 | 81.6 | 84.3 | -2.30 | -2.94 | 952 | 79.2 | 84.3 | -4.34 | -3.79 |
| sl5_tp5 | 1896 | 60.0 | 77.1 | -4.16 | -13.01 | 952 | 57.4 | 77.9 | -5.14 | -11.14 |
| sl5_tp10 | 1896 | 55.9 | 68.9 | -3.70 | -10.04 | 952 | 53.3 | 69.4 | -4.57 | -8.75 |
| sl5_tp15 | 1896 | 53.2 | 63.2 | -3.18 | -7.69 | 952 | 49.9 | 63.1 | -4.11 | -7.21 |
| sl10_tp5 | 1896 | 66.9 | 80.9 | -4.12 | -11.41 | 952 | 64.9 | 81.3 | -4.83 | -9.60 |
| sl10_tp10 | 1896 | 63.4 | 73.9 | -3.59 | -8.52 | 952 | 61.3 | 73.7 | -4.15 | -7.12 |
| sl10_tp15 | 1896 | 60.8 | 68.8 | -3.04 | -6.46 | 952 | 58.0 | 67.7 | -3.55 | -5.55 |
| sl15_tp5 | 1896 | 72.0 | 83.9 | -4.16 | -10.28 | 952 | 69.5 | 84.1 | -5.22 | -8.88 |
| sl15_tp10 | 1896 | 68.7 | 77.6 | -3.63 | -7.70 | 952 | 66.2 | 77.6 | -4.69 | -6.87 |
| sl15_tp15 | 1896 | 66.3 | 73.0 | -2.97 | -5.65 | 952 | 63.2 | 72.5 | -4.09 | -5.47 |
| sl20_tp5 | 1896 | 75.2 | 85.6 | -4.21 | -9.51 | 952 | 72.7 | 85.4 | -5.14 | -8.03 |
| sl20_tp10 | 1896 | 72.4 | 80.0 | -3.56 | -6.97 | 952 | 69.4 | 79.9 | -4.83 | -6.49 |
| sl20_tp15 | 1896 | 70.0 | 75.7 | -2.85 | -5.04 | 952 | 66.7 | 75.4 | -4.34 | -5.28 |
| sl30_tp5 | 1896 | 79.4 | 87.7 | -4.07 | -8.28 | 952 | 78.8 | 87.9 | -4.61 | -6.37 |
| sl30_tp10 | 1896 | 77.2 | 83.2 | -3.42 | -5.97 | 952 | 75.8 | 83.2 | -4.28 | -5.06 |
| sl30_tp15 | 1896 | 74.9 | 79.2 | -2.55 | -4.05 | 952 | 73.4 | 79.3 | -3.61 | -3.90 |
| trail10 | 1896 | 51.4 | 57.9 | -2.46 | -4.74 | 952 | 49.3 | 58.6 | -3.42 | -4.81 |
| time1m | 1896 | 71.5 | 74.8 | -2.25 | -2.79 | 952 | 70.2 | 75.6 | -3.82 | -3.20 |
| tp20 | 1896 | 79.9 | 82.0 | -1.81 | -2.19 | 952 | 77.3 | 82.1 | -4.18 | -3.43 |
| sl10_tp20 | 1896 | 58.9 | 65.3 | -2.59 | -5.10 | 952 | 55.5 | 65.0 | -3.73 | -5.30 |
| sl15_tp20 | 1896 | 64.5 | 69.7 | -2.46 | -4.34 | 952 | 60.9 | 69.7 | -4.10 | -5.09 |
| sl20_tp20 | 1896 | 68.0 | 72.6 | -2.39 | -3.93 | 952 | 64.5 | 72.5 | -4.19 | -4.76 |

**forced:coin:8**

| exit | train n | win % | BE % | net c | t | holdout n | win % | BE % | net c | t |
|---|---|---|---|---|---|---|---|---|---|---|
| hold | 1896 | 49.3 | 53.9 | -3.07 | -3.30 | 952 | 47.3 | 54.7 | -5.01 | -3.73 |
| sl5 | 1896 | 29.1 | 38.2 | -4.05 | -6.99 | 952 | 28.2 | 38.6 | -4.82 | -5.67 |
| sl10 | 1896 | 34.5 | 42.0 | -3.67 | -5.52 | 952 | 32.7 | 42.8 | -5.10 | -5.37 |
| sl15 | 1896 | 38.9 | 45.2 | -3.45 | -4.69 | 952 | 36.3 | 45.5 | -5.05 | -4.80 |
| sl20 | 1896 | 41.5 | 47.6 | -3.54 | -4.53 | 952 | 39.5 | 47.6 | -4.73 | -4.23 |
| sl30 | 1896 | 45.1 | 50.7 | -3.48 | -4.10 | 952 | 43.8 | 50.8 | -4.37 | -3.60 |
| tp5 | 1896 | 66.0 | 77.5 | -4.64 | -7.97 | 952 | 65.7 | 80.8 | -6.05 | -7.52 |
| tp10 | 1896 | 63.2 | 71.8 | -4.03 | -6.19 | 952 | 61.2 | 74.5 | -6.23 | -6.71 |
| tp15 | 1896 | 59.4 | 67.0 | -3.90 | -5.50 | 952 | 57.7 | 68.8 | -5.71 | -5.63 |
| sl5_tp5 | 1896 | 45.5 | 67.8 | -4.99 | -15.98 | 952 | 45.9 | 70.1 | -5.49 | -12.29 |
| sl5_tp10 | 1896 | 41.0 | 58.7 | -4.73 | -13.28 | 952 | 41.4 | 61.3 | -5.33 | -10.48 |
| sl5_tp15 | 1896 | 37.8 | 51.9 | -4.33 | -10.92 | 952 | 37.8 | 53.4 | -4.81 | -8.41 |
| sl10_tp5 | 1896 | 51.6 | 71.1 | -5.06 | -14.14 | 952 | 51.8 | 73.5 | -5.58 | -11.35 |
| sl10_tp10 | 1896 | 47.5 | 62.2 | -4.55 | -11.01 | 952 | 47.0 | 65.3 | -5.56 | -9.76 |
| sl10_tp15 | 1896 | 43.9 | 56.2 | -4.27 | -9.33 | 952 | 43.4 | 57.2 | -4.80 | -7.45 |
| sl15_tp5 | 1896 | 56.2 | 73.6 | -5.08 | -12.64 | 952 | 55.9 | 76.3 | -5.93 | -10.62 |
| sl15_tp10 | 1896 | 52.4 | 65.5 | -4.54 | -9.80 | 952 | 51.1 | 67.8 | -5.80 | -8.83 |
| sl15_tp15 | 1896 | 48.7 | 59.7 | -4.24 | -8.27 | 952 | 47.5 | 60.1 | -4.93 | -6.71 |
| sl20_tp5 | 1896 | 58.8 | 75.0 | -5.19 | -11.82 | 952 | 58.9 | 77.2 | -5.67 | -9.55 |
| sl20_tp10 | 1896 | 55.2 | 67.6 | -4.67 | -9.29 | 952 | 54.2 | 69.5 | -5.67 | -8.07 |
| sl20_tp15 | 1896 | 51.6 | 61.9 | -4.32 | -7.81 | 952 | 50.6 | 62.4 | -4.90 | -6.23 |
| sl30_tp5 | 1896 | 62.4 | 76.6 | -5.12 | -10.34 | 952 | 62.8 | 78.8 | -5.53 | -8.30 |
| sl30_tp10 | 1896 | 59.1 | 70.0 | -4.57 | -8.12 | 952 | 58.4 | 71.6 | -5.45 | -6.95 |
| sl30_tp15 | 1896 | 55.3 | 64.7 | -4.35 | -7.05 | 952 | 54.8 | 65.2 | -4.79 | -5.48 |
| trail10 | 1896 | 33.9 | 43.0 | -3.60 | -6.56 | 952 | 34.2 | 44.5 | -4.10 | -5.14 |
| time1m | 1896 | 47.3 | 54.0 | -3.78 | -4.71 | 952 | 45.9 | 54.9 | -5.25 | -4.42 |
| tp20 | 1896 | 56.7 | 63.3 | -3.65 | -4.83 | 952 | 54.8 | 65.0 | -5.70 | -5.18 |
| sl10_tp20 | 1896 | 41.1 | 51.8 | -4.07 | -8.22 | 952 | 40.0 | 53.3 | -5.14 | -7.19 |
| sl15_tp20 | 1896 | 45.9 | 55.4 | -4.00 | -7.20 | 952 | 44.2 | 56.3 | -5.19 | -6.45 |
| sl20_tp20 | 1896 | 48.6 | 57.7 | -4.12 | -6.89 | 952 | 47.5 | 58.3 | -4.95 | -5.73 |

**forced:underdog:12**

| exit | train n | win % | BE % | net c | t | holdout n | win % | BE % | net c | t |
|---|---|---|---|---|---|---|---|---|---|---|
| hold | 1896 | 33.3 | 36.7 | -3.19 | -3.07 | 952 | 34.5 | 36.6 | -2.02 | -1.36 |
| sl5 | 1896 | 14.1 | 20.9 | -5.03 | -8.14 | 952 | 14.2 | 20.7 | -4.84 | -5.53 |
| sl10 | 1896 | 18.8 | 25.0 | -4.88 | -6.65 | 952 | 19.4 | 25.0 | -4.47 | -4.26 |
| sl15 | 1896 | 22.6 | 28.5 | -4.91 | -5.93 | 952 | 24.4 | 28.6 | -3.57 | -2.95 |
| sl20 | 1896 | 25.6 | 31.2 | -4.94 | -5.54 | 952 | 27.8 | 31.6 | -3.28 | -2.51 |
| sl30 | 1896 | 30.5 | 34.9 | -3.96 | -4.00 | 952 | 31.7 | 34.7 | -2.74 | -1.93 |
| tp5 | 1896 | 59.7 | 74.8 | -5.97 | -11.32 | 952 | 62.9 | 75.4 | -4.83 | -6.66 |
| tp10 | 1896 | 57.1 | 68.9 | -5.67 | -9.40 | 952 | 59.2 | 69.3 | -4.75 | -5.69 |
| tp15 | 1896 | 52.1 | 61.8 | -5.28 | -7.85 | 952 | 54.2 | 62.8 | -4.53 | -4.85 |
| sl5_tp5 | 1896 | 36.2 | 63.2 | -6.02 | -21.32 | 952 | 39.7 | 65.1 | -5.41 | -13.98 |
| sl5_tp10 | 1896 | 32.3 | 54.4 | -5.97 | -18.75 | 952 | 34.0 | 54.1 | -5.32 | -11.95 |
| sl5_tp15 | 1896 | 27.5 | 45.6 | -5.86 | -16.38 | 952 | 29.6 | 46.3 | -5.19 | -10.52 |
| sl10_tp5 | 1896 | 44.7 | 68.4 | -6.13 | -18.50 | 952 | 47.9 | 69.3 | -5.36 | -11.76 |
| sl10_tp10 | 1896 | 40.0 | 59.9 | -6.19 | -16.37 | 952 | 42.5 | 59.6 | -5.23 | -9.91 |
| sl10_tp15 | 1896 | 35.1 | 51.0 | -5.88 | -13.74 | 952 | 38.1 | 51.7 | -4.86 | -8.10 |
| sl15_tp5 | 1896 | 50.1 | 71.2 | -6.22 | -16.36 | 952 | 53.6 | 71.9 | -5.37 | -10.04 |
| sl15_tp10 | 1896 | 46.0 | 63.2 | -6.12 | -13.95 | 952 | 48.7 | 63.2 | -5.17 | -8.27 |
| sl15_tp15 | 1896 | 40.5 | 54.9 | -5.96 | -12.10 | 952 | 43.7 | 55.7 | -4.94 | -6.99 |
| sl20_tp5 | 1896 | 53.3 | 73.1 | -6.48 | -15.37 | 952 | 57.4 | 73.4 | -5.24 | -8.76 |
| sl20_tp10 | 1896 | 49.7 | 65.5 | -6.22 | -12.76 | 952 | 52.8 | 65.9 | -5.17 | -7.51 |
| sl20_tp15 | 1896 | 44.1 | 57.8 | -6.17 | -11.32 | 952 | 47.5 | 58.7 | -5.06 | -6.51 |
| sl30_tp5 | 1896 | 58.0 | 74.3 | -6.06 | -12.50 | 952 | 60.9 | 74.8 | -5.05 | -7.55 |
| sl30_tp10 | 1896 | 55.1 | 68.0 | -5.78 | -10.36 | 952 | 57.1 | 68.2 | -4.90 | -6.34 |
| sl30_tp15 | 1896 | 49.7 | 60.7 | -5.60 | -8.96 | 952 | 52.0 | 61.5 | -4.73 | -5.48 |
| trail10 | 1896 | 22.7 | 33.6 | -5.13 | -9.47 | 952 | 22.8 | 34.1 | -5.02 | -6.86 |
| time1m | 1896 | 34.3 | 39.1 | -3.79 | -4.07 | 952 | 34.3 | 38.6 | -3.54 | -2.61 |
| tp20 | 1896 | 48.8 | 56.6 | -4.59 | -6.30 | 952 | 48.6 | 56.1 | -4.47 | -4.31 |
| sl10_tp20 | 1896 | 31.5 | 44.8 | -5.61 | -11.93 | 952 | 32.4 | 44.7 | -5.20 | -7.75 |
| sl15_tp20 | 1896 | 37.0 | 48.8 | -5.51 | -10.18 | 952 | 38.1 | 49.1 | -5.18 | -6.59 |
| sl20_tp20 | 1896 | 40.7 | 51.9 | -5.66 | -9.52 | 952 | 41.6 | 52.1 | -5.39 | -6.24 |

**taker:edge=5,blend=0.5,floor=1**

| exit | train n | win % | BE % | net c | t | holdout n | win % | BE % | net c | t |
|---|---|---|---|---|---|---|---|---|---|---|
| hold | 204 | 52.9 | 60.5 | -7.07 | -2.06 | 93 | 49.5 | 60.4 | -10.15 | -2.01 |
| sl5 | 204 | 28.4 | 38.5 | -6.12 | -2.81 | 93 | 29.0 | 40.0 | -6.84 | -2.09 |
| sl10 | 204 | 34.3 | 44.6 | -6.90 | -2.82 | 93 | 33.3 | 42.2 | -5.91 | -1.67 |
| sl15 | 204 | 38.7 | 49.5 | -7.84 | -2.95 | 93 | 39.8 | 47.2 | -5.19 | -1.36 |
| sl20 | 204 | 42.2 | 52.2 | -7.92 | -2.76 | 93 | 39.8 | 51.2 | -8.71 | -2.14 |
| sl30 | 204 | 45.1 | 54.9 | -8.23 | -2.71 | 93 | 43.0 | 55.0 | -9.89 | -2.24 |
| tp5 | 204 | 68.6 | 81.6 | -8.23 | -3.53 | 93 | 64.5 | 77.5 | -7.36 | -2.22 |
| tp10 | 204 | 65.2 | 77.6 | -9.39 | -3.53 | 93 | 65.6 | 77.2 | -8.47 | -2.22 |
| tp15 | 204 | 61.3 | 73.8 | -9.65 | -3.48 | 93 | 60.2 | 72.0 | -9.37 | -2.20 |
| sl5_tp5 | 204 | 48.0 | 67.1 | -5.56 | -4.37 | 93 | 48.4 | 64.3 | -5.01 | -2.56 |
| sl5_tp10 | 204 | 42.2 | 60.5 | -6.56 | -4.47 | 93 | 41.9 | 61.1 | -7.25 | -3.29 |
| sl5_tp15 | 204 | 37.7 | 55.4 | -6.98 | -4.47 | 93 | 36.6 | 53.1 | -7.60 | -2.88 |
| sl10_tp5 | 204 | 53.4 | 72.5 | -6.83 | -4.54 | 93 | 50.5 | 66.4 | -5.42 | -2.61 |
| sl10_tp10 | 204 | 48.0 | 66.4 | -7.86 | -4.60 | 93 | 45.2 | 63.6 | -7.56 | -3.23 |
| sl10_tp15 | 204 | 43.1 | 61.5 | -8.52 | -4.71 | 93 | 40.9 | 57.1 | -7.99 | -2.87 |
| sl15_tp5 | 204 | 57.4 | 76.2 | -7.73 | -4.69 | 93 | 53.8 | 69.5 | -5.85 | -2.59 |
| sl15_tp10 | 204 | 52.0 | 70.3 | -9.04 | -4.77 | 93 | 52.7 | 67.4 | -6.75 | -2.64 |
| sl15_tp15 | 204 | 47.1 | 65.3 | -9.60 | -4.80 | 93 | 47.3 | 61.8 | -7.82 | -2.59 |
| sl20_tp5 | 204 | 60.3 | 78.9 | -8.64 | -4.77 | 93 | 55.9 | 72.8 | -6.94 | -2.83 |
| sl20_tp10 | 204 | 55.9 | 72.6 | -9.30 | -4.47 | 93 | 54.8 | 70.8 | -8.07 | -2.93 |
| sl20_tp15 | 204 | 51.0 | 68.2 | -10.11 | -4.61 | 93 | 49.5 | 64.8 | -8.95 | -2.80 |
| sl30_tp5 | 204 | 62.3 | 80.0 | -9.14 | -4.68 | 93 | 60.2 | 73.4 | -6.37 | -2.26 |
| sl30_tp10 | 204 | 58.3 | 74.5 | -9.93 | -4.42 | 93 | 59.1 | 72.9 | -8.21 | -2.58 |
| sl30_tp15 | 204 | 53.9 | 70.5 | -10.70 | -4.53 | 93 | 53.8 | 67.5 | -9.09 | -2.54 |
| trail10 | 204 | 30.9 | 45.2 | -8.03 | -3.76 | 93 | 31.2 | 43.1 | -7.19 | -2.21 |
| time1m | 204 | 49.0 | 58.0 | -6.63 | -2.33 | 93 | 47.3 | 57.5 | -7.99 | -1.81 |
| tp20 | 204 | 58.8 | 69.5 | -8.78 | -2.97 | 93 | 58.1 | 69.2 | -9.10 | -2.08 |
| sl10_tp20 | 204 | 40.7 | 56.0 | -7.97 | -4.04 | 93 | 38.7 | 53.2 | -7.66 | -2.64 |
| sl15_tp20 | 204 | 45.1 | 60.1 | -8.69 | -4.00 | 93 | 45.2 | 57.9 | -7.28 | -2.31 |
| sl20_tp20 | 204 | 48.5 | 62.6 | -9.10 | -3.82 | 93 | 47.3 | 61.2 | -8.55 | -2.55 |

**taker:edge=0,blend=0,floor=0**

| exit | train n | win % | BE % | net c | t | holdout n | win % | BE % | net c | t |
|---|---|---|---|---|---|---|---|---|---|---|
| hold | 1883 | 48.1 | 51.9 | -3.26 | -3.08 | 942 | 47.9 | 51.2 | -2.86 | -1.86 |
| sl5 | 1883 | 21.9 | 30.5 | -4.83 | -8.06 | 942 | 20.5 | 28.8 | -4.96 | -5.73 |
| sl10 | 1883 | 27.5 | 35.7 | -5.06 | -7.33 | 942 | 26.1 | 34.1 | -5.11 | -5.11 |
| sl15 | 1883 | 32.8 | 39.5 | -4.50 | -5.71 | 942 | 32.5 | 37.9 | -3.75 | -3.31 |
| sl20 | 1883 | 36.5 | 42.5 | -4.35 | -5.08 | 942 | 37.2 | 41.8 | -3.41 | -2.73 |
| sl30 | 1883 | 42.0 | 47.1 | -4.03 | -4.21 | 942 | 43.0 | 46.4 | -2.71 | -1.95 |
| tp5 | 1883 | 67.4 | 81.8 | -6.54 | -11.13 | 942 | 68.4 | 82.3 | -6.41 | -7.65 |
| tp10 | 1883 | 66.5 | 77.8 | -6.32 | -9.46 | 942 | 67.3 | 77.3 | -5.71 | -5.86 |
| tp15 | 1883 | 62.3 | 71.2 | -5.49 | -7.35 | 942 | 62.2 | 71.5 | -5.79 | -5.36 |
| sl5_tp5 | 1883 | 42.8 | 69.4 | -6.00 | -20.12 | 942 | 41.9 | 69.1 | -6.06 | -14.62 |
| sl5_tp10 | 1883 | 38.5 | 59.6 | -5.76 | -17.15 | 942 | 36.9 | 59.3 | -6.09 | -12.81 |
| sl5_tp15 | 1883 | 33.8 | 51.2 | -5.53 | -14.72 | 942 | 32.3 | 51.4 | -6.08 | -11.45 |
| sl10_tp5 | 1883 | 50.2 | 73.3 | -6.06 | -17.79 | 942 | 48.5 | 73.3 | -6.42 | -13.51 |
| sl10_tp10 | 1883 | 46.3 | 65.1 | -5.93 | -15.27 | 942 | 44.4 | 64.7 | -6.41 | -11.65 |
| sl10_tp15 | 1883 | 41.1 | 56.7 | -5.73 | -13.03 | 942 | 39.5 | 56.5 | -6.18 | -9.95 |
| sl15_tp5 | 1883 | 55.2 | 76.1 | -6.22 | -16.06 | 942 | 54.9 | 75.6 | -6.10 | -11.32 |
| sl15_tp10 | 1883 | 52.3 | 68.8 | -5.97 | -13.53 | 942 | 51.6 | 67.9 | -5.87 | -9.28 |
| sl15_tp15 | 1883 | 47.1 | 60.7 | -5.65 | -11.25 | 942 | 46.6 | 59.9 | -5.48 | -7.67 |
| sl20_tp5 | 1883 | 58.7 | 78.2 | -6.50 | -15.14 | 942 | 59.6 | 77.9 | -6.14 | -10.11 |
| sl20_tp10 | 1883 | 56.3 | 71.7 | -6.24 | -12.70 | 942 | 56.9 | 70.9 | -5.75 | -8.02 |
| sl20_tp15 | 1883 | 51.3 | 63.7 | -5.73 | -10.28 | 942 | 51.8 | 63.6 | -5.47 | -6.81 |
| sl30_tp5 | 1883 | 63.6 | 80.2 | -6.53 | -13.13 | 942 | 64.5 | 80.3 | -6.17 | -8.77 |
| sl30_tp10 | 1883 | 61.6 | 75.2 | -6.58 | -11.57 | 942 | 62.6 | 74.2 | -5.65 | -6.81 |
| sl30_tp15 | 1883 | 56.8 | 67.9 | -5.97 | -9.26 | 942 | 57.4 | 67.7 | -5.51 | -5.99 |
| trail10 | 1883 | 27.7 | 39.0 | -4.92 | -9.22 | 942 | 26.5 | 39.9 | -5.70 | -7.80 |
| time1m | 1883 | 46.5 | 52.6 | -4.56 | -4.77 | 942 | 47.9 | 52.6 | -3.67 | -2.60 |
| tp20 | 1883 | 59.1 | 66.7 | -5.10 | -6.28 | 942 | 57.7 | 65.6 | -5.29 | -4.47 |
| sl10_tp20 | 1883 | 37.4 | 50.9 | -5.57 | -11.44 | 942 | 34.9 | 49.1 | -5.98 | -8.58 |
| sl15_tp20 | 1883 | 43.1 | 55.1 | -5.54 | -9.96 | 942 | 42.0 | 53.2 | -5.21 | -6.55 |
| sl20_tp20 | 1883 | 47.6 | 58.2 | -5.38 | -8.77 | 942 | 47.0 | 57.0 | -5.19 | -5.78 |

### Exit effect averaged over all 63 entry rules (all 30 days, minute, 1c)

Net change = exit's net c/contract minus hold's on the same entries. sd ratio = exit sd / hold sd.

| exit | entries | mean net change c | min | max | mean sd ratio | mean worst trade c | median maxDD ratio | share exited early |
|---|---|---|---|---|---|---|---|---|
| hold | 63 | 0.00 | 0.00 | 0.00 | 1.00 | -89.1 | 1.00 | 0% |
| sl5 | 63 | -0.69 | -3.75 | 13.17 | 0.63 | -76.5 | 1.55 | 69% |
| sl10 | 63 | -0.75 | -3.55 | 9.10 | 0.71 | -79.0 | 1.48 | 60% |
| sl15 | 63 | -0.53 | -2.71 | 6.35 | 0.78 | -81.5 | 1.34 | 52% |
| sl20 | 63 | -0.64 | -2.70 | 4.94 | 0.84 | -82.9 | 1.30 | 45% |
| sl30 | 63 | -0.35 | -1.50 | 3.93 | 0.91 | -83.8 | 1.12 | 31% |
| tp5 | 63 | -2.04 | -4.22 | 9.07 | 0.63 | -88.2 | 1.91 | 66% |
| tp10 | 63 | -2.02 | -3.83 | 5.33 | 0.71 | -89.0 | 1.80 | 58% |
| tp15 | 63 | -1.62 | -2.97 | 9.51 | 0.77 | -89.1 | 1.67 | 51% |
| sl5_tp5 | 63 | -1.08 | -4.87 | 16.25 | 0.35 | -63.1 | 1.82 | 95% |
| sl5_tp10 | 63 | -1.29 | -4.69 | 15.10 | 0.40 | -69.8 | 1.80 | 93% |
| sl5_tp15 | 63 | -1.12 | -4.49 | 16.41 | 0.44 | -73.2 | 1.74 | 91% |
| sl10_tp5 | 63 | -1.45 | -4.82 | 13.58 | 0.41 | -69.2 | 1.81 | 93% |
| sl10_tp10 | 63 | -1.57 | -4.62 | 12.96 | 0.46 | -74.5 | 1.83 | 89% |
| sl10_tp15 | 63 | -1.39 | -4.30 | 13.07 | 0.51 | -76.4 | 1.75 | 87% |
| sl15_tp5 | 63 | -1.64 | -5.01 | 12.91 | 0.45 | -74.5 | 1.83 | 91% |
| sl15_tp10 | 63 | -1.70 | -4.69 | 10.22 | 0.51 | -79.7 | 1.79 | 87% |
| sl15_tp15 | 63 | -1.42 | -4.27 | 11.88 | 0.56 | -80.6 | 1.71 | 84% |
| sl20_tp5 | 63 | -1.90 | -5.08 | 12.05 | 0.48 | -76.6 | 1.88 | 88% |
| sl20_tp10 | 63 | -1.87 | -4.68 | 9.36 | 0.55 | -82.2 | 1.86 | 84% |
| sl20_tp15 | 63 | -1.60 | -4.24 | 11.88 | 0.61 | -82.4 | 1.74 | 81% |
| sl30_tp5 | 63 | -1.95 | -4.59 | 11.57 | 0.54 | -80.4 | 1.85 | 82% |
| sl30_tp10 | 63 | -1.99 | -4.34 | 8.88 | 0.61 | -83.8 | 1.82 | 77% |
| sl30_tp15 | 63 | -1.72 | -3.77 | 9.51 | 0.67 | -83.8 | 1.74 | 73% |
| trail10 | 63 | -0.81 | -3.57 | 13.88 | 0.60 | -76.0 | 1.53 | 69% |
| time1m | 63 | -0.89 | -2.27 | 3.21 | 0.86 | -87.2 | 1.34 | 88% |
| tp20 | 63 | -1.29 | -2.88 | 3.36 | 0.82 | -89.1 | 1.52 | 45% |
| sl10_tp20 | 63 | -1.24 | -4.02 | 11.98 | 0.55 | -78.9 | 1.67 | 84% |
| sl15_tp20 | 63 | -1.25 | -3.96 | 9.23 | 0.61 | -81.2 | 1.67 | 81% |
| sl20_tp20 | 63 | -1.42 | -3.89 | 7.82 | 0.65 | -82.7 | 1.64 | 77% |

### Stop-loss effect: P&L distribution on the same entries (all 30 days, minute, 1c, c/contract)

**forced:model:8**

| exit | n | mean | sd | worst window | max drawdown | win % |
|---|---|---|---|---|---|---|
| hold | 2825 | -1.83 | 40.8 | -95.9 | 5735 | 49.1 |
| sl5 | 2825 | -3.32 | 26.5 | -90.0 | 9527 | 29.9 |
| sl10 | 2825 | -2.87 | 30.0 | -91.6 | 8445 | 35.5 |
| sl15 | 2825 | -2.42 | 33.0 | -91.6 | 7249 | 39.7 |
| sl20 | 2825 | -2.58 | 34.8 | -91.6 | 7672 | 41.9 |
| sl30 | 2825 | -1.84 | 37.7 | -91.6 | 5652 | 46.0 |
| trail10 | 2825 | -3.11 | 24.1 | -90.0 | 9026 | 35.7 |
| time1m | 2825 | -3.44 | 35.5 | -95.9 | 9903 | 46.8 |
| tp20 | 2825 | -3.11 | 32.3 | -95.9 | 8919 | 57.1 |
| sl15_tp20 | 2825 | -3.38 | 24.3 | -91.6 | 9611 | 47.5 |

**forced:coin:8**

| exit | n | mean | sd | worst window | max drawdown | win % |
|---|---|---|---|---|---|---|
| hold | 2848 | -3.72 | 40.8 | -96.3 | 10689 | 48.6 |
| sl5 | 2848 | -4.31 | 25.6 | -90.8 | 12278 | 28.8 |
| sl10 | 2848 | -4.15 | 29.1 | -90.8 | 11941 | 33.9 |
| sl15 | 2848 | -3.98 | 32.2 | -93.5 | 11444 | 38.0 |
| sl20 | 2848 | -3.94 | 34.1 | -93.5 | 11318 | 40.8 |
| sl30 | 2848 | -3.77 | 37.1 | -93.5 | 10781 | 44.7 |
| trail10 | 2848 | -3.77 | 24.1 | -90.8 | 10864 | 34.0 |
| time1m | 2848 | -4.27 | 35.5 | -95.9 | 12234 | 46.8 |
| tp20 | 2848 | -4.33 | 33.2 | -96.3 | 12525 | 56.1 |
| sl15_tp20 | 2848 | -4.40 | 24.4 | -93.5 | 12740 | 45.3 |

**taker:edge=5,blend=0.5,floor=1**

| exit | n | mean | sd | worst window | max drawdown | win % |
|---|---|---|---|---|---|---|
| hold | 297 | -8.03 | 48.8 | -87.8 | 2486 | 51.9 |
| sl5 | 297 | -6.35 | 31.2 | -74.5 | 1930 | 28.6 |
| sl10 | 297 | -6.59 | 34.7 | -74.5 | 1979 | 34.0 |
| sl15 | 297 | -7.01 | 37.5 | -74.5 | 2103 | 39.1 |
| sl20 | 297 | -8.17 | 40.4 | -74.5 | 2459 | 41.4 |
| sl30 | 297 | -8.75 | 43.1 | -80.2 | 2621 | 44.4 |
| trail10 | 297 | -7.77 | 30.7 | -74.5 | 2328 | 31.0 |
| time1m | 297 | -7.06 | 41.2 | -85.8 | 2218 | 48.5 |
| tp20 | 297 | -8.88 | 42.2 | -87.8 | 2707 | 58.6 |
| sl15_tp20 | 297 | -8.25 | 30.8 | -74.5 | 2450 | 45.1 |

### Minute vs per-print resolution (full-tape windows, all days, 1c)

435 windows with full tapes at most; configs with >= 100 print-mode trades only. Same entry decisions; print resolution fills the entry at the tape and checks every bid print. d = print net - minute net on the same windows (print: sale at the first bid print >= 1 s after the trigger; instant: sale at the trigger print).

| exit kind | configs | mean n print / minute | mean net minute c | mean d (1 s) c | median d (1 s) | mean d (instant) c | mean worst trade minute / print c | early-exit share minute / print |
|---|---|---|---|---|---|---|---|---|
| hold | 86 | 303 / 307 | -3.42 | -0.00 | 0.04 | -0.00 | -85.7 / -85.7 | 0% / 0% |
| stop | 240 | 345 / 350 | -5.09 | 0.31 | 0.22 | 0.34 | -72.8 / -39.6 | 52% / 63% |
| stop+tp | 888 | 341 / 346 | -5.57 | 0.15 | 0.18 | 0.10 | -69.4 / -36.1 | 83% / 87% |
| time | 48 | 345 / 350 | -5.90 | 0.09 | 0.10 | -0.00 | -86.5 / -86.0 | 84% / 85% |
| tp | 193 | 344 / 348 | -5.70 | -0.05 | -0.06 | -0.29 | -88.3 / -87.7 | 53% / 62% |
| trail | 48 | 345 / 350 | -4.58 | 0.02 | 0.09 | 0.17 | -61.7 / -30.3 | 69% / 85% |

Selected rows (net c/contract, all days, tape windows): minute | print 1 s | print instant

| entry | exit | n | minute | print (1 s) | print (instant) |
|---|---|---|---|---|---|
| forced:coin:8 | hold | 435 | -4.22 | -4.16 | -4.16 |
| forced:coin:8 | sl10 | 435 | -4.45 | -4.47 | -4.08 |
| forced:coin:8 | sl10_tp10 | 435 | -4.25 | -5.06 | -4.88 |
| forced:coin:8 | sl20 | 435 | -5.00 | -4.69 | -4.56 |
| forced:coin:8 | sl5 | 435 | -4.81 | -4.17 | -3.77 |
| forced:coin:8 | time1m | 435 | -5.65 | -5.54 | -5.65 |
| forced:coin:8 | tp10 | 435 | -4.03 | -4.68 | -5.08 |
| forced:coin:8 | trail10 | 435 | -3.95 | -4.30 | -3.71 |
| forced:favourite:8 | hold | 435 | 0.39 | 0.45 | 0.45 |
| forced:favourite:8 | sl10 | 435 | -2.31 | -2.41 | -1.80 |
| forced:favourite:8 | sl10_tp10 | 435 | -3.76 | -4.26 | -3.89 |
| forced:favourite:8 | sl20 | 435 | -1.68 | -1.75 | -1.59 |
| forced:favourite:8 | sl5 | 435 | -2.44 | -3.11 | -2.31 |
| forced:favourite:8 | time1m | 435 | -0.65 | -0.42 | -0.63 |
| forced:favourite:8 | tp10 | 435 | -1.69 | -2.38 | -2.62 |
| forced:favourite:8 | trail10 | 435 | -2.03 | -3.35 | -2.60 |
| forced:model:8 | hold | 428 | -2.41 | -2.34 | -2.34 |
| forced:model:8 | sl10 | 428 | -2.85 | -3.22 | -2.82 |
| forced:model:8 | sl10_tp10 | 428 | -3.94 | -4.10 | -3.77 |
| forced:model:8 | sl20 | 428 | -3.28 | -3.03 | -2.85 |
| forced:model:8 | sl5 | 428 | -3.64 | -3.35 | -2.91 |
| forced:model:8 | time1m | 428 | -4.41 | -4.32 | -4.38 |
| forced:model:8 | tp10 | 428 | -3.27 | -3.51 | -3.71 |
| forced:model:8 | trail10 | 428 | -2.02 | -3.08 | -2.49 |

### Best configurations and the multiple-testing correction

Configurations tried (distinct entry x exit at the primary spec, every run counted): **1966**. Bonferroni at family-wise 5%, one-sided: holdout t must exceed **4.05** (p < 0.05/1966 = 2.5e-05).

Top 10 by holdout t (selected ON the holdout, so only the Bonferroni line applies):

| config | train net c | train t | holdout n | holdout net c | holdout t | one-sided p | Bonferroni p |
|---|---|---|---|---|---|---|---|
| calm_regime_tp20:rule=momentum,tau=12,regime=busy|hold | -1.97 | -1.29 | 459 | 2.17 | 1.01 | 0.16 | 1 |
| forced:momentum:12|hold | -1.32 | -1.27 | 952 | 0.50 | 0.34 | 0.37 | 1 |
| taker:edge=1,blend=0.75,floor=0|sl15 | -4.35 | -3.59 | 390 | 0.06 | 0.03 | 0.49 | 1 |
| forced:momentum:4|sl30 | -1.93 | -2.69 | 952 | -0.04 | -0.04 | 0.52 | 1 |
| taker:edge=1,blend=0.75,floor=0|sl20 | -3.89 | -3.04 | 390 | -0.36 | -0.19 | 0.58 | 1 |
| taker:edge=3,blend=0.75,floor=1|trail10 | -14.21 | -3.87 | 34 | -1.60 | -0.27 | 0.61 | 1 |
| calm_regime_tp20:rule=momentum,tau=4,regime=calm|hold | -2.22 | -1.93 | 494 | -0.45 | -0.29 | 0.62 | 1 |
| taker:edge=3,blend=0.75,floor=1|sl20_tp10 | -15.48 | -4.17 | 34 | -1.57 | -0.33 | 0.63 | 1 |
| taker:edge=2,blend=0.75,floor=1|sl10 | -10.69 | -3.71 | 62 | -1.66 | -0.36 | 0.64 | 1 |
| taker:edge=1,blend=0.75,floor=0|hold | -3.46 | -2.38 | 390 | -0.72 | -0.36 | 0.64 | 1 |

Train-selected (the honest procedure: pick on the first 20 days, test once on the last 10). Top 5 by train t per family; correction = Bonferroni over the 10 holdout tests shown:

| family | config | train n | train net c | train t | holdout n | holdout net c | holdout t | Bonferroni p (x10) |
|---|---|---|---|---|---|---|---|---|
| F1 | forced:underdog:4|hold | 1896 | -0.94 | -1.21 | 952 | -1.94 | -1.79 | 1 |
| F1 | forced:momentum:12|hold | 1896 | -1.32 | -1.27 | 952 | 0.50 | 0.34 | 1 |
| F1 | forced:underdog:4|sl20 | 1896 | -1.04 | -1.39 | 952 | -1.82 | -1.75 | 1 |
| F1 | forced:underdog:4|sl30 | 1896 | -1.08 | -1.41 | 952 | -1.92 | -1.80 | 1 |
| F1 | forced:underdog:4|sl15 | 1896 | -1.07 | -1.49 | 952 | -1.81 | -1.80 | 1 |
| F2 | taker:edge=0,blend=0.75,floor=1|hold | 1118 | -0.07 | -0.07 | 499 | -3.22 | -1.81 | 1 |
| F2 | taker:edge=5,blend=0,floor=1|hold | 961 | -0.36 | -0.24 | 463 | -3.05 | -1.41 | 1 |
| F2 | taker:edge=5,blend=0.25,floor=1|hold | 544 | -0.74 | -0.37 | 244 | -3.73 | -1.25 | 1 |
| F2 | taker:edge=8,blend=0,floor=1|hold | 467 | -0.83 | -0.37 | 203 | -4.28 | -1.30 | 1 |
| F2 | taker:edge=3,blend=0.25,floor=1|hold | 1017 | -0.62 | -0.43 | 491 | -3.12 | -1.48 | 1 |

**Candidates (holdout survives Bonferroni over 1966):** none.

Holdout t over all 1840 configs with n >= 30: mean -4.88, sd 3.11, 3 of 1840 > 0, max 1.01, min -15.39. Holdout net c: median -5.14, 3 of 1840 > 0.
