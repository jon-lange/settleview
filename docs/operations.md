# Operations

## Health

`GET /api/health` (the full answer for the PC and signed-in phones; anyone else, the container healthcheck included,
gets only `{"status": "up"}`) reports:
- `status`: `live`, or `degraded` when the Coinbase feed or any venue stream is not live, or when a failed database write batch is waiting for a retry. `degraded` lists the parts at fault, for example `["kalshi_market"]`, `["coinbase"]`, `["writer"]` or `["database"]` (the one read it makes failed).
- `feed`: the Coinbase feed, `connecting`, `live` or `stale`. `lag_ms` is the trade-to-receipt delay, and `last_trade_ts` the newest trade.
- `clock_offset_ms`: how far this machine's clock is from Coinbase. The app corrects for this, but very large offsets suggest syncing the system clock.
- `gaps_open`: data gaps not yet repaired; `null` (and `database` in `degraded`) when the database cannot be read (an outage: the answer is still 200, never an error).
- `streams`: each running venue stream by name: `kalshi_market` (the Kalshi production order book for the Coach), `polymarket_market` (Polymarket US market data) and `trading_<env>` (the trading feed per environment). Each has `venue`, `env`, `connected`, `stale` and `last_msg_age_ms`. A stream that is not running is absent. For Polymarket, `polymarket_market` is the market data right now (stale while an open window's book is not servable, see "Polymarket book freshness") and `trading_pm_prod` is the trading feed with hysteresis (it goes stale only on a dead connection or after 10 s of an unservable book); a quiet book does neither. A `last_msg_age_ms` of several seconds on a quiet Polymarket market is normal.
- `writer_pending`: writes queued for the database writer and not yet committed, including those waiting behind a failed batch. `writer_failed_batches`: batches whose transaction failed and are waiting for a retry; while one waits, newer writes wait behind it so they commit in order. `writer_outage_s`: seconds the database has not taken writes (null while it does); `writer_dead_lettered`: writes this process sent to `data/logs/writer-dead-letter.jsonl`. An unreachable or busy database is retried until it is back (nothing is dropped); only a write the database refuses, or market data shed past the queue cap during a very long outage, goes to the dead-letter file (see "Postgres: timeouts, outages and the dead-letter file").
- `loops`: each supervised background loop with `last_beat_age_s` (seconds since its last heartbeat), `restarts` (times the supervisor restarted it since start) and `critical`. Loops beat at different rates (the web server only at start, `memory` every 10 minutes, `collector` on every trade), so judge the age per loop. Rising `restarts` means a loop keeps failing; the log has its traceback.
- `stalls_last_hour`: event-loop stalls longer than 500 ms that began in the last hour (each one's stack is in the log).
- `recorders`: the research recorders as the app sees them (read-only): `available` (false with a `reason` such as
  "polymarket_us.db not found"; with Postgres the data is the `research` schema), and per recorder (`pm-quotes`, `pm-settle`) `newest_ms`, `age_s`, `limit_s` and
  `state` (`ok`, `silent` past its limit, `empty`: no rows yet), plus `reason` "the venue is sending trades but no
  quotes" when the quotes recorder is silent while prints are still being recorded (a venue-side book outage, see
  "Polymarket quotes recorder"). It never changes `status`: the recorders are research
  data, not the app's health. The Telegram `data` alert (default on) says when one goes silent (quotes after 2
  minutes, settlement after 10; for quotes it adds "trades are still arriving, so Polymarket is sending trades but no
  quotes" when that is the case) and when it is back, and says once if the research database is missing; the daily
  summary adds "Data yesterday (UTC): 5m windows N/288 recorded · longest gap · N/288 settled" (alone on a day
  without trades).
- `uptime_s`: seconds since the app started. `memory_mb`: the process's private memory (null if unreadable).

While the laptop sleeps, windows are recorded as **MISSED** rather than being computed late. On wake, the app repairs the data gap from Coinbase's history.

### Polymarket book freshness

Polymarket prices come only from the markets WebSocket (the REST BBO is a ~30 s cached snapshot and is never used).
The server sends a book or top message only when the book changes, so on a quiet market several seconds between
messages are normal (2-18 s measured 2026-10-31). A cached book or top is **servable** (it can price a ticket, Play
shows it as live, the agents may act on it) when it is from a connection that is up and still carries the market, and
either it arrived within 2 s, or all of these hold (the quiet path):

| Condition | Limit | Why |
|---|---|---|
| The connection is alive: any message on it (a book, a trade, a heartbeat) | within 15 s (`CONN_LIVE_MS`) | a silent socket may be dead; the WebSocket's own pings close a dead one within ~40 s |
| The market's TRADE subscription is confirmed and has covered it since the book arrived | - | a print is what would show the book is outdated |
| No trade for the market arrived after the book | refused at once; after 2 s (`TRADE_GRACE_MS`) the reason says "book feed broken" | a print moves or consumes the book |
| The book's age | at most 30 s (`QUIET_MAX_MS`) | no older than the REST snapshot we refuse |

Every refusal names its condition (`StaleMarketData`, the `problems` in `polymarket_market`, the trading feed's
`reason`). The trading feed (`trading_pm_prod`, `stream_up` / `stream_down` audit rows, the `settleview.stream.flaps`
metric) goes down at once when the private socket or a needed market's subscription or connection is lost, and only
after 10 s (`DOWN_AFTER_MS`) of an unservable book otherwise; it comes back after 5 s (`UP_AFTER_MS`) with no problem.
A window that has just opened has 30 s to send its first book before it counts against the feed (it is never priced without one). While it is down, the trading service polls every 2 s. Entries never depend on that state: every ticket re-reads the
cache's servable check. On 2026-10-31 13:59-14:45 CDT the venue sent trades and no book for 45 minutes: under this rule
every Polymarket book is refused within 2 s of its last update and stays refused until books return
([docs/solutions/2026-10-31-pm-quiet-book-vs-dead-book-feed.md](solutions/2026-10-31-pm-quiet-book-vs-dead-book-feed.md)). Kalshi's
event-contract freshness is unchanged.

**Kalshi perps use the same rule** (`domain/book_freshness.py`, 2026-11-01): the perps quote is judged by the age of
the book's last update (snapshot or delta), never by "any message". The ticker channel sends one message a second
even when nothing changes, so before this a frozen `orderbook_delta` subscription was served as fresh. A ticker or any
other message keeps the connection alive (15 s) but never makes the book fresh; a quiet perps book is served for at
most 30 s and only while the `trade` subscription is confirmed and no print arrived after the book (a print without a
book update within 2 s refuses it as "book feed broken"; a trade sequence gap stops vouching until the next book
update). The view's quote age is the book's age.

**A refused Polymarket private subscription is not a healthy feed** (2026-11-01). The server answers a refused
ORDER / POSITION / ACCOUNT_BALANCE subscription with an error naming our request id (`pmpriv<n>`): the trading feed
goes down at once with the reason ("private ORDER subscription refused: ..."), the service polls every 2 s, and the
socket reconnects and subscribes again; it comes back up only once a session has held every subscription for 5 s.

## Orders whose answer was lost

The rule, for Kalshi event contracts, Polymarket US and Kalshi perps alike (`domain/order_resolution.py`): **an order
whose answer was lost stays UNKNOWN until fills or activities prove its outcome; a list miss is never proof of no
fill; on a late fill the saved protection is placed or the position is marked unprotected and the user is alerted;
committed risk is held until confirmed.** In practice:

- While an entry is unknown it counts at its approved maximum against the per-trade, window and day limits, blocks
  new orders on that market, and shows as unknown (Play, the Agent panel; the perps card's red "checking your order"
  banner). Nothing is ever re-sent for it.
- Every monitor pass looks it up by client_order_id in the order lists (every page). After 60 s
  (`FIND_GIVE_UP_MS`), every 10 s (`PROOF_EVERY_MS`) the fills since the send are read as well: Kalshi and perps read
  each fill's order by id and accept the one carrying our client_order_id; Polymarket US claims the account's activity
  trades in that market since the send that no other order of ours explains, at prices the order could fill at. Only
  when the fills show nothing **and** the position holds no more than our own records explain is it closed as "never
  reached the venue" (`not_found`; a 409 duplicate: `unfilled`).
- A recovered entry is booked at its real average fill price (fees from its fills, else the fee formula) and gets its
  protection: the take profit (event contracts; the stop is watched as for any bracket), or the stop and take profit
  saved with the perps order (`perp_orders.stop_loss` / `take_profit`, migration 0019). A perps bracket that cannot
  be written leaves the position `unprotected` with the no-stop warning.
- Still unprovable (a read fails, or the position shows contracts nothing explains): audited once as
  `order_unresolved` and listed in the trading status' `settle_warnings` with `kind: "unresolved_order"` (Play, the
  Agent panel and the Telegram `settle_warnings` alert show it). **Runbook:** open the venue's own app, find the
  order or fill in that market at that minute, and compare the position. The app resolves it by itself as soon as the
  venue's fills show it; nothing is closed or voided by a timer.
- A create answered 400/401/403/404/422 is a rejection; any other 4xx (408, 425, 499, ...) is treated like a lost
  answer. A Polymarket IOC answered while still pending is read back until it is final.
- Exits, take-profit and stop changes on a position are checked against that position's own environment, never the
  current mode: a production position can be sold by hand or by its agent session after a credential problem has
  dropped the service to sandbox.
- A perps approval holds the trading lock for at most 30 s of venue calls (`PerpConfig.send_deadline_s`); an order
  still unanswered then is left unknown and resolved as above. Nothing queues behind it: another approval, an adopt
  or a perps mode change waits at most 2 s (`PerpConfig.lock_wait_s`) and is then refused `busy` with nothing sent.

What only the venues show (not verified by tests): how long Kalshi's and the perps order lists and fills lists really
lag a placement, whether Polymarket US activity trades are always quoted in YES terms, and how soon they appear. See
[docs/solutions/2026-10-31-lost-order-answers.md](solutions/2026-10-31-lost-order-answers.md).

## Venue retries, reconnects and bounds

One policy for every venue path (`adapters/retry.py`, 2026-11-01): exponential backoff with **full jitter** (a uniform
draw between 0 and the current ceiling), so the Polymarket pool's sockets, both Kalshi streams, the perps stream and
the Coinbase feed never reconnect in lockstep after a network blip. Reconnects: 1 s doubling to 30 s (Coinbase 1.5 s),
reset after a connection that lasted a minute. Request retries (Kalshi REST, perps REST: transport error, 429, 5xx;
our own loop is the only one and re-sends the SAME body, same `client_order_id`): 0.2 s doubling for a transport error
or a 5xx; a 429 waits at least its `Retry-After` (capped at 10 s so an order path never stalls) plus up to 1 s of
jitter, or backs off from 1 s without one. Backfill readers (Kalshi markets and portfolio, Coinbase REST): 1 s
doubling to 30 s, at least a 429's `Retry-After`. Polymarket's shared rate limiter pauses every caller for a 429's
`Retry-After` (1 s without one) plus jitter; a Polymarket create is still never re-sent.

Bounds: the Kalshi and perps WebSockets accept frames up to 8 MB, the Coinbase ws-feed up to 16 MB (a level2 snapshot
is a few MB); the Polymarket SDK keeps its websockets default (1 MB). The Coinbase transport-to-consumer queue holds at
most 10,000 updates: behind a slow consumer the oldest is dropped (WARNING on the first and every 1,000th, a dropped
trade becomes a recorded trade-id gap that the repair job fills). The quotes recorder keeps at most 200,000 rows per
buffer through a database outage, oldest dropped (WARNING). Both drops are counted by the OpenTelemetry counter
`settleview.venue.dropped` {`settleview.component`: `coinbase_stream` | `pm_quotes_recorder`, `settleview.buffer`}.

## Logs

Run directly, the app writes its own log to `data/logs/app.log` (next to the database). The file rotates at 10 MB and keeps 10 old files (about 110 MB at most). The same lines go to stderr; uvicorn's own lines go to stderr only. In containers (`BTCFLOW_FILE_LOG=false`, set by the image) there is no file: everything goes to stderr for `docker compose logs` or the platform's collector. Under the portable supervisor each child's output goes to its own rotated file in `<data dir>/logs/` (`app.log` then holds all of the app's lines, uvicorn's included). A Kalshi live-poll failure that keeps happening logs one WARNING a minute, with the error and the number of failures since the previous warning. A Polymarket balance failure logs a WARNING with the error's type and message.

