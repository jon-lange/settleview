# Infrastructure and observability evaluation (containerized Settleview)

Status: evaluation for the user's decisions. Nothing in it is built. Written 2026-10-31, after the same-day cut-over to
Docker Compose (project `settleview`: app, postgres, rec-pm-quotes, rec-pm-settle, backup) on a Windows 11 ARM64
dev host (Docker Desktop, linux/arm64).

User's goals (2026-10-31): optimal performance and high scalability without over-engineering; Well-Architected
Framework (WAF); no decisions made from the host machine's architecture; OpenTelemetry as the default for logging;
a lightweight local backend that covers both AI/LLM and standard observability (Grafana is not required).

**What "measured" means here.** Measured = read on the live stack today, read-only (`docker compose ps`, `docker
stats --no-stream`, `docker compose logs`, `GET /api/health`, `docker image ls/history`, `docker system df -v`, the
Postgres config file and process list read through `exec -T` with no SQL and no secrets, and throwaway `--network none`
containers of the image). Inferred = read from the code or docs and not run. No load was put on the live stack. No
database query was run.

## 0. Summary

The cut-over is sound. One image, split failure domains, secrets as files, non-root, `init`, healthchecks, stop-first
single replicas, a tested dump/restore, CI on both dialects. The app is quiet on the box: 6 % CPU, about 300 MiB, the
event loop's worst lag in the last hour under 25 ms. The gaps are mostly the next layer:

1. **Live risk: the Polymarket trading feed flaps.** It went `stream_up`/`stream_down` 34 times in 2 minutes. Every
   flip writes a `trading_audit` row, forces a REST poll, and holds `/api/health` at `degraded`. The cause is the
   2 s freshness limit (`MAX_AGE_MS`) on a feed that is quiet for 2 to 7 s right now (section 1, A11).
2. **No telemetry beyond logs and `/api/health`.** There are no metrics, no traces, and no record of request rates or
   latencies. About 85 % of the app's log lines are `httpx` INFO lines for Kalshi perps polling (about 240 a minute).
3. **The scaling ceiling is one process by design** (`workers` must be 1). Inside it, about 29 synchronous database
   calls run on the event loop and 21 sync routes run in a 40-thread pool. A 10-connection pool sits behind them. That
   is fine on a local Postgres. It becomes the first bottleneck in the cloud, where each round trip costs 0.5 to 2 ms.
4. **Some choices came from the Windows ARM64 PC:**
   - `cryptography` was held at 46.0.3 because 50.x has no win_arm64 wheel (done 2026-11-01: one pin, 50.0.2, everywhere; tests and gates run in the Linux dev container).
   - There is no `uvloop` or `httptools`, so uvicorn runs the pure-Python loop and parser.
   - pyarrow and statsmodels are excluded.
   - The app reports peak RSS as `memory_mb` on Linux, while Windows reports current private bytes.
   - CI tests amd64 only, but the only arm64 image is built by hand on the PC.
5. **Postgres runs on stock defaults:** `shared_buffers=128MB`, `max_wal_size=1GB`. No service has a resource limit.
   There is no WAL archiving, so a lost volume loses up to 24 hours (RPO 24 h). The trade archive sits in the same
   database and the same nightly dump, so both grow without bound.

**Observability recommendation:** OpenTelemetry SDK in-process, exporting OTLP and configured only by `OTEL_*`
variables. Locally, add one **OpenObserve** container: logs, metrics and traces plus gen_ai LLM views. Budget about
1 GiB RAM, under 0.1 vCPU idle and under 1 GB of disk for 14 days. In the cloud the same app exports to the Azure
Container Apps managed OTel agent or an ADOT collector. Details are in section 3.

## 1. Host / architecture assumptions audit

Legend: **OK** still right for Linux containers and cloud; **Change** should change; **Watch** right now, revisit
under load or in the cloud.

