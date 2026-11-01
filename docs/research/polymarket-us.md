# Polymarket US BTC Up/Down: history backfill and cross-venue check with Kalshi (2026-10-29)

Polymarket US (CFTC-regulated) lists BTC Up/Down markets in 5-minute, 15-minute and 1-hour windows
(slug `cpc-btc-updown-{5m|15m|1h}-YYYY-MM-DD-HHMMz`, window start in UTC). They settle on CF Benchmarks BRTI, the
simple average of the 60 one-second prices before each instant, with flat = Up. That is the same index and statistic
Kalshi KXBTC15M uses.

- Backfill: `scripts/backfill_polymarket_us.py` (public gateway only, no keys, about 4.5 req/s, resumable) writes to
  `data/research/polymarket_us_history.db` (`pmh_windows`, `pmh_points`). Logs: `data/research/backfill_polymarket_us*.log`.
- Checks: `scripts/quick/polymarket_us_checks.py`, output in `data/research/polymarket_us_checks.out`.
- Live recorder (unchanged): `scripts/record_polymarket_us.py` writes `data/research/polymarket_us.db`. It started
  2026-10-29 12:00Z. Since 2026-10-31 both recorders are package commands
  (`python -m btc_signal_engine.recorders pm-quotes | pm-settle`, run as compose services or under the portable
  supervisor; docs/operations.md); the scripts are thin wrappers with the same tables.

## Coverage

The walk went back until a full day returned 404s, so each span is covered from its launch.

| Span | First window | Last window | Windows (200) | 404 holes inside range | Points stored |
|---|---|---|---|---|---|
| 15m | 2026-09-22 15:15Z | 2026-10-29 12:45Z | 3,500 | 43 | 62,485 |
| 1h | 2026-09-22 15:00Z | 2026-10-29 12:00Z | 884 | 2 | 55,159 |
| 5m | 2026-10-22 16:30Z | 2026-10-29 13:10Z | 1,977 | 0 | 15,626 |

- **404 holes:** the 15m holes are almost all 10:00–12:00Z on Thursdays (09-24, 10-01, 10-08, 10-15, 10-22) plus
  09-25. No markets were listed in those windows, which looks like a weekly maintenance window.
- **Every stored market is `MARKET_STATUS_RESOLVED`.** On the 200 rows, `outcomePrices` (`["1","0"]` = Up) always
  matches settlement ≥ priceToBeat.
- **`priceToBeat` is sometimes null:** 26 times for 15m, 22 for 5m and 2 for 1h, clustered around outages. The
  previous window's `settlementPrice` is the same fixing, and the backfill uses it as a fallback. `settlementPrice`
  is null for 6 windows (15m), all next to outages.

## What the price history is

