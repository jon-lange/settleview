# Startup's trades probe walked the whole primary key (crash-loop on Postgres)

**Problem.** The 2026-11-01 07:18Z deploy (Postgres 17, `app.trades` 13.6M rows, 1.9 GB) restarted three times
before it came up. Each start ran

    SELECT ... FROM trades WHERE trades.ts >= :ts ORDER BY trades.trade_id LIMIT 1

which hit the 5 s `statement_timeout` on a cold cache. The exception left `Runtime.startup()`, the process exited,
and the container restarted. The fourth start found a warm enough cache and got through.

**Cause.** `Runtime.startup()` asked "is any trade from the last 15 h stored?" with the generic
`store.select("trades", filters=[("ts", ">=", ...)], limit=1)`. `select` always appends the table's default order
(for `trades`, the primary key `trade_id`) so that both dialects return rows in the same order. With `ORDER BY
trade_id LIMIT 1`, Postgres picks `trades_pkey`: it expects a match within a few rows. It starts at the oldest trade
and filters by ts. Recent trades have the highest ids, so it read nearly the whole table first. EXPLAIN (ANALYZE,
BUFFERS) on the live database, read-only:

    Limit (actual time=3117 ms)
      -> Index Scan using trades_pkey on trades
           Filter: (ts >= ...)   Rows Removed by Filter: 13580532
           Buffers: shared hit=4059 read=193036          (~1.5 GB read)

That was 3.1 s even with part of the table cached; a cold cache after a restart went past 5 s. A throwaway Postgres
with 13.6M synthetic rows gave the same plan (13.3M rows removed, 161k buffers read).

**Fix.**
- `SqliteStore.has_trades_since(ts)` runs `SELECT EXISTS (SELECT * FROM trades WHERE ts >= :ts)`, with no
  ORDER BY. Live plan: an index-only scan of `ix_trades_ts`, 4 buffers, 0.05 ms. The existing `ix_trades_ts` index
  covers it, so no migration was needed. A `(ts, trade_id)` index would add nothing: hydration's
  `ORDER BY ts, trade_id` already gets an incremental sort over `ix_trades_ts` (38 ms per hour of trades on the
  live data).
- Startup reads that can safely run twice (the probe, `max_trade_id`) go through `startup_read`. On a transient
  database error it retries three times, waiting 2 s and then 4 s, and logs a warning for each failure. It then
  raises after an error line that names the read. Hydration is not retried inside the process, because the engine
  already holds whatever events it read. On failure it logs an error and raises, and the restart tries again.
- Tests: `tests/bootstrap/test_startup_reads.py` (retry behaviour, and the probe on both dialects);
  `tests/storage/test_postgres.py::test_the_startup_probe_reads_the_ts_index_not_the_primary_key` (EXPLAIN of
  the statement the store actually sends, on 200k synthetic rows).

**How to avoid.** On a large table, a filtered query with `limit` that goes through the generic `select` also gets
`ORDER BY <primary key>`. If the filter column is not the primary key, pass `order_by` for that column, or write a
dedicated store method (EXISTS / min / max). Then EXPLAIN it against a table of realistic size. A test run on a
small table will not show this plan.
