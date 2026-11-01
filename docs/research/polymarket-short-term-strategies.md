# Polymarket short-term crypto Up/Down: what the crowd says, what is evidenced, what we can test (2026-10-29)

Web research only (no trading, no app calls). Sources: X posts (via search snippets; x.com itself is not fetchable),
YouTube (titles only: transcripts are blocked, see the end), GitHub READMEs, dev.to / Substack / Medium write-ups,
news coverage of on-chain studies, and two academic working papers. Everything here was checked against
[LEARNINGS.md](LEARNINGS.md) so already-killed ideas are only cross-referenced, not re-proposed.

Two quick read-only checks on our own data were run while writing this (numbers below are from them):

- **Polymarket US nested windows share fixings exactly.** In `polymarket_us_history.db`, windows of different
  spans that end at the same instant have the same `settlement_price` (5m vs 15m 658/658, 1h vs 5m 165/165,
  1h vs 15m 862/873, the 11 misses are null/outage rows), and windows that start together share
  `price_to_beat` (5m vs 15m 651/651). So the last 5m of a 15m window, and the last 15m and last 5m of an hour,
  settle on the *same number*. This makes the nested-window trade (S1 below) a structural relationship, not a
  statistical one.
- **Kalshi's hourly KXBTCD ladder settles on the same number as PM US 1h.** `horizons.db` KXBTCD
  `expiration_value` equals PM US 1h `settlement_price` at the same close in 705/705 hours (max difference $0.00).
  So every Kalshi "above K at the hour" strike, the PM US 1h, its last 15m and its last 5m (and the Kalshi
  KXBTC15M ending at the hour) are all bets on one fixing E, with different strikes.
- **The live recorder has all three spans at once** (`polymarket_us.db:pm_ws_quotes`, 5m/15m/1h, from
  2026-10-29 12:00Z). Mean quoted spread: 5m 1.22¢, 15m 1.10¢, 1h 1.09¢.

## Context that decides transferability

