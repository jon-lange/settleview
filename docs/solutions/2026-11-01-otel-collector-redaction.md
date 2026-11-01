# Redaction moved into the OpenTelemetry Collector: what the real images taught

**Problem.** The app wrapped every OpenTelemetry exporter in its own scrubber (`bootstrap/scrub.py`, a regex
framework of ours) and gave every service the backend's credential. The owner's rule: don't hand-roll what is off the
shelf. The OpenTelemetry Collector's contrib distribution has a standard `redaction` processor; the services now send
to the collector (`deploy/otel-collector/config.yaml`), which alone holds OpenObserve's ingest-only token.

**What we found** (otel/opentelemetry-collector-contrib 0.162.0, OpenObserve v1.1.1, 2026-11-01, checked with the
`debug` exporter and then `tests/telemetry/test_collector_e2e.py`):

1. The `redaction` processor (`allow_all_keys: true` + `blocked_values` + `blocked_key_patterns`) covers span,
   span-event, resource, log and metric data-point attributes, and a log's string body. It does **not** read the
   span name or the span status message: a token in `GET /bot<token>/getMe` survives it. The `transform` processor's
   `replace_pattern` covers those two (and every query value of the URL attributes).
2. `blocked_values` masks only the matching part, with `****`; it runs before `transform` in the pipeline, so a
   `token=...` pair is masked whole by redaction and `transform` never sees its key. Tests should not assert the
   exact shape of a value both touch.
3. A `$` in a collector configuration is environment expansion: a regex replacement group is `$${1}`.
4. Key patterns match substrings: `.*token.*` would mask `gen_ai.token.type` and break the token metrics. Name the
   credential keys (`access_token`, `bot_token`, `id_token`, ...).
5. The image is distroless (no shell, no wget/curl). A compose healthcheck can only run the binary itself:
   `otelcol-contrib validate --config=...` (0.4 s, under 48 MB). The `health_check` extension's `:13133` is for
   platforms that probe over HTTP.
6. `basicauth`'s `password_file` is watched for changes, but a compose secret is a bind mount of the file's inode at
   container start: `ingest_token.py` replaces the file atomically (a new inode), so the running collector keeps the
   old one. Recreate the collector after writing the token.
7. OpenObserve's alert destinations have an SSRF guard: a private, loopback or unresolvable URL is refused (HTTP 400).
   A test that applies the alerts keeps Telegram's public URL with a fake token and applies every alert disabled, so
   nothing is ever sent.
8. OpenObserve stores a span's status message as `status_message` and its events as a JSON string in `events`;
   resource attributes in metrics rows lose their prefix (`deployment_environment_name`), in trace rows they gain
   `service_` (`service_deployment_environment_name`). `service_name` is the same everywhere, so the alerts' SQL is
   unchanged.

**What stayed in the process, and why.** stderr and Docker's logs never pass through the collector. Standard
practice keeps secrets out of logs at the source (no credential is logged, `hide_parameters` on every engine, httpx
at WARNING); the one defence in depth left is a log-record factory that masks the exact values of the app's own
configured secrets (`bootstrap/logs.py`, `mask_secrets`). It has no patterns, so it is not a second redaction
framework: only the values the process itself holds.

**How to avoid repeating it.** Change `deploy/otel-collector/config.yaml`, then run the end-to-end test (steps in
`tests/telemetry/collector_e2e.compose.yaml`); it fails on any planted value that reaches OpenObserve.
