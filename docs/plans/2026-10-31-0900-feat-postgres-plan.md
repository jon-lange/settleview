# Postgres: the app's database moves from SQLite to Postgres (a container locally, managed in the cloud)

Status: approved direction (user, 2026-10-31: "Let's migrate to Postgres as well for this. We can run it as a
container."). Follows docs/plans/2026-10-31-0830-ops-portable-runtime-plan.md (P1–P5 first; this is P7). Targets:
Docker Desktop on the PC run like Linux, then Azure Container Apps, AWS (ECS/Fargate) or a self-hosted Swarm.

## Why

SQLite needs one writer on a local disk. Azure Container Apps (Azure Files) and Fargate (EFS) only offer network
file shares, where SQLite's locking and WAL are unsafe. Postgres removes that limit, gives proper backups and
point-in-time restore, lets the recorders, the app and research read/write concurrently, and has managed versions
on both clouds.

## Coupling today (measured 2026-10-31)

- SQLAlchemy Core in 34 modules; Alembic migrations (17 versions). Raw `sqlite3` only in `adapters/sqlite/db.py`
  (the trades archive). About 34 SQLite-specific lines: PRAGMAs, `INSERT OR IGNORE`, `json_each`, `lastrowid`.
- Data: `btcflow.db` 1.9 GB — trades 13.55M rows, index_1s 2.82M, decisions 704k, book_1s 232k, kalshi_quotes
  187k, then small tables. Research DBs under `data/research/` (recorder output plus frozen history files).

## Key decisions

- **PD1 — Postgres 17, pinned image tag** (exact minor chosen at implementation, ≥ 2 weeks old). Locally and on
  Swarm: a `postgres` compose service on a named volume. **In the cloud: the managed service** (Azure Database for
  PostgreSQL Flexible Server, Amazon RDS/Aurora PostgreSQL), not a container — Well-Architected reliability and
  operations (patching, backups, failover handled by the platform). The app only sees a connection URL.
- **PD2 — Driver: psycopg 3** (`psycopg[binary]`) under SQLAlchemy. **New package: needs the user's OK.** No
  `win_arm64` wheel for the binary build; fine in Linux containers; on bare Windows ARM64 the app keeps SQLite.
- **PD3 — One code path, two dialects.** Postgres for every deployment; SQLite stays for unit tests and hosts
  without Docker. Dialect-specific SQL goes behind small helpers (`insert_ignore`, upsert, JSON array params).
  CI runs the full suite on both (a Postgres service container in CI).
- **PD4 — Schema by Alembic.** `alembic upgrade head` creates the Postgres schema; migrations are checked to run on
  both dialects (SQLite `batch_alter_table` stays where needed). Types: epoch-ms `BIGINT`, money `NUMERIC` where
  the code already treats it as exact, JSON text → `JSONB` only where queried.
- **PD5 — Recorders write to Postgres** (schema `research`), so the cloud needs no file volume at all. Frozen
  historical research files (`horizons.db`, `kalshi_trades.db`, `polymarket_us_history.db`) stay as read-only
  SQLite files used by offline scripts; they are datasets, not runtime state.
- **PD6 — Roles.** `settleview_app` (read/write its schema), `settleview_research` (read-only, used by
  `scripts/` and the experiments harness — the "research is read-only" rule becomes a database permission),
  `settleview_backup`. Passwords via `*_FILE` secrets; never logged.
- **PD7 — Big tables.** `trades` and `index_1s` get time-range partitions (monthly) or keep today's archive move
  — decided after measuring query plans on the copied data; no behaviour change for the app.

## Units

- **G1 Dialect layer.** Replace SQLite-only SQL with dialect-neutral SQLAlchemy (`on_conflict_do_nothing`
  per dialect, `RETURNING` instead of `lastrowid`, JSON params instead of `json_each`); PRAGMAs only on SQLite
  connections. Connection URL setting (`BTCFLOW_DATABASE_URL`, `_FILE` supported); `BTCFLOW_DB_PATH` keeps
  working for SQLite.
- **G2 Migrations on Postgres.** Every Alembic version runs on a fresh Postgres; add a test that upgrades both
  dialects from empty to head and compares the resulting table/column sets.
- **G3 Recorders → Postgres** (`research` schema) with the same columns; research scripts get one read helper
  that opens either a SQLite file read-only or Postgres with the read-only role.
- **G4 Compose.** `postgres` service (named volume `settleview-pg`, healthcheck `pg_isready`, not published to the
  host except optionally 127.0.0.1 for tools), app/recorders depend on it being healthy; Swarm `deploy:` keys.
- **G5 Backups.** `backup` service: nightly `pg_dump -Fc` to the `settleview-backups` volume, keep 7 daily and 4
  weekly, plus a documented and **tested** restore (CI restores a dump into a fresh Postgres and runs a smoke
  query). Off-site copy (S3 / Azure Blob) as a documented hook. Cloud: the managed service's automated backups and
  point-in-time restore instead.
- **G6 Data move tool.** `python -m btc_signal_engine.ops migrate-sqlite-to-pg`: reads `btcflow.db` read-only,
  streams each table with `COPY` in batches, resets sequences, then verifies row counts per table and checksums of
  sampled ranges; resumable per table; refuses to write into a non-empty target unless told.
- **G7 Docs.** README, operations (Postgres runbook: backup, restore, upgrade, role setup), architecture diagram,
  cloud section (managed Postgres per platform), CHANGELOG.

## Cut-over on this PC (with the Docker move, after agent sessions end)

1. Final SQLite backup (as today) and integrity check.
2. `docker compose up -d postgres`; run migrations; run the data move tool; verify counts and checksums.
3. Start app, recorders and backup services on Postgres; check health, Play, Agents, Record, Telegram.
4. Keep the SQLite file untouched for 7 days as the rollback (switch back = one setting), then archive it.

Estimated copy time is measured during G6 on a copy of the data, not guessed.

## Verification

- Full suite green on SQLite and Postgres (CI); Alembic head identical on both.
- Data move on a copy of the live file: all row counts equal, sampled checksums equal, app starts on it and Record
  totals match the SQLite app's to the cent.
- Backup/restore drill passes; the read-only role cannot write.
- Live: one $1 agent trade end to end on Postgres before longer runs.

## Decided (user, 2026-10-31)

1. psycopg 3 (`psycopg[binary]`) approved as the Postgres driver.
2. Cloud uses managed Postgres (Azure Database for PostgreSQL Flexible Server / Amazon RDS); the Postgres container
   is for Docker Desktop and Swarm.

## Build notes (2026-10-31, G1-G7 built on feat/postgres)

Where things are: `adapters/sqlite/` (name kept; both dialects): `urls.py` (BTCFLOW_DATABASE_URL), `db.py` (engines,
schemas `app` / `research`, migrate under an advisory lock, `archive_trades_pg`), `store.py` (per-dialect
`ON CONFLICT`, `RETURNING`, bools, a default order), `tables.py` (`Json()`, unbounded VARCHAR on Postgres,
`trades_archive`, migration 0017), `research_tables.py` + `research.py` (recorders' tables, freshness reader, the
scripts' `readonly_engine`), `pgmove.py` (G6); `deploy/postgres/` (init, backup, restore drill, secrets); compose,
Dockerfile, CI (`postgres` job; the `image` job's roles / backup / restore drill). Runbook: docs/operations.md
"Postgres".

Decisions made while building (each can be changed):
- **PD1**: `postgres:17.10-bookworm` (17.11 was pushed in the last week: under the two-week rule).
- **PD2**: `psycopg[binary]==3.3.6` as the optional extra `postgres`, with a marker that skips Windows ARM64 (no wheel),
  so `pip install -e ".[dev,postgres]"` still works on this PC.
- **PD4 types**: JSON columns stay text on Postgres (`JsonText`: json.dumps/loads, as SQLite), not JSONB: Postgres's
  json types refuse NaN/Infinity, which features and data-quality rows can hold, and nothing queries inside them.
  VARCHAR lengths are dropped on Postgres: SQLite never enforced them, so enforcing them would make a long reason or
  code fail a trading write. Money stays Float (the code treats it as float everywhere).
- **PD7**: no partitions; today's archive move is kept, into `app.trades_archive` in the same database (one transaction
  per batch). Revisit after measuring query plans on the copied data.
- Unordered `select`s get a default order (autoincrement id, else creation time then key, else key) on both
  dialects: Postgres moves an updated row to the end of the heap, where SQLite's rowid kept its place.
- Passwords are separate settings (`BTCFLOW_DATABASE_PASSWORD_FILE`) so one secret file serves both the init script
  and the app; a password in the URL as well is an error. Settings errors no longer echo inputs (`hide_input_in_errors`).
- The recorders write `research` with the app's role (they write); the app reads it with read-only transactions.
- `ops migrate` was added (the cut-over's "run migrations" step and CI use it).

Verified only by compiling the SQL (no Postgres server on this PC): the DDL of every table, every upsert / insert-ignore
statement. Executed only in CI (Postgres service container, compose stack): everything marked `postgres`, the full
suite on Postgres, the init script, the backup script and the restore drill. Not done here: the measured copy time of
the real 1.9 GB file (cut-over step) and the query-plan check for PD7.
