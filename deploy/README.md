# Starting Settleview at boot

The supported way to run Settleview is Docker: `compose.yaml` (restart policies start it at boot). These are thin
examples for a host without Docker only: each starts the portable supervisor (`python -m
btc_signal_engine.supervise`), which runs the app and the research recorders and restarts them, from a Python
environment that has Settleview installed (`pip install .`; the paths in the files are examples). Nothing else lives
in these files; the runbook is in [docs/operations.md](../docs/operations.md). Development never needs any of this:
it runs in the dev container (`scripts/dev.py`).

| Host | File | Install |
|---|---|---|
| Linux (systemd) | [systemd/settleview.service](systemd/settleview.service) | copy, adjust paths, `systemctl enable --now settleview` |
| macOS (launchd) | [launchd/com.settleview.supervisor.plist](launchd/com.settleview.supervisor.plist) | copy to `~/Library/LaunchAgents/`, `launchctl bootstrap gui/$(id -u) <file>` |
| Windows | Task Scheduler, one line (below) | run once in PowerShell |

Windows (at logon of the current user, from the checkout folder, which holds `.env`; `$py` is the `pythonw.exe` of
the environment Settleview is installed in):

```powershell
$py = "C:\path\to\settleview-env\Scripts\pythonw.exe"
schtasks /Create /TN Settleview /SC ONLOGON /RL LIMITED /F /TR "cmd /c cd /d \"$PWD\" && \"$py\" -m btc_signal_engine.supervise"
```

`pythonw.exe` runs it without a console window. Stop it with `python -m btc_signal_engine.ops stop-supervisor`
(graceful: every child through its stop file); Task Scheduler's End kills it hard, and the children end with it
(they are tied to the supervisor by a job object). Remove the task with `schtasks /Delete /TN Settleview /F`.
