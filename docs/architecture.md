# Architecture

Settleview is one Python app process (FastAPI + asyncio) with a plain HTML/JS front end, a database (Postgres 17 in
every deployment; a SQLite file on a host without Docker and in tests, through the same code), and
adapters for each outside service, plus two small research recorder processes next to it (section 2). The code is hexagonal: `domain/` is pure, `application/` holds the use cases and
talks to the outside only through ports, `adapters/` do the I/O, and `bootstrap/` wires it together. The layering is
enforced by `lint-imports` (`.importlinter`).

## 1. System context

```mermaid
flowchart LR
  you([You<br/>phone or PC browser])
  tg([Telegram<br/>your bot])
  subgraph app [Settleview · one local process]
    web[Web app<br/>Play · Agent · Record]
    api[HTTP API + SSE feeds]
    core[Application core<br/>trading · agents · copilot · record]
    db[(Postgres · or SQLite<br/>orders · journal · settings)]
  end
  kalshi[(Kalshi<br/>REST + WebSocket)]
  pm[(Polymarket US<br/>official SDK)]
  cb[(Coinbase<br/>BTC index)]
  claude[(Claude API<br/>via Claude Agent SDK)]

  you <--> web
  web <--> api
  api <--> core
  core <--> db
  core <--> kalshi
  core <--> pm
  core --> cb
  core <--> claude
  tg <-- long polling, outbound only --> core
```

LAN or VPN only: the app is never exposed to the internet. Keys live in secret files (`./secrets` with Docker; `.env` without) and never leave the process.

## 2. Services (how it runs)

One image, one process per service, the same on Windows, Linux and macOS. With Docker the services are in
`compose.yaml`; without Docker the portable supervisor runs the same entry points as child processes.

```mermaid
flowchart LR
  subgraph host [Docker host · compose.yaml]
    app[app<br/>python -m btc_signal_engine<br/>8000 phones · 127.0.0.1:8001 PC · 127.0.0.1:8002 agents]
    rq[rec-pm-quotes<br/>python -m btc_signal_engine.recorders pm-quotes]
    rs[rec-pm-settle<br/>python -m btc_signal_engine.recorders pm-settle]
    subgraph pg [postgres · 17.10 · not published]
      appschema[(schema app<br/>orders · journal · settings · trades)]
      research[(schema research<br/>pm_ws_quotes · pm_trades · pm_windows)]
    end
    backup[backup<br/>nightly pg_dump -Fc]
    migrate[migrate · one-off job<br/>ops migrate, then exits]
    pgvol[(settleview-pg)]
    dumps[(settleview-backups<br/>7 daily + 4 weekly)]
    data[(settleview-data<br/>logs · pid · stop files)]
    o2[openobserve · profile observability<br/>logs · metrics · traces · GenAI<br/>127.0.0.1:5080]
    otelcol[otel-collector · profile observability<br/>redaction · transform · memory_limiter<br/>not published]
    o2vol[(settleview-o2<br/>14 days)]
  end
  pmws[(Polymarket US<br/>markets WebSocket)]
  pmrest[(Polymarket US<br/>public market terms)]
  offsite[(off-site copy<br/>optional hook)]

  app <-- settleview_app --> appschema
  app -. newest rows, read-only .-> research
  app --- data
  migrate -- settleview_app, before the app starts --> appschema
  rq -- settleview_recorder --> research
  rs -- settleview_recorder --> research
  pg --- pgvol
  backup -- settleview_backup, read-only --> pg
  backup --> dumps
  dumps -.-> offsite
  rq <--> pmws
  rs --> pmrest
  app -. OTLP/HTTP .-> otelcol
  rq -. OTLP .-> otelcol
  rs -. OTLP .-> otelcol
  otelcol -. OTLP, redacted, ingest-only token .-> o2
  o2 --- o2vol
```

In the cloud the `postgres` and `backup` services are replaced by the platform's managed Postgres (Azure Database for
PostgreSQL Flexible Server, Amazon RDS): automated backups, point-in-time restore, patching and failover by the
platform; the app sees only `BTCFLOW_DATABASE_URL`. Without Docker, the portable supervisor runs the same three Python
processes on a SQLite file (`btcflow.db`) and the recorders' file (`polymarket_us.db`).

- **Failure domains are split.** A deploy or crash of the app never stops data collection; a recorder never touches
  trading and gets only the Polymarket key it needs (read-only market data; the settlement recorder needs none).