| # | Where | Decision made on / for the PC | Containers / cloud | Recommendation |
|---|---|---|---|---|
| A1 | `pyproject.toml:24`; `docs/plans/2026-10-30-1402-feat-agent-mode-mcp-plan.md:146` | `cryptography==46.0.3` held because 50.x has no win_arm64 wheel | **Change.** A security library pinned for one dev host's wheel. Linux has wheels for both arches. | Done 2026-11-01 (user): one pin, `cryptography==50.0.2`, on every platform; development and tests run in the Linux dev container (`scripts/dev.py`), so no dev host shapes a dependency. |
| A2 | `pyproject.toml:11`; `bootstrap/app.py:752-755` | `uvicorn` without `[standard]`, so no `uvloop` or `httptools` (uvloop has no Windows build) | **Change (cheap).** On Linux, uvloop and httptools are the standard fast path. uvicorn's `loop="auto"` picks them up when installed. | Add `uvloop` and `httptools` behind a `sys_platform != 'win32'` marker. Measure loop lag before and after with the load test (section 2.8). Needs the user's OK. |
| A3 | `README.md:128`; `docs/plans/2026-10-28-0955-...-plan.md:186` (KTD1) | pyarrow, statsmodels, pandas-ta, arch excluded (no win_arm64 wheels) | **Watch.** Nothing in the app needs them. pyarrow would give a Parquet export for the trades archive (D4). | Keep them out of the app image. If an archive export is wanted, put pyarrow in a `research`/`archive` extra, not the runtime. |
| A4 | `docs/plans/2026-10-28-0955-...-plan.md:157` | NautilusTrader / hftbacktest rejected for Windows ARM64 | **Watch.** The reason no longer holds in containers. | No action. Only revisit if execution simulation is wanted. |
| A5 | `pyproject.toml:44` | `psycopg[binary]` skipped on win32+ARM64 by marker; SQLite kept as the bare-Windows store | (Marker removed 2026-11-01 with A1.) **OK** as a dev convenience. **Watch:** two databases means two code paths to keep honest. CI covers both. | Keep. State in README that Postgres is the supported production store. Consider dropping SQLite-as-runtime after the 2026-11-07 rollback window, keeping it for tests only. |
| A6 | `pyproject.toml:32`; `bootstrap/config.py:120-122`; `.env.example:33` | `BTCFLOW_AGENT_CLI_PATH` exists because the win_arm64 sdist has no bundled CLI | **OK.** The image uses the bundled linux-arm64 CLI. It is 257 MB, the largest package in the image (measured). | Keep the setting (pinning a vetted CLI is a valid use). Never set it in container env (the docs already say so). |
| A7 | `pyproject.toml:29` | `tzdata` because Windows has no zoneinfo database | **OK.** Harmless and it makes zoneinfo deterministic. | Keep. |
| A8 | `bootstrap/app.py:204-232` (`private_bytes`) | Windows: current private bytes. Linux: `ru_maxrss`, the **peak** RSS | **Change.** On Linux, `/api/health.memory_mb` and the 10-minute `memory:` log line show a high-water mark. Measured: 410 MB reported vs 296 MiB current in `docker stats`. A leak and an old spike look the same. | Read current RSS on Linux (`/proc/self/statm`, or the cgroup's `memory.current`). Better: let OTel's `process.memory.usage` metric replace it (section 3). |
| A9 | `bootstrap/processes.py`, `bootstrap/supervise.py:221-289`, `bootstrap/ops.py:110` | Win32 job objects, `pythonw.exe`, `.venv` launcher handling | **OK.** Only on the no-Docker supervisor path. Containers never import them. CI runs the tests on all three OSes. | Keep. No container code path depends on them. |
| A10 | `adapters/sqlite/db.py:34-42`, `writer.py:61` | Writer flush 250 ms and `synchronous=FULL`, tuned by measuring SQLite commits on the PC (0.6 / 3.4 ms) | **Watch.** On Postgres a flush is a network round trip plus fsync. Locally `writer_pending` is 0 to 2 (measured). On managed Postgres with zone-redundant HA, commit latency is several ms. | Keep 250 ms. Export flush duration and batch size as metrics (section 3) and re-measure on the cloud database before tuning. |
| A11 | `adapters/polymarket_us/market_data.py:56`; `application/trading.py:2212-2222` | `MAX_AGE_MS = 2_000` for an open window's connection. Set on 2026-10-29 when "current windows stream ~10 msg/s" | **Change, live today.** Not an arch issue: a threshold tuned on one session's traffic. Measured: `last_msg_age_ms` from 2.2 to 7.3 s, and 34 up/down flips in 2 minutes. Each flip writes an audit row (`trading.py:593-598`), forces a poll and logs a line. | Add hysteresis: down after, say, 2 s **and** no message within a second limit; up only after N messages. Or go stale only when the book itself is old. Decide with live data, offline first. Until then, expect `degraded` with Polymarket open. |
| A12 | `bootstrap/stall.py:33-34` | Stall warning at 500 ms, probe every 100 ms | **OK.** These are plain wall-time limits, not tied to the host. Measured: 1,953 probes under 10 ms, 4 under 25 ms, 0 stalls. | Keep. Export the histogram as an OTel metric. |
| A13 | `bootstrap/config.py:129` | `agent_wake_concurrency = 4` (1 to 32): "lower it if wakes slow the PC" | **Watch.** Each wake is one Claude Code CLI process. Its RSS has not been measured (inferred 150 to 300 MB). With a fleet of 30 and no memory limit, 4 at once is about 1 GB on top of the app. On Fargate or ACA the task size sets the cap. | Keep 4 as the default. Measure CLI RSS per wake in the load test. Size the app's memory limit as base + concurrency × CLI RSS. In the cloud, a separate wake-worker service is the scaling path (section 2.3). |
| A14 | `bootstrap/config.py:178, 189-190` | `workers = 1` enforced (single writer, in-memory trading state, KTD3) | **OK by design**, not a host leftover. | Keep. Scale by splitting services (section 2.3), never by uvicorn workers or replicas of `app`. |
| A15 | `adapters/sqlite/db.py:45-52` | `pool_size=5, max_overflow=5` per engine (write + read engine); default `pool_timeout` 30 s | **Watch.** Measured: 7 idle `settleview_app` backends. The 21 sync routes share the read pool in Starlette's 40-thread pool. Under many clients a request can wait up to 30 s for a connection. | Make pool size a setting. Set `pool_timeout` to about 5 s so a saturated pool fails fast and visibly. Export pool checked-out/overflow metrics. PgBouncer is not needed (section 2.4). |
| A16 | `adapters/sqlite/tables.py:30-48` | JSON as TEXT on Postgres (NaN/Infinity), unbounded VARCHAR, money as Float | **OK.** Deliberate and documented (feat-postgres PD4). Nothing queries inside the JSON. | Keep. Revisit JSONB only if a query needs it. |
| A17 | `adapters/sqlite/db.py:119, 204-216`; `config.py:85` | SQLite-era archive: move `trades` older than 60 days into a second table in **the same** database, 50k rows per transaction | **Watch.** On SQLite the archive was a separate file outside the backups. On Postgres it lives inside the dump and the live volume, so both grow forever. Measured: PG volume 4.5 GB vs 1.9 GB as SQLite; first dump 260 MB. | See D4: partition `trades_archive` monthly, or export closed months off the database. |
| A18 | `bootstrap/app.py:1661`; `bootstrap/logs.py:7` | Log format `%(asctime)s` in process local time (`TZ` from `.env`), no offset | **Change (cheap).** Measured: app lines in CDT, Postgres lines in UTC, in the same collector. | UTC with an offset (ISO 8601) in every service, or structured OTLP logs (section 3), which carry their own timestamps. |
| A19 | `compose.yaml` (`ports`, `BTCFLOW_PC_PORT`) | The PC-vs-phone model: port 8001 is published on host loopback only | **OK locally. Must not exist in the cloud** (already documented). | Keep. Before the cloud, add a startup refusal when `BTCFLOW_PC_PORT` is set and a cloud marker is present, or document it as a deploy-check item. |
| A20 | `.github/workflows/ci.yml` (`image` job) | CI builds and tests the image on amd64 only. The arm64 image that runs live is built by hand on the PC. | **Change.** The live arch is never built or tested by CI, and cloud targets differ: Azure Container Apps runs linux/amd64; Fargate offers both. | Build multi-arch with `docker buildx --platform linux/amd64,linux/arm64` (QEMU or a native arm64 runner) and run the smoke tests on both. Push to a registry only once one is chosen. |
| A21 | Host clock | Measured: `clock_offset_ms` 3,394 vs Coinbase. The container clock equals the host's (checked). | **OK in the cloud** (NTP). The app corrects for it. | Sync Windows time (the operations runbook). Not a container issue. |

## 2. Container and infrastructure review (six WAF pillars)

### 2.1 Measurements (2026-10-31 ~18:50 UTC, quiet market, no agent wakes running)

| Service | CPU | Memory | PIDs | Notes |
|---|---|---|---|---|
| app | 6.2 % | 296 MiB (health says 410 MB, peak RSS) | 26 | `lag_ms` 174 to 552; loop lag p100 under 25 ms; writer_pending 0 to 2; `/api/health` answers in 4 to 26 ms |
| postgres | 1.3 % | 2.23 GiB (mostly page cache from the cut-over; no limit) | 13 | 7 app connections; checkpoints every 5 min, 6 to 12 % of buffers; one WAL-triggered checkpoint after the move |
| rec-pm-quotes | 0.6 % | 64 MiB | 5 | |
| rec-pm-settle | 0.03 % | 58 MiB | 2 | about 30 REST lookups a minute, each logged at INFO |
| backup | 0 % | 10 MiB | 3 | |
| **Total** | **~8 % of 1 vCPU** | **~2.7 GiB** (~0.5 GiB without the PG cache) | | |

| Item | Value |
|---|---|
| Image `settleview:local` | 1.36 GB on disk, 320 MB content (compressed). The `pip install` layer is 872 MB. Largest packages: claude_agent_sdk 257 MB (bundled CLI), scipy 148 MB with libs, pandas 83, numpy 68 with libs, ccxt 65, talib 28, psycopg_binary 25 |
| Image `postgres:17.10-bookworm` | 644 MB on disk, 160 MB content |
| Volumes | settleview-pg 4.54 GB (pg_wal 1.1 GB, base 3.3 GB); settleview-backups 260 MB (one dump); settleview-data ~0 |
| Logs (2 min sample) | app 551 lines / 82 KB, so ≈ 275 lines/min, ≈ 59 MB/day. rec-pm-settle 63 lines/9.6 KB; postgres 2; quotes 1. App mix: 8 Kalshi perps GETs every 2 s = 464 of 551 lines; 68 stream up/down; 12 Telegram `getUpdates` (one per 10 s, token redacted correctly) |
| Log driver | Docker Desktop `local` driver (rotates by default: 5 × 20 MB per container). Disk is bounded, but the app's local history is only about 1.7 days at today's rate. |
| Image drift | The recorders run an older image ID (`d565dc3681f1`) than the app (`2c737952438c`). Images carry no git SHA label, so nothing says which commit runs where. |

### 2.2 Image

| Topic | Today | Recommendation | Pillar |
|---|---|---|---|
| Base | `python:3.13-slim` by tag (3.13.18 inside), not by digest | Pin `python:3.13-slim@sha256:...` (and `postgres:17.10-bookworm@sha256:...`). Renovate or Dependabot bumps them. | Security, Reliability |
| Layer caching | Code is copied **before** `pip install -e`, so every code change reinstalls the 872 MB layer | Copy `pyproject.toml` first and install dependencies into their own layer (a non-editable install of the dependencies). Then copy code. Use a BuildKit cache mount for pip. Expect rebuilds in seconds instead of minutes (not measured). | Operational excellence, Cost |
| Reproducibility | Top-level pins only; transitive deps resolve at build time | A lock file (`uv lock` / `uv export --hashes` to `requirements.lock`) installed with `--require-hashes`. uv is already used in CI. | Security, Reliability |
| Size | One fat image for app and recorders | Fine for now: a single image is simpler and pulls once. Only if pull time matters on Fargate: a `recorder` target without claude_agent_sdk, scipy and ta-lib (about −450 MB). Distroless is **not** worth it: the healthchecks need Python, the agent spawns a CLI, and slim is already small in compressed terms. | Cost, Performance |
| Multi-arch | arm64 by hand, amd64 in CI | `buildx` multi-arch (A20). | Reliability, Portability |
| Labels / version | none | OCI labels `org.opencontainers.image.revision` and `.version`; tag `settleview:<git-sha>`; report the SHA in `/api/health` | Operational excellence |
| SBOM / scan | none | `docker buildx build --sbom=true --provenance=true` and a Trivy or Grype scan in CI (fail on fixable criticals). Tools run in CI only; nothing is added to the app. | Security |

### 2.3 Runtime and scaling model

| Topic | Today | Recommendation | Pillar |
|---|---|---|---|
| Resource limits | None on any service (measured: `Memory 0`, `NanoCpus 0`) | `deploy.resources` limits and reservations. Starting point: app 2 GiB / 2 vCPU (agent wakes are the variable part), postgres 3 GiB, recorders 256 MiB / 0.5 vCPU, backup 512 MiB. Set them after the load test. In the cloud these are the task or container sizes. | Reliability, Cost |
| Probes | One healthcheck per service (liveness). `/api/health` returns 200 whether `live` or `degraded` | Keep the healthcheck as liveness: it should answer while the loop is alive. Add `/api/ready` (DB reachable, migrations done, critical loops beating) for ACA/ECS readiness and startup probes. Keep `degraded` as information, never as a restart trigger. | Reliability |
| Healthcheck cost | Each check spawns a new Python process: the app every 30 s, the recorders an import of the package and a DB connect every 30/60 s | Acceptable (small). Optional later: an `ops fresh` HTTP endpoint inside each recorder. | Sustainability |
| Shutdown | `init: true`, SIGTERM, 60 s grace, stop-first | Good. In the cloud: ECS `stopTimeout` 60 (2 min max) and ACA termination grace 60. | Reliability |
| Hardening | Non-root (uid 10001), `init` on; root fs writable; no `cap_drop`; no `no-new-privileges` | Add `read_only: true` with tmpfs for `/tmp` and the agent's `config_dir`/`cwd` on the `/data` volume. Test that the CLI writes only there. Add `cap_drop: [ALL]` and `security_opt: [no-new-privileges:true]` for app and recorders. | Security |
| Singleton by design | `app`: in-memory trading state (OrderGate, sessions, brackets), one writer task, venue WebSockets, Telegram long polling (one poller per token) | Stays `replicas: 1`, stop-first. Never autoscale `app`. | Reliability |
| What could scale out, only if load demands | 1) a **read API / SSE fan-out** service: stateless if it reads Postgres and a pub/sub (Postgres `LISTEN/NOTIFY` is enough) instead of in-process state; 2) **agent wake workers**: the CLI processes are the heaviest per-unit cost, and a worker service could run wakes, talking back through the same OrderGate API; 3) recorders are already split | None now. Load test first (2.8). The likely order: uvloop and offloading sync DB calls (cheap) → raise the agent memory limit → wake workers → read replica service. | Performance |
| Event-loop blocking | 29 direct synchronous store calls in 19 `async def`s (AST scan; e.g. `coinbase/backfill.repair_gaps`, `collector.run`, `perp_trading._read_funding`), plus sync helpers they call. Only the writer and five reads are offloaded. | Locally harmless (measured lag under 25 ms). In the cloud each call adds 0.5 to 2 ms on the loop. Move the hot ones (collector, perps monitor, notifier) to `asyncio.to_thread` or the writer's executor. The stall watch already logs the stack of any stall over 500 ms. | Performance |
| Sync routes | 21 `def` handlers (e.g. `/api/play/now`, polled every 2 s per open Play screen) run in AnyIO's default 40-thread pool and read in-memory state from threads | Fine at a handful of clients. The load test should confirm the pool and the read engine's 10 connections hold at 50 clients (2.8). | Performance |

