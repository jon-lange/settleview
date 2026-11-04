-- Flag closed Polymarket BTC windows whose last recorded quote is >60s before close
-- (or that have no quotes at all). Read-only. No account data.
-- Usage (from the live postgres container, as postgres):
--   psql -U postgres -d settleview -v ON_ERROR_STOP=1 -f scripts/research/flag_gap_windows.sql
-- Or COPY ... TO STDOUT WITH CSV HEADER for a file.

SET default_transaction_read_only = on;
BEGIN READ ONLY;

WITH closed AS (
  SELECT span, slug, window_start, window_end
  FROM research.pm_windows
  WHERE span IN ('15m', '1h')
    AND window_end < extract(epoch FROM now())
),
lastq AS (
  SELECT c.span, c.slug, c.window_start, c.window_end,
         max(q.recv_ms) AS last_recv_ms
  FROM closed c
  LEFT JOIN research.pm_ws_quotes q
    ON q.slug = c.slug
   AND q.span = c.span
   AND q.recv_ms >= c.window_start * 1000
   AND q.recv_ms <  c.window_end * 1000
  GROUP BY 1, 2, 3, 4
)
SELECT span AS lane,
       to_char(to_timestamp(window_start) AT TIME ZONE 'UTC',
               'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS window_start_utc,
       slug,
       CASE WHEN last_recv_ms IS NULL THEN NULL
            ELSE to_char(to_timestamp(last_recv_ms / 1000.0) AT TIME ZONE 'UTC',
                         'YYYY-MM-DD"T"HH24:MI:SS"Z"')
       END AS last_quote_utc,
       CASE WHEN last_recv_ms IS NULL
            THEN round((window_end - window_start)::numeric, 1)
            ELSE round((window_end - last_recv_ms / 1000.0)::numeric, 1)
       END AS missing_seconds,
       CASE WHEN last_recv_ms IS NULL THEN 'no_quotes' ELSE 'early_stop' END AS gap_kind
FROM lastq
WHERE last_recv_ms IS NULL
   OR (window_end - last_recv_ms / 1000.0) > 60
ORDER BY span, window_start;

COMMIT;
