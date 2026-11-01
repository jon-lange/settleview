# Human trading rules for 15-minute BTC up/down markets: survey and out-of-sample test (2026-10-28)

**Question (from the user):** humans sell courses on reading these charts and are often decent at it. Are we missing those artifacts? Can we turn what they teach into rules, test them, and build the useful parts into the Coach?

Part 1 surveys what traders, courses, blogs and forums recommend for short-term crypto up/down contracts (Kalshi KXBTC15M, Polymarket 15-minute and hourly BTC up/down, binary options), and turns each claim into an exact, testable rule. Part 2 tests every rule that can be tested on Kalshi's own trade prints (plus the BTC index where the rule needs it), under the same execution model as `docs/research/2026-10-28-model-vs-market.md`, with criteria written down here before any result was computed. Script: `scripts/test_human_rules.py`.

## Part 1: survey

### What we found, in one paragraph

There is very little real "chart-reading" material for these markets. The 15-minute crypto contracts are young (Kalshi's since late 2025), and almost everything published is (i) bot write-ups on dev.to, GitHub and Reddit's r/algotrading, (ii) affiliate guides that explain the mechanics and add generic tips, and (iii) a few data studies. No source we found shows an audited track record for a manual price-chart rule. The only large, verifiable profits are maker-side or two-sided (buy both outcomes for less than $1 in total), which is a liquidity-provision business, not chart reading. The best evidence on who wins is the opposite of the course pitch: on Polymarket's 15-minute crypto markets in one week of February 2026, 36.8% of 46,945 wallets were profitable before fees, the median wallet lost about $3, and makers as a group made +$728,501 while takers lost (Telonex). On Kalshi as a whole, takers do worse than makers and cheap contracts lose most of the money staked (Bürgi, Deng and Whelan).

### Survey table

Evidence grades: **A** = audited or academic data analysis; **B** = third-party reconstruction from on-chain or exchange data, or a self-reported backtest with stated sample size; **C** = self-reported small live sample; **D** = assertion only, no numbers. "Data needed": **tape** = Kalshi trade prints and official results alone; **tape+index** = also the BTC index (the per-second `index_1s` table read by `scripts/calibrate_coach.py`).

| ID | Claim as published | Exact rule we test (our interpretation) | Source | Evidence | Data needed |
|---|---|---|---|---|---|
| H1 | "Picking up pennies": buy the near-certain side late in the window and hold to settlement; best trades at 85-95c rather than 99c | At 3, 2 and 1 minutes before close (first fill wins), buy the side whose ask is 90-98c; hold to settlement | [dev.to, roswelly: "capture the last 60 seconds"](https://dev.to/roswelly/i-built-a-polymarket-trading-bot-that-tries-to-capture-the-last-60-seconds-of-market-inefficiency-4ki9); [datawallet "favourite compounding"](https://www.datawallet.com/crypto/top-polymarket-trading-strategies) | C: 22 settled trades, +$37.84, author says "nowhere near enough data"; datawallet is hypothetical | tape |
| H2 | Same, inside the last 60 s, at 85-95c | At 1 minute before close, buy the side whose ask is 85-95c | [roswelly](https://dev.to/roswelly/i-built-a-polymarket-trading-bot-that-tries-to-capture-the-last-60-seconds-of-market-inefficiency-4ki9) | C (same 22 trades) | tape |
| H3 | High-probability bot: enter when BTC is far enough from the strike for the time left and the token is above 0.90 (examples: 80 s left & >$70, 110 s & >$90, 150 s & >$150) | At 2 min before close: favourite ask 90-98c AND |BTC index - strike| >= $110 on the favourite's side; at 1 min: same with >= $50 (threshold = seconds left - $10, a line through the published examples) | [dev.to, benjamin_martin: high-probability bot for 5-minute BTC](https://dev.to/benjamin_martin_749c1d57f/building-a-high-probability-trading-bot-for-polymarkets-5-minute-btc-market-56ee) | D: "over 99%" win rate, no sample, no P&L | tape+index |
| H4 | Momentum at the open: wait 3 minutes; if BTC is above +0.4% buy YES, below -0.4% buy NO | At 12 and 10 minutes before close (3 and 5 min elapsed; first fill wins): BTC index vs the Kalshi strike >= +0.4% -> buy UP; <= -0.4% -> buy DOWN; any price | [dev.to, benjamin_martin: best 15-minute strategy guide](https://dev.to/benjamin_martin_749c1d57f/the-best-15-minute-polymarket-trading-bot-strategy-2026-guide-2d8a) | D: no P&L or backtest | tape+index |
| H5 | Kalshi lags BTC by 1-3 minutes: trade minutes 4-10; buy YES when BTC is up >= 0.15% from the open, NO when down >= 0.15% | At 10, 8 and 6 min before close (5, 7, 9 min elapsed; first fill wins): index vs strike >= +0.15% -> UP, <= -0.15% -> DOWN; side ask <= 90c | [clawd research notes: Kalshi crypto scalping](https://clawd-7k1.pages.dev/doc/projects__kalshi-crypto-scalping) | D: phase accuracies are "unsourced estimates", no backtest | tape+index |
| H6 | Favourite-longshot bias: high-priced contracts earn small positive returns, cheap ones lose | At 8, 6 and 4 min before close (first fill wins): buy the side whose ask is 75-90c | [Bürgi, Deng & Whelan, "Makers and Takers: The Economics of the Kalshi Prediction Market" (GWU WP 2026-001; CEPR DP20631)](https://econpapers.repec.org/RePEc:gwc:wpaper:2026-001) | A: 300,000+ Kalshi contracts, all categories (not 15-minute crypto specifically) | tape |
| H7 | "Prefer early entries below 0.20" (cheap early lottery tickets) | At 12 and 10 min before close (first fill wins): buy the side whose ask is 3-20c | [benjamin_martin guide](https://dev.to/benjamin_martin_749c1d57f/the-best-15-minute-polymarket-trading-bot-strategy-2026-guide-2d8a) | D | tape |
| H8 | Follow momentum in the contract price | Scanning each second from 12m10s to 2m before close: if the UP mid rose >= 10c over the last 60 s, buy UP (DOWN mirror); side ask <= 90c | Common advice; [benjamin_martin guide](https://dev.to/benjamin_martin_749c1d57f/the-best-15-minute-polymarket-trading-bot-strategy-2026-guide-2d8a) ("strong buy pressure with rising price raises continuation odds") | D | tape |
| H9 | Fade sharp moves / mean reversion | Same scan: if the UP mid fell >= 10c over the last 60 s, buy UP (DOWN mirror); side ask >= 10c | [closelook "technical analysis for prediction markets"](https://closelook-soccer.pages.dev/education/308-technical-analysis/) ("moves on thin volume vulnerable to reversion") | D | tape |
| H10 | Buy when the price crosses 50 | Scan 12m40s to 2m before close: UP mid was < 50c 30 s ago and is now >= 55c -> buy UP (DOWN mirror) | Folklore listed in the brief; no specific published source found | none found | tape |
| H11 | Follow volume spikes ("catalyst momentum") | Scan 8m10s to 2m before close: contracts printed in the last 30 s >= 3x the average 30-s volume of the 5 min before, and >= 100 contracts -> buy the side takers bought more of in those 30 s; side ask 10-90c | [datawallet "catalyst momentum"](https://www.datawallet.com/crypto/top-polymarket-trading-strategies); [benjamin_martin guide](https://dev.to/benjamin_martin_749c1d57f/the-best-15-minute-polymarket-trading-bot-strategy-2026-guide-2d8a) ("confirm with a volume spike") | D: hypothetical examples only | tape |
| H12 | Support: buy at the rolling low (mean reversion) | Scan 8m30s to 2m before close: a side's ask is at or below its lowest taker-buy print of the previous 5 min, and 20-80c -> buy it | [Quantpedia, Dujava 2026: mean reversion on Polymarket binaries](https://quantpedia.com/exploiting-mean-reversion-in-decentralized-prediction-markets-evidence-from-polymarket-binary-contracts/) (5-20 day lows on long-dated contracts, scaled down to minutes here) | B: backtest on 3 contracts; high-turnover variants turn negative at 10 bp costs | tape |
| H13 | Round numbers (25/50/75c) act as support and resistance | Scan 12m10s to 2m before close: for L in {25, 50, 75}c, a side's mid touched L-3c..L+1c in the last 60 s and is now L+4c..L+8c (a confirmed bounce) -> buy that side | [closelook](https://closelook-soccer.pages.dev/education/308-technical-analysis/) | D | tape |
| H14 | Hedge / pair both sides when YES + NO costs less than $1 | Scan 13m10s to 10 s before close: YES ask + NO ask <= 98c, both seen in the last 5 s -> buy one of each at or under the seen prices (each leg fills within 2 s or not at all; a lone filled leg is held to settlement) | [gabagool22 profile (third-party)](https://polymarketbot-site.pages.dev/top-bots/gabagool22); [0xinsider analysis](https://0xinsider.com/research/gabagool22-polymarket-trader-analysis); [benjamin_martin guide](https://dev.to/benjamin_martin_749c1d57f/the-best-15-minute-polymarket-trading-bot-strategy-2026-guide-2d8a) (YES+NO < 0.98) | B: on-chain reconstruction, avg pair cost 0.96-0.97, est. $400K-$788K; mostly maker fills on Polymarket | tape |
| H15 | "Only trade when the spread is tight" (applied to the late favourite) | Scan 3 min to 10 s before close: favourite ask 90-98c AND spread (YES ask proxy - YES bid proxy) <= 2c with both proxies <= 5 s old | [predictionmarketspicks: clear the cost wall](https://predictionmarketspicks.com/articles/kalshi-bitcoin-15-minute-markets); r/algotrading slippage comments (below) | D | tape |
| H16 | Late favourite, continuous scan (control for H15) | Same as H15 without the spread condition | as H1 | C | tape |
| H17 | "Trade the first minutes" (fill count peaks at minute 0) | Scan 13m10s to 12m before close (the earliest our tapes allow): buy the side whose ask is 55-75c (the early leader) | [Telonex, top crypto traders on Polymarket 15m](https://telonex.io/research/top-crypto-traders-polymarket-15m) (activity peaks at minute 0); [benjamin_martin guide](https://dev.to/benjamin_martin_749c1d57f/the-best-15-minute-polymarket-trading-bot-strategy-2026-guide-2d8a) ("minutes 2-5 best entries") | D | tape |

Claims surveyed but **not testable as a taker rule** on this data:

| Claim | Source | Evidence | Why not tested |
|---|---|---|---|
| Be the maker: quote both sides, collect the spread and rebates | [Telonex](https://telonex.io/research/top-crypto-traders-polymarket-15m) (makers +$728,501 in a week; takers net negative); [Bürgi, Deng & Whelan](https://econpapers.repec.org/RePEc:gwc:wpaper:2026-001) (makers earn more than takers) | A/B | Needs order-book queue position and fill modelling; the tape shows only executions. The strongest evidence in the survey points here. |
| Read the order book: walls, imbalance | [benjamin_martin guide](https://dev.to/benjamin_martin_749c1d57f/the-best-15-minute-polymarket-trading-bot-strategy-2026-guide-2d8a); [r/algotrading Kalshi 15m thread](https://reddit.com/r/algotrading/comments/1ru03le/is_anyone_interested_in_discussing_a_kalshi_15/) | D | No historical book depth |
| Cross-venue / cross-market arbitrage (Kalshi vs Polymarket, 5m vs 15m, options-implied for hourly) | [datawallet](https://www.datawallet.com/crypto/top-polymarket-trading-strategies); [Indie Hackers 5m vs 15m](https://www.indiehackers.com/post/building-a-polymarket-arbitrage-bot-exploiting-inefficiencies-between-5-minute-and-15-minute-crypto-markets-a6b2af442f); [predictionmarketspicks hourly](https://predictionmarketspicks.com/articles/bitcoin-edge-kalshi-hourly-settles) | D | Needs a second venue's tape |
| Partial hedge when "price reverses" | benjamin_martin guide | D | No trigger defined |
| Exit early at 75-90c instead of holding | benjamin_martin guide; [TradingView NADEX script](https://vn.tradingview.com/script/JZOHKDHI-NADEX-Binary-Options) | D | An exit policy, not an entry rule; the earlier study already tested the Coach's exits |
| Strike selection by price velocity and time left (binary options course) | [Stinger5 Nadex course listing](https://coursesity.com/course-detail/stinger5-nadex-5-min-expiration-binary-option-strategy) | D (sales page) | No rule given |

Two forum threads are worth recording as evidence even though they hold no rule: a Kalshi 15-minute bot builder who ran 48 hours and concluded that "between the floor set by Kalshi and their fees the edge is at best 52%", and a Polymarket 5-minute bot whose broad strategy "turns negative once realistic execution is applied" after 177 paper trades ([r/algotrading, 5-min bot update](https://reddit.com/r/algotrading/comments/1tv6vug/update_1_i_was_bored_so_i_though_of_making_a_5min/)); and a site that grades its own short-horizon model and reports it "has not beaten the market's Brier score in any minute-remaining bucket" ([predictionmarketspicks](https://predictionmarketspicks.com/articles/kalshi-bitcoin-15-minute-markets)).

## Pre-registered criteria

Written before any result was computed and before any profit or win figure was looked at. Not edited afterwards.

- **Family.** The 17 rules H1-H17 above, each with exactly one primary specification (the "exact rule" column plus the definitions below). Nothing is added to or dropped from the family after results are seen.
- **SURVIVES** iff, on the holdout (the last third of UTC days, i.e. the last 10 of the 30 days 2026-09-29 .. 2026-10-28): (1) net cents per contract after the exact taker fee at 1c slippage is > 0; (2) the window-clustered t is > 2 AND the Holm-adjusted two-sided p-value (Holm step-down over all 17 rules, from the normal approximation p = 2(1 - Phi(|t|))) is below 0.0455, the two-sided p of t = 2; (3) net cents per contract at 1c slippage is positive in both chronological halves of the full period (first 15 and last 15 days). Otherwise the rule FAILS. A rule with fewer than 30 holdout entries FAILS as untestable at this sample size.
- No parameter of any rule is fitted. The training two-thirds is reported for information only.

## Operational definitions (fixed with the criteria)

- **Markets and outcome.** The settled KXBTC15M markets in `data/research/kalshi_trades.db` (windows opening 2026-09-29 00:00 .. 2026-10-28 23:45 UTC), outcome = Kalshi's official `result`. Day = UTC date of the open.
- **Quote proxies** (as in the model-vs-market study). At decision second s: UP ask = latest taker-buys-YES print in [s-30 s, s); DOWN ask = 1 - latest taker-buys-NO print (YES terms) in [s-30 s, s). UP mid = average of the UP ask proxy and the YES bid proxy (the taker-NO print) when both exist, else the one that exists. DOWN mid = 1 - UP mid. "Spread" = UP ask proxy - YES bid proxy.
- **Execution.** Once a rule fires at s for a side, it fills at the first taker-buys print for that side in [s, s+2 s) priced at or under the limit; limit = seen ask + 1c, capped at the rule's price-band maximum when it has one (H14: limit = seen ask, no +1c). Paid = that print + slippage (1c primary, 0c reported), capped at 99c, plus the exact Kalshi taker fee 0.07 x C x P x (1-P) rounded up to $0.0001 (`btc_signal_engine/domain/settlement/fees.py`, 1 contract). No fill: the scan continues. At most one entry per window per rule. Held to settlement. Net per contract = 1{won} - paid - fee. For H14 the unit is one pair (one YES + one NO contract) and net is per pair.
- **Fixed-time rules (H1-H7)** decide at s = close - tau - 2 s for each listed tau and fill in [s, close - tau). They run on every market with prints there: the full tape where one was fetched, otherwise the panel prints the other study fetched before each tau (which contain every print from their earliest timestamp up to tau, so any print of a side found there before s is the true latest one; a side with no print in that span counts as missing and the rule does not fire).
- **Scanning rules (H8-H17)** evaluate every whole second in the stated range and need the full tape (the last 13m40s of each window), so they run only on the random sample of markets with full tapes.
- **BTC index (H3-H5).** Latest `index_1s` price with ts < s - 1 s from `data/btcflow.db` (opened read-only), against Kalshi's `floor_strike` for the window. A window with no index second in the 10 s before s - 1 s is skipped.
- **Statistics.** Win % = share of entries that won. Break-even % = average cost per contract including fee (the win rate needed to break even). t = mean / (sd / sqrt(n)) over entries; one entry per window, so this is window-clustered.

## Results

(Filled in by `scripts/test_human_rules.py` after the criteria above were written.)

Period 2026-09-29 to 2026-10-28 (30 UTC days; holdout 2026-10-19 to 2026-10-28). 2848 settled markets; 435 have full tapes (the random sample the scanning rules H8-H17 run on); the fixed-time rules H1-H7 run on every market with prints at the check times. Generated 2026-10-29 02:49 UTC by `scripts/test_human_rules.py`.

### Holdout (pre-registered test): no rule survives

Net and break-even are per contract (H14: per pair). t is window-clustered; Holm p is adjusted over all 17 rules; H1/H2 net = first/last 15 days of the full period at 1c slippage.

| rule | n | win % | break-even % | net c/contract (1c slip) | net at 0c slip | t | Holm p | H1 net | H2 net | verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| H1 Late favourite 90-98c at 3/2/1 min left | 445 | 94.4 | 95.9 | -1.49 | -0.55 | -1.37 | 1.000 | -1.62 | -2.36 | FAILS |
| H2 Last-minute favourite 85-95c at 1 min left | 87 | 88.5 | 92.3 | -3.76 | -2.82 | -1.12 | 1.000 | -3.21 | -4.79 | FAILS |
| H3 Late favourite 90-98c + BTC far from strike (2/1 min left) | 12 | 91.7 | 97.9 | -6.27 | -5.34 | -0.74 | 1.000 | 2.43 | -7.00 | FAILS (n < 30) |
| H4 BTC momentum +-0.4% at 3/5 min elapsed | 10 | 100.0 | 94.6 | 5.41 | 6.35 | 7.71 | 0.000 | 5.94 | 6.23 | FAILS (n < 30) |
| H5 BTC lag momentum +-0.15%, min 5-9 elapsed, ask <= 90c | 81 | 81.5 | 87.7 | -6.27 | -5.32 | -1.45 | 1.000 | -1.05 | -2.83 | FAILS |
| H6 Favourite 75-90c at 8/6/4 min left (longshot bias) | 572 | 81.3 | 84.5 | -3.16 | -2.21 | -1.95 | 0.775 | -2.76 | -3.01 | FAILS |
| H7 Cheap early longshot 3-20c at 12/10 min left | 275 | 17.5 | 16.1 | 1.34 | 2.39 | 0.59 | 1.000 | -4.64 | -1.64 | FAILS |
| H8 Contract momentum: mid +10c in 60 s | 133 | 60.2 | 63.1 | -2.98 | -2.00 | -0.74 | 1.000 | -2.38 | -2.28 | FAILS |
| H9 Fade: buy side whose mid fell 10c in 60 s | 133 | 39.8 | 42.7 | -2.87 | -1.86 | -0.71 | 1.000 | -3.47 | -3.61 | FAILS |
| H10 Cross 50: mid <50c 30 s ago, now >=55c | 90 | 48.9 | 60.6 | -11.72 | -10.74 | -2.20 | 0.440 | -0.80 | -7.92 | FAILS |
| H11 Volume spike (3x, >=100 contracts): follow taker flow | 29 | 48.3 | 58.6 | -10.35 | -9.36 | -1.38 | 1.000 | 16.02 | -7.08 | FAILS (n < 30) |
| H12 Rolling 5-min low (mean reversion), 20-80c | 52 | 34.6 | 41.6 | -6.99 | -5.98 | -1.11 | 1.000 | 1.53 | 2.22 | FAILS |
| H13 Round-number (25/50/75c) support bounce | 115 | 58.3 | 58.6 | -0.37 | 0.62 | -0.08 | 1.000 | -7.07 | 0.19 | FAILS |
| H14 Pair both sides when YES ask + NO ask <= 98c | 12 | 41.7 | 77.8 | 13.82 | 15.40 | 1.60 | 1.000 | -10.19 | 8.13 | FAILS (n < 30) |
| H15 Late favourite 90-98c, spread <= 2c (scan last 3 min) | 91 | 93.4 | 94.4 | -0.98 | -0.04 | -0.38 | 1.000 | -4.92 | 1.71 | FAILS |
| H16 Late favourite 90-98c, no spread filter (scan last 3 min) | 91 | 92.3 | 94.3 | -2.01 | -1.07 | -0.73 | 1.000 | -4.89 | 1.10 | FAILS |
| H17 First minutes: early leader 55-75c (min 1:50-3:00 elapsed) | 124 | 65.3 | 65.9 | -0.54 | 0.45 | -0.13 | 1.000 | -4.05 | -1.45 | FAILS |

H14 pairs: 21 both legs filled, 38 one leg only (held naked to settlement).

### Training two-thirds and full period (information only)

| rule | train n | train win % | train BE % | train net c | train t | all n | all net c | all t |
|---|---|---|---|---|---|---|---|---|
| H1 | 962 | 93.6 | 95.8 | -2.20 | -2.80 | 1407 | -1.98 | -3.10 |
| H2 | 180 | 87.8 | 91.9 | -4.14 | -1.71 | 267 | -4.02 | -2.05 |
| H3 | 28 | 96.4 | 97.7 | -1.25 | -0.35 | 40 | -2.76 | -0.79 |
| H4 | 17 | 100.0 | 93.5 | 6.52 | 8.25 | 27 | 6.11 | 10.86 |
| H5 | 193 | 87.0 | 87.2 | -0.13 | -0.05 | 274 | -1.94 | -0.92 |
| H6 | 1240 | 81.6 | 84.4 | -2.76 | -2.51 | 1812 | -2.88 | -3.17 |
| H7 | 563 | 11.9 | 17.3 | -5.38 | -4.00 | 838 | -3.17 | -2.70 |
| H8 | 287 | 62.0 | 64.0 | -2.03 | -0.75 | 420 | -2.33 | -1.04 |
| H9 | 287 | 38.0 | 41.8 | -3.84 | -1.43 | 420 | -3.54 | -1.58 |
| H10 | 188 | 60.6 | 61.5 | -0.87 | -0.24 | 278 | -4.38 | -1.47 |
| H11 | 45 | 68.9 | 54.9 | 14.02 | 2.09 | 74 | 4.47 | 0.86 |
| H12 | 130 | 44.6 | 39.2 | 5.38 | 1.24 | 182 | 1.84 | 0.51 |
| H13 | 254 | 55.5 | 60.5 | -4.99 | -1.77 | 369 | -3.55 | -1.49 |
| H14 | 47 | 34.0 | 65.2 | -7.74 | -1.34 | 59 | -3.36 | -0.67 |
| H15 | 226 | 92.0 | 94.2 | -2.17 | -1.22 | 317 | -1.83 | -1.24 |
| H16 | 226 | 92.0 | 94.2 | -2.13 | -1.19 | 317 | -2.10 | -1.40 |
| H17 | 274 | 61.7 | 65.5 | -3.82 | -1.31 | 398 | -2.80 | -1.16 |

### Calibration of the market mid (descriptive, all periods, not a tested rule)

- **12 min left** (2848 markets): 0-10c: n=26, mid 7.9%, UP won 0.0%; 10-25c: n=284, mid 19.2%, UP won 17.6%; 25-40c: n=611, mid 32.6%, UP won 30.9%; 40-60c: n=980, mid 49.8%, UP won 48.7%; 60-75c: n=594, mid 67.3%, UP won 66.0%; 75-90c: n=320, mid 81.4%, UP won 84.1%; 90-100c: n=33, mid 92.3%, UP won 97.0%
- **6 min left** (2848 markets): 0-10c: n=461, mid 5.0%, UP won 5.6%; 10-25c: n=432, mid 17.1%, UP won 18.1%; 25-40c: n=311, mid 32.0%, UP won 30.5%; 40-60c: n=400, mid 49.7%, UP won 47.5%; 60-75c: n=342, mid 67.8%, UP won 63.2%; 75-90c: n=447, mid 83.0%, UP won 81.9%; 90-100c: n=455, mid 95.2%, UP won 96.3%
- **3 min left** (2846 markets): 0-10c: n=820, mid 2.9%, UP won 3.3%; 10-25c: n=288, mid 16.7%, UP won 15.3%; 25-40c: n=196, mid 31.8%, UP won 33.2%; 40-60c: n=228, mid 49.7%, UP won 45.2%; 60-75c: n=213, mid 67.6%, UP won 65.7%; 75-90c: n=267, mid 83.0%, UP won 79.4%; 90-100c: n=834, mid 97.3%, UP won 97.8%
- **1 min left** (2479 markets): 0-10c: n=1035, mid 1.2%, UP won 1.5%; 10-25c: n=127, mid 16.0%, UP won 15.0%; 25-40c: n=66, mid 32.7%, UP won 15.2%; 40-60c: n=74, mid 49.7%, UP won 45.9%; 60-75c: n=69, mid 68.2%, UP won 65.2%; 75-90c: n=98, mid 82.9%, UP won 82.7%; 90-100c: n=1010, mid 98.8%, UP won 98.9%

### Reading the results

- **Nothing survives.** 0 of 17 rules meet the pre-registered bar. 14 have a negative net on the holdout. The three positive ones either have fewer than 30 entries (H4 +5.4c, H14 +13.8c) or are negative in both chronological halves and lose over the full period (H7 +1.3c on the holdout, -3.2c with t -2.7 overall).
- **The folklore loses roughly the fee.** The market's price is close to the realised win rate in every bucket (calibration above), so buying at the price loses about the taker fee plus slippage, and slightly more for favourites: the late 90-98c favourite (H1) won 94.4% of the time but needed 95.9%; over the whole period it is -1.98c per contract with t = -3.1, a reliable small loss. The 75-90c favourite (H6) is -2.9c with t = -3.2 over the full period. On this market, unlike Kalshi as a whole, the favourite side of the longshot bias does not pay a taker.
- **The spread filter does not rescue the late favourite.** H15 (spread <= 2c) and H16 (no filter) fire in the same windows almost every time and both lose.
- **Momentum vs fade in the contract price is a wash.** The interim run (115 tapes) showed contract momentum (H8) at +12.9c; with all 435 tapes it is -3.0c and its mirror (H9) is -2.9c. Both pay the fee; neither direction has an edge. This is a live example of why small samples mislead.
- **Cross-50 (H10) is the worst popular rule** at -11.7c on the holdout (t -2.2), because it buys after the move at a fee-heavy price near 55c.
- **The one lead, not a result: H4 (BTC up or down 0.4% from the strike within the first 3-5 minutes, buy that side).** 27 entries over 30 days, all won, +6.1c per contract over the full period; but only 10 in the holdout, so it fails the n >= 30 rule. It fires in about 1% of windows. It is the "price lags a big BTC move" idea, and it would need a new pre-registered test on a longer period before it means anything.
- **Pairing both sides (H14)** filled both legs in only 21 of 59 attempts; the other 38 became naked single bets. Crossed prints on the tape are mostly timing artefacts, not a free dollar.

### Caveats

- **Sampling.** H1-H7 use every market with prints at the check times (2,848 markets; 2,479 at 1 minute left). H8-H17 use the 435 markets whose full tapes were fetched before the time box closed: a random subset of a day-stratified 40% sample, about 15% of all windows. Their holdout n is 12-133.
- **Period.** 30 days (2026-09-29 to 2026-10-28), one market regime. Holdout = the last 10 days.
- **Execution model.** Quotes are proxies from taker prints, not the order book; fills assume we get the next taker print within 2 s at that print plus 1c. That is optimistic when the book is thin, and the fill can come in below a rule's price band. No maker strategies were tested.
- **Interpretation.** Every rule is our exact reading of vague advice; a different reading (other thresholds or timing) could score differently, and testing many readings would need its own multiple-testing correction.
- An interim run on partial data (115 full tapes, 1,835 markets) was reported to the coordinator before this final run; it reached the same verdict for H1-H7 and is superseded by the tables above.


## Process lessons (what good traders and the research agree on)

The survey's rules are mostly weak, but the process advice is consistent across courses, forums and the academic record, and it rests on much better evidence than any entry rule.

1. **Most short-horizon speculators lose, and losing does not make them stop.** In Brazil, 97% of people who day-traded equity futures for more than 300 days lost money and only 1.1% earned more than the minimum wage; there was no sign of learning ([Chague, De-Losso & Giovannetti, "Day Trading for a Living?"](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3423101)). In Taiwan, fewer than 1% of day traders earn predictable positive returns net of fees ([Barber, Lee, Liu & Odean 2014, J. Financial Markets](https://econpapers.repec.org/RePEc:eee:finmar:v:18:y:2014:i:c:p:1-24)), and 74% of day-trading volume comes from traders with a history of losses, which fits overconfidence and biased learning rather than people rationally discovering their skill ([Barber, Lee, Liu & Odean, "Learning, Fast or Slow", RAPS 2020](https://ideas.repec.org/a/oup/rasset/v10y2020i1p61-93..html)). The same shape shows up in these exact markets: 36.8% of 46,945 Polymarket 15-minute crypto wallets were profitable in one week, before fees ([Telonex](https://telonex.io/research/top-crypto-traders-polymarket-15m)).
2. **Costs decide the game, and which side of the spread you are on matters more than the chart.** On Kalshi, makers earn more than takers and cheap contracts lose most of what is staked ([Bürgi, Deng & Whelan 2026](https://econpapers.repec.org/RePEc:gwc:wpaper:2026-001)). On Polymarket's 15-minute markets, takers won 53% of fills and still lost money, while makers won 47% and made +$728,501 in a week ([Telonex](https://telonex.io/research/top-crypto-traders-polymarket-15m)). Bot builders who modelled execution honestly found their edges vanished ([r/algotrading, 5-minute bot](https://reddit.com/r/algotrading/comments/1tv6vug/update_1_i_was_bored_so_i_though_of_making_a_5min/); a Kalshi 15-minute bot owner in the same thread: "the edge is at best 52%").
3. **Losses change behaviour in a predictable, costly direction.** After a loss people prefer bets that offer a chance to get back to even (the break-even effect); after a gain they take more risk with "house money" ([Thaler & Johnson 1990, Management Science](https://ideas.repec.org/a/inm/ormnsc/v36y1990i6p643-660.html)). Investors sell winners too early and hold losers ([Odean 1998, J. Finance](https://doi.org/10.1111/0022-1082.00072)), and the most active traders earn the least ([Barber & Odean 2000, J. Finance](https://doi.org/10.1111/0022-1082.00226)). Traders' cortisol rises with the variance of their P&L and with market volatility ([Coates & Herbert 2008, PNAS](https://doi.org/10.1073/pnas.0704025105)). The retail guides say the same in plain words: after a loss, "take a break and re-evaluate before making another trade" ([sailgp guide](https://sailgp.com/prediction-markets/kalshi/15-minute-markets)); size tiny, because fast resolution and correlated windows let a losing streak compound ([predictionmarketspicks](https://predictionmarketspicks.com/articles/kalshi-bitcoin-15-minute-markets)).
4. **A small sample proves nothing; keep score properly.** Forum reviewers of bot results repeat it: 15 trades is noise, 76 may be one regime, a 53-55% win rate needs roughly 200-300 trades before its confidence interval excludes a coin flip, and testing many variants inflates the best one ([r/algotrading thread](https://reddit.com/r/algotrading/comments/1tv6vug/update_1_i_was_bored_so_i_though_of_making_a_5min/)). The superforecasting work found that accuracy improves with scorekeeping (Brier scores), short training on biases, teaming, and tracking the best performers ([Mellers et al. 2014, APS summary](https://www.psychologicalscience.org/news/minds-business/superforecasters-the-art-of-accurate-predictions.html)).
5. **Where human judgment helps, and where it does not.** Tetlock's forecasters beat crowds on slow geopolitical questions months out. Even there, a liquid market beat the simple average of forecasters; polls won only after statistical aggregation, and their biggest edge was early in long questions, before information was priced ([Atanasov et al. 2017, Management Science](https://pubsonline.informs.org/stoken/default+domain/EC-crowds/full/10.1287/mnsc.2015.2374)). A 15-minute BTC contract is the opposite case: the only information is the BTC price, which bots read in milliseconds, and the contract reprices continuously. There is nothing slow for a human to know first. Human skill here is the discipline in points 2-4, not the chart reading.
6. **Sitting out is a position.** Every serious source ends either at "the cost wall is brutal" or at "we have not found an edge yet": predictionmarketspicks lists its 15-minute model as "research" and says its graded model "has not beaten the market's Brier score in any minute-remaining bucket" ([predictionmarketspicks](https://predictionmarketspicks.com/articles/kalshi-bitcoin-15-minute-markets)). Our own model-vs-market study reached the same verdict for the Coach's model (taker viable: no).

## What to build into the Coach

- **"Tested, didn't work" explainers.** When a proposed or manual trade matches a popular rule (late 90c+ favourite, chasing a 10c jump, buying the cross of 50, a volume spike, a "bounce" off 25/50/75), show a one-line card: the rule, where it comes from, and our holdout numbers (n, win %, break-even %, net cents after fees). This turns course folklore into the user's own evidence at the moment it matters.
- **Break-even on every ticket.** Before the user says Yes, show the win rate the price plus fee requires (for example, at 94c you need to win about 94.4% of the time) next to the market's own recorded win rate at that price and time left (the calibration table above), and the fee in cents.
- **Personal edge ledger against the market.** Log every trade with the price paid and, optionally, the user's own probability typed before seeing the price. Track results against the market price at entry (net cents per contract, win % vs break-even %) with a confidence interval, and say plainly how many more trades are needed before the number means anything. Score the user's typed probabilities (Brier) against the market's.
- **Tilt and loss-chasing warnings.** Detect the documented patterns: an entry within a few minutes of a loss, size above the user's usual after a loss, more trades per hour than usual, a run of losses. Respond with a cooldown prompt the user can override in one tap, not a block, because the user stays in control.
- **Sizing guardrails.** A per-window maximum, a daily loss limit that ends the session, and no size increase after losses. With no measured edge, the Kelly fraction is zero, so the guardrail is about survival, not optimisation.
- **Sit-out as the default.** The Coach should be comfortable proposing nothing for hours and saying why: no rule we tested beats the price after fees.
- **Maker-side, as a future research item only.** The only large verified profits in the survey are maker-side or two-sided. Testing that needs order-book history and a queue model, which this tape cannot provide.