### 2.4 Data

| Topic | Today | Recommendation | Pillar |
|---|---|---|---|
| Tuning | Stock: `shared_buffers=128MB`, `max_wal_size=1GB`, `max_connections=100`, no `work_mem`/`effective_cache_size` | A small `command:` with `-c` flags, sized for a 16 GB host shared with everything else: `shared_buffers=1GB`, `effective_cache_size=3GB`, `work_mem=16MB`, `maintenance_work_mem=256MB`, `max_wal_size=4GB`, `checkpoint_completion_target=0.9`, `random_page_cost=1.1` (SSD), `log_min_duration_statement=250ms`, `track_io_timing=on`, plus `shared_preload_libraries=pg_stat_statements`. Managed servers get the same through their parameter group. **Not arch-specific**: sized to RAM, not CPU type. | Performance |
| Indexes, hot tables | `trades` (13.6M): PK `trade_id` + `ix_trades_ts`; `index_1s` (2.8M): PK `ts`; `trading_audit`: `ts`, `bracket_id` | Likely adequate (time-range reads). The PD7 query-plan check was never run. With `pg_stat_statements` on, take the top 10 statements after a day and `EXPLAIN (ANALYZE, BUFFERS)` them **read-only** with the research role. A BRIN on `trades_archive.ts` is the cheap win if it is ever queried. | Performance |
| Retention / growth | `trades` capped at 60 days, but the archive is in the same DB. `index_1s`, `book_1s`, `pm_ws_quotes` (1/s), `pm_trades` (raw JSON) and `trading_audit` (flapping, A11) are never pruned | Decide a retention per table (D4). Simplest durable option: partition `trades_archive` (and `pm_*`) by month; a closed month can be detached, dumped once off-site and dropped. Keep `index_1s` (small). | Cost, Sustainability |
| Backups | Nightly `pg_dump -Fc`, 7 + 4 kept on the **same disk**; restore drill in CI; `BACKUP_HOOK` exists but no off-site copy is set | **P0**: set up the off-site hook (a cloud bucket or another disk). **P1** locally: WAL archiving for PITR (`archive_mode=on`, `archive_command` to a second volume, or pgBackRest) only if a 24 h RPO is not acceptable. Orders live on the venues, but the journal and Record do not. In the cloud, the managed service's PITR (7 to 35 days) replaces both. | Reliability |
| Pooling | 2 engines × 10 max + recorders 1 to 2 each, so under 30 connections | **No PgBouncer.** Under 30 connections it only adds a moving part. Revisit if a read API scales out (each replica's pool adds up), or on a managed tier with low `max_connections`. | Cost (don't over-engineer) |
| Cloud | Documented: Azure Flexible Server / RDS 17, private network, `sslmode` | Use `verify-full` with the provider CA. Burstable B2s/db.t4g.small is enough for this load (inferred from 1 % CPU). Turn on Query Store / Performance Insights. | All |

