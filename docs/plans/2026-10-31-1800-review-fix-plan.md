# Whole-solution review: consolidated findings and fix plan

Status: draft for the user's go. Source: six parallel code reviews (2026-10-31) of the product split into review
branches `review/1-orderpath-security` … `review/6-web` (criteria: Well-Architected, performance under high load,
STRIDE, OWASP Top 10, DRY, best practice). ~100 findings, deduplicated below by root cause. "B1#4" = branch 1,
finding 4. Findings marked (unverified) came from the branch-2 local review, which lists candidates without a
verification pass; each is checked against the code before it is fixed.

## P0 — money safety and security (fix first, one batched deploy between agent sessions)

| # | Theme | Findings | Fix |
|---|---|---|---|
| 1 | **PC identity by network position** | B1#1 sibling containers reach `app:8001` as the PC; B4#1 Swarm drops the 127.0.0.1 host IP so 8001/5080 go public; B1#2 any local process without its agent token is the PC; B4#11 loopback inside the container is the PC | Pair the PC browser: one-time code from `ops pair-pc` (run in the container), then a signed HttpOnly SameSite=Strict PC cookie; the PC listener requires it. Agent/MCP tool routes move to their own listener whose actor can never be `pc`. Healthcheck uses an unauthenticated `/api/health`. Swarm override drops 8001/5080; CI fails if `docker stack config` publishes them. |
| 2 | **Lost-response orders resolved wrongly** | B1#4 recovered entry booked at limit not fill; B1#5 filled entry closed as "not found" (no stop, limits freed); B3#2 Polymarket lost create judged from a lagging positions list; B3#5 IOC still PENDING read as canceled; B3#1 perps late fill gets no stop/TP and no "unprotected" banner; B3#3 perps give-up without checking fills | One rule for every venue: an order whose answer was lost stays **unknown** until fills/activities (not lagging lists) prove filled or not; never close as unfilled on a list miss; book the real average price; on a late fill place the saved protection or mark the position unprotected and alert. |
| 3 | **Exits blocked after a mode change** | B1#3 `mode_changed` refuses hand and agent exits on an open prod position (e.g. after a credential blip switches to sandbox) | Exits check readiness against the bracket's env, never the current mode. |
| 4 | **Agent stop/pause races** | B2 (unverified): Stop doesn't await an in-flight rule entry (fill lands on an ended session, rule row lost → auto-retire undercounts); a queued wake still runs after Pause; `_scan(push=False)` swallows other positions' stop-outs; global API limit has no shared reservation; failed tool calls skip the spend/stop gate | Stop drains every in-flight order task and journals it; wakes re-check session state when they start; scans re-baseline only the touched bracket; global reservation in the fleet; gate on PostToolUseFailure/deny too. |
| 5 | **Database outages and stalls** | B4#2 writer dead-letters everything after ~35 s of outage (no replay, no alert); B4#3 no statement/lock/idle timeouts or TCP keepalives, sync calls on the loop → a lock or half-open socket freezes stops and streams | Connection errors retry forever (queue capped + alert); bisect bad batches; `ops replay-dead-letter`; Postgres timeouts + keepalives + pool_timeout; hot-path store calls off the loop. |
| 6 | **Secrets in logs/env** | B4#5 stderr not scrubbed, SQL params in errors; B2 Telegram token via child loggers/tracebacks; B4#6 venue/API keys as env vars (`docker inspect`, child processes); B2 agent CLI env is a denylist | Scrubber as a logging filter on all handlers; `hide_parameters=True`; compose `secrets:` + `*_FILE` for every key; CLI env built from an allowlist. |
| 7 | **Docker logs unrotated** | B4#8 json-file grows until the disk (shared with Postgres) fills | `x-logging` (local driver, size caps) on every service. |

## P1 — performance under load and correctness

- **Event loop and database** (B1#6, B2, B4#3): sync repository calls in async paths, N+1 (`agent_list`, notifier
  `_live` per second, `close_note` ×N), unbounded `ORDERS` scans, `tick` holding the trading lock across REST calls,
  per-session per-tick queries (~200–300/s at 30 agents), `RuleBook.enforce` per session. Batch per fleet tick, cache,
  bound scans, move I/O off the lock and off the loop.
