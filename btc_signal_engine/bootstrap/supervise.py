"""The portable process supervisor: `python -m btc_signal_engine.supervise` (Windows, Linux, macOS; no Docker needed).

It runs the app and the research recorders as child processes and keeps them running:
- one log file per child under `<data dir>/logs` (`app.log`, `pm-quotes.log`, `pm-settle.log`), rotated at 10 MB,
  10 old files kept (the app's own file log is turned off for its child, so its lines are written once), plus the
  supervisor's own `supervisor.log` (and stderr);
- a child that exits is started again: 10 s after a clean exit (code 0, e.g. a deploy's `ops stop`, as the old
  watchdog did), with backoff after a crash (1 s, doubling to 60 s; reset once a child ran 60 s). A child that exits
  with 78 (a configuration error, e.g. no Polymarket keys) is not restarted: its log says why;
- `<data dir>/<name>.hold` (written by `ops stop --hold`) keeps that child stopped until `ops release` removes it;
- stop: SIGTERM / SIGINT / SIGHUP (Ctrl+C in its console; SIGBREAK on Windows) or the file `<data dir>/supervisor.stop`
  (`python -m btc_signal_engine.ops stop-supervisor`). Each child is asked to stop through its stop file (the app's
  `stop.request`, a recorder's `--stop-file`), given 30 s, then killed (with its children). Children run in their own
  process group, so a console Ctrl+C reaches only the supervisor;
- one supervisor per data folder (`<data dir>/supervisor.pid`); on Windows the children are tied to it (a job object:
  if the supervisor is killed, they end too, so a new supervisor never runs duplicates).

Options: --only NAMES / --without NAMES (comma-separated: app, pm-quotes, pm-settle; an unknown name is an error),
--log-dir DIR. The OS starts it at boot through a thin example in deploy/ (systemd, launchd, Task Scheduler).
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path

from btc_signal_engine.bootstrap import processes
from btc_signal_engine.bootstrap.logs import EX_CONFIG, SCRUB, UtcFormatter, install_scrubbing, log_to_stderr

log = logging.getLogger("btc_signal_engine.supervise")

NAMES = ("app", "pm-quotes", "pm-settle")
APP_STOP_FILE = "stop.request"  # the app's own graceful-stop file (bootstrap/app.py STOP_FILE)
# How long a child gets to stop through its stop file before it is killed: the app's writer flush and agent / perps
# drains take up to ~45 s, the same 60 s as compose's stop_grace_period.
STOP_TIMEOUT_S = 60.0
SUPERVISOR_STOP_FILE = "supervisor.stop"
SUPERVISOR_PID_FILE = "supervisor.pid"
LOG_BYTES = 10 * 2**20
LOG_BACKUPS = 10


@dataclass
class ChildSpec:
    name: str
    argv: list[str]
    stop_file: Path  # created to ask the child to stop; removed after it exits and before it starts
    hold_file: Path | None = None  # while it exists the child is not started
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class _Child:
    spec: ChildSpec
    proc: subprocess.Popen | None = None
    started: float = 0.0
    failures: int = 0
    next_start: float = 0.0
    done: bool = False  # exited with EX_CONFIG: not restarted
    reader: threading.Thread | None = None
    held_logged: bool = False


def child_logger(log_dir: Path, name: str, max_bytes: int, backups: int) -> logging.Logger:
    """A logger that writes a child's lines verbatim to `<log_dir>/<name>.log`, rotated."""
    lg = logging.getLogger(f"btc_signal_engine.supervise.child.{name}.{id(log_dir)}")
    lg.propagate = False
    lg.setLevel(logging.INFO)
    for h in list(lg.handlers):
        lg.removeHandler(h)
        h.close()
    log_dir.mkdir(parents=True, exist_ok=True)
    h = RotatingFileHandler(log_dir / f"{name}.log", maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(message)s"))
    h.addFilter(SCRUB)  # a child's own lines are scrubbed by the child; this catches anything else it printed
    lg.addHandler(h)
    return lg


class ProcessSupervisor:
    def __init__(self, specs: list[ChildSpec], log_dir: Path, *, stop_file: Path | None = None,
                 backoff_min_s: float = 1.0, backoff_max_s: float = 60.0, stable_s: float = 60.0,
                 clean_restart_s: float = 10.0, stop_timeout_s: float = STOP_TIMEOUT_S, poll_s: float = 0.5,
                 log_bytes: int = LOG_BYTES, log_backups: int = LOG_BACKUPS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.children = [_Child(s) for s in specs]
        self.log_dir = log_dir
        self.stop_file = stop_file
        self.backoff_min_s, self.backoff_max_s, self.stable_s = backoff_min_s, backoff_max_s, stable_s
        self.clean_restart_s, self.stop_timeout_s, self.poll_s = clean_restart_s, stop_timeout_s, poll_s
        self.clock = clock
        self._stop = threading.Event()
        self._loggers = {s.name: child_logger(log_dir, s.name, log_bytes, log_backups) for s in specs}

    # -- control ---------------------------------------------------------------------------------------------------
    def request_stop(self, *_args) -> None:
        self._stop.set()

    @property
    def stopping(self) -> bool:
        if not self._stop.is_set() and self.stop_file is not None and self.stop_file.exists():
            log.info("stop file %s found: stopping every child", self.stop_file)
            self._stop.set()
        return self._stop.is_set()

    def child(self, name: str) -> _Child:
        return next(c for c in self.children if c.spec.name == name)

    # -- one pass --------------------------------------------------------------------------------------------------
    def tick(self) -> None:
        now = self.clock()
        for c in self.children:
            if c.proc is not None:
                code = c.proc.poll()
                if code is None:
                    continue
                self._exited(c, code, now)
            if c.done or now < c.next_start:
                continue
            if c.spec.hold_file is not None and c.spec.hold_file.exists():
                if not c.held_logged:
                    log.info("%s is held (%s): not starting it until the file is removed (ops release)", c.spec.name,
                             c.spec.hold_file)
                    c.held_logged = True
                continue
            c.held_logged = False
            self._start(c, now)

    def _exited(self, c: _Child, code: int, now: float) -> None:
        ran = now - c.started
        c.proc = None
        if c.reader is not None:  # its last lines (a grandchild still holding the pipe keeps it open: do not wait)
            c.reader.join(timeout=1)
        c.spec.stop_file.unlink(missing_ok=True)
        if code == EX_CONFIG:
            c.done = True
            log.error("%s exited with a configuration error (code %d); not restarting it (see %s.log)", c.spec.name,
                      code, c.spec.name)
            return
        if code == 0:
            c.failures = 0
            delay = self.clean_restart_s
        else:
            c.failures = 1 if ran >= self.stable_s else c.failures + 1
            delay = min(self.backoff_max_s, self.backoff_min_s * 2 ** (c.failures - 1))
        c.next_start = now + delay
        log.warning("%s exited with code %d after %.0f s; restarting in %.0f s", c.spec.name, code, ran, delay)

    def _start(self, c: _Child, now: float) -> None:
        c.spec.stop_file.unlink(missing_ok=True)  # a leftover would stop it at once
        env = {**os.environ, "PYTHONUNBUFFERED": "1", **c.spec.env}
        try:
            c.proc = subprocess.Popen(c.spec.argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,  # noqa: S603 - argv from the supervisor's own service specs
                                      stdin=subprocess.DEVNULL, env=env, **processes.child_popen_kwargs())
        except OSError as exc:
            c.failures += 1
            c.next_start = now + min(self.backoff_max_s, self.backoff_min_s * 2 ** (c.failures - 1))
            log.error("%s could not be started (%s)", c.spec.name, exc)
            return
        c.started = now
        lg = self._loggers[c.spec.name]
        c.reader = threading.Thread(target=self._pump, args=(c.proc, lg), name=f"log-{c.spec.name}", daemon=True)
        c.reader.start()
        log.info("started %s (pid %d)", c.spec.name, c.proc.pid)

    @staticmethod
    def _pump(proc: subprocess.Popen, lg: logging.Logger) -> None:
        for raw in iter(proc.stdout.readline, b""):
            lg.info(raw.decode("utf-8", "replace").rstrip("\r\n"))
        proc.stdout.close()

    # -- run and stop ----------------------------------------------------------------------------------------------
    def run(self) -> None:
        log.info("supervising %s", ", ".join(c.spec.name for c in self.children))
        try:
            while not self.stopping:
                self.tick()
                self._stop.wait(self.poll_s)
        finally:
            self.stop_all()

    def stop_all(self) -> None:
        """Ask every running child to stop through its stop file, wait up to stop_timeout_s, then kill the rest."""
        running = [c for c in self.children if c.proc is not None and c.proc.poll() is None]
        for c in running:
            try:
                c.spec.stop_file.parent.mkdir(parents=True, exist_ok=True)
                c.spec.stop_file.write_text("supervisor stop requested\n", encoding="ascii")
            except OSError:
                log.warning("could not write %s", c.spec.stop_file, exc_info=True)
        deadline = time.monotonic() + self.stop_timeout_s
        while any(c.proc.poll() is None for c in running) and time.monotonic() < deadline:
            time.sleep(0.1)
        for c in running:
            if c.proc.poll() is None:
                log.warning("%s did not stop within %.0f s: killing it (pid %d)", c.spec.name, self.stop_timeout_s,
                            c.proc.pid)
                processes.force_kill(c.proc.pid, tree=True)
                try:
                    c.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    log.error("%s (pid %d) is still running after the kill", c.spec.name, c.proc.pid)
            else:
                log.info("%s stopped (code %s)", c.spec.name, c.proc.returncode)
            if c.reader is not None:
                c.reader.join(timeout=5)
            c.spec.stop_file.unlink(missing_ok=True)
            c.proc = None
        for lg in self._loggers.values():
            for h in lg.handlers:
                h.close()


def console_python(exe: str) -> str:
    """pythonw.exe (no console, used to start the supervisor hidden on Windows) -> python.exe next to it: the
    children need real stdout / stderr for their logs; CREATE_NO_WINDOW keeps them windowless."""
    p = Path(exe)
    if p.name.lower() == "pythonw.exe" and (p.parent / "python.exe").exists():
        return str(p.parent / "python.exe")
    return exe


def child_specs(names: list[str], data_dir: Path, python: str | None = None) -> list[ChildSpec]:
    python = python or console_python(sys.executable)
    out = []
    for n in names:
        hold = data_dir / f"{n}.hold"
        if n == "app":
            # The app's lines go to the supervisor's app.log; its own file log would write them twice.
            out.append(ChildSpec(n, [python, "-m", "btc_signal_engine"], data_dir / APP_STOP_FILE, hold,
                                 env={"BTCFLOW_FILE_LOG": "false"}))
        else:
            stop = data_dir / f"{n}.stop"
            out.append(ChildSpec(n, [python, "-m", "btc_signal_engine.recorders", n, "--stop-file", str(stop)], stop,
                                 hold))
    return out


def select(only: str | None, without: str | None) -> list[str]:
    def names(arg: str) -> list[str]:
        got = [x.strip() for x in arg.split(",") if x.strip()]
        bad = [x for x in got if x not in NAMES]
        if bad:
            raise SystemExit(f"unknown child name(s): {', '.join(bad)} (known: {', '.join(NAMES)})")
        return got
    chosen = names(only) if only else list(NAMES)
    if without:
        drop = set(names(without))
        chosen = [n for n in chosen if n not in drop]
    if not chosen:
        raise SystemExit("nothing to supervise")
    return chosen


def _claim_pid_file(path: Path) -> bool:
    """Write our pid unless another live supervisor already holds the file."""
    try:
        other = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        other = 0
    if other and other != os.getpid():
        p = processes.process(other)
        if p is not None and processes.runs_module(p.cmdline, "btc_signal_engine.supervise"):
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()), encoding="ascii")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m btc_signal_engine.supervise",
                                 description="Run the Settleview app and research recorders, restarting them.")
    ap.add_argument("--only", help="comma-separated children to run (app, pm-quotes, pm-settle)")
    ap.add_argument("--without", help="comma-separated children to leave out")
    ap.add_argument("--log-dir", type=Path, help="where the logs go (default <data dir>/logs)")
    a = ap.parse_args(argv)
    names = select(a.only, a.without)
    from btc_signal_engine.bootstrap.config import Settings
    data_dir = Path(Settings().db_path).resolve().parent
    log_dir = a.log_dir or data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.getLogger().setLevel(logging.INFO)
    if sys.stderr is not None:  # None under pythonw.exe (started hidden): the file below is the only log then
        log_to_stderr(logging.INFO)
    fh = RotatingFileHandler(log_dir / "supervisor.log", maxBytes=LOG_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
    fh.setFormatter(UtcFormatter())
    logging.getLogger().addHandler(fh)
    install_scrubbing()
    pid_file = data_dir / SUPERVISOR_PID_FILE
    if not _claim_pid_file(pid_file):
        log.error("another supervisor is running for %s (%s); not starting a second one", data_dir, pid_file)
        return 3
    stop_file = data_dir / SUPERVISOR_STOP_FILE
    stop_file.unlink(missing_ok=True)
    if processes.tie_children_to_this_process():
        log.info("children are tied to this supervisor (Windows job object)")
    sup = ProcessSupervisor(child_specs(names, data_dir), log_dir, stop_file=stop_file)
    for name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, sup.request_stop)
    log.info("data folder %s, logs in %s", data_dir, log_dir)
    try:
        sup.run()
    finally:
        stop_file.unlink(missing_ok=True)
        try:
            if pid_file.read_text(encoding="ascii").strip() == str(os.getpid()):
                pid_file.unlink()
        except OSError:
            pass
        log.info("supervisor stopped")
    return 0