Every line (app, recorders, supervisor) starts with its time in **UTC, ISO 8601, with milliseconds**, whatever `TZ`
is: `2026-10-31T18:50:00.123Z WARNING btc_signal_engine.x: ...`, so app, recorder and Postgres lines (Postgres logs
UTC too) line up in one collector. The HTTP client libraries `httpx` and `httpcore` log at WARNING
(`BTCFLOW_HTTP_CLIENT_LOG_LEVEL`): at INFO httpx wrote one line per request, about 85 % of the app's lines (Kalshi perps
polling every 2 s); request counts and latencies are OpenTelemetry metrics instead (next section). With an OTLP
endpoint set, the same records also go to OpenTelemetry; stderr stays on and stays the source of truth.

**Every line is scrubbed** before any handler writes it (stderr, `app.log`, the supervisor's files, uvicorn's own
lines, the OpenTelemetry bridge), with the same rules as the telemetry exporters (`bootstrap/scrub.py`): the exact
values of the app's secrets (Telegram token, Anthropic key, Polymarket secret, key ids, database passwords), Telegram
bot tokens in URLs, `Authorization` / API-key / cookie headers, `pin=` / `password=` / `token=` values, URL
credentials, and long base64-looking runs. That covers the message after its arguments are filled in, the exception
text and the exception object, and stack dumps, from any logger (`bootstrap/logs.py`). A side effect: a long opaque
value such as a Kalshi pagination cursor shows as `<redacted>`. Database errors never include bound parameters (every
engine has `hide_parameters`).

**Container logs** (compose): every service uses Docker's `local` log driver, at most 5 files of 10 MB per container
(`x-logging` in `compose.yaml`), so logs can never fill the disk Postgres shares. `docker compose logs` reads them as
before. In the cloud the platform's collector takes stderr.

## Observability (OpenTelemetry)