### 2.5 Security

- **Good:** secrets as files (`*_FILE`), passwords never in env or URLs, three DB roles with a read-only research role,
  non-root image, DB port not published, 8001 on loopback, CSP, a Host allow-list, Telegram token redaction on the
  `httpx`/`telegram` loggers (measured: `bot<telegram-token>` in the logs).
- **Gaps:** no image scan or SBOM (2.2); writable root fs and default capabilities (2.3); base images by tag; Postgres
  dumps unencrypted on the same disk; port 8000 is plain HTTP on the LAN (accepted, documented).
- **New risk the OTel work introduces:** HTTP client instrumentation records `url.full`. The Telegram API puts the bot
  token in the URL path (`/bot<token>/getUpdates`), so a default httpx instrumentation would ship the token to the
  telemetry backend. The log redactor does not cover span attributes. The OTel work **must** scrub URLs: an httpx
  instrumentation request hook that rewrites `/bot…/` before the span is exported, or exclude `api.telegram.org`
  from tracing. It must also never capture request headers (Kalshi signatures) or GenAI prompt/response content by
  default. A test should fail if a token appears in an exported span.
- The agent harness blanks every inherited `OTEL_*` variable for the CLI (`adapters/agent_sdk/harness.py:71-72`).
  That is the right default (headers can hold backend tokens). Turning on Claude Code's own telemetry would be an
  explicit, narrow pass-through: endpoint and protocol only.

