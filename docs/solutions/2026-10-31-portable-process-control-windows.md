# Portable process control: the Windows traps

**Problem.** Porting `deploy_stop.ps1` and the `.cmd` watchdog to Python (`bootstrap/processes.py`,
`bootstrap/supervise.py`) hit three Windows behaviours that a Linux-first implementation gets wrong.

**Causes and fixes.**
- `os.kill(pid, 0)` is the POSIX "is it alive?" probe, but on Windows any signal other than CTRL_C / CTRL_BREAK is
  `TerminateProcess`: the probe kills the process. Liveness uses `OpenProcess` + `GetExitCodeProcess` (259 = still
  active) through ctypes instead.
- A `.venv\Scripts\python.exe` is a launcher that starts the base interpreter as a child with the same command line.
  `app.pid` holds the child's pid; killing only the launcher leaves the real app running. `ops stop` kills both (the
  parent too when its command line is also the app), and the supervisor's forced kill uses `taskkill /F /T`.
- A child in the supervisor's console gets Ctrl+C too, and a supervisor that is killed (Task Scheduler's End, a closed
  window) leaves its children orphaned, so the next supervisor would run two recorders writing the same rows. Children
  get their own process group (`CREATE_NEW_PROCESS_GROUP`) and are stopped through their stop files; the supervisor
  puts itself in a job object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, so its children die with it.

**How to avoid.** Never call `os.kill` on Windows for anything but console events; treat every venv python process as
a launcher + child pair; test process code on all three OSes (the CI `portable` matrix job).