- **Threads touching loop state** (B1#7): sync routes iterating `_brackets`, `asyncio.Event.set()` from a worker.
- **Agent fleet scale** (B2): a Claude CLI process per wake (~100 MB) with a shared config dir → semaphore + per-wake
  config dir + jitter; memory block grows with all brackets → last N + summary.
- **Front end** (B6#1–5,#8,#9): Play polling without an in-flight guard (old replies overwrite new); stale prices stay
  live after failures; slide approves the ticket unchecked; hidden tabs keep ticking; ~2.5 req/s per Play screen →
  push Play over SSE like Perps, visibility gating, stale state greys the slide, bind/check the ticket, one presence
  heartbeat, 401 handling. Done 2026-11-01 (`fix/p1-play-money`): docs/solutions/2026-11-01-play-stream-stale-and-signed-out.md.
- **Money display** (B5#1–5): scoreboard break-even ignores fees; Play "today" UTC vs Record local; record cache
  freezes unknown results; 500-trade cap; DST edge without tz. Done 2026-11-01 (`fix/p1-play-money`):
  docs/solutions/2026-11-01-one-users-day.md.
- **Hot paths** (B5#6–8): coach series recomputed per request (bisect + cache), tick re-reads 31 min of index, Record
  "all" scans the whole journal.
- **Venue resilience** (B3#6–8,#10–12): recorder socket leak + unbounded buffers in a DB outage; private-subscription
  refusal reported healthy; perps book freshness by any message (same as the 13:59 Polymarket outage); no jitter /
  Retry-After; unbounded queues and frame sizes; perps approve holds the lock for minutes.
- **Other correctness** (B1#8–11, B2): `/login` reads an unbounded body; shared lockout behind the gateway; `from_pc`
  fails open; WebSocket upgrades skip the Origin check; `/note` number parsing; proposal tap marked done before
  approve; Telegram HTML truncation; Auto vs Assist price cap differ.

## P2 — hardening, supply chain, DRY, docs

- **Supply chain** (B4#10): hashed lock file, base image and Actions pinned by digest/SHA, pip-audit/Trivy in CI.
- **Least privilege** (B4#4,#14): `settleview_recorder` role (research only); ingest-only OpenObserve account.
- **Container** (B4#12,#13,#16,#17): tini in the image, read_only + cap_drop + no-new-privileges, 60 s stop timeout
  for the supervisor, process-local liveness for recorders, frozen 0001 migration + migrations as a one-off job,
  missing `trading_orders.order_id` index (B4#18).
- **Network** (B4#9): phone port bind address a variable (default loopback on servers).
- **Backups** (B4#7): off-site + encryption + WAL archiving — deferred by the user ("later").
- **CSP** (B6#6): Alpine CSP build so `unsafe-eval` can go.
- **UX safety** (B6#11): Assist Approve as press-and-hold/slide.
- **DRY**: one bracket-net helper (~6 copies, different rounding), shared formatters (money/cents/side, minus signs),
  one Kalshi signer base with host pinning (B3#13), one ISO-time parser, one retry helper with jitter, one settings
  upsert, the stop-for-side and market lookups. *Done 2026-11-01 (docs/solutions/2026-11-01-one-rule-per-figure.md).*
- **Docs** (B6#13): routes missing from api.md/api-agents.md (copilot, perps, presets, prompts, proposals, telegram),
  stale "only client" text, response fields the UI relies on; `trading.py` docstring still says no unattended entry.
- **Fee wording**: Polymarket fee rounds to the nearest cent (production fills: 2 @ 22¢ → 2¢), not up; fix the
  LEARNINGS/strategy-doc wording and name the rule unambiguously in `VenueSpec` (B5#9).

## Checked and sound (all reviews)

Approval tokens (single use, bound, 10 s), limits can't race, agent gate (session from context, ownership, assist,
idempotency), kill switch (re-read, fails closed), market-buy worst case, settlement from final results, PIN (PBKDF2
310k, constant time, lockout), HMAC session cookie (HttpOnly, SameSite=Strict), Host allow-list, Origin/Sec-Fetch
CSRF checks, CSP and no-store, no SQL injection (move tool included), role grants and backup/restore scripts, Postgres
memory vs limits, non-root image, telemetry scrubbing of exports, SDK retries off, no redirects, same client_order_id
on retry, MarginAuth allow-list, money math core (fees, sizing, payoffs, settlement and ties, fair value, perps),
no XSS (agent/LLM text never parsed as HTML), Perps' bound tickets and stale handling.

## Regular quality gates: Trivy and linters (user 2026-10-31: "add a trivy scan as well to our regular things", "as well as a linter")

Baseline run 2026-10-31 on main / the live image (read-only):
- **Trivy 0.76.0** (`aquasec/trivy`, image scan, vuln+secret, HIGH/CRITICAL): 0 secrets, 0 critical; 9 HIGH in Python
  packages, all fixable — `cryptography` 46.0.3 (4 CVEs; held back only because newer releases have no win_arm64 wheel:
  a host-arch decision leaking into the Linux image), `msgpack` 1.1.2, `setuptools` 70.3.0, `urllib3` 2.7.0; 44 HIGH in
  Debian base packages with no fix yet.
- **Ruff 0.17.1** (rules E,F,W,B,UP,S,ASYNC,SIM; line length 120): ~550 findings, mostly style (E501, E70x, UP017);
  worth reading: 28 B905 zip without strict, 10 B023 loop-variable closures, 20 S608 SQL built from strings (expected
  to be constant identifiers — verify), 6 S110 try/except/pass, 2 ASYNC110 busy waits, 5 F841 / 7 F401. `ruff format`
  would rewrite 117 of 167 files.
- **Hadolint v2.12.0** (Dockerfile): clean. **ShellCheck v0.10.0** (`deploy/*.sh`, warning+): clean.

Status: built 2026-10-31 on `feat/quality-gates` (`scripts/quality.py`, CI jobs `lint` and `trivy`; docs/operations.md,
"Quality gates"). The image's 9 fixable HIGH are fixed (the msgpack / urllib3 / setuptools ones were pip's vendored
copies: docs/solutions/2026-10-31-trivy-findings-in-pips-vendored-packages.md); Hadolint's DL3008 is ignored inline
with its reason (the baseline above missed it); `ruff format` is still a separate decision.

Plan (after the P0 branches merge, to avoid conflicts with four parallel builds):
1. Dependency fixes: platform-marked pins so the Linux image takes the fixed `cryptography`/`msgpack`/`urllib3` while
   Windows ARM64 keeps its wheel-available pin; `setuptools` floor raised; Debian packages via a refreshed base image
   (pinned by digest) and `apt-get upgrade` in the build.
2. Ruff in `[project.optional-dependencies].dev` (pinned), config in pyproject; fix the real-bug rules (B905, B023,
   S110, ASYNC110, F841/F401, S608 review) by hand, auto-fix the safe ones, and keep style rules that would churn
   (E501, E70x) off or per-file until a deliberate one-time `ruff format` commit (separate, mechanical, reviewed by diff).
3. CI: jobs for `ruff check` (+ `ruff format --check` once adopted), Trivy (image: vuln+secret, fail on HIGH/CRITICAL
   with a fix; config/IaC scan of Dockerfile + compose; SARIF upload), Hadolint, ShellCheck — tools pinned by version
   and Actions by SHA.
4. Local: one portable command (`python scripts/quality.py`) that runs the same checks (Ruff natively, Trivy/Hadolint/
   ShellCheck from their pinned containers), so dev and CI match on any OS.
5. AGENTS.md "done" gains: quality gates green (lint + Trivy) for any change to code, image or dependencies.

## P3 — enterprise readiness (user 2026-10-31: "any other enterprise gaps?" → "Yes please")

In order:
1. **TLS for phones** — today the PIN and session cookie cross Wi-Fi in plain HTTP. Tailscale (HTTPS + remote access,
   no open ports) or a Caddy container with a local CA; then cookies `Secure`.
2. **Alerting on telemetry** — SLIs/SLOs (feed freshness, event-loop lag, order-gate errors, writer queue, agent spend,
   recorder coverage) as OpenObserve alerts → Telegram.
3. **Staging** — a `staging` compose profile on Kalshi demo + the shadow exchange; automated smoke tests gate every deploy.
4. **Release engineering** — images built in CI, pushed to GHCR, semver tags, SBOM (Trivy), cosign signatures; deploy
   by digest; one-command rollback.
5. **Append-only audit** — INSERT-only role for `trading_audit`, per-row hash chain, nightly export to write-once storage.
6. **Agent evals** — see docs/plans/2026-10-31-1900-agent-evals-plan.md (user: "I can help define eval best
   practices; I'd like those in place").
7. Before cloud / multi-user: OIDC + MFA and roles (viewer/trader/admin); HA design (active/passive with a Postgres
   leader lease; read API/SSE split from the order path); DR targets (RPO/RTO), off-site encrypted backups, scheduled
   restore drills, multi-AZ managed Postgres; Key Vault / Secrets Manager + key-rotation runbook; security programme
   (SECURITY.md, CodeQL/Semgrep, branch protection + required checks, CODEOWNERS, Dependabot/Renovate, STRIDE threat
   model in the repo, ZAP against staging); LLM cost budgets/alerts per agent and model; ADRs; venue terms check for
   automated trading.