### 2.6 Cost and sustainability

- On this box the stack idles at ~8 % of one core and ~0.5 GiB plus Postgres cache. Cost is the PC's electricity.
- The two largest recurring wastes are both cheap to fix: about 240 INFO log lines a minute from perps polling (set
  the `httpx` logger to WARNING and use OTel metrics for request counts), and the A11 flapping (audit rows, forced
  polls).
- Cloud estimate (inferred, list prices vary): one 0.5 to 1 vCPU / 2 GiB app task plus two 0.25 vCPU recorders plus a
  burstable managed Postgres, roughly USD 40 to 90 a month. The managed database is the largest line. Fargate arm64
  (Graviton) is about 20 % cheaper than amd64, which is a reason to keep the arm64 build (A20).

### 2.7 WAF pillar scorecard

| Pillar | State | Biggest gap |
|---|---|---|
| Operational excellence | Good runbooks, CI drills | No metrics/traces; no image version label; noisy logs |
| Security | Good secrets and role model | No scan/SBOM, writable root fs, unencrypted dumps on-host; future span-token leak |
| Reliability | Split failure domains, restarts, tested restore | Dumps on the same disk; RPO 24 h; A11 flapping; no limits |
| Performance efficiency | Quiet, low lag | Stock PG config, no uvloop, sync DB on the loop, never load-tested |
| Cost optimization | One image, small footprint | Archive and dumps grow forever |
| Sustainability | Idle-friendly | Log and poll noise |

### 2.8 Load test plan (not run; for a copy of the stack, never the live one)

Run it against a second compose project (`-p settleview-load`) on a restored dump, trading off (shadow modes only),
with real public feeds or a recorded replay. Tool: **Locust**, which is Python and handles SSE. That is a new dev
package; k6 in its own container is the no-Python alternative. Watch the loop-lag histogram, `writer_pending`, pool
checked-out, CPU/RSS per service and p95 latency.