- **Each service restarts on its own** (compose `restart: unless-stopped`, Swarm / cloud `replicas: 1`; or the
  supervisor's backoff) and has a liveness check: the app's `/api/health`, each recorder's newest-row age
  (`python -m btc_signal_engine.ops fresh`).
- **The app watches the recorders** read-only (application/recorder_health.py): a Telegram `data` alert after 2
  minutes of silence and when one is back, and yesterday's coverage in the daily summary.
- **Configuration** is environment only (`.env`, or `BTCFLOW_<NAME>_FILE` secrets); logs go to stderr in containers.
- **Telemetry is OpenTelemetry, off until an endpoint is set** (docs/operations.md, "Observability"): each service
  exports traces, metrics and logs over OTLP/HTTP to the OpenTelemetry Collector (`otel-collector`,
  deploy/otel-collector/config.yaml), whose standard redaction and transform processors remove tokens, keys, key
  paths, headers, query values and client addresses before anything reaches a backend; the services emit no prompts
  or bodies and hold no backend credential. An agent wake is a GenAI span with its tool calls as children. Locally the
  collector forwards to the opt-in `openobserve` service; in the cloud the same collector configuration runs beside
  the services (Azure Monitor, CloudWatch via ADOT, any OTLP backend).
- **Every service has a memory and CPU limit** (docs/operations.md, "Resource limits and Postgres settings"); Postgres's
  memory settings are sized to its limit.
- **One database server, four roles** (docs/operations.md, "Postgres"): `settleview_app` writes the schema `app` (the
  app, the migrate job) and only reads `research`; `settleview_recorder` writes `research` (the recorders) and cannot
  reach `app` at all; `settleview_research` reads only (research scripts: "research is read-only" is a database
  permission); `settleview_backup` reads everything for `pg_dump`. Passwords are secret files.
- **Migrations are a one-off job** (`migrate`), finished before the app starts; the app only checks the revision.
- **One code path, two databases.** The store's SQL is dialect-neutral SQLAlchemy (per-dialect `ON CONFLICT`,
  `RETURNING`); each engine sets its schema on the connection, so the same statements run on SQLite and Postgres.
  CI runs the whole suite on both and checks that every migration gives the same tables, columns and indexes.

## 3. Layers

```mermaid
flowchart TB
  subgraph bootstrap [bootstrap · wiring, settings, startup]
    appw[app.py · config.py]
  end
  subgraph adapters [adapters · all I/O]
    http[http<br/>routes, access, SSE]
    kal[kalshi<br/>SDK + our WebSocket, margin]
    poly[polymarket_us<br/>official SDK]
    coin[coinbase]
    sdk[agent_sdk<br/>Claude Agent SDK harness]
    tele[telegram<br/>python-telegram-bot]
    sql[sqlite]
  end
  subgraph application [application · use cases, ports]
    trading[trading<br/>OrderGate · settlement]
    play[play · copilot · setups<br/>your_record · record_summary]
    agents[agent_sessions · agent_runner<br/>agent_rules · agent_tools · agent_events<br/>agent_proposals · agent_presets]
    notify[notifier · rule_scores]
  end
  subgraph domain [domain · pure]
    models[markets · settlement · strategies<br/>features · coach · perps]
  end
  bootstrap --> adapters --> application --> domain
```

## 4. One order path for everyone

A person's slide, an Assist approval (in the app or on Telegram) and an Auto agent all reach the venue through the
same gate. Nothing has a side door.

```mermaid
flowchart LR
  slide[Your slide on Play] --> prep
  approve[Approve a proposal<br/>app or Telegram] --> prep
  agent[Auto agent<br/>rule entry or tool call] --> prep
  prep[prepare<br/>price, size, fees, bracket] --> gate{OrderGate}
  gate -->|kill switch, venue mode,<br/>limits, presence| refuse[refused · reason shown]
  gate -->|session step for agents:<br/>budget, per-trade cap, end time| ok[approve · one token, 10 s]
  ok --> venue[(Kalshi / Polymarket)]
  venue --> fills[fills · stops · settlement<br/>booked from the official result]
```

## 5. An agent run

Rules are code and run every tick; the model is woken only by events and decides with the same signals you see.

```mermaid
sequenceDiagram
  participant M as Market feed
  participant E as agent_events
  participant R as agent_rules (code)
  participant Q as wake queue
  participant L as Claude (Agent SDK)
  participant G as OrderGate
  participant J as Journal
  M->>R: every tick
  R-->>G: rule matched → place entry with its stop (Auto) / proposal (Assist)
  M->>E: window ready, entry check, call changed, near stop, settled …
  E->>Q: wake (urgent first)
  Q->>L: memory + events + observe
  L->>G: tools: exit, tighten stop, note, record_decision
  L->>J: decision with thesis, notes
  G->>J: every order, fill, refusal
  J-->>L: next wake reads notes and recent decisions
```

## 6. Learning loop (planned)

```mermaid
flowchart LR
  trades[Settled trades<br/>and decisions] --> rev[Reviewer agent<br/>after each trade / run]
  rev --> cand[Lesson candidates<br/>claim · evidence · n]
  cand --> mem[Agent memory<br/>read by the next run]
  cand -->|enough trades| test[Offline test<br/>pre-registered, holdout]
  test -->|passes| adopt[New rule, retirement<br/>or prompt change you approve]
  test -->|fails| killed[Killed · never re-proposed]
  adopt --> reg[(LEARNINGS.md)]
  killed --> reg
```

## Where things live

| Path | What |
| --- | --- |
| `btc_signal_engine/domain/` | Pure models: markets, settlement, strategies, features |
| `btc_signal_engine/application/` | Use cases: trading and the OrderGate, Play, agents, copilot, notifier, record |
| `btc_signal_engine/adapters/` | Kalshi, Polymarket US, Coinbase, Claude Agent SDK, Telegram, HTTP, the database (SQLite or Postgres) |
| `btc_signal_engine/bootstrap/` | Settings and wiring; the supervisor, `ops` commands and the recorders' entry point |
| `compose.yaml`, `Dockerfile`, `deploy/` | The services, the image, and thin start-at-boot examples per OS |
| `web/` | The front end (Alpine.js views, Lightweight Charts) |
| `scripts/` | Research harness (`experiments.py`) and tools |
| `docs/research/LEARNINGS.md` | The hypothesis register: every result, including the losses |
| `docs/plans/` | Plans for each feature |
| `tests/` | Unit, API, agent, Telegram and browser (Playwright) tests |
