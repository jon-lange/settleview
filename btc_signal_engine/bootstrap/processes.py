"""Operating-system process helpers for the supervisor and `ops stop`, standard library only, on Windows, Linux and
macOS: is a pid alive, the process table (pid, parent pid, command line), a forced kill, and keeping children tied to
the supervisor on Windows (a job object that kills them if the supervisor dies).

Windows notes: `os.kill(pid, 0)` would TERMINATE the process there (any signal but the console ones is
TerminateProcess), so liveness uses OpenProcess + GetExitCodeProcess. A virtual environment's python.exe (venv, uv) is
a launcher that starts the real interpreter as its child, so a command line match and the parent pid matter (`ops
stop` kills both).
The command lines come from PowerShell's Get-CimInstance (the same source scripts/deploy_stop.ps1 used).
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

WINDOWS = sys.platform == "win32"
# `python -m btc_signal_engine` (the app), not `-m btc_signal_engine backfill` and not a submodule such as
# `-m btc_signal_engine.recorders` (the same pattern as scripts/deploy_stop.ps1).
_APP = re.compile(r'-m\s+btc_signal_engine(\s|"|$)')
_BACKFILL = re.compile(r"-m\s+btc_signal_engine\s+backfill")


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    cmdline: str


def is_app(cmdline: str) -> bool:
    return bool(_APP.search(cmdline or "")) and not _BACKFILL.search(cmdline or "")


def runs_module(cmdline: str, module: str) -> bool:
    return bool(re.search(rf'-m\s+{re.escape(module)}(\s|"|$)', cmdline or ""))


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if WINDOWS:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5  # access denied: it exists
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie (exited, not yet reaped by its parent) still answers kill(pid, 0) but is not alive.
    if Path("/proc/self/stat").exists():  # Linux
        try:
            with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as f:
                return f.read().rsplit(")", 1)[-1].split()[0] != "Z"
        except OSError:
            return True
    return _ps_alive(pid)  # macOS / BSD: no /proc


def _ps_alive(pid: int) -> bool:
    """ps's state column: Z = zombie, no row = gone (ps exits non-zero)."""
    try:
        res = subprocess.run(["ps", "-o", "stat=", "-p", str(int(pid))], capture_output=True, text=True,  # noqa: S603, S607 - the OS's own ps, fixed argv and an int pid
                             timeout=30)
    except (OSError, subprocess.SubprocessError):
        return True  # cannot tell: kill(pid, 0) said it exists
    state = res.stdout.strip()
    return bool(state) and not state.startswith("Z")


def _windows_table(pid: int | None) -> list[Proc]:
    flt = f"ProcessId = {int(pid)}" if pid is not None else "Name LIKE 'python%'"
    ps = (f"Get-CimInstance Win32_Process -Filter \"{flt}\" | Select-Object ProcessId,ParentProcessId,CommandLine | "
          "ConvertTo-Json -Compress")
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,  # noqa: S603, S607 - the OS's own tool, fixed argv (the filter is an int pid or a constant)
                         text=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
    if not out:
        return []
    rows = json.loads(out)
    rows = rows if isinstance(rows, list) else [rows]
    return [Proc(int(r["ProcessId"]), int(r.get("ParentProcessId") or 0), r.get("CommandLine") or "") for r in rows]


def _linux_table(pid: int | None) -> list[Proc]:
    out = []
    pids = [pid] if pid is not None else [int(p) for p in os.listdir("/proc") if p.isdigit()]
    for p in pids:
        try:
            raw = Path(f"/proc/{p}/cmdline").read_bytes()
            stat = Path(f"/proc/{p}/stat").read_text(encoding="ascii", errors="replace")
        except OSError:
            continue
        ppid = int(stat.rsplit(")", 1)[-1].split()[1])
        out.append(Proc(p, ppid, " ".join(a.decode("utf-8", "replace") for a in raw.split(b"\0") if a)))
    return out


def _ps_table(pid: int | None) -> list[Proc]:
    args = ["ps", "-o", "pid=,ppid=,command="] + (["-p", str(pid)] if pid is not None else ["-ax"])
    res = subprocess.run(args, capture_output=True, text=True, timeout=30)  # noqa: S603 - the OS's own ps, fixed argv and an int pid
    out = []
    for line in res.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit():
            out.append(Proc(int(parts[0]), int(parts[1]), parts[2] if len(parts) > 2 else ""))
    return out


def process_table(pid: int | None = None) -> list[Proc]:
    """Every process (on Windows: every python*), or just `pid`; [] when it does not exist."""
    if WINDOWS:
        return _windows_table(pid)
    if Path("/proc/self/cmdline").exists():
        return _linux_table(pid)
    return _ps_table(pid)


def process(pid: int) -> Proc | None:
    rows = [p for p in process_table(pid) if p.pid == pid]
    return rows[0] if rows and pid_alive(pid) else None


def force_kill(pid: int, *, tree: bool = False) -> None:
    """Kill `pid` now (Windows: taskkill /F, with /T its children too; POSIX: SIGKILL, with `tree` its process
    group when it leads one). A process that is already gone is not an error."""
    if WINDOWS:
        subprocess.run(["taskkill", "/F", *(["/T"] if tree else []), "/PID", str(pid)], capture_output=True,  # noqa: S603, S607 - the OS's own tool, an int pid
                       creationflags=subprocess.CREATE_NO_WINDOW)
        return
    try:
        if tree and os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def child_popen_kwargs() -> dict:
    """Children get their own process group, so a console Ctrl+C reaches the supervisor only (it then stops them in
    order, through their stop files)."""
    if WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def tie_children_to_this_process() -> bool:
    """Windows: put this process in a job object that kills every process in it when the last handle closes, i.e.
    when this process ends in any way (a closed console window, Task Scheduler's End, a crash). Children started
    afterwards are in the job too. POSIX: nothing to do here (systemd / launchd stop the whole group; a terminal
    close sends SIGHUP, which the supervisor handles). Returns whether the job was set up."""
    if not WINDOWS:
        return False
    import ctypes
    from ctypes import wintypes

    class IOCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IOCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return False
    info = Extended()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info)):
        return False  # 9 = JobObjectExtendedLimitInformation
    if not k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(k32.GetCurrentProcess())):
        return False
    tie_children_to_this_process.job = job  # the handle stays open for this process's life
    return True