| Scenario | Shape | Pass bar (proposed) |
|---|---|---|
| Play screens | 1 → 10 → 50 virtual phones, each polling `/api/play/now` every 2 s, odds every 5 s, kill every 5 s, score every 10 s (the real `web/views/play.js` cadence) | p95 under 150 ms; loop lag under 50 ms; no pool timeouts |
| SSE fan-out | 10 → 100 open `/api/perps/stream` + agent session streams | Frames still coalesce at 100 ms; app CPU under 1 vCPU; RSS growth flat over 30 min |
| Market bursts | Replay the busiest recorded Coinbase second ×5 and Polymarket books at 10 to 50 msg/s per market | `writer_pending` bounded; no stall over 500 ms |
| Agent fleet | 30 shadow sessions on a fake model endpoint (the harness's test `base_url`) at concurrency 4 / 8 / 16 | Per-CLI RSS measured; queue latency per wake; app memory limit sized from it |
| Record / reads | 5 clients hitting `/api/record/summary` (all ranges) while the above runs | p95 under 500 ms |

Over-engineering guard: run once now on this box and once on the first cloud target; only scale out what fails a bar.

## 3. Observability

### 3.1 Current state (measured)

- **Logs:** Python `logging`, plain text (`%(asctime)s %(levelname)s %(name)s: %(message)s`) to stderr in local time.
  The Docker `local` driver keeps 100 MB per container. No shipping, no structure, no trace ids.
- **Health:** `/api/health` is rich: streams, loop beats, loop-lag histogram, writer backlog, recorders, memory.
  But it is a point-in-time JSON with no history.
- **Alerts:** Telegram (`data` alert for recorders, daily summary, trading notices).
- **Agent usage:** already captured in-process per wake: tokens, cost, duration, models, tool calls through the
  Pre/PostToolUse hooks (`adapters/agent_sdk/harness.py`). This is exactly what GenAI spans need. It is stored and
  shown, not exported.
- **Missing:** metrics over time, request latency, DB query timing, traces across HTTP → OrderGate → venue → writer,
  agent wake traces.

### 3.2 Recommendation: OpenTelemetry as the default

- **SDK in-process, OTLP out, configured by standard env only** (`OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`,
  `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_PROTOCOL`, `OTEL_{TRACES,METRICS,LOGS}_EXPORTER`).
  - No endpoint set: telemetry is off (stderr logs as today).
  - An unknown exporter is an error, per the "no silent fallbacks" rule.
  - The same image feeds a local backend, the Azure Container Apps managed OTel agent (it injects the endpoint),
    an ADOT collector on ECS, or any OTLP vendor.
- **Traces:** the FastAPI, httpx, SQLAlchemy and asyncio instrumentations, plus a few manual spans:
  - `order.prepare → gate → send → fill`;
  - `writer.flush`;
  - `agent.wake` with GenAI attributes: `gen_ai.operation.name=invoke_agent`, `gen_ai.provider.name=anthropic`,
    `gen_ai.request.model`, `gen_ai.usage.input_tokens` / `output_tokens` (and cache tokens), a cost attribute, tool
    calls as `execute_tool` child spans from the existing hooks, latency.
  - Exclude `/api/health` and the 2-second polls (`OTEL_PYTHON_EXCLUDED_URLS`), or sample them, to keep volume small.
- **Metrics:**
  - the loop-lag histogram, `writer_pending` and flush duration;
  - pool checked-out and overflow;
  - stream connected/stale per stream and recorder age (the `/api/health` fields as gauges);
  - request duration from instrumentation;
  - agent tokens, cost and wakes queued/running;
  - process CPU and RSS (replaces A8).
- **Logs:** the OTel logging instrumentation bridges `logging` records to OTLP with trace ids. stderr stays on as the
  fallback. Python's OTel **logs signal is still "Development"** (traces and metrics are stable). Keep stderr as the
  source of truth until it is stable.
- **Claude Code CLI's own telemetry** (metrics and events; beta traces that nest under a `TRACEPARENT` the app sets
  per wake) is **optional and off**. The app's own wake span already carries tokens and cost. Turn it on only for
  debugging, through an explicit pass-through (2.5).
- **Content capture off by default:** no prompts, completions, tool arguments or HTTP headers in telemetry. A trading
  app's prompts hold balances and positions.
- **Redaction (must-have):** see 2.5 (Telegram token in URLs).

### 3.3 Local backends compared (research 2026-10-31; sources below)

| Backend | Version seen | Containers | RAM (documented or reported) | OTLP T / M / L | LLM / GenAI views | arm64 | Licence | Fit |
|---|---|---|---|---|---|---|---|---|
| **OpenObserve** | v1.1.2 (2026-10-20) | 1 (Rust binary, local disk; S3 optional) | ~0.5 GB idle, 1 to 2 GB recommended (third-party; not in official docs) | yes / yes / yes | gen_ai spans, LLM views, AI session search, prompt registry (v1.1.0, vendor-claimed) | yes (SIMD/NEON image) | AGPL-3.0 | **Best single-container fit** |
| SigNoz | v0.148.1 (2026-10-29) | 5 (signoz, collector, ClickHouse, Keeper, Postgres) | ≥ 4 GB (docs) | yes / yes / yes | AI Observability since v0.143 (2026-09): tokens, cost, gen_ai mapping, Anthropic formatting | yes (not verified) | MIT core + ee | Most mature APM; ClickHouse weight |
| OpenLIT | platform 2.1.1 (Oct) | 2 to 3 (openlit, ClickHouse, NATS optional) | not documented (ClickHouse-bound) | yes / yes / yes | gen_ai-native; its SDK covers Anthropic, Claude Agent SDK, Claude Code | not verified | Apache-2.0 | Strong on AI, thinner general APM |
| grafana/otel-lgtm | 0.29.x (Grafana 13, Tempo 2.10, Loki 3.7, Prometheus 3.11) | 1 | ~2 GB (its script defaults to 2G) | yes / yes / yes | none built in (DIY dashboards) | yes (not verified) | Apache-2.0 | Dev/demo only (its README) |
| Arize Phoenix | v21.0.0 (2026-10-30) | 1 (SQLite or Postgres) | not documented, light | traces only | excellent agent/LLM UX, evals | yes (not verified) | ELv2 (not verified) | AI only; pairs with an APM |
| Langfuse | v4.72.0 (2026-10-29) | 6 (web, worker, Postgres, Redis, ClickHouse, S3) | 16 GiB recommended for compose | traces only (HTTP) | best-in-class LLM product | yes (not verified) | MIT + licensed add-ons | Too heavy, AI only |
| HyperDX / ClickStack | 2.43.0 (2026-10-30) | 1 all-in-one (ClickHouse, Mongo inside) | not documented | yes / yes / yes | basic LLM dashboard | not verified | MIT (not verified) | Docs: not for production |
| Uptrace | v2.0.3 | 4 (ClickHouse, Postgres, Redis) | large tiers only | yes / yes / yes | none documented | yes | not verified | No |
| Jaeger v2 | v2.22.0 (2026-10-06) | 1 | very low | traces only | partial (gen_ai tool attributes) | yes (not verified) | Apache-2.0 | Traces only |

**Recommendation.** One **OpenObserve** container as the local backend for all three signals and the agent spans.
The app exports OTLP/HTTP straight to it on the compose network. No collector locally (one less moving part). UI
published on `127.0.0.1` only, like 8001.

