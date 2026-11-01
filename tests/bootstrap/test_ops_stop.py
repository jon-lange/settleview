"""`python -m btc_signal_engine.ops stop` (P2): the deploy_stop.ps1 protocol on any OS. The decision paths run against
a fake process table (the real one has the live app in it on the developer's PC); one test runs the whole protocol
against a real child process that behaves like the app (pid file, stop file, exit 0)."""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from btc_signal_engine.bootstrap import ops, processes
from btc_signal_engine.bootstrap.processes import Proc

APP = "python -m btc_signal_engine"


class FakeOS:
    def __init__(self, procs: list[Proc], *, exits_on_stop: bool = True, survives_kill: bool = False,
                 data_dir=None) -> None:
        self.procs = {p.pid: p for p in procs}
        self.exits_on_stop, self.survives_kill = exits_on_stop, survives_kill
        self.data_dir = data_dir
        self.killed: list[int] = []

    def alive(self, pid: int) -> bool:
        if pid in self.procs and self.exits_on_stop and self.data_dir is not None \
                and (self.data_dir / "stop.request").exists():
            self.procs.pop(pid)  # the app saw the stop file and exited
        return pid in self.procs

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(processes, "pid_alive", self.alive)
        monkeypatch.setattr(processes, "process", lambda pid: self.procs.get(pid))
        monkeypatch.setattr(processes, "process_table", lambda pid=None: list(self.procs.values()))
        monkeypatch.setattr(processes, "force_kill", self.kill)

    def kill(self, pid: int, *, tree: bool = False) -> None:
        self.killed.append(pid)
        if not self.survives_kill:
            self.procs.pop(pid, None)


def test_patterns_match_the_powershell_script():
    assert processes.is_app('"C:\\x\\python.exe" -m btc_signal_engine')
    assert processes.is_app("/usr/bin/python3 -m btc_signal_engine run")
    assert not processes.is_app("python -m btc_signal_engine backfill")
    assert not processes.is_app("python -m btc_signal_engine.recorders pm-quotes")
    assert not processes.is_app("python -m btc_signal_engine.ops stop")
    assert processes.runs_module("python -m btc_signal_engine.supervise --only app", "btc_signal_engine.supervise")


def test_no_app_running_cleans_leftovers_and_exits_0(tmp_path, monkeypatch):
    FakeOS([]).install(monkeypatch)
    (tmp_path / "stop.request").write_text("x")
    (tmp_path / "app.pid").write_text("4242")
    assert ops.stop_app(tmp_path, 1) == 0
    assert not (tmp_path / "stop.request").exists() and not (tmp_path / "app.pid").exists()


def test_cannot_tell_does_nothing_and_exits_3(tmp_path, monkeypatch):
    FakeOS([Proc(77, 1, APP)]).install(monkeypatch)
    assert ops.stop_app(tmp_path, 1) == 3
    assert not (tmp_path / "stop.request").exists()


def test_a_reused_pid_that_is_not_the_app_is_not_trusted(tmp_path, monkeypatch):
    FakeOS([Proc(4242, 1, "notepad.exe")]).install(monkeypatch)
    (tmp_path / "app.pid").write_text("4242")
    assert ops.stop_app(tmp_path, 1) == 0  # not the app, and no app runs


def test_clean_stop_exits_0_and_removes_the_stop_file(tmp_path, monkeypatch):
    os_ = FakeOS([Proc(4242, 1, APP)], data_dir=tmp_path)
    os_.install(monkeypatch)
    (tmp_path / "app.pid").write_text("4242")
    assert ops.stop_app(tmp_path, 5, poll_s=0.01) == 0
    assert not (tmp_path / "stop.request").exists() and os_.killed == []


def test_hold_writes_the_hold_file_before_the_stop(tmp_path, monkeypatch):
    FakeOS([Proc(4242, 1, APP)], data_dir=tmp_path).install(monkeypatch)
    (tmp_path / "app.pid").write_text("4242")
    assert ops.stop_app(tmp_path, 5, hold=True, poll_s=0.01) == 0
    assert (tmp_path / "app.hold").exists()
    assert ops.release(tmp_path) == 0 and not (tmp_path / "app.hold").exists()


def test_fallback_kill_takes_the_launcher_too_and_exits_2(tmp_path, monkeypatch):
    os_ = FakeOS([Proc(10, 1, APP), Proc(4242, 10, APP)], exits_on_stop=False, data_dir=tmp_path)
    os_.install(monkeypatch)
    (tmp_path / "app.pid").write_text("4242")
    assert ops.stop_app(tmp_path, 0.05, poll_s=0.01, kill_wait_s=0.05) == 2
    assert os_.killed == [4242, 10]
    assert not (tmp_path / "stop.request").exists() and not (tmp_path / "app.pid").exists()


def test_still_running_after_the_kill_exits_1(tmp_path, monkeypatch):
    os_ = FakeOS([Proc(4242, 1, APP)], exits_on_stop=False, survives_kill=True, data_dir=tmp_path)
    os_.install(monkeypatch)
    (tmp_path / "app.pid").write_text("4242")
    assert ops.stop_app(tmp_path, 0.05, poll_s=0.01, kill_wait_s=0.05) == 1
    assert (tmp_path / "app.pid").exists()  # kept: the process may still own it


FAKE_APP = """
import os, sys, time
d = sys.argv[1]
open(os.path.join(d, "app.pid"), "w").write(str(os.getpid()))
while not os.path.exists(os.path.join(d, "stop.request")):
    time.sleep(0.05)
os.remove(os.path.join(d, "stop.request"))
os.remove(os.path.join(d, "app.pid"))
"""


def test_the_whole_protocol_against_a_real_process(tmp_path):
    """A child whose command line contains `-m btc_signal_engine` (as an argument to -c) and behaves like the app."""
    script = tmp_path / "fake_app.py"
    script.write_text(FAKE_APP, encoding="utf-8")
    code = f"import runpy, sys; sys.argv = ['x', sys.argv[1]]; runpy.run_path({str(script)!r})"
    proc = subprocess.Popen([sys.executable, "-c", code, str(tmp_path), "-m", "btc_signal_engine"])
    try:
        end = time.monotonic() + 30
        while not (tmp_path / "app.pid").exists() and time.monotonic() < end:
            time.sleep(0.05)
        assert ops.stop_app(tmp_path, 30, poll_s=0.1) == 0
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()


def test_pid_alive_on_this_os():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert processes.pid_alive(proc.pid)
    finally:
        proc.kill()
        proc.wait()
    assert not processes.pid_alive(proc.pid)
    assert not processes.pid_alive(0)


@pytest.mark.parametrize("argv", [["stop", "--help"], ["fresh", "--help"]])
def test_cli_help(argv):
    with pytest.raises(SystemExit) as e:
        ops.main(argv)
    assert e.value.code == 0
