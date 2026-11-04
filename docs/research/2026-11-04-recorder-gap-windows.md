# Recorder gap windows (2026-11-04)

## Rule for research

Exclude or flag any closed BTC Polymarket window (15m, and 1h when checking longer spans) whose **last quote is more than 60 seconds before window close**, or that has **no quotes at all**. Those windows are incomplete for backtests and for any study that needs the closing path.

Reusable SQL: `scripts/research/flag_gap_windows.sql` (read-only against `research.pm_windows` + `research.pm_ws_quotes`).

## Cause (engineering)

`book_silence()` only fired while trades kept arriving. A markets socket that went fully silent mid-window was not reconnected until Polymarket's server 1001 "going away" at the next window boundary. Fix (branch `fix/recorder-all-quiet-reconnect`, not deployed): reconnect when a connection that already received data gets no quote or trade message for `ALL_QUIET_S` (30 s).

## Snapshot used for the flag list

- Queried live Postgres read-only on 2026-11-04 (~12:30 CT).
- Closed 15m and 1h windows only (`window_end < now()`).
- Flagged when `missing_seconds > 60` or `gap_kind = no_quotes`.
- Date range of flagged windows: **2026-10-29T11:00:00Z → 2026-11-04T18:15:00Z** (see CSV).
- Counts in that snapshot: **103 × 15m**, **75 × 1h** (178 total).
- The CSV is kept **out of the public tree** (operational snapshot); regenerate anytime with the SQL above.

No account data, keys, balances, or hostnames are in the CSV.
