# What the first real SQLite → Postgres dry run found (2026-10-31)

The suite passed on SQLite and compiled for Postgres, but the first run on Docker with the live data (a copy of the
1.9 GB `btcflow.db` and the 0.6 GB `polymarket_us.db`) still found seven things. None lost data; each stopped loudly.

| Found | Cause | Fix |
|---|---|---|
| `BTCFLOW_LOG_FILE` stopped every container (exit 78) | The `<NAME>_FILE` read-from-file convention claimed a setting whose own name ended in `_file` | Renamed to `BTCFLOW_FILE_LOG`; a test fails if any setting name ends in `_file` |
| `docker compose build` asked for `--allow=fs.read=Dockerfile` | Folder typed `documents` but really `Documents`; Bake compares paths case-sensitively | Build from the real-case path, or `docker build -t settleview:local .` (compose-bake-path-case note) |
| NaN test failure | SQLite stores a NaN REAL as NULL, so the source never held NaN | Test expects NULL on both sides |
| `trading_brackets.setup_key`: "a str that is not a whole number" | A BIGINT column that also held the removed strategy lab's text keys; SQLite allowed both | Migration 0018: the column is text; the move tool turns numbers into text for text columns |
| `trading_settings`: checksum range 7 vs 6 rows | SQLite compares text bytewise, Postgres by locale; "between lo and hi" picked other rows | Verification compares text keys `COLLATE "C"` on Postgres |
| backup service: `set: pipefail: invalid option name` | This checkout had CRLF in `backup.sh` (mounted into the container as a config) | Re-checked out with LF (`.gitattributes` says `eol=lf`); a fresh clone is fine |
| Record "today" differed by 5 h | The container's local day is UTC | `TZ` in compose (from `.env`, default UTC) |

Timing on a Windows ARM64 dev host (Docker Desktop): 47 tables, 19.5M rows, about 5 minutes (trades 13.6M rows in 58 s;
`pm_trades` 612k rows with raw JSON in 122 s). A dump of the result: 248 MB in 30 s; the restore drill passed.
Record (all time, week, today) on the copy equalled the live SQLite app to the cent once TZ was set.

**How to avoid:** run the move on a copy of the real data before any cut-over; tests with generated data don't
contain the oddities years of SQLite's loose typing leave behind.

## The real cut-over (same day, 13:34–13:48 CDT, ~14 min down)

Final SQLite backups 18 s; move 266 s (47 tables, 19,566,050 rows, all verified); Record equal to the SQLite app to
the cent; first dump 249 MB in 32 s. Two more things only the real run showed:
- **The image lacked `docs/agent/default-system-prompt.md`** (read at startup when agent mode is on; the keyless dry
  run had agent mode off). Fixed in the Dockerfile and `.dockerignore`. Dry runs should use the real settings with
  trading off, not an empty `.env`, so every startup path runs.
- **A container whose first start failed (port 8001 still held by the dry-run app) kept broken networking** ("failed
  to resolve host 'postgres'") until `docker compose up -d --force-recreate app`. Remove dry-run stacks before
  starting the real one.
