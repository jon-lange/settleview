# Portable runtime: containers first, the same on Windows, Linux and macOS

Status: draft for the user's approval. User, 2026-10-31: "Don't make this Windows specific … have we containerized
this? … it could be run on Linux or Mac or anything" and "Following well architected framework". Trigger: the
Polymarket US quote recorder died silently for 31 hours (docs/solutions/2026-10-31-pm-recorder-died-silently.md).

## Where we are (checked 2026-10-31)

| Piece | Today | Portable? |
|---|---|---|
| App code | Python 3.13, FastAPI, SQLite; CI runs the suite on ubuntu-latest and builds the Docker image | yes |
| `Dockerfile` / `compose.yaml` | one service (the app), non-root, healthcheck, `./data` volume, port bound to 127.0.0.1; README calls them unmaintained (stale: the image is 3.13) | yes, untested on a real host |
| Agent CLI | Claude Agent SDK; on Linux/macOS the wheel bundles the CLI; on Windows ARM64 `BTCFLOW_AGENT_CLI_PATH` points at a local `claude.exe` | yes |
| Supervisor | `btcflow-watchdog.cmd`, a console window started by hand; not restarted after a reboot | **no** |
| Deploy | `scripts/deploy_stop.ps1` + `git checkout` in a second checkout | **no** |
| Recorders | `scripts/record_polymarket_us*.py`, started by hand; the WS one hard-codes a personal `.env` path | **no** |
| Docker on this PC | not installed | — |

## Target

One image, several services, the same everywhere:

```
compose.yaml
  app            python -m btc_signal_engine                      :8000 (127.0.0.1 only)
  rec-pm-quotes  python -m btc_signal_engine.recorders pm-quotes  writes data/research/polymarket_us.db
  rec-pm-settle  python -m btc_signal_engine.recorders pm-settle  same DB, settlement rows
  (later) rec-spot  raw exchange ticks for S3, only if the 5m plan earns it
```

Each service: `restart: unless-stopped`, its own healthcheck, the shared data volume, config from env / `.env`
(names only in docs). Without Docker, the same entry points run under a portable supervisor:
`python -m btc_signal_engine.supervise` (app + recorders, restart with backoff, one log per child, clean stop on a
stop file or signal). OS integration is a thin, documented example per platform that only starts the supervisor
at boot (systemd unit, launchd plist, Windows Task Scheduler); no logic lives in them.

## Well-Architected review