- **Footprint on this box (budget; not yet measured):**
  - RAM: limit it at 1 GiB, expected ~0.5 GiB.
  - CPU: under 0.1 vCPU idle.
  - Disk: today's log rate is ~59 MB/day raw. After cutting the httpx noise and sampling polls, logs plus traces
    should fit in roughly 20 to 60 MB/day compressed. With 14-day retention, budget **under 1 GB**.
- **Measure in the pilot:** RSS, CPU and disk after 24 hours.
- **Cloud mapping:**
  - Azure: the Container Apps managed OTel agent sends traces and logs to Application Insights (it does not take
    metrics there; send metrics to a generic OTLP destination or Azure Monitor managed Prometheus), or use
    `azure-monitor-opentelemetry`.
  - AWS: an ADOT collector sidecar sends to CloudWatch's OTLP endpoints (X-Ray traces, Logs, Metrics; SigV4).
  - Self-hosted Swarm: OpenObserve again.
- **If better agent views are wanted later:** add **Arize Phoenix** as a second container on the existing Postgres.
  That is the moment to add an OTel Collector that fans traces out to both backends. Not now.
- **Why not the others:**
  - SigNoz is the stronger APM but needs five containers and at least 4 GB. It is the fallback if OpenObserve's LLM
    views prove thin.
  - otel-lgtm is dev-only and has no LLM view.
  - Langfuse and Phoenix take traces only, and Langfuse is heavy.
- **Caveat:** OpenObserve is AGPL. That is fine for running it unmodified next to the app, but say so in
  docs/PUBLISHING.md.

### 3.4 Sources (fetched 2026-10-31)

- OpenObserve releases https://github.com/openobserve/openobserve/releases ; quickstart https://openobserve.ai/docs/quickstart/
- SigNoz releases https://github.com/SigNoz/signoz/releases ; Docker install https://signoz.io/docs/install/docker/
- OpenLIT https://github.com/openlit/openlit , https://docs.openlit.io/latest/openlit/installation
- grafana/otel-lgtm https://github.com/grafana/docker-otel-lgtm
- Phoenix https://github.com/Arize-ai/phoenix/releases , https://arize.com/docs/phoenix/self-hosting/configuration
- Langfuse https://github.com/langfuse/langfuse/releases , https://langfuse.com/self-hosting/deployment/docker-compose ,
  https://langfuse.com/integrations/native/opentelemetry
- ClickStack all-in-one https://clickhouse.com/docs/use-cases/observability/clickstack/deployment/all-in-one
- Uptrace https://uptrace.dev/get/hosted/install ; Jaeger https://github.com/jaegertracing/jaeger/releases
- OTel Python status https://opentelemetry.io/docs/languages/python/ ; env config
  https://opentelemetry.io/docs/zero-code/python/configuration/ ; logging instrumentation
  https://opentelemetry-python-contrib.readthedocs.io/en/latest/instrumentation/logging/logging.html
- GenAI semconv https://opentelemetry.io/docs/specs/semconv/gen-ai/ ; GenAI instrumentations
  https://github.com/open-telemetry/opentelemetry-python-genai
- Claude Code telemetry https://code.claude.com/docs/en/monitoring-usage
- Azure Monitor distro https://pypi.org/project/azure-monitor-opentelemetry/ ; ACA OTel agent
  https://learn.microsoft.com/en-us/azure/container-apps/opentelemetry-agents
- CloudWatch OTLP metrics https://aws.amazon.com/es/about-aws/whats-new/2026/06/amazon-cloudwatch-otel-metrics/

Footprints marked "not documented" or "third-party" are not vendor numbers. Arm64 availability not checked today is
marked "not verified".

## 4. Prioritized actions

Effort: S under half a day, M one to two days, L more. "Now" = this week on the PC; "Cloud" = before the first cloud
deploy; "Load" = only if the load test or real use demands it.

