# Git Bash rewrites container paths in `docker exec` / `run` arguments

**Problem.** In the 2026-11-01 live deploy, `docker compose exec -T postgres bash /docker-entrypoint-initdb.d/20-roles.sh`
from Git Bash failed: `bash` in the container found no such file.

**Cause.** Git Bash (MSYS2) converts any argument that looks like an absolute POSIX path into a Windows path under its
install folder (`C:/Program Files/Git/docker-entrypoint-initdb.d/...`) when it starts a native Windows program such as
`docker.exe`. The container then receives the rewritten path. PowerShell, cmd, Linux and macOS shells do not do this.
A Python script started from Git Bash is a native program too: the arguments typed after `python script.py` are
already rewritten when Python sees them, but the commands Python itself starts with `subprocess` are not.

**Fix.** `MSYS_NO_PATHCONV=1` (and `MSYS2_ARG_CONV_EXCL=*` for MSYS2 builds) turns the conversion off. scripts/deploy.py
and scripts/smoke.py set both for every command they run, and deploy.py refuses an argument that already arrived
rewritten (it starts with a Git install folder) and says to prefix the command with `MSYS_NO_PATHCONV=1`.

**How to avoid.** From Git Bash, prefix any `docker ... exec|run` line that names an absolute container path with
`MSYS_NO_PATHCONV=1`; in scripts, pass container paths through `subprocess` rather than through the shell.
