#!/usr/bin/env bash
# The `backup` service (compose.yaml): a nightly `pg_dump -Fc` of the Settleview database to /backups (the
# `settleview-backups` volume), keeping the 7 newest daily dumps plus the newest of each of the 4 ISO weeks before
# them (the same rule as the app's SQLite backups). Runs in the official postgres image, so pg_dump matches the server.
#
#   bash backup.sh            run forever: a dump soon if today's is missing (after BACKUP_START_DELAY), then every
#                             day at BACKUP_AT (UTC)
#   bash backup.sh --once     one dump now (today's file is replaced), prune, exit (manual backups, CI's drill)
#
# Settings (environment): PGHOST, PGPORT, PGDATABASE, PGUSER (settleview_backup), PGPASSWORD_FILE (a secret file),
# BACKUP_AT (HH:MM UTC, default 03:17: a quiet minute), BACKUP_START_DELAY (seconds, default 300: the start-of-day dump
# waits this long after the service starts, so a deploy's migrations are not behind pg_dump's table locks;
# docs/solutions/2026-11-01-migration-vs-backup-lock.md), BACKUP_DIR (default /backups), BACKUP_HOOK (optional: an
# executable run with the new dump's path after each good dump, for an off-site copy; its failure is logged, the dump
# stays). Each dump is written to <name>.part, checked with `pg_restore --list`, then renamed: a file named
# settleview-YYYY-MM-DD.dump is always complete. <dir>/last-ok holds the time of the last good dump (the healthcheck).
set -euo pipefail

DIR="${BACKUP_DIR:-/backups}"
AT="${BACKUP_AT:-03:17}"
START_DELAY="${BACKUP_START_DELAY:-300}"
case "$START_DELAY" in ''|*[!0-9]*) echo "backup: BACKUP_START_DELAY must be whole seconds" >&2; exit 78 ;; esac
KEEP_DAILY=7
KEEP_WEEKLY=4

log() { echo "$(date -u +%FT%TZ) backup: $*"; }

if [ -n "${PGPASSWORD_FILE:-}" ]; then
  [ -s "$PGPASSWORD_FILE" ] || { log "the password file $PGPASSWORD_FILE is missing or empty"; exit 78; }
  PGPASSWORD="$(tr -d '\r\n' < "$PGPASSWORD_FILE")"
  export PGPASSWORD
fi
mkdir -p "$DIR"

prune() {
  # Newest first; keep 7, then the newest dump of each of the next 4 ISO weeks.
  local kept=0 weeks="" n=0 f d w
  for f in $(ls -1 "$DIR"/settleview-????-??-??.dump 2>/dev/null | sort -r); do
    n=$((n + 1))
    if [ "$n" -le "$KEEP_DAILY" ]; then continue; fi
    d="$(basename "$f" .dump)"; d="${d#settleview-}"
    w="$(date -u -d "$d" +%G-%V)"
    case " $weeks " in
      *" $w "*) ;;
      *) if [ "$kept" -lt "$KEEP_WEEKLY" ]; then weeks="$weeks $w"; kept=$((kept + 1)); continue; fi ;;
    esac
    rm -f -- "$f"
    log "pruned $(basename "$f")"
  done
}

dump() {
  local day out t0 size
  day="$(date -u +%F)"
  out="$DIR/settleview-$day.dump"
  t0="$(date +%s)"
  rm -f -- "$out.part"
  # Explicit checks: `set -e` does not apply inside a function called from `dump || ...`.
  if ! pg_dump --format=custom --compress=6 --no-password --file="$out.part"; then
    rm -f -- "$out.part"; return 1
  fi
  if ! pg_restore --list "$out.part" > /dev/null; then  # a readable archive, or no file named .dump
    rm -f -- "$out.part"; return 1
  fi
  mv -f -- "$out.part" "$out" || return 1
  size="$(du -h "$out" | cut -f1)"
  date -u +%s > "$DIR/last-ok"
  log "dumped ${PGDATABASE:-?} to $(basename "$out") ($size) in $(( $(date +%s) - t0 )) s"
  prune
  if [ -n "${BACKUP_HOOK:-}" ]; then
    if "$BACKUP_HOOK" "$out"; then log "hook $BACKUP_HOOK ok"; else log "hook $BACKUP_HOOK FAILED (the dump is kept)"; fi
  fi
}

if [ "${1:-}" = "--once" ]; then
  dump
  exit 0
fi

log "started: daily at $AT UTC into $DIR (keep $KEEP_DAILY daily + $KEEP_WEEKLY weekly)"
# Today's dump file is the "done" marker: at start, a dump (after START_DELAY) unless today's exists; then each day
# once the clock passes BACKUP_AT. A failed dump is retried after 10 minutes. The start delay: pg_dump holds ACCESS
# SHARE on every table for its whole run, and a deploy starts this service with the app, whose migrations need
# stronger locks (they wait it out too, db.migrate; this keeps them from having to).
first=1
if [ ! -f "$DIR/settleview-$(date -u +%F).dump" ] && [ "$START_DELAY" -gt 0 ]; then
  log "no dump today yet: the first one in $START_DELAY s (BACKUP_START_DELAY)"
  sleep "$START_DELAY" &
  wait $!
fi
while true; do
  pause=60
  if [ ! -f "$DIR/settleview-$(date -u +%F).dump" ]; then
    if [ "$first" = 1 ] || [[ ! "$(date -u +%H:%M)" < "$AT" ]]; then
      dump || { log "FAILED (retrying in 10 minutes)"; pause=600; }
    fi
  fi
  first=0
  sleep "$pause" &
  wait $!  # a stop signal ends the sleep at once
done