| P | Action | Pillar | Effort | When |
|---|---|---|---|---|
| **P0** | **A11 flapping**: analyse PM message gaps from `research.pm_ws_quotes` offline, then add hysteresis or a book-age rule. Until then, note that `degraded` means nothing for Polymarket. **Live risk.** | Reliability | S to M | Now |
| **P0** | **Off-site dumps**: set up `BACKUP_HOOK` (bucket or another disk). Today the only dumps share the disk with the database. **Live risk.** | Reliability | S | Now |
| **P0** | **Log noise**: `httpx` logger to WARNING (keep our own WARNINGs). This cuts about 85 % of app lines and lengthens the local log history from about 1.7 days to about 10. | Ops, Sustainability | S | Now |
| **P0** | **Resource limits** on all five services (start with 2.3's numbers) and Postgres `-c` tuning (2.4) with `pg_stat_statements`. Apply at the next planned restart only (deploy freeze). | Reliability, Performance | S | Now (batched) |
| P1 | OTel foundation: SDK + OTLP env config, FastAPI/httpx/SQLAlchemy/asyncio instrumentation, **URL/token scrubbing with a test**, metrics from `/api/health` fields, `agent.wake` GenAI spans from existing usage data; OpenObserve compose service (profile `observability`) | Ops | M to L | Now (after package OK) |
| P1 | UTC ISO-8601 log timestamps in every service; image git-SHA label and `/api/health.version` | Ops | S | Now |
| P1 | Fix `memory_mb` on Linux (current RSS) | Ops | S | Now |
| P1 | Dockerfile: dependency layer before code, pip cache mount, digest-pinned bases, lock file with hashes | Security, Ops | M | Before cloud |
| P1 | CI: multi-arch build + smoke on arm64 and amd64; SBOM + Trivy scan | Security, Reliability | M | Before cloud |
| P1 | Hardening: `read_only` root fs + tmpfs, `cap_drop: ALL`, `no-new-privileges` (verify agent CLI writes only to `/data`) | Security | S to M | Before cloud |
| P1 | `/api/ready` readiness probe; `pool_timeout` ~5 s and pool size as a setting | Reliability | S | Before cloud |
| P1 | `cryptography` unpinned from the win_arm64 constraint (A1) | Security | S | Now (package OK) |
| P1 | Retention decision (D4) and monthly partitions for `trades_archive` / `pm_*` | Cost | M | Before cloud |
| P1 | Run the load test (2.8) once on a copy of the stack | Performance | M | Before cloud |
| P2 | uvloop + httptools on Linux (A2), measured with the load test | Performance | S | Load |
| P2 | Offload hot sync DB calls from the loop (collector, perps monitor, notifier) | Performance | M | Load / cloud latency |
| P2 | WAL archiving / PITR locally (pgBackRest or archive_command) | Reliability | M | If 24 h RPO is not acceptable |
| P2 | Lean `recorder` image target | Cost | S | Load (Fargate pull time) |
| P2 | Split services: agent wake workers, then a stateless read/SSE service with LISTEN/NOTIFY | Performance | L | Load only |
| P2 | Phoenix next to OpenObserve with an OTel Collector fan-out | Ops | S | Only if agent trace UX is lacking |
| P2 | PgBouncer | Performance | S | Only with many replicas or low managed `max_connections` |

## 5. Open decisions for the user

1. **OTel packages (new; need your OK). Exact names; versions pinned at build time to releases at least two weeks
   old:**
   - `opentelemetry-api`, `opentelemetry-sdk` (1.45.x line);
   - `opentelemetry-exporter-otlp-proto-http`;
   - `opentelemetry-instrumentation-fastapi`, `opentelemetry-instrumentation-httpx`,
     `opentelemetry-instrumentation-sqlalchemy`, `opentelemetry-instrumentation-asyncio`,
     `opentelemetry-instrumentation-logging` (contrib 0.66bX line, still beta on PyPI).

   Optional:
   - `opentelemetry-instrumentation-psycopg` (driver-level spans; SQLAlchemy's are usually enough);
   - `opentelemetry-instrumentation-aiohttp-client` (the Kalshi SDK's client).

   Not recommended now: `opentelemetry-instrumentation-genai-anthropic` (beta; the app calls the CLI rather than the
   SDK for wakes, so hand-written GenAI spans from the existing usage data cover it), `opentelemetry-distro` (zero-code
   auto-instrumentation; explicit setup is clearer), and `azure-monitor-opentelemetry` (only if Azure is chosen and
   plain OTLP to the ACA agent is not enough).

   **Recommendation:** approve the core set.
2. **Local backend:** OpenObserve (one container, about 1 GiB) or SigNoz (five containers, at least 4 GB, more mature).
   **Recommendation:** OpenObserve now, behind a compose profile so it is opt-in. Re-evaluate after two weeks.
3. **Other packages (need your OK):**
   - `uvloop` and `httptools` (Linux only, by marker). Recommendation: yes, but only after the load test baseline.
   - The `cryptography` bump. Recommendation: yes.
   - `locust` as a dev tool for the load test. Recommendation: yes, or k6 in a container with no Python package.
4. **Retention (D4):**
   - `trades_archive` and `pm_*`: keep forever in Postgres, or partition monthly and move closed months off the
     database (dump or Parquet)? Recommendation: partition monthly; keep 12 months online.
   - `trading_audit`: keep forever.
5. **RPO:** is losing up to 24 hours of journal and Record history acceptable on the PC? Recommendation: yes, with
   off-site dumps (P0). Add PITR only in the cloud, where it is built in.
6. **The A11 fix:** change the Polymarket freshness rule (it gates entries) after an offline gap analysis.
   Recommendation: yes; it goes through a normal plan and test, not a hot fix.
7. **Cloud arch:** keep arm64 as a first-class target (cheaper on Fargate), and build multi-arch because Azure
   Container Apps needs amd64. Recommendation: build both from CI.
8. **When to apply:** limits, Postgres tuning and the log level all need a restart. Recommendation: batch them into
   one restart between agent sessions, when you say so (deploy freeze).

## Decided (user, 2026-10-31)

1. OpenTelemetry core packages approved: `opentelemetry-api`, `opentelemetry-sdk`,
   `opentelemetry-exporter-otlp-proto-http`, `opentelemetry-instrumentation-fastapi`,
   `opentelemetry-instrumentation-httpx`, `opentelemetry-instrumentation-sqlalchemy`,
   `opentelemetry-instrumentation-asyncio`, `opentelemetry-instrumentation-logging` (optional extras not approved).
2. Local backend: OpenObserve, as an opt-in compose profile.
3. Off-site backup copies: later (same-disk dumps accepted for now).

## Built (2026-10-31, branch feat/observability)

- OpenTelemetry in the app and both recorders, inert without an endpoint; scrubbing with tests; GenAI wake spans;
  the metrics of section 3.2 (process metrics from /proc, not a new package). docs/operations.md, "Observability".
- `openobserve` (v1.1.1) as the opt-in compose profile `observability`; credential files from `make_secrets.py`.
- P0 log noise (httpx at WARNING), UTC ISO-8601 lines; P0 resource limits and Postgres tuning with pg_stat_statements
  (docs/operations.md, "Resource limits and Postgres settings"); image revision label; multi-arch CI build.
- Not in this change: A11 (Polymarket freshness; only the flap counter was added), off-site backups, `/api/ready`,
  hardening (read-only root fs, cap_drop), the lock file, the `memory_mb` fix in `/api/health` (OTel's
  `process.memory.usage` reports current RSS instead), the load test.