- **It is a display price, not trades or a mid.** `/v1/price-history` returns `longPrice` (Yes = Up, "normally
  derived from the best ask") and `shortPrice` (No, "normally 1 − best bid"). On non-empty points, long + short is
  1.01 in 78% of 15m points, 1.00 in 15% (a locked book) and 1.02 or more in about 7%. In other words it is the ask
  plus the implied No ask.
- **Resolution is 1 minute at best.** Custom ranges with `fidelity=1` return exactly one point per minute, stamped
  at :00. There are no sub-minute observations even though the docs allow for them. In-window points per window:
  15m 14.86 of 15 (98.9% complete), 1h 59.4 of 60, 5m 4.92 of 5.
- **Placeholders are not prices.** A point of 0/1 means an empty book, which happens before the open and in
  outages. After the close the series is pinned to 1/0 or 0/1 (the settled value) for about 24 hours.
- **Gaps:** windows with no in-window points: 15m 27 (13 of them on 10-05, 8 on 10-29), 5m 29 (25 on 10-29), 1h 4.
  The history store has a 10:10–11:56Z gap on 2026-10-29 even though those markets traded.
- **The timestamp labels the start of the minute; the value is the book at about the end of that minute.** This
  comes from 160 overlapping points, about 15 windows that the live recorder also covers. Error against the
  recorder's ask is smallest when the point stamped T is compared with the book at T+60–70 s (on our clock): mean
  |long − ask| 3.5¢, median 2¢, within 1¢ 47%, exact 15%, p90 8¢. At T+0 it is median 9¢ and mean 14.6¢.
  **Reading the point stamped T as the price at T is about 1 minute of look-ahead.**
- **Precision is limited.** Even at the best alignment only 15% of points equal the recorder's ask exactly, and
  only 34 of 137 (long, short) pairs appear anywhere in our 1-second bid/ask tape within ±1–2 min. The recorder's
  ask moves a median 4¢ (p90 15¢) in 30 s, so a one-per-minute snapshot taken at an unknown second is only good to a
  few cents. The display long sits about the mid (mean +0.1¢, median |.| 1.5¢) because the spread is usually 1¢
  (recorder median 1.0¢, mean 1.1¢).

## Settlement agreement with Kalshi KXBTC15M

The join is Polymarket 15m `windowStart` = Kalshi `open_ts`. Kalshi tickers are named by ET close, so
KXBTC15M-26OCT290815-15 is the Polymarket 12:00Z window. There are 3,452 overlapping settled windows. 48 Polymarket
windows have no Kalshi row, mostly 2026-09-24 07:00Z onward, which is a hole in our Kalshi table.

| Period | n | priceToBeat = floor_strike (to the cent) | settlementPrice = expiration_value | Result agrees |
|---|---|---|---|---|
| Launch, 2026-09-22 15:15Z → 09-24 06:45Z | 159 | 3 / 158 | 3 / 157 | 158 / 159 |
| From 2026-09-24 07:00Z | 3,293 | 3,265 / 3,268 | 3,286 / 3,289 | **3,293 / 3,293** |

- **Launch period:** the two venues' fixings differ by cents up to $2.67 (median $0.25), so Polymarket was
  computing its own value then. One result disagrees: `cpc-btc-updown-15m-2026-09-22-1515z` /
  KXBTC15M-26SEP221130-30. Polymarket had 86,291.74 → 86,291.34 and resolved Down; Kalshi had 86,291.01 → 86,291.30
  and resolved Yes (Up).
- **Since 2026-09-24 07:00Z:** the numbers are identical to the cent apart from 3 fixings that differ by
  $0.01–$0.12. Each shows up twice, once as a settlement and once as the next window's strike: 09-24 15:45/16:00Z,
  09-24 21:45/22:00Z and 09-28 10:30/10:45Z. None of them flipped a result.
- **Tie rule:** the same on both venues. Kalshi's `rules_primary` says Yes "if the simple average of the sixty
  seconds of CF Benchmarks' BRTI before <close> is **at least** the [average before open]". Polymarket says Up if
  the end price "is greater than or equal to" the start price. There were no exact ties in the overlap.

## What blocks a fair backtest

- **No ask history at tradable resolution.** History gives a 1-minute display snapshot of uncertain timing,
  accurate to a few cents (p90 8¢ at the best alignment). There is no depth, no trade prints and no bid/ask at the
  moment of a decision. Only the live recorder, from 2026-10-29 12:00Z, has real 1-second bid/ask and depth.
- **Look-ahead.** A point stamped T reflects the book at about T+60 s, so a backtest must use the point stamped
  T−60 for a decision at T, or better, use recorder data only.
- **Fees.** Polymarket US markets carry `feeCoefficient` 0.0695, about Kalshi's 0.07. The exact formula and
  rounding are not verified.
- **Short history.** 5m has only 7 days. 15m and 1h have about 37 days, with weekly Thursday 10–12Z holes and
  outage days (10-05, 10-29).
- **Settlement outcomes are reliable for cross-venue work.** For KXBTC15M results the two venues are
  interchangeable from 2026-09-24 onward (3,293 of 3,293).
