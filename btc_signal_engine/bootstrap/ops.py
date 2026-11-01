"""Operations commands, the same on Windows, Linux and macOS: `python -m btc_signal_engine.ops <command>`.

  stop [--data-dir D] [--timeout 60] [--hold]
      Stop the app gracefully before a deploy (KTD9; replaces scripts/deploy_stop.ps1, which now calls this). The
      app writes its pid to <D>/app.pid at start; only that file is trusted to find it: the process must be alive and
      really run `-m btc_signal_engine` (not backfill, not a submodule). Then <D>/stop.request is created and the app
      gets --timeout seconds to exit (it flushes its writer, stops its tasks, deletes both files, logs "clean shutdown
      (deploy)" and exits 0). If it has not exited, the FALLBACK is a forced kill of that process (and of a virtual
      environment's python.exe launcher that started it on Windows), said loudly; the stop file is removed so the
      next start does not stop at once. It never kills any other process and never calls the app over HTTP. With no
      valid pid file but some `python -m btc_signal_engine` running, it cannot tell which is the app: nothing is done
      (exit 3).
      --hold also writes <D>/app.hold first, so the supervisor does not start the app again until `release`.
      Exit codes: 0 stopped cleanly (or no app running), 1 still running, 2 stopped by the fallback kill, 3 cannot tell.
  release [--data-dir D] [--name app]
      Remove <D>/<name>.hold: the supervisor starts that child again within a second.
  stop-supervisor [--data-dir D] [--timeout 90]
      Ask the portable supervisor to stop every child and exit (<D>/supervisor.stop); waits for <D>/supervisor.pid's
      process to end. Exit 0 stopped (or none running), 1 still running.
  alive pm-quotes | pm-settle [--max-age S]
      Liveness (the recorders' container healthcheck): exit 0 when that recorder's process-local heartbeat file
      (bootstrap/heartbeat.py, in the temp folder) was touched at most S seconds ago (defaults: 60 quotes, 180
      settlement), else 1 with one line saying why. Reads no database and no network: a venue or database outage is
      not a reason to restart a recorder (that is what `fresh` and the app's data alert report).
  fresh pm-quotes | pm-settle [--max-age S] [--research-dir R]
      Healthcheck: exit 0 when that recorder's newest row is at most S seconds old (defaults: 120 quotes, 600
      settlement), else 1 with one line saying why (no database, no rows, too old). Read-only (SQLite `mode=ro`;
      Postgres read-only transactions in the `research` schema when BTCFLOW_DATABASE_URL is set).

  pair-pc [--data-dir D]
      Pair a browser as this PC: prints a one-time code (good for 5 minutes, one browser) and the link that fills it
      in (http://localhost:<BTCFLOW_PC_PORT, else BTCFLOW_API_PORT>/pair?code=...). Open the link on this PC and press
      Pair; the app sets the PC cookie. Writes only <D>/pc-pairing.json (the code's hash), never calls the app over
      HTTP. Docker: `docker compose exec app python -m btc_signal_engine.ops pair-pc`. A new code replaces a pending
      one.
  signout-pc [--data-dir D]
      Sign every PC browser out (a new <D>/pc-secret): each must pair again. The app picks it up within seconds.

  migrate
      Upgrade the app's database (BTCFLOW_DATABASE_URL, else BTCFLOW_DB_PATH) to the latest schema, as the app does at
      start; on Postgres it also makes the schemas app and research if they are missing. Exit 0 done, 78 bad settings.
  migrate-sqlite-to-pg [--sqlite F] [--archive A] [--research R] [--force-table T ...]
      Copy SQLite files into the Postgres database named by BTCFLOW_DATABASE_URL (the cut-over, docs/operations.md
      "Postgres"): F (default BTCFLOW_DB_PATH) into the schema app, the trade archive A (btcflow-archive.db) into
      app.trades_archive, the recorders' R (polymarket_us.db) into the schema research. Every file is opened
      read-only. Migrates the target to the latest schema, streams each table with COPY, sets id sequences, verifies
      row counts and sampled checksums, and records each verified table so a re-run continues where it stopped. A
      target table with rows that is not recorded as copied is refused unless named with --force-table (emptied
      first). Exit 0 all copied and verified, 1 a refusal or a failed check, 78 no Postgres configured.

  replay-dead-letter [--file F] [--apply]
      Replay the writer's dead-letter file F (default <D>/logs/writer-dead-letter.jsonl: writes the database refused,
      market data shed during a long outage, and what was still queued when the app stopped during one) into the
      app's database, oldest first, through the store's own write methods. Without --apply it only lists what it would
      apply (a dry run). Idempotent: applied writes are recorded in F.replayed and skipped by later runs, and inserts
      ignore rows that already exist; F itself is never changed. Safe while the app runs. Exit 0 nothing left to
      apply, 1 some writes were refused again or the database was unreachable (run it again later), 78 bad settings.

<D> defaults to the folder of BTCFLOW_DB_PATH and <R> to BTCFLOW_RESEARCH_DIR (each printed when used).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from btc_signal_engine.bootstrap import processes

STOP_FILE = "stop.request"
PID_FILE = "app.pid"
SUPERVISOR_STOP_FILE = "supervisor.stop"
SUPERVISOR_PID_FILE = "supervisor.pid"
FRESH_DEFAULT_S = {"pm-quotes": 120, "pm-settle": 600}
# The quotes recorder beats every second; a settlement pass can take a minute (a few 10 s REST lookups).
ALIVE_DEFAULT_S = {"pm-quotes": 60, "pm-settle": 180}


def say(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _settings():
    from btc_signal_engine.adapters.sqlite.urls import use_pg_timeouts
    from btc_signal_engine.bootstrap.config import Settings
    s = Settings()
    use_pg_timeouts(s.pg_timeouts)
    return s


def _data_dir(arg: Path | None) -> Path:
    if arg is not None:
        d = arg.resolve()
        if not d.is_dir():
            raise SystemExit(f"--data-dir {d} is not a folder")
        return d
    d = Path(_settings().db_path).resolve().parent
    say(f"data folder: {d} (from BTCFLOW_DB_PATH)")
    return d


def _read_pid(path: Path) -> int:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return 0


def stop_app(data_dir: Path, timeout_s: float = 60.0, *, hold: bool = False, poll_s: float = 0.5,
             kill_wait_s: float = 2.0) -> int:
    stop_file, pid_file = data_dir / STOP_FILE, data_dir / PID_FILE
    if hold:
        (data_dir / "app.hold").write_text("held by ops stop --hold; remove with ops release\n", encoding="ascii")
        say(f"holding the app: the supervisor will not start it until `ops release` ({data_dir / 'app.hold'})")
    app = None
    pid = _read_pid(pid_file)
    if pid > 0:
        p = processes.process(pid)
        if p is not None and processes.is_app(p.cmdline):
            app = p
    if app is None:
        others = [p for p in processes.process_table() if processes.is_app(p.cmdline) and processes.pid_alive(p.pid)]
        if others:
            warn(f"CANNOT TELL: no valid {pid_file}, but python -m btc_signal_engine is running (PID "
                 f"{', '.join(str(p.pid) for p in others)}). Nothing was done; do not check out new code. Stop the "
                 "app by hand (or deploy code that writes the pid file first).")
            return 3
        say("No app (python -m btc_signal_engine) is running; nothing to stop.")
        stop_file.unlink(missing_ok=True)  # a leftover would stop the next start at once
        pid_file.unlink(missing_ok=True)  # stale: its process is gone
        return 0
    kill = [app.pid]
    # A Windows virtual environment's python.exe is a launcher that runs the real interpreter (the pid file's process)
    # as a child.
    launcher = processes.process(app.ppid) if app.ppid else None
    if launcher is not None and processes.is_app(launcher.cmdline):
        kill.append(launcher.pid)
    say(f"Asking the app to stop (PID {app.pid}) via {stop_file}")
    stop_file.write_text(f"deploy stop requested {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n", encoding="ascii")
    started = time.monotonic()
    deadline = started + timeout_s
    while processes.pid_alive(app.pid) and time.monotonic() < deadline:
        time.sleep(poll_s)
    if not processes.pid_alive(app.pid):
        say(f"App stopped cleanly in {time.monotonic() - started:.1f} s.")
        stop_file.unlink(missing_ok=True)
        return 0
    warn(f"FELL BACK TO A FORCED KILL: the app did not exit within {timeout_s:.0f} s; killing PID "
         f"{', '.join(map(str, kill))}. Rows still in its writer buffer may be lost.")
    for k in kill:
        processes.force_kill(k)
    end = time.monotonic() + kill_wait_s
    while processes.pid_alive(app.pid) and time.monotonic() < end:
        time.sleep(0.1)
    stop_file.unlink(missing_ok=True)
    if processes.pid_alive(app.pid):
        warn(f"Still running after the forced kill: PID {app.pid}")
        return 1
    pid_file.unlink(missing_ok=True)  # the killed process could not delete it
    say("Stopped by the fallback kill.")
    return 2


def release(data_dir: Path, name: str = "app") -> int:
    hold = data_dir / f"{name}.hold"
    if hold.exists():
        hold.unlink()
        say(f"released {name}: the supervisor starts it again")
    else:
        say(f"{name} was not held")
    return 0


def stop_supervisor(data_dir: Path, timeout_s: float = 90.0) -> int:
    pid = _read_pid(data_dir / SUPERVISOR_PID_FILE)
    p = processes.process(pid) if pid > 0 else None
    if p is None or not processes.runs_module(p.cmdline, "btc_signal_engine.supervise"):
        say("No supervisor is running for this data folder; nothing to stop.")
        return 0
    (data_dir / SUPERVISOR_STOP_FILE).write_text("stop requested\n", encoding="ascii")
    say(f"Asking the supervisor (PID {pid}) to stop every child and exit")
    deadline = time.monotonic() + timeout_s
    while processes.pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.5)
    if processes.pid_alive(pid):
        warn(f"The supervisor is still running after {timeout_s:.0f} s (PID {pid})")
        return 1
    say("Supervisor stopped.")
    return 0


def fresh(name: str, max_age_s: float, research, now_ms: int | None = None) -> tuple[bool, str]:
    """`research` is the research folder (SQLite) or a ResearchDb (either store)."""
    from btc_signal_engine.adapters.sqlite.research import ResearchDb
    db = research if isinstance(research, ResearchDb) else ResearchDb(Path(research))
    try:
        newest = db.newest_ms()[name]
    except FileNotFoundError:
        return False, f"{name}: {db.where} not found"
    except Exception as exc:
        return False, f"{name}: the research database could not be read ({type(exc).__name__})"
    if newest is None:
        return False, f"{name}: no rows yet"
    age = ((now_ms if now_ms is not None else time.time_ns() // 1_000_000) - newest) / 1000
    if age > max_age_s:
        return False, f"{name}: newest row is {age:.0f} s old (limit {max_age_s:.0f} s)"
    return True, f"{name}: newest row {age:.0f} s old"


def pair_pc(data_dir: Path, port: int | None = None) -> int:
    from btc_signal_engine.adapters.pc_credentials import PAIRING_TTL_MS, create_pairing_code
    if port is None:
        try:
            s = _settings()
        except ValueError as exc:
            warn(f"configuration error: {exc}")
            return 78
        port = s.pc_port if s.pc_port is not None else s.api_port
    code, _ = create_pairing_code(data_dir)
    say(f"Pairing code: {code}  (good for {PAIRING_TTL_MS // 60_000} minutes, for one browser)")
    say(f"Open on this PC: http://localhost:{port}/pair?code={code}")
    say("Then press Pair. That browser is the PC from now on (no PIN). Sign PC browsers out: ops signout-pc.")
    return 0


def signout_pc(data_dir: Path) -> int:
    from btc_signal_engine.adapters.pc_credentials import rotate_secret_file
    rotate_secret_file(data_dir)
    say("Every PC browser is signed out (new PC secret); the app applies it within seconds. Pair again: ops pair-pc")
    return 0


def alive(name: str, max_age_s: float) -> tuple[bool, str]:
    from btc_signal_engine.bootstrap import heartbeat
    p = heartbeat.path(name)
    age = heartbeat.age_s(p)
    if age is None:
        return False, f"{name}: no heartbeat at {p}"
    if age > max_age_s:
        return False, f"{name}: last heartbeat {age:.0f} s ago (limit {max_age_s:.0f} s)"
    return True, f"{name}: alive ({age:.0f} s since the last heartbeat)"


def migrate_db() -> int:
    from btc_signal_engine.adapters.sqlite.db import migrate
    from btc_signal_engine.adapters.sqlite.urls import describe
    try:
        s = _settings()
    except ValueError as exc:
        warn(f"configuration error: {exc}")
        return 78
    # With an OTLP endpoint (compose passes it to the job) the failure's ERROR record reaches OpenObserve as
    # service settleview-migrate, where the migration_failed alert reads it (docs/operations.md, "Alerting").
    import logging

    from btc_signal_engine.bootstrap import telemetry
    from btc_signal_engine.bootstrap.logs import log_to_stderr
    log_to_stderr()
    try:
        tel = telemetry.setup("settleview-migrate", secrets=telemetry.secret_values(s), instrument=False)
    except ValueError as exc:
        warn(f"configuration error: {exc}")
        return 78
    try:
        say(f"migrating {describe(s.database)} to the latest schema")
        migrate(s.database)
        say("done")
        return 0
    except Exception:
        logging.getLogger("btc_signal_engine.migrate").exception("migration failed")
        return 1
    finally:
        tel.shutdown()  # flushes the last records (bounded)


def migrate_sqlite_to_pg(a: argparse.Namespace) -> int:
    from btc_signal_engine.adapters.sqlite.urls import describe, is_postgres
    try:
        s = _settings()
    except ValueError as exc:
        warn(f"configuration error: {exc}")
        return 78
    if not is_postgres(s.database):
        warn("BTCFLOW_DATABASE_URL is not set: name the Postgres database to copy into (and its password in "
             "BTCFLOW_DATABASE_PASSWORD or BTCFLOW_DATABASE_PASSWORD_FILE)")
        return 78
    try:
        import psycopg  # noqa: F401  (the optional `postgres` extra)
    except ImportError:
        warn("the Postgres driver is not installed: pip install -e \".[postgres]\" (psycopg 3)")
        return 78
    from btc_signal_engine.adapters.sqlite import pgmove
    jobs = [pgmove.app_job(a.sqlite if a.sqlite is not None else Path(s.db_path))]
    if a.archive is not None:
        jobs.append(pgmove.archive_job(a.archive))
    if a.research is not None:
        jobs.append(pgmove.research_job(a.research))
    say(f"copying into {describe(s.database)}: " + ", ".join(str(j.path) for j in jobs) + " (read-only)")
    t0 = time.monotonic()
    try:
        results = pgmove.move(jobs, s.database, force_tables=a.force_table, batch=a.batch, say=say)
    except pgmove.MoveError as exc:
        warn(f"STOPPED: {exc}")
        warn("Tables verified before this stay done; fix the cause and run the same command again to continue.")
        return 1
    copied = sum(r.rows for r in results if r.status in ("copied", "skipped (done before)"))
    say(f"done in {time.monotonic() - t0:.0f} s: {len(results)} tables, {copied} rows in Postgres, every copied "
        "table's row count and sampled checksums equal to the SQLite file's")
    for r in results:
        if r.status == "missing in source":
            say(f"  {r.table}: not in {r.source} (left empty)")
    return 0


def replay_dead_letter(a: argparse.Namespace) -> int:
    from btc_signal_engine.adapters.sqlite.db import read_engine, write_engine
    from btc_signal_engine.adapters.sqlite.store import SqliteStore
    from btc_signal_engine.adapters.sqlite.urls import describe
    from btc_signal_engine.adapters.sqlite.writer import replay_dead_letter as replay
    try:
        s = _settings()
    except ValueError as exc:
        warn(f"configuration error: {exc}")
        return 78
    path = a.file if a.file is not None else _data_dir(a.data_dir) / "logs" / "writer-dead-letter.jsonl"
    say(f"{'replaying' if a.apply else 'dry run (add --apply to write)'}: {path} -> {describe(s.database)}")
    we = write_engine(s.database, name="settleview-replay")
    try:
        res = replay(path, SqliteStore(we, read_engine(s.database, name="settleview-replay-read")), we,
                     apply=a.apply, say=say)
    finally:
        we.dispose()
    if a.apply:
        say(f"done: {res.applied} writes applied, {res.skipped} applied before, {res.refused} refused")
    else:
        say(f"dry run: {res.pending} writes would be applied, {res.skipped} applied before, {res.refused} cannot be")
    return 1 if (res.stopped or res.refused) else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m btc_signal_engine.ops", description="Settleview operations.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("stop", help="stop the app gracefully before a deploy")
    s.add_argument("--data-dir", type=Path)
    s.add_argument("--timeout", type=float, default=60.0)  # compose's stop_grace_period
    s.add_argument("--hold", action="store_true", help="keep the supervisor from starting it again until release")
    r = sub.add_parser("release", help="let the supervisor start a held child again")
    r.add_argument("--data-dir", type=Path)
    r.add_argument("--name", default="app", choices=("app", "pm-quotes", "pm-settle"))
    ss = sub.add_parser("stop-supervisor", help="stop the portable supervisor and its children")
    ss.add_argument("--data-dir", type=Path)
    ss.add_argument("--timeout", type=float, default=90.0)
    pp = sub.add_parser("pair-pc", help="print a one-time code that pairs a browser as this PC")
    pp.add_argument("--data-dir", type=Path)
    pp.add_argument("--port", type=int, help="the port in the printed link (default: BTCFLOW_PC_PORT, else "
                                             "BTCFLOW_API_PORT)")
    so = sub.add_parser("signout-pc", help="sign every paired PC browser out")
    so.add_argument("--data-dir", type=Path)
    sub.add_parser("migrate", help="upgrade the app's database to the latest schema")
    m = sub.add_parser("migrate-sqlite-to-pg", help="copy the SQLite files into Postgres (the cut-over)")
    m.add_argument("--sqlite", type=Path, help="the app's SQLite file (default: BTCFLOW_DB_PATH)")
    m.add_argument("--archive", type=Path, help="the trade archive file (btcflow-archive.db) -> app.trades_archive")
    m.add_argument("--research", type=Path, help="the recorders' file (polymarket_us.db) -> schema research")
    m.add_argument("--force-table", action="append", default=[], metavar="TABLE",
                   help="empty this target table and copy it again (repeatable)")
    m.add_argument("--batch", type=int, default=50_000, help="rows per COPY batch (default 50000)")
    rd = sub.add_parser("replay-dead-letter", help="replay the writer's dead-letter file (idempotent)")
    rd.add_argument("--file", type=Path, help="the dead-letter file (default <data dir>/logs/writer-dead-letter.jsonl)")
    rd.add_argument("--data-dir", type=Path)
    rd.add_argument("--apply", action="store_true", help="write (without it: a dry run that lists what it would do)")
    al = sub.add_parser("alive", help="liveness: does a recorder's main loop still turn?")
    al.add_argument("name", choices=tuple(ALIVE_DEFAULT_S))
    al.add_argument("--max-age", type=float, default=None)
    f = sub.add_parser("fresh", help="readiness / alert: is a recorder writing rows?")
    f.add_argument("name", choices=tuple(FRESH_DEFAULT_S))
    f.add_argument("--max-age", type=float, default=None)
    f.add_argument("--research-dir", type=Path)
    a = ap.parse_args(argv)
    if a.cmd == "stop":
        return stop_app(_data_dir(a.data_dir), a.timeout, hold=a.hold)
    if a.cmd == "release":
        return release(_data_dir(a.data_dir), a.name)
    if a.cmd == "stop-supervisor":
        return stop_supervisor(_data_dir(a.data_dir), a.timeout)
    if a.cmd == "pair-pc":
        return pair_pc(_data_dir(a.data_dir), a.port)
    if a.cmd == "signout-pc":
        return signout_pc(_data_dir(a.data_dir))
    if a.cmd == "migrate-sqlite-to-pg":
        return migrate_sqlite_to_pg(a)
    if a.cmd == "migrate":
        return migrate_db()
    if a.cmd == "replay-dead-letter":
        return replay_dead_letter(a)
    if a.cmd == "alive":
        ok, line = alive(a.name, a.max_age if a.max_age is not None else ALIVE_DEFAULT_S[a.name])
        (say if ok else warn)(line)
        return 0 if ok else 1
    if a.research_dir is not None:  # an explicit folder: that SQLite file
        rd = a.research_dir
    else:  # where the recorders write: Postgres's research schema with BTCFLOW_DATABASE_URL, else the folder
        from btc_signal_engine.adapters.sqlite.research import ResearchDb
        s = _settings()
        rd = ResearchDb.for_target(s.database, s.research_path)
    ok, line = fresh(a.name, a.max_age if a.max_age is not None else FRESH_DEFAULT_S[a.name], rd)
    (say if ok else warn)(line)
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