| | Polymarket international (polymarket.com) | Polymarket US (what we trade) | Kalshi KXBTC15M |
|---|---|---|---|
| Settlement | Chainlink BTC/USD stream. Single print until 2026-08-06; **60 s Chainlink TWAP for both strike and close from 2026-08-07** (15m/4h); 5m moved from a 30 s to a 60 s TWAP on 2026-08-14 ([tradoxvps](https://tradoxvps.com/polymarket-twap-settlement/), [casatrick](https://casatrick.substack.com/p/polymarket-twap-latency-trading-bots), [onekey](https://onekey.so/blog/ecosystem/polymarket-to-upgrade-crypto-updown-market-settlements-from-single-price-snapshots-to-twap-20260730231730/)). Hourly/daily: Binance candles | CF Benchmarks BRTI, 60 s simple average, ≥ = Up | identical to PM US (3,293/3,293 results) |
| Book | Separate Up and Down token books; complete sets can be minted/merged for $1 | **One book per market** (Down = BUY_SHORT at 1 − Up bid) | one book |
| Fees | Taker fee on short crypto (peak ≈1.56–3.15% at 50¢, sources disagree), rebated to makers (100% at launch, reportedly ~20% later); 50–250 ms taker delay | taker 0.0695·C·P·(1−P), **rounded to the nearest cent per order** (1 contract at 33–68¢ cost 2¢; 2 at 22¢, raw 2.39¢, cost 2¢, not 3¢); maker 0; no rebate below $10M/month | taker 0.07·C·P·(1−P) rounded up to $0.0001; maker 0 |
| Identities | public wallets (copy-trading possible) | no public wallets | none |

Consequences: (1) "YES + NO asks < $1" cannot exist on a single book (Up ask + Down ask = Up ask + 1 − Up bid ≥ 1),
so the most famous international arb has no Polymarket US equivalent except across markets or venues. (2) The
international market now uses a 60 s average too, so "forecast the in-flight average" ideas transfer in
structure, but Chainlink-vs-exchange lag and last-print sniping do not. (3) Copy-trading is impossible on PM US.

## Catalogue by family

Format per idea: **Source** · **Claim** · **Claimed results** · **Mechanics** · **Our rule** · **Data / when** ·
**Transfer to PM US** · **Red flags**.

### A. Latency / oracle arbitrage (spot moves before the prediction-market book)

**A1. "$313 → $414k" bot (wallet 0x8dxd), 15m BTC/ETH/SOL, Dec 2025.**
Source: Dexter's Lab X post 2026-01-05, reported by [coinspectator/Finance Magnates](https://coinspectator.com/mainstream/2026/01/07/polymarket-introduces-dynamic-fees-to-curb-latency-arbitrage-in-short-term-crypto-markets/); "98% win rate across 6,615 predictions" in [dev.to](https://dev.to/andrew-ooo/how-ai-trading-bots-are-making-millions-on-polymarket-l5g).
Mechanics: buy the side spot has already decided when the Polymarket book is seconds stale. Claimed lag "2–15 s"
([bot sales page](https://polymarketbot-site.pages.dev/top-bots/313-bot), $299); others say 50–100 ms
([asksurf](https://asksurf.ai/pulse/en/latency-arbitrage-dominates-polymarket-btc): "Bots are pulling $15k daily
with 68.7% win rates") or 100–500 ms Chainlink lag. Polymarket answered with taker fees in January 2026
([The Block](https://www.theblock.co/post/384461/polymarket-adds-taker-fees-to-15-minute-crypto-markets-to-fund-liquidity-rebates)),
"the taker fee is highest when odds are closest to 50%, precisely where latency-driven strategies were most active"
([Finance Magnates](https://www.financemagnates.com/cryptocurrency/polymarket-introduces-dynamic-fees-to-curb-latency-arbitrage-in-short-term-crypto-markets/)).
Our rule: when BTC spot (Coinbase/Binance WS, < 300 ms old) has moved so that the fair Up probability exceeds the
PM US Up ask by ≥ fee + 1¢, buy at the ask; hold to settlement. Data: needs a fresh spot tap aligned to
`pm_ws_quotes.recv_ms`; **our `index_1s` is 2.7–3.6 s stale, so we cannot test this now.**
Transfer: the lag that mattered was Chainlink → Polymarket book; on PM US the analogue is BRTI-constituent spot →
PM US book. Red flags: survivorship (one wallet), killed by fees at 50¢, requires co-location against bots with
sub-100 ms paths; we trade from a home Windows machine. Our H5 ("Kalshi lags BTC", minute resolution) is KILLED.

**A2. "55-second oracle lag" / oracle-lag-sniper repo.** Source: [hashnode thread](https://hashnode.com/forums/thread/how-i-used-python-asyncio-to-trade-a-55-second-oracle-lag-on-polymarket)
(body not retrievable), repo `JonathanPetersonn/oracle-lag-sniper` — **disabled by GitHub staff for ToS
violation** (fetched 2026-10-29). Claim: "the Chainlink oracle ... updates in under a second but the order book takes
about 55 seconds to react". No numbers recoverable. Red flags: the repo was pulled; 2026 saw several malware
campaigns in "Polymarket bot" repos that steal `.env` keys ([StepSecurity, 2026-03-15](https://www.stepsecurity.io/blog/malicious-polymarket-bot-hides-in-hijacked-dev-protocol-github-org-and-steals-wallet-keys),
[OSV MAL-2026-4210..4217](https://osv.dev/vulnerability/MAL-2026-4211)). **Never run third-party Polymarket bot
code on this machine.**

**A3. "Watch Chainlink, not Binance."** [dev.to/lkto1m](https://dev.to/lkto1m/why-my-polymarket-bot-watches-chainlink-not-binance-the-oracle-gap-that-changes-every-close-1e96) (Jun 27).
Claim: Binance and Chainlink diverge "by 0.3% to 0.8% for 10 to 30 seconds" in volatile windows; win rate on 5m rose
"from 52% to 57-58%" after switching feeds (no n, no prices paid). Rules: no new positions in the last 15 s, cut at
T−90 s, skip FOMC/CPI/NFP. Transfer: the lesson transfers exactly — model the *settlement index* (BRTI 60 s
average), not one exchange. We already know our Coinbase proxy disagrees with Kalshi's result in 2.1% of windows
(28% when within $5). Not a strategy by itself.

**A4. TWAP-era latency: "forecast the running average."** [casatrick, 2026-08-03](https://casatrick.substack.com/p/polymarket-twap-latency-trading-bots):
"TWAP genuinely closes that door" (last-tick sniping) but "this is closer to a forecasting problem than a reaction
problem". No thresholds or results. Transfer: high in structure (PM US has always used a 60 s average) — this is
our H-FM final-minute mode. See S3.

### B. Settlement manipulation (international only)

**B1. Dai, Jia & Yu, "Settlement Manipulation in Prediction Markets", arXiv [2606.31675](https://arxiv.org/abs/2606.31675) (2026-06-30).**
821 wallets (≈1 in 300) earned $8.22M in cycles with anomalous Binance one-sided bursts just before 5m settlement;
retail lost $7.61M; Chainlink result matched Binance ~85% of the time
([Cointelegraph](https://cointelegraph.com/news/stanford-study-exposes-market-manipulation-in-polymarkets-short-term-bitcoin-contracts) coverage).
Follow-up (Wang, via [CryptoSlate](https://cryptoslate.com/polymarket-reforms-bitcoin-contracts-and-binance-trading-spikes-immediately-drop/)):
final-10-second share of Binance volume 3.05% before 5m launch, 5.68% under single-print settlement, 3.02% after
the 60 s average. Transfer: none as a strategy (and manipulation is off-limits). As a hazard it is small on PM US:
BRTI averages several exchanges over 60 s. Possible side test (low priority): does spot reverse after
:00/:05/:10 boundaries? Not pursued.

### C. Last-seconds / late near-certainty (buy the 85–99¢ favourite)

**C1. Late-window maker/taker at 90–95¢ "T−10 s, direction ~85% determined".** [X snippet](https://x.com/poly_data/status/2042994670964224082)
(">90% win rate, $344,992 PnL, 5,000+ positions"), [Archetapp gist](https://gist.github.com/Archetapp/7680adabc48f812a561ca79d73cbac69)
(7-indicator score, window delta weight 5–7, fire at T−10…T−5 s, GTC at $0.95; no results; assumes Binance
candles settle — wrong), [benjamincup Medium](https://benjamincup.medium.com/unlocking-edges-in-polymarkets-5-minute-crypto-markets-last-second-dynamics-bot-strategies-and-db8efcb5c196) (403),
freelance job "buy when odds ≥ 99 in the last 30–60 s".
Evidence against, with numbers: [layerx (2026-02-04)](https://layerx.xyz/blog/polymarketbots): final-60 s,
80–99¢, ≤3% spread, live $5 bets → **−37.81%** of allocated capital, "no edge identification";
[manja316 176-trade log](https://dev.to/manja316/176-trades-on-polymarket-what-my-bot-actually-made-its-not-what-you-think-3iib):
late momentum (min 8–12, BTC ±0.20%, cap 72¢) ~80% live wins but "Net negative... One reversal erased 3-4 wins";
late snipe (min 13, ±0.10%, 93–97¢) "Net negative". [DeFi Rate Kalshi 15m table](https://defirate.com/odds/crypto/):
50–89¢ favourites win 3–4 pts less than priced, 90–100¢ +1 pt (68–198 windows/bucket).
Status here: **already KILLED on Kalshi 15m** (H1, H2, H3, H15/16, H6). The crowd's high win rates are exactly
the "win rate without price" trap. The only untested variant is the 5m version on PM US (see S3), where the 60 s
average is 20% of the window, so more of the outcome is locked earlier.

**C2. Late momentum "first 3–4 minutes carry into the close" / "5-minute momentum edge".**
[benjamincup Substack, 2026-05-12](https://benjamincup.substack.com/p/the-5-minute-momentum-edge-on-polymarket):
BTC move ≥ 0.5% from the 5m open, enter T−70…T−30 s, price ≥ 5¢ off 50, hold; "65–78% win rates on highly
filtered trades... estimated edge per trade around 0.3–1%" — explicitly "community claims". A 0.5% move in 5 min is
rare (our H4 used ±0.4% over 3–5 min of a 15m window and fired in ~1% of windows). [casatrick 2026-08-31](https://casatrick.substack.com/p/polymarket-trading-bot-strategies-5m-15m):
"[380 trades] [54.2%] [1.8%]" — **bracketed template placeholders**, thresholds withheld. Translation = S3/H4 family.

### D. Cheap side / longshots / mid-window "mispricing"

**D1. Argona "Grok bot", 5m BTC, $10,774 in 23 days (6,692 trades, 20/21 green days).** Source: X post by
@Argona0x via [Digital Zoom Studio, 2026-10-28](https://digitalzoomstudio.net/2026/10/how-spacexai-engineers-run-grok-bot-and-the-polymarket-bot-behind-a-10774-claim/).
Rules: wait until ~2 min left; buy the losing side at 20–40¢ ("still wins 44% of the time"), or the leader at
~50¢; never above 70¢; only when "at least 5 points too cheap after the fee"; "sits out 7 windows in 10"; $14
normal, up to $170. Red flags: wallet not identified, sold as an X-subscription course, the "5 points too cheap"
needs a fair-value model (our H-MM1: models lose to the market). 44% wins at 20–40¢ would be a big edge (break-even
~30–40%) — testable cheaply on 5m. → **S4.**

**D2. "mo-money" 5m, +$139,200 in a month, 32,816 predictions** ([X, Chinese](https://x.com/Mikocrypto11/status/2081265895025103359)).
Snipes 9¢/17¢/23¢ contracts, session-based (Asia mean reversion to VWAP, London sweep, NY trend with volume/ATR),
"never click before the 5-minute candle closes". Showcases four big winners (9¢ → +677%). Red flags: cherry-picked
wins, no win rate, indicator soup. Our H7 (3–20¢ lottery) and H-HOD are KILLED / weak. Not prioritised.

**D3. Buy the ~1¢ side (CHUTSKO/polymarket-btc-backtest, now 404).** Per search snippet: shelved 2026-10-16
because the only profit came from 2026-04-21 → 05-04 and "every other day lost money at realistic order delays".
Matches our H-SB (0.1¢ side won 0/285) and H7. KILLED-equivalent.

**D4. Crypticorn "5-minute strategy I actually use (Tested)"** ([2026-06-29](https://www.crypticorn.com/polymarket-5-minute-strategy/)):
15m market, no entry in first 5 min, direction held ≥ 60 s, spread ≤ 2¢, don't chase if BTC already moved 0.5%,
sell at 85–90¢, sit out if BTC moved < 0.3% in the hour. Claim: "maybe 55–65% of trades win" — no data; sells
an AI odds product. Covered by H17/H-PNW/H-GRID (TP exits lose ~1–2¢). Not prioritised.

### E. Sum-to-under-$1 / both-sides (gabagool family)

**E1. gabagool22 and copies.** [Medium (moonsat)](https://moonsat.medium.com/5-15-minute-up-down-top-3-bots-that-can-print-you-money-strategies-9a815fb64afd)
(403; via search): "resting bids on both sides slightly below mid... total cost around $0.95–0.96", "$824,341 since
Oct 2025"; [Grok on X](https://x.com/grok/status/2043185768105980270). Kunal Doshi (Blockworks) on the 5m launch
(via [KuCoin](https://www.kucoin.com/news/flash/polymarket-s-5-minute-btc-market-hits-25-2m-in-40-hours)):
7.1% of addresses bought Yes and No in one window; they were profitable in 80% of windows with median combined price
$0.9823; 72.3% of their orders filled as limits. [manja316](https://dev.to/manja316/176-trades-on-polymarket-what-my-bot-actually-made-its-not-what-you-think-3iib):
98 "true arb" pairs at $0.10–0.30, "The only strategy that made money", but two half-fills "cost $41.50 in 15
minutes" and the whole 176-trade log netted "about $2". Telonex: 100%-maker wallets traded 2,000+ markets.
Transfer: the taker version (both asks < $1) is **impossible on PM US** (single book). The maker version —
bid Up at b and bid Down at d with b + d ≈ 0.96 — is, on a single book, simply a two-sided quote 4¢ wide: that is
our H-MK market-making test, with no rebate. Our H14 (Kalshi) was impractical: both legs filled in 21 of 59.

### F. Market making / spread capture

**F1. achillekrtf/polymarket-mm-backtest** ([GitHub](https://github.com/achillekrtf/polymarket-mm-backtest)) — the
best primary source found. International BTC 5m, 2026-09-05 → 10-04, 8,640 markets, 12.9M taker trades. Taker fees
$3.86M on $205M (188 bp). **Maker edge vs spot fair value: +108 bp at fill, +57 bp after 30 s, +5.5 bp at
resolution (95% CI −9 to +20); with rebates +42 bp (27 to 56).** Simulated two-sided quoter (20 shares/side,
inventory ≤ 100, held to resolution) "Loses in every configuration" under realistic latency. VPIN does not predict
adverse selection. Market price Brier 0.162 vs spot model 0.170 (the market beats the model, as on Kalshi).
Transfer: PM US has no rebate at our tier, so the relevant number is the **+5.5 bp, CI spanning 0**, and a
simulated quoter that loses. Strong prior against H-MK on PM US.
**F2. Telonex one-week study** ([telonex.io](https://telonex.io/research/top-crypto-traders-polymarket-15m),
2026-02-02→08, 2,688 15m markets, 15.3M fills, 46,945 wallets): "Takers won 53% of fills but lost money overall";
makers +$728,501, takers −$728,501, pre-fee; 36.8% of wallets profitable; biggest loser a majority-maker wallet
(−$139,971). Already in LEARNINGS as external evidence.
**F3. poly-maker (warproxxx)**: profitability "relies on... Liquidity Rewards" — rewards, not spread. Not on PM US.

### G. Cross-venue and cross-market arbitrage

**G1. Polymarket ↔ Kalshi.** [CarlosIbCu/polymarket-kalshi-btc-arbitrage-bot](https://github.com/CarlosIbCu/polymarket-kalshi-btc-arbitrage-bot)
(hourly, compares "Price to Beat" with Kalshi strike, "If the total cost is less than $1.00, a risk-free profit
opportunity exists"; no fees, no results, no settlement-mismatch handling); dev.to pieces (no numbers). On the
international venue this is NOT risk-free (Chainlink/Binance vs BRTI). **On PM US vs KXBTC15M it is:** identical
fixing to the cent (3,293/3,293). → **S2.**
**G2. 5m vs 15m "inefficiencies" between Polymarket windows** ([indiehackers](https://www.indiehackers.com/post/building-a-polymarket-arbitrage-bot-exploiting-inefficiencies-between-5-minute-and-15-minute-crypto-markets-a6b2af442f),
403, title only); casatrick: "Track several overlapping averaging windows when running 5-minute, 15-minute, and
4-hour contracts together". Nobody publishes numbers. On PM US the nested windows provably share the final
fixing → **S1** (our top idea).
**G3. Polymarket price as a signal for exchange trades.** [Tsybko, 2026-08-06](https://viktoriatsybko.substack.com/p/polymarket-predicts-btc-direction):
43,864 5m and 25,907 15m international markets; "The direction is right" (realized win rates track implied
probabilities) "But wins are 10-15% smaller than losses"; 5m ≥ 3¢ skew +115% at zero fees → **−94% at 0.015%
maker fee per side**. Independent confirmation that these markets are calibrated.

### H. Models / signals (volatility, TA, ML, order flow)

**H1. Spot z-score logistic** ([rolflelieveld predictor](https://rolflelieveld.com/BTC/predictor/)): z = log-return
since open / realised vol, ~8,800 windows; directional accuracy 65.7% at minute 1, 70.9% at minute 2 (vs a coin,
not vs the market); author: "5-minute BTC direction is mostly noise... edges a few points over chance once
Polymarket's fees and slippage are subtracted". This is our random-walk model; H-MM1 KILLED it vs the market.
**H2. ML order-flow bots** (X: "73% win rate... 60,452 trades in backtesting", [0xPhilanthrop](https://x.com/0xPhilanthrop/status/2040752696760610963);
"56% win rate, 68,397 predictions, +$127,020 in 85 days... fast market-making flow" [Dan1ro0](https://x.com/Dan1ro0/status/2078561762681721079)).
Copy-trade links, no prices paid. Our O2 (taker imbalance) KILLED.
**H3. Reddit 5m paper bot (May 2026)** ([snapshot](https://reddit.sentinel-team.org/posts/1tpu2nz/snapshots/2026-05-28T21%3A56%3A49.980206Z)):
baseline "looked mildly profitable under naive assumptions, but progressively died as execution realism
increased"; edge "dies with ~0.01 additional slippage"; median quote freshness ~1.5 s, p95 ~67 s. Same lesson as
our stale-quote paper record.
**H4. layerx CEX momentum** (2-min Binance momentum 0.2–0.5%, max entry 55–65¢): paper 36.7% wins on Gamma bid
prices; live "complete failure... fantasy profits due to API price mismatch" (bid used in paper, ask in live).
**H5. Order-book imbalance**: no study found for Polymarket short-term markets (only spot-market OFI papers and
the Polymarket-v1 database paper, [arXiv 2606.04217](https://arxiv.org/abs/2606.04217), which finds trade-sign
rules ~50% accurate). Our O2 KILLED on Kalshi.

### I. Copy-trading top wallets

[Ratio blog, 2026-03-10](https://ratio.you/blog/copy-trading-polymarket-speed-markets-btc-5min): algorithm wallets
52–55% wins entering < 10 s after open ("almost certainly" not copyable), "even a 30-second delay in copying a trade
can destroy your edge", needs 500+ trades to separate skill from noise. Lookonchain: three wallets $2.35M in a month
([index.vn](https://index.vn/en/news/bitcoin-bets-on-polymarket-generate-dollar235-million-in-profit-across-three-wallets-in-february-2026)).
Transfer: **none** — PM US has no public wallets. Not testable.

### J. Gambling systems
Martingale on 5m ([X, bored2boar](https://x.com/bored2boar/status/2058500601462288393)) — sizes up a fair coin;
our H-STK shows no streak edge. Not a strategy.

## Crowd claims vs evidence

| Claim (typical) | What is actually evidenced |
|---|---|
| 80–99% win rates (late favourite, "Claude bots") | Win rates without prices. Every priced test (ours H1/H2/H6/H15; layerx −37.81%; manja316 net ≈ $0) loses about the fee |
| Latency arb made $400k+ | One pre-fee wallet in Dec 2025; fees were added to kill it; now 50–150 ms taker delay and sub-100 ms bots |
| Both-sides arb is "risk-free" | Real on international separate books (median combined $0.9823, Doshi), but legging risk dominates small bots; impossible on single-book PM US |
| Makers win | Pre-fee, on international with rebates (+42 bp). Without rebates +5.5 bp, CI spans 0; a simulated quoter loses with realistic latency |
| Markets are mispriced | Three independent calibration checks (Tsybko 70k markets, achillekrtf Brier, ours on Kalshi) say they are calibrated |
| Momentum in the first minutes carries | True directionally (the market prices it); our H17/H5/H8 show no edge after the price |

## Ranked shortlist (most testable and plausible after Polymarket US fees first)

1. **S1 — Nested-window no-arbitrage box on PM US (5m inside 15m inside 1h).** Structural: shared final fixing,
   zero free parameters, payoff floor $1 with a $2 bonus. No public source tests it on PM US. Testable from today's
   recorder data.
2. **S2 — Cross-venue PM US 15m ↔ Kalshi KXBTC15M lock.** Same fixing to the cent; risk is execution/legging and
   two funded accounts. Testable from today, if `kalshi_quotes` is second-resolution with both sides.
3. **S3 — 5m final-minute in-flight-average favourite (PM US).** The crowd's late-favourite idea is dead on 15m,
   but on 5m the averaging minute is 20% of the window and 288 windows/day make it fast to test. Needs a fresh
   index (our 1-s index is 2.7–3.6 s late).
4. **S4 — Argona 5m late cheap side (20–40¢ at T−120 s).** A cheap, specific kill test of a current, popular
   claim; most likely to die.
5. Market making on PM US (H-MK, already in the shadow lab): external evidence is negative (F1); keep the lab
   running, no new test.

Not shortlisted: latency arb (needs co-location and a new spot recorder), copy-trading (impossible), sum-under-$1 on
one market (impossible), settlement manipulation (off-limits), lotteries/streaks/TA (already killed).

## Pre-registered test specs

Common rules (AGENTS.md): entries only at prices that really showed in the WebSocket book (`pm_ws_quotes`, ask for
buys) at or after the decision time, plus 1¢ slippage per leg; **PM US fee 0.0695·C·P·(1−P) rounded to the NEAREST
cent per order** (half up; we observed 2¢ on 1-lots and 2¢ on 2 @ 22¢ — size used for scoring: 10 contracts, fee
rounded to the nearest cent per 10-lot; corrected 2026-11-01 from "rounded UP", before any of these specs was scored) and Kalshi
fee 0.07·C·P·(1−P) rounded up to $0.0001; official `pm_windows` / Kalshi results; one entry per window (per nested
group for S1/S2); t clustered by window; chronological first 2/3 of days = train, last 1/3 = holdout; survivor only
if holdout net > 0, both chronological holdout halves > 0, n ≥ 30 holdout trades, and t above the Bonferroni bar the
harness prints. A survivor is a CANDIDATE → shadow → pennies. Windows with null strike/settlement or a recorder gap
> 5 s in the decision second are excluded *before* looking at outcomes.

### S1 — PM-NEST: nested-window box

- **Groups.** (a) each 15m window W and its third 5m sub-window V (V starts at W+10 min; both settle on the same
  fixing E). (b) each 1h window H with its last 15m window and its last 5m window. Strikes: S_long = W's
  `price_to_beat`, S_short = V's `price_to_beat` (known from V's open, i.e. the 5m#2 fixing).
- **Legs.** If S_short ≥ S_long: buy W Up (long) + buy V Down (short). Else: buy V Up + buy W Down. Payoff is $1 in
  all cases and $2 when E falls between the strikes (inclusive at the Up strike, ≥ rule).
- **Entry (A, pure lock).** At any second from V's open + 5 s to V's close − 5 s, enter once if
  `ask_leg1 + ask_leg2 + 2¢ slippage + fees < $1.00` with ≥ 10 contracts displayed on both legs in the same
  1-second snapshot *and* in the next snapshot (≥ 1 s later; models our reaction latency). Down ask = 1 − Up bid
  with the bid's depth.
- **Entry (B, EV variant, frozen now).** Same, but threshold `cost + fees < 1 + 0.5·p_between`, where p_between =
  max(0, mid_long_Up − mid_short_Up) for case S_short ≥ S_long (mirror otherwise), measured at the same second.
  B is the only tunable thing; no tuning — 0.5 is fixed now.
- **Exit.** Hold to settlement. No stops.
- **Pass bar.** A: report events/day, mean net ¢ per pair, fraction of the opportunity that the next-second
  snapshot confirms. Pass = ≥ 30 confirmed holdout events and net > 0 (it is riskless if filled, so the question is
  frequency and fillability; t is reported but A cannot lose at settlement). B: standard bar above.
- **Data.** `polymarket_us.db` pm_ws_quotes (5m/15m/1h) + pm_windows. Groups/day: 96 (a) + 24 (b).
- **Dates.** First read **2026-11-05** (7 days ≈ 670 (a) + 168 (b) groups; frequency only). Train/holdout read
  **2026-11-19** (21 days ≈ 2,000 (a) groups; holdout ≈ 670).
- **Kill if** confirmed events < 1/day or net ≤ 0 after fees at the next-second price.

### S2 — XV-LOCK: PM US 15m ↔ Kalshi KXBTC15M

- **Legs.** Same window (PM `window_start` = Kalshi `open_ts`). Option 1: buy PM Up + buy Kalshi No. Option 2: buy
  Kalshi Yes + buy PM Down. Payoff exactly $1 (identical fixing and tie rule).
- **Entry.** First second in [open + 5 s, close − 30 s] where `cost + 2¢ slippage + PM fee + Kalshi fee < $1.00`,
  ≥ 10 contracts on both, confirmed in the next snapshot on both venues (clock-aligned on recv time; Kalshi
  book from `btcflow.db:kalshi_quotes` — verify it is ≤ 1 s resolution with depth before running; if not, start
  recording the Kalshi orderbook WS alongside the PM recorder).
- **Extensions (same spec, separate configs).** (i) Kalshi 15m vs PM 5m#3 with the S1 box payoff. (ii) Kalshi
  KXBTCD "above K" at the hour vs PM US 1h / last-15m / last-5m Up (same fixing, 705/705): for strikes K ≥ S buy PM
  Up + KXBTCD No(K), for K ≤ S buy KXBTCD Yes(K) + PM Down — payoff floor $1, $2 between strikes. Needs a live
  KXBTCD orderbook recorder (we only have its candles); 24 hours/day.
- **Exit.** Hold. **Pass bar** as S1-A. **Dates** as S1 (96 windows/day): first read 2026-11-05, final 2026-11-19.
- **Risk notes.** Capital locked on two venues; settlement mismatch only at PM US launch (one flip, 2026-09-22).

### S3 — 5M-FM: final-minute in-flight-average favourite on PM US 5m

- **Rule.** At T−20 s (40 of the 60 settlement seconds fixed), compute the in-flight average A40 of a live BRTI
  proxy (≥ 3 constituent exchanges' mid, < 500 ms old) and project E = (40·A40 + 20·spot)/60. If |E − strike| ≥
  2.5·σ_20 (σ_20 = 20-second spot vol scaled to 20 of 60 s weight), buy that side at the ask if ask ≤ 97¢; hold.
- **Pass bar.** Standard; report win % vs break-even % and the proxy-vs-official result mismatch rate.
- **Data.** pm_ws_quotes 5m + a **new** spot recorder (our `index_1s` is too stale; using it would be look-ahead in
  reverse/unfair). 288 windows/day; expect ~30–50% to qualify.
- **Dates.** Starts when the spot recorder starts (day D). Final read D + 9 days (≈ 2,600 windows, holdout ≈ 860).
  If no new recorder: **not testable**; run the Kalshi 15m lab H-FM instead.
- **Prior.** Weak: Kalshi market beats our model most at 1 min (0.083 vs 0.123 log loss).

### S4 — ARGONA: 5m late cheap side

- **Rule.** At T−120 s (first snapshot at or after), if one side's ask is in [20¢, 40¢], buy it; hold. (The
  "5 points too cheap after the fee" filter needs a fair-value model; our model loses to the market, so the plain
  rule is tested; a variant with our model's fair − ask ≥ 5¢ + fee is a second, separately counted config.)
- **Pass bar.** Standard. Crowd claim to compare: 44% wins.
- **Data.** pm_ws_quotes 5m + pm_windows (spot not needed for the plain rule).
- **Dates.** 288/day, fires maybe 40% → final read **2026-11-07** (9 days ≈ 2,600 windows; holdout ≈ 860).
- **Expected.** KILL (H7, H17 and calibration evidence).

## Transcripts / pages the user could paste

YouTube blocks transcript fetches from here (youtubetranscript.com returned "YouTube is blocking subtitle
retrieval"); x.com and some Medium/indiehackers pages return 403. Worth pasting:

- https://www.youtube.com/watch?v=5ftDQdRoFvM — "I Built a 76%+ Win Rate Polymarket Bot (BTC Up or down 5 min)"
- https://www.youtube.com/watch?v=1jByDAWZWic — "I Found the Only Polymarket Bot That Actually Works on BTC 5-Min Markets"
- https://www.youtube.com/watch?v=8u6jy8v56ww and https://www.youtube.com/watch?v=upN8hNXVGNI — "Polymarket BTC 5-minute trading strategy research"
- https://www.youtube.com/watch?v=iXBXUGAngbU — "Polymarket Bot That Steals 5% Every 5 Minutes w/ Claude"
- https://www.youtube.com/watch?v=Yp3gpNXF2RA — latency-arb bot demo linked from several dev.to posts
- https://www.youtube.com/watch?v=npjKgDlxpks, https://www.youtube.com/watch?v=YKlEhqU-1mc
- X: Argona0x's post/article behind the $10,774 claim (wallet address is in a reply); Kunal Doshi's 5m launch
  analysis; https://x.com/BimbaCrypto/status/2050300061272113172 (baloneigh, 14,282 trades)
- https://moonsat.medium.com/5-15-minute-up-down-top-3-bots-that-can-print-you-money-strategies-9a815fb64afd
- https://www.indiehackers.com/post/building-a-polymarket-arbitrage-bot-exploiting-inefficiencies-between-5-minute-and-15-minute-crypto-markets-a6b2af442f
- arXiv 2606.31675 full text (settlement manipulation; useful for the post-boundary reversal side question)


## S6 "Mega liquidation rider" (YouTube transcript pasted by the user, 2026-10-29)

Claim (creator's own words): a $5-size live bot on Polymarket 5-minute BTC that scans "trailing 2-minute one-sided
liquidations over $500,000 per asset" on perp exchanges and "rides the flush" — after a large LONG liquidation, bet
the price keeps flushing down (and vice versa). He shows a live LOSS on camera, says "nobody's going to give you a
profitable trading bot ... bring your own edge", and gives no results. Code shown is infrastructure (order placement,
cancel, positions, kill switch, Gamma API winner lookup), not evidence.
Testable rule (pre-registered): at any second t in a window with >= 60 s left, if one-sided liquidations over the
trailing 120 s exceed $500k (long liqs -> buy Down; short liqs -> buy Up) and that side's ask is in 5-95c, buy at the
ask + 1c, one trade per window, hold to settlement; score per AGENTS.md (real asks, fees, clustered t, chronological
split). Related prior: order-flow O1-O3 (KILLED) found moves continue slightly (1-2c) but below round-trip cost.
Data: Binance/Bybit liquidation feeds are geo-blocked here (HTTP 451/403); OKX public liquidation-orders (REST + WS)
and Hyperliquid are reachable — OKX alone is a partial view of market-wide liquidations. Needs a liquidation recorder
(not built); earliest read ~7 days after it starts. Prior expectation: KILLED (the price already reflects the flush).
