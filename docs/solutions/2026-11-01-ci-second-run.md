# The second CI run: a late run read, a socket-only Postgres, a SARIF upload's missing permission

**1. The paused card said "No reason sent." (e2e, Linux only).** `test_a_mode_change_pauses_the_agent...` failed in
CI on two looks; a 2-CPU Linux container (Playwright 1.64 image) failed it 4 times in 10.
- Cause: the late-read class of `2026-11-01-late-reads-overwrite-newer-ui-state.md`, in the agent store. Opening a run
  sends `GET /api/agent/sessions/{id}` (`loadSel`) and opens the run's stream. The stream's snapshot arrived first, the
  test paused the run, the stream's delta applied `paused` with its reason, and then the read (served before the pause)
  answered and `applyState` replaced the session with the running one. Nothing re-reads the run after that, so the
  screen said "Watching" until the next change. The assertion read the hidden card's `<b>`, whose binding falls back to
  "No reason sent." when `pause_reason` is null. The toast (from the stream's CUSTOM event) had the right reason.
- Fix: `stateSeq` counts every applied state; `loadSel` applies its answer only if no state was applied since it was
  sent. While the stream is live it is the source of truth; with no stream the read still applies. 20 x 4 looks green
  in the same container after the fix.

**2. Restore drill: "the database system is shutting down".** The official Postgres image's entrypoint runs initdb and
the init scripts on a temporary server that listens on the Unix socket only, stops it, then starts the real server on
TCP and the socket. `pg_isready` over the socket passes on the temporary server; `pg_restore` then connected during its
shutdown. Fix: the drill waits for `select 1` over TCP (127.0.0.1) three times in a row, and runs every command over
TCP. compose.yaml's healthcheck uses `pg_isready -h 127.0.0.1` for the same reason (`up --wait` could pass early).

**3. "Resource not accessible by integration" (workflow-runs, 3x, Trivy job).** `github/codeql-action/upload-sarif`
reads the current workflow run (status report and run metadata); on a private repo the token needs `actions: read`.
The Trivy job now grants it, alongside `contents: read` and `security-events: write`; no other job changes.

**How to avoid.** Any HTTP read that writes state a stream also writes needs a newer-wins check at apply time. Never
take a container's socket check as readiness when its image restarts the server after init: check the transport the
clients use.
