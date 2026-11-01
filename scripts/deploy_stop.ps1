<#
.SYNOPSIS
Stop the app gracefully before a deploy (KTD9): call this BEFORE checking out new code.

.DESCRIPTION
Thin Windows wrapper, kept for one release: the protocol now lives in `python -m btc_signal_engine.ops stop`
(btc_signal_engine/bootstrap/ops.py), the same on Windows, Linux and macOS. It trusts only <DataDir>\app.pid,
creates <DataDir>\stop.request, waits up to -TimeoutSeconds, then falls back to a forced kill of that process (and its
virtual environment's python.exe launcher, if any). -Hold also keeps the portable supervisor from starting the app
again until `python -m btc_signal_engine.ops release`.

Exit codes (unchanged): 0 stopped cleanly (or no app is running), 1 still running, 2 stopped by the fallback kill,
3 cannot tell which process is the app (nothing was done); 64 BTCFLOW_PYTHON is not set (nothing was done).

The Python used is $env:BTCFLOW_PYTHON: the python.exe that has Settleview installed on this Windows host. It is
required (there is no default environment to guess); with Docker, stop the app with `docker compose stop app`.

.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\deploy_stop.ps1 -DataDir C:\path\to\btc-flow-engine\data
#>
param(
    [Parameter(Mandatory = $true)][string]$DataDir,
    [int]$TimeoutSeconds = 60,
    [switch]$Hold
)

$ErrorActionPreference = "Stop"
$python = $env:BTCFLOW_PYTHON
if (-not $python) {
    [Console]::Error.WriteLine("BTCFLOW_PYTHON is not set: point it at the python.exe that has Settleview installed (nothing was done).")
    exit 64
}
$argsList = @("-m", "btc_signal_engine.ops", "stop", "--data-dir", $DataDir, "--timeout", $TimeoutSeconds)
if ($Hold) { $argsList += "--hold" }
& $python @argsList
exit $LASTEXITCODE