Every Python service (the app and both recorders) exports **traces, metrics and logs** over OTLP/HTTP when an
endpoint is configured, and nothing at all when not (the default): no endpoint, no SDK set-up, no patched library.
Plan and decisions: [docs/plans/2026-10-31-1400-infra-observability-evaluation.md](plans/2026-10-31-1400-infra-observability-evaluation.md).
The code: `bootstrap/telemetry.py` (set-up, sampling, the app's gauges), `bootstrap/scrub.py` (what never leaves),
`adapters/telemetry.py` (the agent wake's GenAI spans, the writer's batch metrics). The SDK is the `otel` extra
(in the Docker image and the dev container).

**Settings** (the standard OpenTelemetry variables; names and purpose only):

| Variable | Purpose |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | The OTLP/HTTP base URL (e.g. `http://openobserve:5080/api/default`); `/v1/traces`, `/v1/metrics`, `/v1/logs` are added. Unset: telemetry is off. Per signal: `OTEL_EXPORTER_OTLP_{TRACES,METRICS,LOGS}_ENDPOINT` (used as given). |
| `OTEL_EXPORTER_OTLP_HEADERS_FILE` | A file holding the backend's headers (`key=value,key2=value2`, values may be percent-encoded), e.g. `Authorization=Basic%20...`. Compose mounts the `otlp_headers` secret and sets this. The one addition to the standard set (the `_FILE` secrets convention); read only when an endpoint is set. Setting it and `OTEL_EXPORTER_OTLP_HEADERS` is an error. |
| `OTEL_EXPORTER_OTLP_HEADERS` | Headers as an environment value: works, but prefer the file (a credential in the environment shows in `docker inspect`). |
| `OTEL_SERVICE_NAME` | Who reports. Compose sets `settleview-app`, `settleview-rec-pm-quotes`, `settleview-rec-pm-settle`; unset, the same names are the defaults. `service.version` is the package version plus the image's commit (`0.1.0+<sha>`), `vcs.ref.head.revision` the full commit. |
| `OTEL_RESOURCE_ATTRIBUTES` | Extra resource attributes; compose's default is `deployment.environment.name=local`. |
| `OTEL_TRACES_EXPORTER`, `OTEL_METRICS_EXPORTER`, `OTEL_LOGS_EXPORTER` | `otlp` (default), `console` (stdout, for debugging) or `none`, per signal. Anything else stops the start with an error naming the variable (exit 78 for a recorder). |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | Must be `http/protobuf` if set (the only exporter installed); `grpc` is an error, not a fallback. |
| `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG` | Unset sampler: Settleview's (below), whose background ratio is `OTEL_TRACES_SAMPLER_ARG` (default `0.01`). Set it to a standard sampler (`parentbased_traceidratio`, `always_on`, ...) to replace it. |
| `OTEL_SDK_DISABLED` | `true` turns everything off even with an endpoint set. |
| `OTEL_METRIC_EXPORT_INTERVAL` | Milliseconds between metric exports (default 60000). |
| `OTEL_PYTHON_FASTAPI_EXCLUDED_URLS` | Routes not traced or measured (default: `api/health`). |
| `OTEL_PYTHON_ASYNCIO_COROUTINE_NAMES_TO_TRACE` (and `_TO_THREAD_FUNCTION_NAMES_TO_TRACE`, `_FUTURE_TRACE_ENABLED`) | Debugging only: names of coroutines / functions to trace; the asyncio instrumentation is installed only when one is set. |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | Defaults to `http,database` (the stable HTTP and database conventions: `http.route` on server metrics, not one series per raw path). |

Only the app reads other `OTEL_*` variables from `.env` (its `env_file`); compose passes the recorders the endpoint,
the headers file, the service name and the resource attributes.

**Local backend: OpenObserve** (one container, opt-in compose profile `observability`; logs, metrics, traces and
GenAI views in one UI; AGPL-3.0, run unmodified):

```bash
python deploy/postgres/make_secrets.py        # adds secrets/openobserve.env and secrets/otlp_headers (keeps the rest)
echo OTEL_EXPORTER_OTLP_ENDPOINT=http://openobserve:5080/api/default >> .env
docker compose --profile observability up -d  # OpenObserve, and the services recreated with the endpoint
# UI: http://localhost:5080 (this PC only), user admin@settleview.local, the password in secrets/openobserve.env
docker compose --profile observability stop openobserve   # pause it (the app keeps running; exports retry, then drop)
# off again: remove the line from .env, then `docker compose up -d`; `docker volume rm settleview_settleview-o2` deletes its data
```

It keeps 14 days (`ZO_COMPACT_DATA_RETENTION_DAYS`) on its own volume `settleview-o2`, caps its caches (its defaults are
a share of the host's memory, not of its 1 GiB limit), sends no usage reports and downloads no GeoLite IP databases.
Its write-ahead log files are 512 MB sparse files (one per signal), so `docker system df` shows about 2 GB for the
volume while the disk actually used is a few MB to start with (`du` inside a container mounting it). Its UI is published on `127.0.0.1`
only. The root password is read once, on its first start, from the env file (its image has no shell, so it cannot read
a secret file). To change it: change it in the UI, delete `secrets/openobserve.env` and `secrets/otlp_headers`, run
`O2_ROOT_PASSWORD=<new> python deploy/postgres/make_secrets.py`, then `docker compose up -d --force-recreate app
rec-pm-quotes rec-pm-settle`. Another backend: write its headers into `secrets/otlp_headers` and point the endpoint at
it. If the backend is down, exports fail quietly in the background (a few WARNINGs from `opentelemetry`); the app does
not wait for it, and a stop waits at most 5 s for the last export.

**What is exported**

- Traces: HTTP requests (FastAPI: method, route, status, duration; no per-message send / receive spans), the httpx
  calls and SQL statements inside them (SQLAlchemy: the statement with its `$1` / `%(x)s` placeholders, never the
  values), and every agent wake.
- GenAI: one `invoke_agent settleview` span per wake (`gen_ai.operation.name`, `gen_ai.provider.name` and
  `gen_ai.system` = `anthropic`, `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.usage.input_tokens` (cached
  tokens included, as the conventions define it) and `output_tokens`, `gen_ai.usage.cache_read.input_tokens` /
  `cache_creation.input_tokens`, `gen_ai.usage.cost` (USD as the Claude Code CLI reports it), `gen_ai.response.finish_reasons`,
  `gen_ai.conversation.id` = the session id, the status, turns and tool-call count), with one `execute_tool <name>`
  child span per tool call (`gen_ai.tool.name`, result `ok` / `error`).
- Metrics: `http.server.request.duration` and `http.client.request.duration` (by route / host), and the app's own:
  `settleview.event_loop.lag` (ms histogram) and `.stalls`, `settleview.loop.heartbeat_age` and `.restarts` (per loop),
  `settleview.writer.pending`, `.failed_batches`, `.flush.duration`, `.batch.size`, `settleview.db.pool.checked_out`,
  `.overflow`, `.size` (per pool) and `db.client.connections.usage`, `settleview.stream.connected`, `.stale`,
  `.last_message_age` (per stream) and `settleview.stream.flaps` (trading-stream up/down transitions, each an audit
  row; for Polymarket only real ones after the feed's hysteresis, "Polymarket book freshness"), `settleview.recorder.age`
  and `.silent`, `settleview.agent.wake_queue.depth`, `.wait` (ms), `.oldest_wait`, `settleview.agent.wakes.running`,
  `settleview.agent.wakes` (by status), `gen_ai.client.token.usage`, `gen_ai.client.operation.duration`,
  `settleview.agent.cost`, `settleview.agent.tool_calls`, `settleview.order_gate.decisions` (by strategy, stage
  prepare / approve, result and code), `process.memory.usage` (current RSS, unlike `/api/health`'s `memory_mb`, which
  is the peak on Linux), `process.cpu.time` and `settleview.venue.dropped` (rows or messages a bounded venue buffer
  dropped, by component and buffer: "Venue retries, reconnects and bounds").
- Logs: every record the root logger handles, with the trace and span id when it was written inside a span.

**What is never exported**: prompts, model output, thinking, tool arguments or results, order tickets, request or
response bodies, HTTP headers, cookies, URL query values, the Telegram bot token (it is in the bot API's URL path),
API keys, signatures, passwords, PEM keys, and the app's own secret values. Every exporter is wrapped by the scrubber
(`bootstrap/scrub.py`), so this holds even if someone turns on header capture (`OTEL_INSTRUMENTATION_HTTP_CAPTURE_*`);
`tests/telemetry` fails if a planted token, key, cookie, signature or query secret, or anything shaped like one,
reaches an exporter. The agent's Claude Code CLI gets no `OTEL_*` variable (the harness blanks them), so its own
telemetry stays off.

**Sampling** (the default sampler): a span with a parent follows it. Requests that change something (POST, PUT, PATCH,
DELETE: the order path) are always traced; GET polls (Play's scoreboard, odds and kill-switch reads) and `/api/trading/heartbeat` at
the background ratio (1 %); `/api/health` and every SSE `/stream` never. A background HTTP call (Kalshi perps polling,
Coinbase, Polymarket) is traced at the background ratio; api.telegram.org never; a SQL statement outside a request or
a wake (the writer's flush every 250 ms) is never a trace of its own: its timing is in the writer and pool metrics.
Agent wakes are always traced. Metrics count everything.

**In the cloud** the same image exports to the platform's collector; only the endpoint and the headers file change:
- **Azure Container Apps**: the environment's managed OpenTelemetry agent injects the endpoint and sends traces and
  logs to Application Insights (Azure Monitor); for metrics, add a generic OTLP destination (or Azure Monitor managed
  Prometheus behind a collector). `azure-monitor-opentelemetry` is not needed.
- **AWS ECS / Fargate**: an ADOT (AWS Distro for OpenTelemetry) collector sidecar on `localhost:4318` forwards to
  CloudWatch (X-Ray traces, Logs, Metrics) with SigV4; the app needs no AWS credentials for it.
- **Anything else** that speaks OTLP/HTTP (Grafana Cloud, Honeycomb, SigNoz, Datadog's OTLP intake, a self-hosted
  OpenTelemetry Collector, OpenObserve on a server): its URL in `OTEL_EXPORTER_OTLP_ENDPOINT`, its key in the headers file.

**Checked end to end** (2026-10-31, a throwaway stack with OpenObserve v1.1.1 on linux/arm64, no keys, a fake bot token,
an agent wake of the bundled Claude Code CLI against the repo's local fake Anthropic API): traces, 71 metric streams and
logs arrived per service; the wake's spans nested with tokens, cost and tool results; a log line carried its trace id;
no planted token, query secret, password or prompt marker appeared in any stream. See
[docs/solutions/2026-10-31-otel-instrumentation-gotchas.md](solutions/2026-10-31-otel-instrumentation-gotchas.md) for
what that run caught.

## Clock

The app learns exchange time from live trades, so a skewed PC clock doesn't break decisions. Keeping Windows time in sync still helps: Settings → Time & language → Date & time → Sync now. On Linux, run NTP or chrony.

## Data and backups

All state lives in one database: **Postgres** when `BTCFLOW_DATABASE_URL` is set (every Docker and cloud deployment;
see "Postgres" below, with its own backups), else **one SQLite file** (`data/btcflow.db` by default; WAL mode, so
`-wal` and `-shm` files sit next to it). The rest of this section is about the SQLite file. Commits use
`synchronous=FULL` (Postgres: its default `synchronous_commit=on`), so a power cut can't lose a committed row.

**Daily backup and trade archive.** Once a day (the first quiet minute, two minutes past a quarter-hour, of each UTC
day; after a restart, the next quiet minute unless today's backup already exists) the app, in a worker thread:

1. backs the database up with SQLite's online backup API to `data/backups/btcflow-YYYY-MM-DD.db` (UTC date). The copy
   is a single file (not WAL), consistent as of one moment, and takes a few seconds (about 4 s for 1.8 GB). It keeps
   the 7 newest backups plus the newest backup of each of the 4 ISO weeks before them (about 5 weeks back) and deletes
   the rest. Each backup is a full copy of the database, so budget 11 times its size on disk.
2. moves Coinbase `trades` rows older than `BTCFLOW_TRADES_RETENTION_DAYS` (default 60) to `data/btcflow-archive.db`,
   which has the same `trades` table. Nothing is deleted: each batch of 50,000 rows is copied and checked in the
   archive before it is removed from the database in its own transaction (about 0.1 s at most on the real database,
   so trading's writes barely wait). An interrupted run leaves rows in both files, never in neither; the next run
   finishes it. `index_1s`, `book_1s`, the Kalshi tables and every trading table are never archived.

The log shows each step (`maintenance: backed up ...`, `pruned ...`, `archived N Coinbase trades ...`), and
`/api/health` lists the job as the `maintenance` loop (it beats every 15 minutes; a failure shows as a restart, with
its traceback in the log). A failed backup is retried at the next quiet minute; an archive run that failed or was
stopped by a shutdown continues the next day. The database file does not shrink when rows move (SQLite reuses the
freed pages), but it stops growing beyond about 60 days of trades.

`data/btcflow-archive.db` is the only copy of trades older than the window and is not in the daily backups: copy it
off the machine now and then (it changes only once a day, so a plain copy outside the job's minute is fine). To read
old and recent trades together, attach it read-only:

```python
import sqlite3
c = sqlite3.connect("file:data/btcflow.db?mode=ro", uri=True)
c.execute("ATTACH 'file:data/btcflow-archive.db?mode=ro' AS archive")
rows = c.execute("SELECT * FROM archive.trades UNION ALL SELECT * FROM trades ORDER BY ts").fetchall()
```

No research script reads Coinbase `trades` from `btcflow.db` today (they read `index_1s`, `candles` and the Kalshi
tables). `python -m btc_signal_engine backfill` refetches trades only for `BTCFLOW_BACKFILL_DAYS` (default 30), inside
the window.

**A manual backup** at any other time, while the app runs, uses the same online backup:

```bash
sqlite3 data/btcflow.db ".backup 'data/backups/btcflow-manual-$(date +%F).db'"
```

Or from Python: `sqlite3.connect(src).backup(sqlite3.connect(dst))` (copy in one step, the default: a backup in steps
restarts whenever the app writes and may never finish). A name other than `btcflow-YYYY-MM-DD.db` is never pruned.
Don't copy the file with a plain `cp` while the app is writing.

## Postgres

Every deployment keeps the app's data in Postgres 17 (plan: docs/plans/2026-10-31-0900-feat-postgres-plan.md): on
Docker Desktop and a Swarm the `postgres` service in `compose.yaml`, in the cloud the platform's managed Postgres. A
host without Docker keeps the SQLite file (above); the code is the same, chosen by one setting. What differs between
the two and why (JSON as text, unbounded VARCHAR, booleans, row order):
[docs/solutions/2026-10-31-sqlite-to-postgres-type-traps.md](solutions/2026-10-31-sqlite-to-postgres-type-traps.md).

**Settings** (names and purpose only; `compose.yaml` sets them for its services):

| Variable | Purpose |
|---|---|
| `BTCFLOW_DATABASE_URL` | `postgresql://<role>@<host>:5432/<database>`, no password in it. Set: the app and the recorders use Postgres; unset: SQLite at `BTCFLOW_DB_PATH`. Anything but a `postgresql://` URL is an error that stops the start (never a quiet fallback). Extra libpq options go in the query, e.g. `?sslmode=require` for a managed server. |
| `BTCFLOW_DATABASE_PASSWORD` / `_PASSWORD_FILE` | The role's password; use the `_FILE` form (a secret file). A password in the URL as well is an error. |
| `BTCFLOW_RESEARCH_DATABASE_URL`, `BTCFLOW_RESEARCH_DATABASE_PASSWORD_FILE` | The research scripts' read-only connection (the `settleview_research` role). |
| `BTCFLOW_DB_PATH` | Still used with Postgres: its folder is the app's local folder (logs, `app.pid`, `stop.request`, the writer's dead-letter file). |
| `BTCFLOW_DB_STATEMENT_TIMEOUT_MS` | Server-side `statement_timeout` of every connection (default 5000). 0 = none. |
| `BTCFLOW_DB_LOCK_TIMEOUT_MS` | `lock_timeout` (default 2000): a statement waiting longer for a lock fails instead of freezing its caller. |
| `BTCFLOW_DB_IDLE_IN_TRANSACTION_TIMEOUT_MS` | `idle_in_transaction_session_timeout` (default 30000): the server ends a session left inside a transaction. |
| `BTCFLOW_DB_LONG_STATEMENT_TIMEOUT_MS` | The statement timeout of migrations, `ops migrate-sqlite-to-pg`, the trade-archive batches and research scripts (default 600000, 10 min); they keep the short lock timeout. |
| `BTCFLOW_DB_POOL_TIMEOUT_S` | How long a request waits for a free pooled connection before it fails (default 5). |
| `BTCFLOW_DB_MIGRATION_LOCK_WAIT_S` | How long a migration keeps retrying a table lock someone else holds (the backup's `pg_dump`) before the start fails (default 120, at most 3600; 0 = fail on the first lock timeout). |

The driver is psycopg 3 (`pip install -e ".[postgres]"`, in the Docker image and the dev container).

**Schemas and roles.** The app's tables are in the schema `app` (made by its Alembic migrations: `ops migrate` or the
app's start), the recorders' in `research` (made by the recorders on start). Each connection sets its schema, so no
SQL names one. Three roles, made by `deploy/postgres/initdb.sh` on the first start of a new `settleview-pg` volume:

| Role | Used by | Rights |
|---|---|---|
| `settleview_app` | the app, both recorders, migrations, `ops migrate-sqlite-to-pg` | owns the schemas `app` and `research` |
| `settleview_research` | research scripts, notebooks, ad-hoc `psql` | `SELECT` on both schemas (also on tables made later), every transaction read-only |
| `settleview_backup` | the `backup` service | `pg_read_all_data`, read-only |
| `postgres` (superuser) | administration only | its password is a secret file; no service uses it |

No other role may connect, and nobody may create objects in `public`.

**First start (compose).**

```bash
python deploy/postgres/make_secrets.py   # ./secrets/pg_{superuser,app,research,backup}_password (kept if present)
docker compose up -d --build
docker compose logs postgres | grep "settleview initdb"   # roles and schemas were made
```

The passwords are random, never printed, git- and docker-ignored. On Swarm create them as secrets instead
(`docker secret create pg_app_password <file>`) and mark the compose `secrets:` entries `external: true`. The init
script runs only on an empty volume: to change a password later, `ALTER ROLE settleview_app PASSWORD '...'` in psql,
write the same value to its file, then `docker compose up -d --force-recreate app rec-pm-quotes rec-pm-settle`.

**Tools.** `docker compose exec postgres psql -U postgres -d settleview` (inside the container, no port needed). For a
desktop tool on this PC, uncomment the `127.0.0.1:5432:5432` port of the `postgres` service and connect as
`settleview_research`; never publish it on another address.

**Research reads.** `btc_signal_engine.adapters.sqlite.research.readonly_engine(source)` takes a SQLite file (opened
`mode=ro`) or `Settings().research_database` (Postgres as `settleview_research`, schemas `research` then `app`) and
works with `pandas.read_sql(sql, engine)`. The frozen research datasets (`horizons.db`, `kalshi_trades.db`,
`polymarket_us_history.db`) stay SQLite files.

**The trade archive on Postgres.** The daily maintenance moves Coinbase `trades` older than
`BTCFLOW_TRADES_RETENTION_DAYS` into `app.trades_archive` in batches of 50,000, each batch one transaction (a row is
always in exactly one of the two tables). To read old and new together: `select * from trades_archive union all
select * from trades`. The app takes no file backup on Postgres; that is the `backup` service's job.

### Postgres: timeouts, outages and the dead-letter file

Every Postgres connection (app, recorders, ops, research scripts) is opened with `statement_timeout` 5 s,
`lock_timeout` 2 s and `idle_in_transaction_session_timeout` 30 s (settings above), libpq TCP keepalives
(`keepalives=1`, idle 10 s, interval 5 s, count 3: a half-open connection is noticed in about 25 s instead of hours)
and a 10 s connect timeout; each pool hands out a connection only after a ping and waits at most 5 s for one. So a
lock, a stuck statement or a vanished server makes a call fail within seconds instead of freezing a stop or a
stream. Migrations wait for another process's migration without the lock timeout, then take table locks under it, so
a migration never queues behind a long query while every other query queues behind it. On a managed server these are
per-connection options: no parameter-group change is needed.

**A migration behind a lock waits, it does not crash.** When an attempt hits the lock timeout (typically the backup
service's `pg_dump`, which holds ACCESS SHARE on every table for its ~30 s run), the whole upgrade rolls back (one
transaction: nothing half-applied) and is retried after 1, 2, 4, then every 5 s, for up to
`BTCFLOW_DB_MIGRATION_LOCK_WAIT_S` (120 s). Each attempt still waits at most `lock_timeout` (2 s), so the app's other
queries never queue behind a waiting ALTER for longer. Each retry logs who holds the lock, by application name, role
and pid only:

```
migration: waiting for a lock held by pg_dump as settleview_backup, pid 4242; attempt 3, retrying in 4.0 s (gives up after 111 s more)
```

then `migration: done after N attempts`. Past the limit the start fails with the lock-timeout error as before (compose
restarts the app). Why: [docs/solutions/2026-11-01-migration-vs-backup-lock.md](solutions/2026-11-01-migration-vs-backup-lock.md).

**The writer during an outage** (`adapters/sqlite/writer.py`): when the database is unreachable or busy (connection
refused, DNS failure, timeout, "database is locked") the oldest batch is retried until it commits, backing off to one
try every 5 s, and every newer write waits behind it in order. Nothing is dropped. The log says it once when it starts
(`writer: the database is not taking writes (...)`), every minute while it lasts and once when it ends (`... takes
writes again after N s; committing the N queued writes`); `/api/health` shows `writer_outage_s` and the
`settleview.writer.outage` / `.retries` / `.dead_lettered` metrics follow it. Memory stays bounded: past 200,000 queued
writes (hours of market data) the oldest Coinbase trades, book and index seconds move to the dead-letter file
(`overflow`; re-derivable and replayable); trading and app rows are never shed. A write the database **refuses**
(an integrity or data error) is not retried forever: its batch is split in halves until only the bad write(s) are
left, those go to the dead-letter file (`data`), every good write around them commits. A stop during an outage writes
everything still queued to the file (`shutdown`) at once instead of trying batch after batch against a dead server.
Measured on a throwaway stack (2026-10-31): Postgres stopped for 78 s, 566 writes queued, all committed when it came
back, nothing dead-lettered.

**Replaying the dead-letter file** (`data/logs/writer-dead-letter.jsonl`, in the `settleview-data` volume under
Docker): first a dry run, then apply. Safe while the app runs; idempotent (applied writes are recorded in
`writer-dead-letter.jsonl.replayed` and skipped later; inserts ignore rows that already exist); the file itself is
never changed.

```bash
docker compose exec app python -m btc_signal_engine.ops replay-dead-letter           # lists what it would apply
docker compose exec app python -m btc_signal_engine.ops replay-dead-letter --apply   # applies it, oldest first
```

A write refused again (a `data` line, a value that was not saved as data) is reported and left for a person; exit 1
then, or while the database is unreachable (run it again later). Updates replay the values they had when queued: read
the dry run's list before applying `update` lines to rows that may have changed since.

### Postgres: backups

The `backup` service (`deploy/postgres/backup.sh`, the same `postgres:17.10` image, so `pg_dump` matches the server)
writes `settleview-YYYY-MM-DD.dump` (`pg_dump -Fc`, compressed) to the `settleview-backups` volume:

- at start when today's dump is missing (after `BACKUP_START_DELAY` seconds, default 300, so a deploy's migrations
  are not behind its table locks), then every day at `BACKUP_AT` (UTC, default 03:17); a failure is logged and
  retried after 10 minutes;
- each dump goes to `<name>.part`, is read back with `pg_restore --list`, then renamed: a `.dump` file is complete;
- keeps the 7 newest plus the newest of each of the 4 ISO weeks before them (the SQLite backups' rule);
- `/backups/last-ok` holds the time of the last good dump; the service is healthy while it is under 26 hours old;
- `BACKUP_HOOK` (optional): an executable you mount, run with the new dump's path after each good dump, for the
  off-site copy (for example `aws s3 cp "$1" s3://bucket/settleview/` or `az storage blob upload --file "$1" ...`,
  with that tool's own credentials, in an image that has the tool). Its failure is logged; the dump stays.

A dump now: `docker compose run --rm --no-deps backup bash /usr/local/bin/settleview-backup --once`. Copy dumps off the
host with `docker compose cp backup:/backups ./backups-copy` (the service must be running).

In the cloud the managed server's automated backups and point-in-time restore replace this service (below).

### Postgres: restore

**Drill** (any time; touches nothing running): `bash deploy/postgres/restore-drill.sh [settleview-YYYY-MM-DD.dump]`
starts a throwaway `postgres:17.10`, restores the newest (or named) dump into it, prints the Alembic version, the
table counts and a few row counts, and removes it. CI runs it after a real dump on every push.

**Real restore** (the database is lost or damaged):

1. Stop the writers: `docker compose stop app rec-pm-quotes rec-pm-settle backup`.
2. Copy the dump into the database container and restore it into a new database next to the old one, as the
   superuser, keeping the roles' ownership:
   ```bash
   docker compose cp ./settleview-YYYY-MM-DD.dump postgres:/tmp/restore.dump
   docker compose exec postgres createdb -U postgres settleview_restored
   docker compose exec postgres pg_restore -U postgres -d settleview_restored --exit-on-error /tmp/restore.dump
   ```
3. Check it: `docker compose exec postgres psql -U postgres -d settleview_restored -c "select version_num from
   app.alembic_version"`, and the row counts you care about.
4. Swap, in `psql -U postgres -d postgres`: `ALTER DATABASE settleview RENAME TO settleview_damaged; ALTER DATABASE
   settleview_restored RENAME TO settleview; REVOKE ALL ON DATABASE settleview FROM PUBLIC; GRANT CONNECT, TEMPORARY
   ON DATABASE settleview TO settleview_app; GRANT CONNECT ON DATABASE settleview TO settleview_research,
   settleview_backup;` (database-level grants are not in a dump).
5. `docker compose up -d`; drop `settleview_damaged` once you are sure.

A lost volume (the roles are gone too): start on a new `settleview-pg` volume (the init script makes the roles and
empty schemas), then `pg_restore --clean --if-exists -d settleview` the dump. Everything after the dump's time is lost:
trades and quotes are history the app collects again; orders and positions live on the venues, which the app
reconciles on start.

### Postgres: upgrades

- **Minor version** (17.10 to a later 17.x): change the tag in `compose.yaml` (`x-postgres-image`), in
  `deploy/postgres/restore-drill.sh` and in CI, then `docker compose up -d postgres backup`. The data files stay; the
  app reconnects (its pool checks a connection before use).
- **Major version** (17 to 18): a new data directory. Take a dump (`--once` above), stop the writers, start the new
  version on a new volume (a new volume name in `compose.yaml`), restore the dump, check it, start the services; keep
  the old volume until the new one has run a week. Run the restore drill with the new image first.
- **Schema** (a new Alembic migration): the app migrates on start, in one transaction under an advisory lock (two
  services starting together never migrate twice). By hand: `docker compose run --rm --no-deps app python -m
  btc_signal_engine.ops migrate`. Take a dump first for anything more than an added table or column.
- **0022** (2026-11-01) adds two indexes, `ix_trading_brackets_session` and `ix_agent_journal_kind` (kind, id). Both
  tables are small (about 200 brackets and 7,600 journal rows on the live database that day), so the plain CREATE INDEX
  in the migration's transaction takes milliseconds; it holds writes to those two tables only for that long and, like
  any DDL, waits out a `pg_dump` lock with the retry above.

**Hot paths that read incrementally** (review fix plan 2026-10-31 P1): the agent fleet reads every live session's
brackets in one query per tick (in a worker thread), not three or more per session; the stop monitor reads its REST
quotes before it takes the trading lock; the recent per-second index (the Coach's tick and odds series, Play's fair
odds and chart) comes from a cache that re-reads only the last 10 s, and the whole range once a minute, so a row the
writer lands later than that (after an outage) shows within a minute; Record "all" reads the journal's rule links
once, then only new rows. Why and what was measured: [docs/solutions/2026-11-01-per-session-reads-and-locked-io.md](
solutions/2026-11-01-per-session-reads-and-locked-io.md).

### Postgres: moving the SQLite data in (cut-over)

`python -m btc_signal_engine.ops migrate-sqlite-to-pg` copies the SQLite files into the database named by
`BTCFLOW_DATABASE_URL` (run it where psycopg is installed: with Docker, `docker compose run`):

- `--sqlite F` the app's database (default `BTCFLOW_DB_PATH`) into `app`; `--archive A` the trade archive
  (`btcflow-archive.db`) into `app.trades_archive`; `--research R` the recorders' `polymarket_us.db` into `research`.
- Every file is opened read-only (`mode=ro`). Copy from a **stopped** database or a backup: a backup is a single file
  (no `-wal`), which a read-only mount can open.
- It migrates the target to the latest schema, streams each table with `COPY` (memory bounded by one 50,000-row
  batch), sets each id sequence past the largest id, then verifies: equal row counts and equal checksums over 8
  sampled key ranges spread across the table. A verified table is recorded in `<schema>._sqlite_migration`; a re-run
  skips it, so an interrupted run continues where it stopped (a table cut short is copied again from the start).
- A target table that already has rows and is not recorded as copied is refused; `--force-table T` empties it and
  copies it again. Columns the target lacks stop the run (start the current app once on that file first). Tables the
  app no longer defines are listed and left in the file.
- Exit 0 all copied and verified, 1 a refusal or a failed check (the message names the table), 78 no Postgres
  configured. It prints the URL without its password and never a value.

**Cut-over on a PC that runs the SQLite app today** (between trades, after agent sessions end; a restart ends them):

1. Install Docker Desktop. In the checkout that runs the app: deploy this code, then
   `python deploy/postgres/make_secrets.py`, `docker compose build`, `docker compose up -d postgres`,
   `docker compose run --rm --no-deps app python -m btc_signal_engine.ops migrate`.
2. Stop the SQLite app and the recorders: `python -m btc_signal_engine.ops stop-supervisor` (or `ops stop` and the
   recorders' own stop, however they were started).
3. Final copies with an integrity check (the app is stopped, so they are consistent), each must print `ok`:
   `python -c "import sqlite3; s=sqlite3.connect('file:data/btcflow.db?mode=ro', uri=True);
   d=sqlite3.connect('data/backups/btcflow-final.db'); s.backup(d); print(d.execute('pragma
   integrity_check').fetchone()[0])"`, and the same for `data/btcflow-archive.db` (to
   `data/backups/btcflow-archive-final.db`) and `data/research/polymarket_us.db` (to
   `data/backups/polymarket_us-final.db`).
4. Copy them in, the folder mounted read-only (write the host path your way, e.g. `C:/path/to/data/backups`):
   `docker compose run --rm --no-deps -v <host data/backups>:/import:ro app python -m btc_signal_engine.ops
   migrate-sqlite-to-pg --sqlite /import/btcflow-final.db --archive /import/btcflow-archive-final.db --research
   /import/polymarket_us-final.db`. It prints each table's rows and time; record the total in
   docs/research/LEARNINGS.md.
5. `docker compose up -d`. Check: `docker compose ps` (all healthy), `/api/health` (`status` live, `recorders` ok),
   Play, the Agents tab, Record's totals equal to the SQLite app's to the cent, a Telegram message, and the next
   morning's dump. Then one $1 trade end to end before longer runs.
6. **Rollback (7 days):** the SQLite files are untouched. To go back: `docker compose stop app rec-pm-quotes
   rec-pm-settle`, then start the SQLite app and recorders as before (the supervisor). What was written to Postgres
   after the switch is not in the SQLite file: market data is collected again, orders and positions are on the venues
   (the app reconciles them), but those days' journal, settings changes and Record history stay in Postgres (readable
   with the research role). After 7 days without a rollback, archive the SQLite files off the machine and delete them
   from `data/`.

## Resource limits and Postgres settings

Every compose service has a memory limit (a leak or a runaway agent CLI stops at its own service, never the host),
a memory reservation and a CPU cap. They are sized for one small host from the 2026-10-31 measurements (plan section
2.1) and are **the place to scale up**: on a bigger host raise the limit and, for Postgres, its memory settings with
it. In the cloud they become the task / container sizes.

| Service | Limit | Reservation | Why |
|---|---|---|---|
| `app` | 2 GiB, 2 CPUs | 512 MiB | ~300 MiB idle (230 MiB measured with telemetry on), plus up to `BTCFLOW_AGENT_WAKE_CONCURRENCY` (4) Claude Code CLI processes during agent wakes (~150 to 300 MB each, not yet measured). Raise it with the concurrency. |
| `postgres` | 2 GiB, 2 CPUs | 1 GiB | sized to its settings below: shared_buffers 512 MB + autovacuum (3 × 128 MB at most) + ~30 backends (~10 MB each, plus work_mem while sorting) ≈ 1.2 GB; the rest is OS cache, which the kernel reclaims first under the limit. |
| `backup` | 512 MiB, 1 CPU | 64 MiB | `pg_dump` streams; ~10 MiB idle. |
| `rec-pm-quotes` | 256 MiB, 0.5 CPU | 96 MiB | ~64 MiB measured. |
| `rec-pm-settle` | 256 MiB, 0.5 CPU | 64 MiB | ~58 MiB measured (70 MiB with telemetry on). |
| `openobserve` | 1 GiB, 1 CPU | 256 MiB | ~350 MiB measured just after start with its caches capped; budget from the plan: ~0.5 GiB, under 0.1 CPU idle, under 1 GB of disk for 14 days. |

**Postgres settings** (`command:` flags in `compose.yaml`; a managed server takes the same values from its parameter
group): `shared_buffers=512MB` (~25 % of the limit), `effective_cache_size=1536MB` (~75 %: the planner's estimate of
buffers plus OS cache, nothing is allocated), `work_mem=8MB` (per sort / hash node: small, since ~30 connections can
each use several), `maintenance_work_mem=128MB` (vacuum, index builds, the archive move), `max_wal_size=2GB` and
`min_wal_size=256MB` with `checkpoint_timeout=15min` and `checkpoint_completion_target=0.9` (fewer, smoother
checkpoints than the stock 1 GB / 5 min; crash recovery replays at most ~2 GB), `random_page_cost=1.1` and
`effective_io_concurrency=200` (SSD), `track_io_timing=on`, `log_min_duration_statement=500ms` (slow statements in
`docker compose logs postgres`), and `shared_preload_libraries=pg_stat_statements`. `max_connections` stays 100.
On a host with more memory: shared_buffers 25 % of the container's limit, effective_cache_size 75 %, work_mem up to
16 to 32 MB, maintenance_work_mem 256 MB to 1 GB.

**Query statistics.** `deploy/postgres/initdb.sh` creates `pg_stat_statements` on a new volume and grants
`pg_read_all_stats` to `settleview_research`. On a volume made before 2026-10-31, run once (after the restart that
loads the library):

```bash
docker compose exec -T postgres psql -U postgres -d settleview -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements" -c "GRANT pg_read_all_stats TO settleview_research"
```

Then, read-only as the research role: `select calls, round(mean_exec_time::numeric, 2) ms, query from
pg_stat_statements order by total_exec_time desc limit 10`, and `EXPLAIN (ANALYZE, BUFFERS)` the top ones. Statement
texts are normalized (`$1` for each value).

## Development and tests (the dev container)

Production runs in Linux containers, and so does everything a developer runs: the tests, the quality gates, research
scripts and experiments, the evals and the README screenshots, on every host. There is no host virtualenv.
`scripts/dev.py` (standard library only) drives `deploy/dev/compose.yaml`; the host needs Docker and any Python 3
(`python3 scripts/dev.py ...`), or only uv (`uv run --no-project python scripts/dev.py ...`: uv's own interpreter, no
project environment created). The image (`deploy/dev/Dockerfile`) is the
production base (`python:3.13.18-slim-trixie`, the same digest as the root Dockerfile), uv, every extra of
`pyproject.toml` (`dev`, `postgres`, `otel`) at its pins, and Playwright's Chromium with its system libraries; the
checkout is bind-mounted at `/app`, so an edit needs no rebuild. Every command first runs `docker compose build`
(cached: a no-op unless `pyproject.toml` or the dev Dockerfile changed).

```bash
python scripts/dev.py test [pytest args]     # the suite; e2e in Chromium (SETTLEVIEW_E2E_BROWSER_CHANNEL=chromium)
python scripts/dev.py postgres-tests         # `-m postgres` on a throwaway Postgres 17, none may skip (CI's rule)
python scripts/dev.py postgres-tests --full  # ... then the whole suite with the store on that Postgres
python scripts/dev.py quality [args]         # scripts/quality.py: ruff + lint-imports in the container, the rest
                                             # (hadolint, shellcheck, trivy) in their own containers from the host
python scripts/dev.py experiments ARGS...    # scripts/experiments.py (run, grid, report, selftest), data read-only
python scripts/dev.py script PATH [ARGS...]  # any research script (scripts/*.py, scripts/quick/*.py), the same way
python scripts/dev.py evals test | run PIPELINE | graders | export ... | review ...   # the agent evals (evals/)
python scripts/dev.py shell | run CMD...     # a shell, or any command, in the container
python scripts/dev.py down                   # remove the dev project's containers, network and uv cache volume
```

Isolation from the live stack: the compose project is `settleview-dev` (passed with `-p` on every call, and any
`COMPOSE_*` or `BTCFLOW_*` variable in the shell is dropped first), its network and volume (`settleview-dev_uv-cache`)
are its own, and no service publishes a port. The test Postgres keeps its data on tmpfs, has a dev-only password and
is removed after the run. On Linux and macOS the container runs as the host user, so anything it writes into the
checkout (e2e screenshots under `tests/e2e/.artifacts/`) is yours. The image has the docker CLI and compose plugin
for `tests/deploy/test_compose.py` (`docker compose config` only); no Docker socket is mounted.

A second checkout (a git worktree) used at the same time sets `SETTLEVIEW_DEV_PROJECT=settleview-dev-<name>`: its
own compose project, so two `postgres-tests` runs never share, or remove, each other's Postgres (any other name is
refused; never `settleview`). The checkout's `data/` (git-ignored, recorded data) is mounted read-only in every dev
container, so no test, shell or command run there can change it. No dependency carries a platform marker: the
container is the one environment.

**Research scripts and experiments** (`dev.py experiments`, `dev.py script`) run in the `research` service: `dev`
with different mounts, so a script can write only its outputs. `data/` is git-ignored and exists only on the
machine that recorded it; `--data DIR` (before the command) or `SETTLEVIEW_DEV_DATA` names it, default
`<checkout>/data` (from a worktree: `python scripts/dev.py --data ../btc-flow-engine/data experiments report`).

| In the container | Mode | Why |
|---|---|---|
| `/app` (the checkout) | read-only | a research run cannot change code, tests or settings |
| `/app/docs/research/` | read-write | write-ups and tables |
| `/app/data/` (`btcflow.db`, backups, logs) | read-only | the app's data is only ever read |
| `/app/data/research/` | read-write | new output files (CSV, JSON, Markdown, caches) and the `experiments_results.csv` append |
| every `*.db` in `data/research/` (with its `-wal`, `-shm`, `-journal`) | read-only | the research databases are only ever read |

The scripts still open every database with SQLite `mode=ro` (the rule); the mounts are a second, independent guard. A
script that tries to write an existing database fails with "Read-only file system"; a script that creates a new
database file under `data/research/` (a fetch or backfill into a new file) can. Each file is mounted by name at the
start of the run, so a database created during a run stays writable until the next one. The live stack keeps its data
in Postgres and its own volumes, never in a checkout's `data/`.

**Evals** (`dev.py evals`): `evals test` runs `pytest tests/evals` (what CI runs); `evals run|graders|grade ...` is
`python -m evals ...`; `evals export ...` and `evals review ...` are `python -m evals.export|review ...`. The checkout
is read-write (results go to `evals/results/`, packs to `evals/review/`, both git-ignored) and `data/` is mounted
read-only, so `--source data/btcflow.db` works. A live run's key: `ANTHROPIC_API_KEY` in the host's environment is
passed through by name, and `--api-key-file F` is mounted read-only into the container for that run.

**README screenshots** (`docs/images/`) come from the dev container only: `tests/e2e/test_design_pass.py` and
`tests/e2e/test_fleet_ui.py` write them on every full run, so a screen change is re-captured with
`python scripts/dev.py test tests/e2e/test_design_pass.py tests/e2e/test_fleet_ui.py` and the images committed with it.
Images from another browser or OS (fonts and anti-aliasing differ) are not committed.

## Quality gates (Ruff, Trivy, Hadolint, ShellCheck)

One command runs every gate, locally on any OS and in CI (`.github/workflows/ci.yml`, jobs `lint` and `trivy`, which
call the same script). Locally the default is the dev container (`scripts/dev.py`, "Development and tests" above),
which runs Ruff and lint-imports inside it and the container tools from the host's Docker:

```bash
python scripts/dev.py quality              # everything, as below; any scripts/quality.py arguments after it
python scripts/dev.py quality --only ruff  # or imports, hadolint, shellcheck, trivy-image, trivy-config, trivy-fs, trivy
python scripts/dev.py quality --only trivy --image settleview:local    # scan an image you already built
```

It exits non-zero when any gate fails. Ruff and lint-imports run from the project's environment (the dev container;
CI's runner installs its own); the other tools run from their official images with Docker, pinned by version and
digest in `scripts/quality.py` (no install), so `python scripts/quality.py --only hadolint,shellcheck,trivy` also runs
from any host Python:

| Gate | Tool | What fails it |
|---|---|---|
| `ruff` | Ruff 0.17.1 (the `dev` extra) | any finding of the rules in `pyproject.toml` `[tool.ruff]`: pyflakes, pycodestyle errors, bugbear, pyupgrade, bandit (`S`), async, simplify |
| `imports` | import-linter (the `dev` extra) | a broken contract in `.importlinter` (the layers; no I/O in domain and application; the Kalshi SDK only in the adapters; the supervisor never loads the app) |
| `hadolint` | `hadolint/hadolint:v2.12.0` | a warning or error in `Dockerfile` or `deploy/dev/Dockerfile` |
| `shellcheck` | `koalaman/shellcheck:v0.10.0` | a warning or error in `deploy/**/*.sh` |
| `trivy-image` | `aquasec/trivy:0.76.0` | a HIGH / CRITICAL vulnerability **with a fixed version** in the image (OS and Python packages), or a secret in it |
| `trivy-config` | Trivy | a HIGH / CRITICAL misconfiguration (Trivy recognizes the Dockerfile; it has no checks for compose or systemd files) |
| `trivy-fs` | Trivy | a secret in the repository (`.env*`, `secrets/`, `keys/`, `*.pem`, `*.key`, `compose.override.yaml`, `data/` and `.venv` are never read) |

Style rules that would rewrite hundreds of lines (line length, one-line `if x: return`) are off until a deliberate
one-time `ruff format` commit; `ruff format --check` is not a gate yet. Where a rule is silenced on one line, the
`# noqa` says why (for example S608 on SQL whose interpolated parts are identifiers from code, values always bound).

**Vulnerabilities without a fix** (`--ignore-unfixed`): Debian publishes many HIGH entries with no fixed package yet
(44 in the image on 2026-10-31); they are not actionable and do not fail the gate. When Debian ships a fix, the
next image build takes it (`apt-get upgrade` in the Dockerfile) and the gate goes red until it does.

**Accepting a finding:** only in `.trivyignore` (none today), one entry per line with the reason, the date and who
accepted it, and an `exp:` date so it is reviewed again:

```
# 2026-11-05 owner: CVE-XXXX-YYYY in libfoo, not reachable (no TLS in this path); fix expected in Debian 13.3
CVE-XXXX-YYYY exp:2026-12-31
```

**The base image** is pinned by digest in the Dockerfile (`python:3.13.18-slim-trixie@sha256:...`); the build runs
`apt-get upgrade`, so Debian's security fixes since that digest land in the image (the layer depends on the build day:
reproducible-ish, on purpose). To move to a newer base: `docker buildx imagetools inspect python:3.13.<n>-slim-trixie`,
put its digest in the Dockerfile, rebuild with `--pull --no-cache`, and run the gates. pip is removed from the image
after the install: its vendored msgpack, urllib3 and setuptools copies were the Python findings Trivy reported
([docs/solutions/2026-10-31-trivy-findings-in-pips-vendored-packages.md](solutions/2026-10-31-trivy-findings-in-pips-vendored-packages.md)).

## Deploying

**Docker**: `git pull`, then `docker compose up -d --build` (with `GIT_SHA=$(git rev-parse HEAD)` in the shell, the
image is labelled with its commit: `org.opencontainers.image.revision`, and telemetry's `service.version`). Compose stops the old app with SIGTERM (a clean shutdown,
up to 60 s: the log says `SIGTERM: stopping gracefully`, then `process exiting`; before 2026-10-31 uvicorn re-raised
the signal and the app died at once with exit 143, nothing flushed: [docs/solutions/2026-10-31-sigterm-uvicorn-reraise-and-db-outage.md](solutions/2026-10-31-sigterm-uvicorn-reraise-and-db-outage.md)) and starts the new one; a recorder is restarted
only when its image changed, and loses at most a second of quotes. A change to the `postgres` service's settings in
`compose.yaml` recreates it too: a database outage of a few seconds that the app's writer rides out.

**Without Docker**: stop the app gracefully **before** checking out new code, then check out, then let it start
again. The command is the same on every OS. `--data-dir` is the folder that holds the app's database (the parent of
`BTCFLOW_DB_PATH`, which is the default), and need not be the folder being deployed:

```bash
python -m btc_signal_engine.ops stop --data-dir /path/to/data --hold   # continue only on exit code 0 or 2
git -C /path/to/run-folder checkout <new code>
python -m btc_signal_engine.ops release --data-dir /path/to/data       # the supervisor starts the app again
```

`--hold` keeps the supervisor from starting the app again until `release` (without it, the supervisor restarts it 10 s
after the clean exit, as the old watchdog did). On Windows, `scripts\deploy_stop.ps1 -DataDir <dir> [-Hold]` is a thin
wrapper around the same command, kept for one release.

At start the app writes its process id to `<data dir>/app.pid` and deletes the file when it exits; `ops stop` finds the app only through that file (checking that the process really runs `python -m btc_signal_engine`). It creates `<data dir>/stop.request` and waits up to 60 s (`--timeout`, the same as compose's `stop_grace_period`). The app notices the file within a second, flushes its database writer, stops its tasks, deletes both files, logs `clean shutdown (deploy)` and exits with code 0. If the app hasn't exited by then, the command falls back to a forced kill of that process (and, on Windows, the virtual environment's `python.exe` launcher that started it, if any) and prints `FELL BACK TO A FORCED KILL` (rows still in the writer's buffer may be lost). It never touches other processes and never calls the app over HTTP.

Exit codes: 0 stopped cleanly (or no app is running), 1 still running, 2 stopped by the fallback, 3 **cannot tell**: there is no valid pid file but some `python -m btc_signal_engine` is running (an app started before pid files existed, or with another data folder). Nothing was done; do not check out new code until the app is stopped by hand.

Every exit that passes through Python logs `process exiting (pid …, up … s)`. An exit without that line was a forced kill. Every 10 minutes the app logs `memory: private bytes …`.

## Where it runs: one section per host

The same entry points everywhere:

| Process | Command | Writes |
|---|---|---|
| the app | `python -m btc_signal_engine` | Postgres schema `app` (`BTCFLOW_DATABASE_URL`), else `<data dir>/btcflow.db` (`BTCFLOW_DB_PATH`) |
| quotes recorder | `python -m btc_signal_engine.recorders pm-quotes` | `pm_ws_quotes`, `pm_trades` in Postgres schema `research`, else `<research dir>/polymarket_us.db` |
| settlement recorder | `python -m btc_signal_engine.recorders pm-settle` | `pm_windows` in the same place |

The recorders read market data only (no orders). The quotes recorder needs the Polymarket US key
(`BTCFLOW_POLYMARKET_KEY_ID`, `BTCFLOW_POLYMARKET_SECRET_KEY`); the settlement recorder needs none. Without the key the
quotes recorder exits with code 78 and says why (the supervisor does not restart it then). `--check` checks the
configuration and prints where a recorder would write, without the network. `scripts/record_polymarket_us*.py` still
work and run the same code.

**Polymarket quotes recorder** (`pm-quotes`): a live connection does not mean quotes are flowing (2026-10-31 13:59 CDT:
the venue kept sending trades and sent no book for 45 minutes; the recorder recorded nothing on its open sockets).
Each lane (5m, 15m, 1h) therefore watches its own connection:
- trades with no quote for 60 s, or no quote for 120 s while trades arrived, is a **book silence**: a WARNING
  (`5m lane: book silence on a live connection (...)`) and a fresh connection;
- three book silences in a row without a single quote are a **venue-side book outage**: one WARNING ("a
  Polymarket-side book outage"), then reconnects with a doubling wait (5 s up to 120 s); trades are still recorded;
- no quote from any lane for **10 minutes** despite reconnects, and no venue outage: the process logs an ERROR and
  exits with code **75**, and its restart policy (Docker `unless-stopped`, the portable supervisor) starts a fresh one.
  This matters because plain Docker does not restart an *unhealthy* container: the compose healthcheck (quotes at
  most 2 minutes old) only reports. During a venue outage it stays up, since a restart cannot bring the venue's book
  back.
The app's `data` alert and `/api/health` tell the two cases apart by comparing the newest recorded print with the
newest quote ([docs/solutions/2026-10-31-pm-quiet-book-vs-dead-book-feed.md](solutions/2026-10-31-pm-quiet-book-vs-dead-book-feed.md)).

**Settings for running** (names and purpose only; all optional):

| Variable | Purpose |
|---|---|
| `BTCFLOW_RESEARCH_DIR` | Folder of the research databases the recorders write and the app reads (default: `research` next to the database). |
| `BTCFLOW_FILE_LOG` | `false` turns the app's own rotating file log off (stderr only). The image and the supervisor set it. Formerly `BTCFLOW_LOG_FILE`, which read as "the setting `log` from a file" and failed every start; the old name now stops the start with an error naming the new one. No setting name ends in `_FILE`. |
| `BTCFLOW_PC_PORT` | Optional second listener (same process, same app): the only place a browser can pair as this PC (`ops pair-pc`) and where its PC cookie counts. The PC is that listener AND the cookie, never the port alone: without the cookie it shows only "Pair this browser". Unset (default): none (then a loopback client of the main port can pair). Must differ from `BTCFLOW_API_PORT`. See "Running in Docker: who counts as the PC". |
| `BTCFLOW_PC_HOST` | The PC listener's bind address (default `127.0.0.1`; only with `BTCFLOW_PC_PORT`). `compose.yaml` sets `0.0.0.0` inside the container so its `127.0.0.1` port publish can reach it. |
| `BTCFLOW_AGENT_PORT` | Optional listener for external agents (MCP): it serves only `/api/agent/tools`, `/api/agent/call/{tool}` and `/api/agent/events/wait`, its requests are never the PC or a person, and every tool call needs a live external session's token. Those paths answer 403 `agent_port` on the other listeners. Unset (default): external agents cannot call tools. Must differ from the other two ports. |
| `BTCFLOW_AGENT_HOST` | The agent listener's bind address (default `127.0.0.1`; only with `BTCFLOW_AGENT_PORT`; compose: `0.0.0.0` inside the container). |
| `BTCFLOW_PUBLIC_URL` | The address phones open, e.g. `http://<this PC's LAN address>:8000` (no path). The Phone access card shows it, and its host joins the allowed Hosts. In a container the app cannot see the host's LAN address, so set it there; without it the card falls back to the first `BTCFLOW_ALLOWED_HOSTS` name, then (not in a container) the detected LAN address. |
| `BTCFLOW_LOCAL_CLIENTS` | Without a PC listener only: extra client IPs that may pair as this PC on the main port (they still need a pairing code and then the cookie). Default: loopback. A non-loopback entry logs a WARNING. |
| `BTCFLOW_<NAME>_FILE` | Read any setting from a file instead (Docker / Swarm secrets under `/run/secrets`), e.g. `BTCFLOW_POLYMARKET_SECRET_KEY_FILE`, `BTCFLOW_TELEGRAM_BOT_TOKEN_FILE`, `BTCFLOW_ANTHROPIC_API_KEY_FILE`. One trailing newline is dropped. Setting both forms, an unknown name or an unreadable file stops the start with an error that names the variable, never the value. |
| `BTCFLOW_TELEGRAM_BOT_USERNAME` | The Telegram bot's @username (a leading `@` is optional), shown in the pairing instruction "Send /start CODE to @name". Optional: unset, the app learns it from Telegram's `getMe` when the bot starts, and until then the Agent tab says "your bot". A value that is not a bot username (5–32 letters, digits or `_`, ending in `bot`) stops the start. |
| `BTCFLOW_HTTP_CLIENT_LOG_LEVEL` | The `httpx` / `httpcore` loggers' level: `WARNING` (default), `INFO` (one line per request), `DEBUG`, `ERROR`. Anything else stops the start. |
| `BTCFLOW_PYTHON` | `scripts/deploy_stop.ps1` only (a Windows host install): the Python that has Settleview installed. Required; the script stops with an error when it is unset. |

### Docker (Windows, macOS, Linux)

**Docker: key files.** Kalshi keys are files (`BTCFLOW_*_KEY_PATH`). Mount each one read-only from a git-ignored
`compose.override.yaml` next to `compose.yaml` (compose loads it automatically) and point the setting at the container
path, for example:

```yaml
services:
  app:
    volumes:
      - settleview-data:/data
      - {type: bind, source: /path/on/host/kalshi-prod.pem, target: /run/keys/prod, read_only: true}
```

with `BTCFLOW_TRADING_PROD_KEY_PATH=/run/keys/prod` in `.env`. Windows-only settings from a native install
(`BTCFLOW_AGENT_CLI_PATH`, `BTCFLOW_DB_PATH`, `BTCFLOW_API_HOST`) do not belong in a container's env: compose sets
what it needs, and the Linux image bundles the agent CLI.

`compose.yaml` runs five services: `postgres` and `backup` (the official `postgres:17.10` image) and, from one app
image, `app`, `rec-pm-quotes`, `rec-pm-settle`; a sixth, `openobserve`, only with `--profile observability` (see
"Observability"). First start: `python deploy/postgres/make_secrets.py` once (see "Postgres"); run it again after an
update that adds a secret file (it keeps every existing one). Each service's memory and CPU limits: "Resource limits
and Postgres settings".

```bash
docker compose up -d --build      # start or deploy (rebuilds, restarts what changed)
docker compose exec app python -m btc_signal_engine.ops pair-pc   # once per browser: open the link it prints
docker compose ps                 # each service's state and health
docker compose logs -f app        # every service logs to stderr
docker compose stop               # stop all (the app gets 60 s to shut down cleanly)
```

- **Configuration**: the app reads `.env` next to `compose.yaml` (optional): settings only, no secrets. Every secret
  is a compose secret file under `./secrets/` (see "Docker: secrets as files"); the recorders get only the database
  and the Polymarket key files.
- **Data** lives on named volumes: `settleview-pg` (Postgres's data directory: the app's and the recorders' data),
  `settleview-backups` (the nightly dumps) and `settleview-data` (`/data`: the app's logs, pid and stop files). On
  **Docker Desktop (Windows, macOS) keep the named volumes**: a bind mount of a host folder goes through a file-sharing
  layer that is slow and unsafe for a database's files. `docker volume inspect settleview_settleview-pg` shows where a
  volume lives. The database port is not published (see "Postgres: Tools").
- **Restarts and health**: every service has `restart: unless-stopped` and a healthcheck: Postgres's `pg_isready`
  (the app and the recorders start once it is healthy), the backup's last good dump (26 hours), the app's
  `/api/health`, each recorder's **process-local heartbeat** (`python -m btc_signal_engine.ops alive pm-quotes`: its
  loop touched `/tmp/settleview-pm-quotes.alive` within 60 s; `pm-settle`: 180 s). The recorders' liveness reads no
  database and no network on purpose: a venue or database outage is not cured by a restart. Data freshness stays an
  alert and readiness signal: `ops fresh pm-quotes` (2 minutes) / `pm-settle` (10 minutes), the app's `data` alert and
  `/api/health` `recorders`. Plain compose restarts an *exited* container, not an *unhealthy* one; Swarm and cloud
  platforms do, so these checks double as liveness probes there.
- **Network**: this PC uses `http://localhost:8001` (published on `127.0.0.1` only) in a browser paired once with
  `ops pair-pc`; phones use `http://<this PC's LAN address>:8000` with the PIN; external agents use
  `http://localhost:8002` (published on `127.0.0.1` only) with their session token (see "Running in Docker: who
  counts as the PC" below). The phone
  port's host address is `BTCFLOW_PHONE_BIND` (default `0.0.0.0`, every interface, as on this PC). **On a Linux
  server set it** in `.env` to the LAN address (or `127.0.0.1` behind a reverse proxy or VPN): Docker publishes ports
  with its own iptables rules, ahead of ufw / firewalld, so a host firewall does not protect a port published on
  `0.0.0.0`.
- **Logs**: the `local` driver, 5 x 10 MB per container (see "Logs").

**Docker: secrets as files.** No secret is an environment value (`docker inspect` and every child process, such as
the agent's CLI, would see it): each is a compose secret mounted under `/run/secrets/` and read through its
`BTCFLOW_<NAME>_FILE` setting.

| File in `./secrets/` | Setting | Mounted into |
|---|---|---|
| `polymarket_key_id`, `polymarket_secret_key` | `BTCFLOW_POLYMARKET_KEY_ID`, `BTCFLOW_POLYMARKET_SECRET_KEY` | app, rec-pm-quotes |
| `anthropic_api_key` | `BTCFLOW_ANTHROPIC_API_KEY` | app |
| `telegram_bot_token` | `BTCFLOW_TELEGRAM_BOT_TOKEN` | app |
| `kalshi_key_id`, `trading_demo_key_id`, `trading_prod_key_id` | `BTCFLOW_KALSHI_KEY_ID`, `BTCFLOW_TRADING_{DEMO,PROD}_KEY_ID` | app |
| `pg_*_password`, `otlp_headers` | the database passwords, the OTLP headers (see "Postgres", "Observability") | as before |

An **empty file means "not configured"** (that feature stays off), so every file always exists:
`deploy/postgres/make_secrets.py` creates the missing ones empty. A value in both `.env` and a non-empty file is an
error that stops the start, naming the setting ("both BTCFLOW_X and BTCFLOW_X_FILE are set"). The Kalshi **key files**
(`*_KEY_PATH`, the PEMs) are not values: keep mounting them read-only ("Docker: key files"). Without Docker nothing
changes: `.env` (or the `_FILE` forms) works as before. On Swarm, `docker secret create <name> <file>` and mark the
entries `external: true`; in the cloud use the platform's secret store mounted as files (Azure Container Apps secret
volumes, ECS secrets from Secrets Manager).

**One-time cut-over from keys in `.env`** (an existing install; between agent sessions, as any deploy):

```bash
git checkout <this commit>                       # in your deploy checkout
python deploy/postgres/make_secrets.py           # adds the empty secret files; keeps every existing one
python deploy/secrets_from_env.py --dry-run      # names what it would move (never a value)
python deploy/secrets_from_env.py                # moves each value to its file, then removes the line from .env
docker build --build-arg GIT_SHA=$(git rev-parse HEAD) -t settleview:local .
docker compose up -d                             # recreates the services with the secrets mounted
docker compose ps                                # all healthy; the app logs no configuration error
docker inspect settleview-app-1 --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -c _FILE=   # 9: 7 venue / API keys, the database password, the OTLP headers
```

`secrets_from_env.py` writes each value to its file, reads it back, and only then replaces the `.env` line by a
comment naming the file (the rest of `.env` is kept byte for byte; no backup copy is made). A file that already
holds a different value is a conflict: that setting is left in both places, the script exits 1, and the app would
refuse to start with that pair: settle it by hand. A `BTCFLOW_X_FILE` line in `.env` is reported, not moved (compose
now sets that variable itself). Rollback: put the values back into `.env` and empty the files (or check out the
previous commit, whose compose passes `.env` through). Verify after the cut-over that Play shows the Polymarket
balance, the agent's key shows as working and the Telegram bot answers.

**Docker: hardening.** The app and both recorders run as uid 10001 with a **read-only root filesystem**: writes go
to the `/data` volume (the app) and to tmpfs mounts at `/tmp` (128 MB: heartbeat files, the agent CLI's temporary
files) and `/home/app` (64 MB). The agent's Claude Code CLI writes its config under `/data/agent-cli` and was tested
read-only (the SDK's real-CLI tests pass in the image with `--read-only`, tmpfs `/tmp` and `$HOME`, no capabilities).
They drop **every Linux capability** and run with `no-new-privileges`. PID 1 is **tini** (from the image's
ENTRYPOINT: signals forwarded, zombies reaped on every platform, so no `init: true`). Postgres: read-only root with
tmpfs `/var/run/postgresql` and `/tmp`, only `CHOWN DAC_OVERRIDE FOWNER SETGID SETUID` (its entrypoint hands the data
folder to the postgres user), `no-new-privileges`. The backup service: read-only root, no capabilities. OpenObserve: no
capabilities, `no-new-privileges`; its root filesystem stays writable (not tested read-only). In the cloud the same:
ACA and ECS task definitions take `readonlyRootFilesystem`, dropped capabilities and tmpfs / ephemeral volumes.
- **Build fails with "additional privileges requested: pass --allow=fs.read=Dockerfile"**: the shell's folder differs
  in letter case from the real path (`documents` vs `Documents` on Windows), so compose's build (Bake) sees the
  Dockerfile outside the context. `cd` to the folder with its real case, or build with `docker build -t
  settleview:local .` and start with `docker compose up -d` (no `--build`). See
  [docs/solutions/2026-10-31-compose-bake-path-case.md](solutions/2026-10-31-compose-bake-path-case.md).
- **Moving existing data in**: `ops migrate-sqlite-to-pg` from the SQLite files (see "Postgres: moving the SQLite
  data in").

### Portable supervisor (Linux, macOS, Windows; no Docker)

```bash
python -m btc_signal_engine.supervise              # the Python that has Settleview installed (`pip install .`)
python -m btc_signal_engine.supervise --only app   # or --without pm-quotes (an unknown name is an error)
```

Run it from the folder that holds `.env`. It runs the app and both recorders as child processes and:
- writes one log per child to `<data dir>/logs/` (`app.log`, `pm-quotes.log`, `pm-settle.log`, 10 MB × 10 files) and
  its own `supervisor.log`;
- restarts a child 10 s after a clean exit (as the old watchdog did; a deploy's `ops stop` is a clean exit) and with
  backoff after a crash (1 s doubling to 60 s, reset once it ran a minute). A configuration error (exit 78) is not
  restarted;
- keeps a child stopped while `<data dir>/<name>.hold` exists (`ops stop --hold`, `ops release`);
- stops on Ctrl+C, SIGTERM or `python -m btc_signal_engine.ops stop-supervisor`: every child is asked through its
  stop file, given 60 s (as compose's `stop_grace_period`), then killed with its own children;
- runs once per data folder (`supervisor.pid`). On Windows the children are tied to it: if the supervisor is killed,
  they end too, so a new one never runs duplicates.

Start at boot: thin examples in [`deploy/`](../deploy/README.md) (systemd unit, launchd agent, one Task Scheduler
line on Windows). They only start the supervisor.

**Linux / macOS**: systemd's stop (SIGTERM to the supervisor, `KillMode=mixed`) and launchd's `bootout` stop the
children cleanly. **Windows**: start it hidden with `pythonw.exe` (the children still log to their files); stop it
with `ops stop-supervisor`. Why process control is done the way it is on Windows:
[docs/solutions/2026-10-31-portable-process-control-windows.md](solutions/2026-10-31-portable-process-control-windows.md).

### Deploying to the cloud (Azure Container Apps, AWS ECS / Fargate, Docker Swarm)

Not tried yet; the image and services are built for it. The rules that matter on every platform:

- **One replica per service, always.** The app keeps in-memory state (agent sessions, the order gate, streams) and
  each recorder holds one WebSocket per window length; two copies would trade or record twice. Use `replicas: 1`
  (Swarm; the compose file's `deploy:` keys), min = max = 1 replica (Container Apps), desired count 1 with a minimum
  healthy percent of 0 (ECS), and stop the old task before the new one starts on an update (`order: stop-first`).
- **The database is the platform's managed Postgres 17**, not a container (Well-Architected: patching, backups,
  point-in-time restore and failover are the platform's job). Leave out the `postgres` and `backup` services and point
  `BTCFLOW_DATABASE_URL` at the server, with `?sslmode=require` (or `verify-full` with the provider's CA bundle):
  - **Azure Database for PostgreSQL Flexible Server**: version 17, private access (VNet integration) in the Container
    Apps environment's VNet, automated backups with point-in-time restore (7 to 35 days; geo-redundant if wanted),
    zone-redundant high availability optional. The URL user is `settleview_app` (password auth; Entra ID auth is not
    used by the app).
  - **Amazon RDS for PostgreSQL** (or Aurora PostgreSQL): version 17, in private subnets with a security group open to
    the ECS tasks only, automated backups with point-in-time restore (retention 7 to 35 days), Multi-AZ optional; the
    password in Secrets Manager, given to the task as `BTCFLOW_DATABASE_PASSWORD` (or a file).
  - **Roles on a managed server**: run the SQL of `deploy/postgres/initdb.sh` once as the admin user (replace the
    `\set ...` lines with the passwords; the platform's admin is not a superuser, which this SQL does not need, and
    on RDS `pg_read_all_data` is granted the same way). Then `ops migrate` (or the app's first start) makes the tables,
    and `ops migrate-sqlite-to-pg` from a container in the same network copies existing data.
  - The recorders use the same database (schema `research`), so no file volume is needed in the cloud at all.
- **Secrets**: no `.env` is needed. Give each secret to the service as a file and set `BTCFLOW_<NAME>_FILE`
  (Swarm: `docker secret create` and `secrets:`, mounted under `/run/secrets/`; Container Apps: secrets mounted as a
  volume or referenced as environment variables; ECS: Secrets Manager / Parameter Store values). The recorders need
  only the Polymarket key; never give them the Kalshi or Anthropic keys.
- **Logs**: stderr in every container; the platform's collector reads it (Container Apps: Log Analytics; ECS:
  CloudWatch via `awslogs`; Swarm: `docker service logs`).
- **Telemetry**: point `OTEL_EXPORTER_OTLP_ENDPOINT` at the platform's OpenTelemetry collector (Container Apps' managed
  agent, an ADOT sidecar on ECS) and give its credential, if any, as the headers file (see "Observability").
- **Sizes**: the compose limits are the starting task / container sizes ("Resource limits and Postgres settings").
- **Health**: the image's healthcheck is the app's liveness probe (`GET /api/health` on loopback: open without a
  session, it answers such a caller only `{"status": "up"}`); each recorder's is
  `python -m btc_signal_engine.ops fresh <name>` (exit 0 / 1). On a platform without exec probes, the app's Telegram
  `data` alert covers the recorders.
- **Network exposure**: never on the internet. Keep ingress internal (Container Apps internal ingress, an ECS service
  without a public load balancer, Swarm without a public port) and reach it over a VPN (WireGuard, Tailscale, the
  cloud's VPN gateway). The app has no HTTPS and no internet-grade login. Never route ingress to the PC listener
  (`BTCFLOW_PC_PORT`) or the agent listener (`BTCFLOW_AGENT_PORT`): being the PC needs a paired browser's cookie, but
  pairing is meant for one person's own machine. To be the PC on a remote host, open an SSH tunnel to the PC
  listener (`ssh -L 8001:<the app container's address>:8001 <host>`, then `http://localhost:8001`) and pair through
  it with `ops pair-pc` run in the container. The recorders need outbound internet only.
- **Docker Swarm**: `docker stack deploy` cannot read `compose.yaml` itself (it rejects `depends_on` conditions and
  numeric CPU limits) and drops the host IP of every publish, so `127.0.0.1:8001:8001` would open the PC listener on
  every node through the routing mesh. Render the stack file instead:

  ```bash
  python deploy/swarm/render_stack.py [--profile observability]   # writes settleview-stack.json
  docker stack deploy -c settleview-stack.json settleview
  ```

  It merges [`deploy/swarm/compose.swarm.yaml`](../deploy/swarm/compose.swarm.yaml) over `compose.yaml` (phones' 8000
  only; no 8001, 8002 or 5080; no `.env`: settings from the deploying shell, secrets as Swarm secrets and
  `BTCFLOW_<NAME>_FILE`) and refuses to write a file that would publish 8001, 8002 or 5080; CI renders it and checks
  `docker stack config` on the result. Reach the PC listener and OpenObserve through an SSH tunnel to the node.
  `docker stack deploy` ignores `build:`: build and push the image to your registry and set `image:`. Pin each
  service to the node that holds its volume with a placement constraint (`postgres` and `backup` with the
  `settleview-pg` and `settleview-backups` volumes; `app` with `settleview-data`, which holds the PC secret). Swarm
  ignores `depends_on`: a service that starts before the database is up exits and is restarted until it answers.
  OpenObserve's root password comes from `ZO_ROOT_USER_PASSWORD` in the deploying shell. Keep the phone port off the
  public interface.

## Moving to a server (home web)

1. From a host already on Postgres: a dump (`backup --once`), copied to the server and restored there (see "Postgres:
   restore"). From the SQLite app: the final copies of the cut-over (see "Postgres: moving the SQLite data in").
2. Copy the project and a `.env` to the server, run `python deploy/postgres/make_secrets.py`, then
   `docker compose up -d --build` and restore or copy the data in. Each container runs one process as a non-root
   user. Schema migrations apply on start.
3. The compose file publishes the PC listener (8001) and the agent listener (8002) on `127.0.0.1` only and the phone
   port (8000) on the network. Pair the browser you administer from: `docker compose exec app python -m
   btc_signal_engine.ops pair-pc` (from another machine, through an SSH tunnel to the server's 8001).

The image runs Python 3.13 (`python:3.13-slim`), and CI builds it on every push (no push to a registry). Its
`HEALTHCHECK` calls `/api/health` on loopback inside the container every 30 s; `docker compose ps` shows `healthy`
once the app answers.

### Running in Docker: time zone

Every "today" in the app is one day in one zone (`domain/days.py`, since 2026-11-01): Play's day limit and its today /
week P&L, Record, the Agents fleet and global limits, the daily Telegram summary. The zone is read once at startup from
`TZ` (an IANA name such as `America/Chicago`; a leading `:` is accepted); unset means `UTC`, and a name the zone
database does not know stops the start with an error naming it (never another zone). The log says
`days run midnight to midnight <zone> (TZ)` at startup, and Record, Play's scoreboard and the trade-limits card name
the zone; when the browser is in another zone the screen says so. A container runs in UTC unless told otherwise: set
`TZ=<IANA zone>` in the `.env` next to `compose.yaml`; compose passes it to the app and the recorders (default `UTC`).
Changing it moves where "today" starts (the day limit's use included) from the next start. Found in the 2026-10-31 dry
run: the copied data matched to the cent, but "today" started at UTC midnight until TZ was set; before 2026-11-01
Play's day limit and P&L always used the UTC day even then (review B5#2;
docs/solutions/2026-11-01-one-users-day.md).

### Running in Docker: who counts as the PC

**The PC is a paired browser, never a network position** (2026-10-31, review fix P0 #1). In a container every
request, the PC's, every phone's and every other container's, reaches the app from a bridge address, and a request on
the PC port can come from a sibling container (`http://app:8001` with any `Host` it likes) or from a process inside
the container. So the port proves nothing alone: the PC is a request on the PC listener that carries the **PC
cookie**, which only a browser paired with a one-time code gets.

| Who | Address | Published as | Gate |
|---|---|---|---|
| This PC | `http://localhost:8001` | `127.0.0.1:8001:8001` (loopback of the host only) | the PC cookie (pair once); without it only "Pair this browser" |
| Phones | `http://<this PC's LAN address>:8000` | `0.0.0.0:8000:8000` | the PIN |
| External agents (MCP) | `http://localhost:8002` | `127.0.0.1:8002:8002` | the session token on every tool call; never the PC or a person |

**Pairing (once per browser, and after "Sign out PC browsers").**

```bash
docker compose exec app python -m btc_signal_engine.ops pair-pc     # Docker
python -m btc_signal_engine.ops pair-pc                             # without Docker, from the checkout
```

It prints a one-time code (`XXXX-XXXX-XXXX`, good for 5 minutes, one browser) and a link,
`http://localhost:8001/pair?code=...`. Open the link on this PC and press **Pair** (or open `/pair` and type the
code). The app sets the PC cookie (`settleview_pc`: HttpOnly, SameSite=Strict, 400 days, an HMAC under the secret
`<data dir>/pc-secret`) and opens Play. Only the code's SHA-256 is written (`<data dir>/pc-pairing.json`); it is
deleted when used, after 5 wrong codes, or replaced by the next `pair-pc`. Pairing works only on the PC listener (or,
without one, from a loopback client on the main port), never on the phone port, and the link only fills the form in:
pairing is the POST of the Pair button (a cross-site POST is refused).

**Signing PC browsers out**: the Phone access card's **Sign out PC browsers** (this browser too), or
`python -m btc_signal_engine.ops signout-pc` (the app picks it up within 5 seconds). Both replace the PC secret; every
PC browser pairs again. A restart or a deploy keeps paired browsers (the secret is a file on the `settleview-data`
volume). The secret lives in the app's data folder, not the database, on purpose: the recorders share the app's
database role and only the app's container mounts that folder.

**What the listeners serve.** The PC listener answers only to `localhost`, `127.0.0.1` and `[::1]` with its own port
(anything else gets 400) and refuses cross-site changes. The agent listener serves only `/api/agent/tools`,
`/api/agent/call/{tool}` and `/api/agent/events/wait` (everything else 404; those paths answer 403 `agent_port` on the
other two), its actor is `agent-api`, and every tool call needs the token of a live external session. The approval,
skip, resume, limits, band, mode and presence-heartbeat routes need the PC cookie or a phone's PIN session, never just
a network position or the absence of an `X-Agent-Token` header. `GET /api/health` needs no session (the healthcheck)
and answers such a caller `{"status": "up"}` only. Loopback inside the container is not the PC: administer with
`ops` commands (`docker compose exec app python -m btc_signal_engine.ops ...`), not loopback HTTP.

**Why keep a PC listener once there is a cookie.** It is the only place pairing is allowed and the only place the PC
cookie counts, so a stolen cookie is useless from the phone port, and the phone port can never mint a PC. It also
keeps the PC's address (`localhost:8001`) on its own Host allow-list, which stops DNS rebinding against it.

Rules:

- **Publish 8001 and 8002 on `127.0.0.1` only**, never point a reverse proxy or tunnel at them for other people, and
  keep both sides of the publish the same (`8001:8001`: the browser's `Host` carries the host side's port). The
  Dockerfile does not `EXPOSE` them, so `docker run -P` does not publish them either.
- **Swarm drops the `127.0.0.1`**: deploy with `deploy/swarm/render_stack.py` (see "Docker Swarm"), which publishes
  neither.
- **Phones in Docker**: set `BTCFLOW_PUBLIC_URL=http://<this PC's LAN address>:8000` in `.env` (the Phone access card
  shows it, and its host joins the allowed Hosts; the container cannot see the host's address), then set the PIN at
  `http://localhost:8001`. Every phone may arrive from the same gateway address, so the per-address login lockout
  covers all phones together. Allow port 8000 on the Private firewall profile only.
- **In the cloud**: do not route ingress to 8001 or 8002; reach the PC listener through an SSH tunnel and pair
  through it.
- **Without Docker** (no `BTCFLOW_PC_PORT`): the PC's place is a loopback client of the main port
  (`http://127.0.0.1:8000`, plus `BTCFLOW_LOCAL_CLIENTS`); it pairs the same way, with `ops pair-pc` from the
  checkout.

**Cut-over (the deploy that brings this in).** After `docker compose up -d`, the PC's open tab shows "Pair this
browser": run `docker compose exec app python -m btc_signal_engine.ops pair-pc` once and open the link. External agents
move from port 8001 to `http://localhost:8002` (same token, same paths; reads now need the token too). Phones are
unchanged. See [docs/solutions/2026-10-31-pc-identity-by-pairing.md](solutions/2026-10-31-pc-identity-by-pairing.md).

### Before publishing to your home web

- Put a reverse proxy in front (Caddy or nginx) with HTTPS.
- Require a login for everything: the app's own phone PIN (below) is meant for the home LAN only and has no HTTPS.
- Keep any Kalshi key file outside the image and mount it read-only (see `compose.yaml`). Never put it in `.env` files you commit.
- Set up off-box copies of the backups: in Docker the `backup` service's `BACKUP_HOOK` (see "Postgres: backups");
  without Docker `data/backups/`, `data/btcflow-archive.db` and `data/research/polymarket_us.db`.

## Phone access on the home network (Play screen)

The phone app lives at `http://<this PC's LAN address>:8000/#play` (a tab bar: Play · Agent · Record; Play's top has the
Kalshi | Polymarket toggle and the market picker, where BTC Perps sits under Kalshi; `#perps`, `#agent` and `#record`
open those directly; "Add to Home Screen" opens straight into Play, with the Settleview icon). The install files
(`/manifest.json`, `/icon.svg` and the PNG / SVG icons under `/icons/`) are served without a sign-in: they carry no
data. This PC's browser is the PC once paired (`ops pair-pc`, "Running in Docker: who counts as the PC"); other
devices are refused until a PIN is set, then sign in with it (`adapters/http/access.py`).

1. In `.env`: `BTCFLOW_API_HOST=0.0.0.0` (optional: `BTCFLOW_ACCESS_SESSION_HOURS`, default 12), then restart.
2. On the PC: the Play screen's settings sheet (the gear) -> **Phone access** card. Type a 6-8 digit PIN twice -> "Turn on phone access".
   The card shows the phone URL (Copy), how many phones are signed in, "Sign out all phones" and "Turn phone access
   off", plus "Sign out PC browsers". The card and its routes (`/api/access*`) work only for the paired PC browser. The PIN is stored only as a salted
   PBKDF2-SHA256 hash (310k iterations) in `strategy_settings` (strategy "access"); changing or clearing it, or "Sign
   out all phones", ends every phone session, and so does every restart.
3. Allow the port on the Private network profile only (PowerShell as administrator):
   `New-NetFirewallRule -DisplayName "Settleview 8000 (Private)" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private`

Until a PIN is set, every non-local request gets 403 "Phone access is off — set a PIN on the PC (Phone access card)"
and no data. A PIN set under the old 4-digit rule no longer works: phones see "Phone access needs a new 6–8 digit PIN —
set it on the PC." and the Phone access card shows "New PIN needed" until a new PIN is set there. Five wrong PINs from
one client within 10 minutes lock that client out for 15 minutes, and 20 wrong PINs across all clients within 10
minutes lock every login for 15 minutes (both logged at WARNING, without the PIN). A client is its address plus its
device: the login page gives each browser a signed `btcflow_device` cookie (HttpOnly, SameSite=Strict, path `/login`),
so behind Docker's port publishing, where every phone arrives from the bridge gateway's one address, one phone's
wrong PINs no longer lock the others out. A client without that cookie (a script) is counted under its bare address;
`X-Forwarded-For` is never read, and uvicorn's proxy-header rewriting is off. Lockouts live in memory: a restart
or a new PIN clears them. A login body over 2 KB is refused with 413 before it is read further. There is **no HTTPS**:
the PIN and the session cookie cross the network in clear text. Use it only on the home LAN or over a VPN, and
**never port-forward port 8000**. A reverse proxy on this same computer connects from 127.0.0.1, which (without a PC
listener) is the PC's place: it still needs the PC cookie, but do not put one in front without its own login.

**Host names.** The app answers only to the addresses it knows, from this PC too: `127.0.0.1`, `localhost`, `[::1]` and
this PC's own LAN/VPN IP addresses, on its port. Any other name in the address bar gets **400** (logged at WARNING as
"refused a request for Host ..."); this stops a web page from reaching the app through a DNS name it controls (DNS
rebinding). A new DHCP address or a VPN interface brought up after the start is picked up within 30 s. **If you reach
the PC by a VPN DNS name** (for example a Tailscale MagicDNS name), add it in `.env` and restart:
`BTCFLOW_ALLOWED_HOSTS=pc.tailnet-name.ts.net` (comma-separated for several; the port is added unless you write one).

Changes (POST and the like) that a browser sends from another site are refused with 403, from this PC too, and so is
a WebSocket upgrade whose `Origin` is not one of the app's own; scripts and `curl`, which send no `Origin` or
`Sec-Fetch-Site` header, pass that check (they still need the PC cookie or a PIN session). Every response carries a
Content-Security-Policy: pages run only the app's own script files (the login keypad is allowed by its hash; the
PC-only API docs at `/api/docs` may also load Swagger UI from cdn.jsdelivr.net).

## Kalshi read-only link (optional)

Create an API key at kalshi.com. Save the private key file somewhere outside the project folder, then set:

```
BTCFLOW_KALSHI_KEY_ID=...
BTCFLOW_KALSHI_PRIVATE_KEY_PATH=C:/Users/you/.kalshi/btcflow.pem
```

The client can only send GET requests; the app reads the balance (cash and positions value) every 60 s for Play. The key is loaded into memory at start and never written to the database or sent to the browser.

## Agent fleet settings

How the fleet works: `docs/agent/multi-agent.md`.

- `BTCFLOW_AGENT_WAKE_CONCURRENCY` (`.env`): the most agent model wakes (Claude Code CLI processes) that run at once
  across every agent session; the rest wait in one queue, urgent first. Lower it if wakes slow the PC; each running
  wake is one CLI process. The same number bounds the CLI processes in the harness itself (a semaphore: a wake
  started outside the fleet's queue waits too), and each wake waits a random 0 to 0.25 s before it spawns, so wakes
  due at one window boundary do not start in the same instant.
- Each wake's CLI gets its own config directory, `<data dir>/agent-cli/config/<session>-<random>`, removed when the
  wake ends (concurrent CLI processes never share one). A leftover directory there after a crash is safe to delete.
- `BTCFLOW_AGENT_ENV_PASS` (`.env`, default empty): the agent CLI's environment is an allowlist. It keeps `PATH`,
  `PATHEXT`, `HOME`, `USERPROFILE`, `HOMEDRIVE`, `HOMEPATH`, `APPDATA`, `LOCALAPPDATA`, `TMP`, `TEMP`, `TMPDIR`,
  `LANG`, `LANGUAGE`, `LC_*`, `TZ`, `TERM`, `SHELL`, `USER`, `USERNAME`, `LOGNAME`, `SYSTEMROOT`, `SYSTEMDRIVE`,
  `WINDIR`, `COMSPEC`, `PROGRAMDATA`, `PROGRAMFILES`, `NUMBER_OF_PROCESSORS`, `PROCESSOR_ARCHITECTURE`, `OS`, plus
  the app's key as `ANTHROPIC_API_KEY` and its own `CLAUDE_*` settings; every other inherited variable (proxies,
  `NODE_OPTIONS`, CA overrides, database URLs, venue keys) is blanked for the CLI. Name extra variables to keep,
  comma-separated (for example a proxy on a host that needs one: `HTTPS_PROXY,NO_PROXY`); `ANTHROPIC_*`, `CLAUDE_*`
  and `BTCFLOW_*` names are refused at start-up.
- Global limits (set on the PC from the app, `POST /api/agent/global_limits`; stored in the database, off until set):
  `max_loss_day` (the most all agents together may lose in a local day, midnight to midnight as Record) and `api_day` (all agents' estimated API spend
  in that day). Reaching either pauses every agent with the code `global_limit`. `api_day` counts every running
  wake's reserved worst case too (a fleet-wide ledger), so wakes that start together cannot overshoot it; a wake
  that does not fit beside the others' reservations is treated as the limit reached.
- Stop waits up to 30 s for an agent's order in flight (its own or a rule's) to be answered and recorded before the
  session ends; an order still unanswered then keeps running and is recorded when it finishes (logged as a warning).
- **Wake inputs** (migration 0021, table `agent_wake_inputs`, one row per wake keyed by the wake's `agent_journal`
  id): the exact user message the model got (`<memory>` + `<events>` + `<observe>`, verbatim after the log scrubber:
  the secret patterns and the settings' exact secret values), its sha256, a `fingerprint` (system prompt sha256, the
  user layer versions, the tool specs sha256, model, effort, max_tokens, max_turns, SDK and CLI version, git sha from
  `SETTLEVIEW_GIT_SHA`, else `unknown`) and `sensitive` (what an eval export must redact or drop:
  `observe.balances`, numbers after cash / balance words in free text, ids). The wake row itself carries the
  fingerprint and `input` {sha256, chars, scrubbed, stored}, not the message. Balances are stored on purpose (they
  are part of the input); never export the table raw. About 5 to 30 KB a wake (the observe is most of it); no
  retention job yet (an evals-plan open question). Build the image with `--build-arg GIT_SHA=...` so the sha is known.


## Telegram bot picture

The bot's profile picture is the Settleview mark, `web/icons/settleview-512.png` (512 × 512 PNG, rendered from
`web/icons/tile-square.svg`). Set it once in Telegram: open @BotFather → `/setuserpic` → pick the bot → send that file.
