"""The portable supervisor (P2) with real child processes (tiny Python scripts), on every OS CI runs: restart with
backoff after a crash, a delay after a clean exit, no restart after a configuration error, the hold file, a clean stop
through each child's stop file, the forced kill of a child that ignores it, and one rotated log file per child."""

from __future__ import annotations

import sys
import time

import pytest

from btc_signal_engine.bootstrap import supervise as sv
from btc_signal_engine.bootstrap.logs import EX_CONFIG

GRACEFUL = """
import os, sys, time
stop = sys.argv[1]
print("child up", flush=True)
while not os.path.exists(stop):
    time.sleep(0.05)
print("child got the stop file", flush=True)
"""
STUBBORN = "import time\nprint('stubborn up', flush=True)\nwhile True: time.sleep(0.2)\n"


def _script(tmp_path, name, body):
    p = tmp_path / f"{name}.py"
    p.write_text(body, encoding="utf-8")
    return str(p)


def _spec(tmp_path, name, body, *, hold=False):
    stop = tmp_path / f"{name}.stop"
    return sv.ChildSpec(name, [sys.executable, _script(tmp_path, name, body), str(stop)], stop,
                        tmp_path / f"{name}.hold" if hold else None)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _wait_exit(c, timeout=20.0):
    end = time.monotonic() + timeout
    while c.proc is not None and c.proc.poll() is None and time.monotonic() < end:
        time.sleep(0.02)
    assert c.proc is None or c.proc.poll() is not None, "child did not exit"


def _wait_for(pred, timeout=20.0):
    end = time.monotonic() + timeout
    while not pred() and time.monotonic() < end:
        time.sleep(0.05)
    assert pred()


def test_a_crashing_child_restarts_with_doubling_backoff(tmp_path):
    clock = Clock()
    sup = sv.ProcessSupervisor([_spec(tmp_path, "crash", "raise SystemExit(3)\n")], tmp_path / "logs",
                               clock=clock, backoff_min_s=1, backoff_max_s=4, stable_s=60)
    c = sup.child("crash")
    delays = []
    for _ in range(4):
        sup.tick()  # starts it (it is due)
        assert c.proc is not None
        _wait_exit(c)
        sup.tick()  # sees the exit and schedules the restart
        delays.append(c.next_start - clock.t)
        assert c.proc is None
        clock.t = c.next_start
    assert delays == [1, 2, 4, 4]  # doubling, capped
    sup.stop_all()


def test_backoff_resets_after_a_stable_run(tmp_path):
    clock = Clock()
    sup = sv.ProcessSupervisor([_spec(tmp_path, "crash", "raise SystemExit(1)\n")], tmp_path / "logs",
                               clock=clock, backoff_min_s=1, stable_s=60)
    c = sup.child("crash")
    c.failures = 5
    sup.tick()
    _wait_exit(c)
    clock.t += 61  # it "ran" more than stable_s
    sup.tick()
    assert c.failures == 1 and c.next_start - clock.t == 1
    sup.stop_all()


def test_a_clean_exit_restarts_after_the_watchdog_delay_and_a_config_error_never(tmp_path):
    clock = Clock()
    sup = sv.ProcessSupervisor([_spec(tmp_path, "clean", "pass\n"),
                                _spec(tmp_path, "badcfg", f"raise SystemExit({EX_CONFIG})\n")], tmp_path / "logs",
                               clock=clock, clean_restart_s=10)
    sup.tick()
    for c in sup.children:
        _wait_exit(c)
    sup.tick()
    clean, bad = sup.child("clean"), sup.child("badcfg")
    assert clean.next_start - clock.t == 10 and clean.failures == 0
    assert bad.done and bad.proc is None
    clock.t += 3600
    sup.tick()
    assert bad.proc is None  # never restarted
    sup.stop_all()