- **Operational excellence.** Everything that runs is in the repo (compose, entry points, supervisor), started one
  way per host. Deploy = `git pull` + `docker compose up -d --build` (or the supervisor's restart); the Windows
  `deploy_stop.ps1` becomes `python -m btc_signal_engine.ops stop` (same stop-file protocol, any OS). Runbooks in
  docs/operations.md. Every incident gets a docs/solutions note.
- **Security.** Unchanged boundaries: port on 127.0.0.1/LAN only, never the internet; keys only via env/`.env` or
  read-only mounted files, never baked into the image or logged; non-root container user; recorders get only the
  Polymarket keys they need (read-only market data), not Kalshi's. The hard-coded `.env` path goes away.
- **Reliability.** Failure domains split: a deploy or crash of the app no longer stops data collection, and a
  recorder crash never touches trading. Restart policies everywhere, start at boot. Detection: the app reads each
  recorder's newest row (read-only) and alerts on Telegram after 2 minutes of silence; the daily summary shows
  coverage (5m windows recorded of 288, longest gap). Backups: the existing nightly copy, plus a pre-migration copy
  on every schema change (today's manual step, automated).
- **Performance efficiency.** Recorders are light (one WebSocket, a few writes/second). SQLite stays: one writer
  per file (app → btcflow.db, recorders → research DB). On Docker Desktop (Windows/macOS) the database lives on a
  named volume, not a bind mount (WAL on a virtualized host folder is slow and unsafe); Linux can bind-mount.
- **Cost optimization.** No new services or paid infrastructure; one image for all services; recorders run only
  what the research plan needs. The fast spot recorder (S3) waits until results justify it.
- **Sustainability.** Small, idle-friendly processes; logs rotate (10 MB × 10, as the app already does);
  recorded data is pruned/archived on the existing trades-archive pattern.

## Units

- **P1 Recorders as package entry points.** Move the two recorders into `btc_signal_engine/recorders/` (adapters
  for I/O, no domain changes), config from Settings (`BTCFLOW_RESEARCH_DIR`, the Polymarket key settings), no
  hard-coded paths; scripts stay as one-line shims for compatibility. Tests with a fake SDK stream.
- **P2 Portable supervisor + stop command.** `btc_signal_engine.supervise` and `btc_signal_engine.ops stop`;
  replaces `btcflow-watchdog.cmd` and `deploy_stop.ps1` (kept one release as wrappers). Tests on all three OSes in
  CI (matrix: ubuntu, macos, windows).
- **P3 Compose services.** Add the recorder services, healthchecks (newest-row age), named volume on Desktop hosts;
  CI builds the image and runs `docker compose config` + a smoke start of each service.
- **P4 Freshness alerts.** App reads recorder freshness read-only; Telegram alert kind `data` (default on) and the
  coverage line in the daily summary; a row on the Record or settings screen only if cheap.
- **P5 Docs.** README (tested platforms, Docker quick start, no "unmaintained" note), docs/operations.md (one
  section per host type), CHANGELOG, architecture.md (services diagram).
- **P6 Cut-over on this PC.** Run the app and recorders under the supervisor (or Docker, if installed); retire the
  `.cmd` window. Between trades, after agent sessions end (a restart ends them).

## Decisions for the user

1. **Docker on this PC:** install Docker Desktop (Windows ARM64 is supported) and run the live app in containers,
   or keep the live PC on the portable supervisor and use containers for Linux/macOS/servers? Recommendation:
   supervisor here now (no new install, no 1.9 GB database move), containers proven in CI and on a Linux host later.
2. **Start at boot:** OK to register the supervisor to start at logon on this PC (a one-line Task Scheduler entry
   that runs the portable supervisor)?

## Verification

- Unit: recorder parsing/writes, supervisor restart/backoff/stop, freshness thresholds.
- CI matrix on ubuntu/macos/windows; image build; compose config; each service starts and passes its healthcheck.
- Live: kill a recorder → restarted within 10 s and a Telegram alert only if silent > 2 min; deploy the app → the
  recorders keep writing (no gap in pm_ws_quotes); reboot → everything back without a hand.

## Build notes (2026-10-31, P1-P5 built on feat/portable-runtime)

- Scope added by the user: Docker Desktop is the target on this PC ("like Linux"); the same image and services must
  go to Azure Container Apps, AWS ECS / Fargate or a Swarm. Built: named volumes (`settleview-data`,
  `settleview-research`), `BTCFLOW_<NAME>_FILE` secrets, stderr logs in containers (`BTCFLOW_LOG_FILE=false`),
  `deploy:` keys (one replica, stop-first updates), the cloud section in docs/operations.md.
- Then the user moved the database to **Postgres in a container (P7, next)**. Deferred to P7, not built here:
  - DONE in P7 (feat/postgres): a `backup` compose service (consistent snapshots of each database to a `settleview-backups` volume on
    a schedule, keep 7 daily, a documented restore, an off-site copy hook). The app's nightly SQLite backup
    (bootstrap/maintenance.py) keeps running meanwhile, onto the `settleview-data` volume.
  - DONE in P7 (`ops migrate-sqlite-to-pg`): a portable data-migration command that copies the host's database files into the new store and
    verifies them (integrity check, row counts), for the cut-over.
  - The `postgres` service slots into compose.yaml next to `app`; the app and recorders already take their data
    locations from settings.
- Decisions made while building: the settlement recorder's silence limit is 10 minutes, not 2 (it writes one row per
  closed window, every 5 minutes at best); the daily coverage line also goes out on a day without trades when the
  `data` alert is on; under the supervisor the app is restarted 10 s after a clean exit (watchdog parity) unless held
  (`ops stop --hold` / `ops release`).
