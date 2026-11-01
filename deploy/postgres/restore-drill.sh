#!/usr/bin/env bash
# Restore drill: prove a dump from the `backup` service restores. Starts a throwaway Postgres (the same image as
# compose.yaml, no published port), restores the dump into it, runs smoke queries, prints what it found and removes
# the container. Touches nothing of the running stack (the backups volume is mounted read-only).
#
#   bash deploy/postgres/restore-drill.sh                     the newest dump in the backups volume
#   bash deploy/postgres/restore-drill.sh settleview-2026-11-01.dump
#
# Environment: BACKUP_VOLUME (default settleview_settleview-backups: compose project "settleview"), PG_IMAGE (default:
# the image compose.yaml pins). Exit 0 when the dump restored and the schema is at an Alembic version; 1 otherwise.
# CI runs it after a real `backup --once` (.github/workflows/ci.yml, "image" job).
set -euo pipefail

VOLUME="${BACKUP_VOLUME:-settleview_settleview-backups}"
IMAGE="${PG_IMAGE:-postgres:17.10-bookworm}"
NAME="settleview-restore-drill-$$"
DUMP="${1:-}"

cleanup() { docker rm -f "$NAME" > /dev/null 2>&1 || true; }
trap cleanup EXIT

docker run -d --name "$NAME" -e POSTGRES_PASSWORD="drill-$$-$RANDOM" -e POSTGRES_DB=drill \
  -v "$VOLUME":/backups:ro "$IMAGE" > /dev/null
for _ in $(seq 1 60); do
  docker exec "$NAME" pg_isready -U postgres -d drill > /dev/null 2>&1 && break
  sleep 1
done
docker exec "$NAME" pg_isready -U postgres -d drill > /dev/null

if [ -z "$DUMP" ]; then
  DUMP="$(docker exec "$NAME" sh -c 'ls -1 /backups/settleview-*.dump 2>/dev/null | sort | tail -n 1')"
  [ -n "$DUMP" ] || { echo "restore drill: no dump in volume $VOLUME" >&2; exit 1; }
  DUMP="$(basename "$DUMP")"
fi
echo "restore drill: restoring $DUMP into a fresh $IMAGE"

# Ownership and grants name roles this throwaway server does not have: restore the data and schema only.
docker exec "$NAME" pg_restore -U postgres -d drill --no-owner --no-privileges --exit-on-error "/backups/$DUMP"

docker exec "$NAME" psql -U postgres -d drill -v ON_ERROR_STOP=1 -At -c "
  select 'alembic version: ' || version_num from app.alembic_version;
  select 'app tables: ' || count(*) from information_schema.tables where table_schema = 'app';
  select 'research tables: ' || count(*) from information_schema.tables where table_schema = 'research';
  select 'rows: trades ' || (select count(*) from app.trades) || ', index_1s ' || (select count(*) from app.index_1s)
         || ', trading_brackets ' || (select count(*) from app.trading_brackets)
         || ', agent_sessions ' || (select count(*) from app.agent_sessions);"
echo "restore drill: OK"
