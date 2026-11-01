# A deploy crash-looped the app behind the backup's pg_dump

Found at the live deploy of 2026-11-01.

**Problem.** After `docker compose up -d`, the app exited six times in a row with `canceling statement due to lock
timeout` in migration 0019 (`ALTER TABLE perp_orders ...`), then started normally about 35 s later. Nothing was wrong
with the migration.

**Cause.** The `backup` service was recreated with the app. Its loop dumps at start when today's dump is missing, so
`pg_dump` started at the same moment as the app's migrations. pg_dump takes ACCESS SHARE on every table it dumps and
holds them until it finishes (~33 s on the live database). ALTER TABLE needs ACCESS EXCLUSIVE, which conflicts with
ACCESS SHARE. Every Postgres connection carries `lock_timeout` 2 s (deliberately, so DDL never queues behind a long
query while every other query queues behind the DDL), so each start failed after 2 s and compose restarted the app
until the dump ended.

**Fix.** `db.migrate` treats a lock timeout (SQLSTATE 55P03) as "wait": the whole upgrade, one transaction, rolls back
and is retried after 1, 2, 4, then every 5 s, for up to `BTCFLOW_DB_MIGRATION_LOCK_WAIT_S` (120 s), logging
`migration: waiting for a lock held by pg_dump as settleview_backup, pid N` (from `pg_locks` + `pg_stat_activity`:
application name, role and pid are visible to every role; query text is not, and is not logged). Each attempt keeps
the short 2 s lock timeout, so the property above still holds; a single long `lock_timeout` for migrations was
rejected because the app's own queries would queue behind the waiting ALTER for the whole wait. The backup's
start-of-day dump also waits `BACKUP_START_DELAY` (300 s) after the service starts. A compose
`depends_on: app: service_healthy` for `backup` was rejected: backups must keep running when the app is down, and
Swarm ignores `depends_on`. Tests: `tests/storage/test_pg_timeouts.py` (`..._waits_out_a_dump_lock_...`, a thread
holding ACCESS SHARE as `pg_dump` while 0019 is pending; and the bounded give-up, which leaves the database at 0018).

**How to avoid.** Any service that reads every table (pg_dump, a VACUUM FULL, an analytics export) conflicts with
DDL. Short lock timeouts need a retry around DDL, not a crash; and anything started by the same deploy as the
migrations should not do heavy work in its first minutes.