def test_a_held_child_is_not_started_until_released(tmp_path):
    spec = _spec(tmp_path, "held", GRACEFUL, hold=True)
    spec.hold_file.write_text("held")
    sup = sv.ProcessSupervisor([spec], tmp_path / "logs", clock=Clock())
    sup.tick()
    assert sup.child("held").proc is None
    spec.hold_file.unlink()
    sup.tick()
    assert sup.child("held").proc is not None
    sup.stop_all()


def test_stop_asks_through_the_stop_file_and_logs_each_child(tmp_path):
    spec = _spec(tmp_path, "graceful", GRACEFUL)
    spec.stop_file.write_text("leftover")  # removed before the start, so it does not stop at once
    sup = sv.ProcessSupervisor([spec], tmp_path / "logs", clock=Clock(), stop_timeout_s=20)
    sup.tick()
    c = sup.child("graceful")
    assert not spec.stop_file.exists()
    log = tmp_path / "logs" / "graceful.log"
    _wait_for(lambda: log.exists() and "child up" in log.read_text(encoding="utf-8"))
    proc = c.proc
    t0 = time.monotonic()
    sup.stop_all()
    assert proc.returncode == 0 and time.monotonic() - t0 < 15  # it stopped by itself, not by the kill
    assert "child got the stop file" in log.read_text(encoding="utf-8")
    assert not spec.stop_file.exists()


def test_a_child_that_ignores_the_stop_file_is_killed(tmp_path):
    spec = _spec(tmp_path, "stubborn", STUBBORN)
    sup = sv.ProcessSupervisor([spec], tmp_path / "logs", clock=Clock(), stop_timeout_s=0.5)
    sup.tick()
    proc = sup.child("stubborn").proc
    _wait_for(lambda: "stubborn up" in (tmp_path / "logs" / "stubborn.log").read_text(encoding="utf-8")
              if (tmp_path / "logs" / "stubborn.log").exists() else False)
    sup.stop_all()
    assert proc.poll() is not None and proc.returncode != 0


def test_child_logs_rotate(tmp_path):
    chatty = "for i in range(400):\n    print('x' * 100, flush=True)\n"
    sup = sv.ProcessSupervisor([_spec(tmp_path, "chatty", chatty)], tmp_path / "logs", clock=Clock(),
                               log_bytes=5_000, log_backups=2)
    sup.tick()
    c = sup.child("chatty")
    _wait_exit(c)
    c.reader.join(timeout=10)
    sup.stop_all()
    names = sorted(p.name for p in (tmp_path / "logs").iterdir())
    assert names == ["chatty.log", "chatty.log.1", "chatty.log.2"]


def test_run_ends_on_the_supervisor_stop_file(tmp_path):
    spec = _spec(tmp_path, "graceful", GRACEFUL)
    stop = tmp_path / "supervisor.stop"
    sup = sv.ProcessSupervisor([spec], tmp_path / "logs", stop_file=stop, poll_s=0.05, stop_timeout_s=20)
    import threading
    t = threading.Thread(target=sup.run)
    t.start()
    _wait_for(lambda: sup.child("graceful").proc is not None)
    stop.write_text("stop")
    t.join(timeout=30)
    assert not t.is_alive() and sup.child("graceful").proc is None


def test_child_specs_and_selection(tmp_path):
    specs = sv.child_specs(sv.select(None, "pm-settle"), tmp_path, python="py")
    assert [s.name for s in specs] == ["app", "pm-quotes"]
    app, quotes = specs
    assert app.argv == ["py", "-m", "btc_signal_engine"] and app.stop_file == tmp_path / "stop.request"
    assert app.env == {"BTCFLOW_FILE_LOG": "false"} and app.hold_file == tmp_path / "app.hold"
    assert quotes.argv == ["py", "-m", "btc_signal_engine.recorders", "pm-quotes", "--stop-file",
                           str(tmp_path / "pm-quotes.stop")]
    assert sv.select("pm-quotes,pm-settle", None) == ["pm-quotes", "pm-settle"]
    with pytest.raises(SystemExit, match="unknown child name"):
        sv.select("app,recorder", None)
    with pytest.raises(SystemExit, match="nothing to supervise"):
        sv.select("app", "app")
