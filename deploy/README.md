# Starting Settleview at boot

Thin examples only: each starts the portable supervisor (`python -m btc_signal_engine.supervise`), which runs the
app and the research recorders and restarts them. Nothing else lives in these files; the runbook is in
[docs/operations.md](../docs/operations.md). With Docker, use `compose.yaml` instead (restart policies do this).

| Host | File | Install |
|---|---|---|
| Linux (systemd) | [systemd/settleview.service](systemd/settleview.service) | copy, adjust paths, `systemctl enable --now settleview` |
| macOS (launchd) | [launchd/com.settleview.supervisor.plist](launchd/com.settleview.supervisor.plist) | copy to `~/Library/LaunchAgents/`, `launchctl bootstrap gui/$(id -u) <file>` |
| Windows | Task Scheduler, one line (below) | run once in PowerShell |

Windows (at logon of the current user, from the checkout folder, which holds `.env`):

```powershell
schtasks /Create /TN Settleview /SC ONLOGON /RL LIMITED /F /TR "cmd /c cd /d \"$PWD\" && \"$PWD\.venv\Scripts\pythonw.exe\" -m btc_signal_engine.supervise"
```

`pythonw.exe` runs it without a console window. Stop it with `python -m btc_signal_engine.ops stop-supervisor`
(graceful: every child through its stop file); Task Scheduler's End kills it hard, and the children end with it
(they are tied to the supervisor by a job object). Remove the task with `schtasks /Delete /TN Settleview /F`.
