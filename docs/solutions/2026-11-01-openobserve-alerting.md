# OpenObserve v1.1.1 alerting: what the API actually does

Context: P3 item 2 (SLO alerts to Telegram), built on OpenObserve's own alerts, destinations and templates
(docs/operations.md, "Alerting"). Checked against a throwaway `openobserve/openobserve:v1.1.1` container with data
from the real OpenTelemetry Python SDK, 2026-11-01.

## What we learned

- **Streams and columns.** A metric's stream is its OTel name with dots as underscores (`settleview.feed.stale` ->
  `settleview_feed_stale`); attributes become columns the same way (`settleview.component` -> `settleview_component`);
  the point is `value`. A counter's points are cumulative and carry `start_time` (the process start, nanoseconds as
  text); a gauge's `start_time` is `"0"`. OTel logs land in the logs stream `default`, severity as `severity`
  (`ERROR`) and `severity_number` (17), not `severity_text`.
- **An alert on a stream that does not exist yet is refused** (`404 Stream ... not found`). A metric appears only
  once a service exported it (perps off: never). `alerts.py apply` reports those `pending` and exits 2; run it again.
- **Firing, repeats, recovery.** A scheduled SQL alert fires when the query returns at least one row. With
  `silence: 0` it notifies on every evaluation; `silence: N` suppresses repeats for N minutes. `notify_on_recovery`
  sends one notification with `{alert_status}` = `resolved` when it stops firing, to `recovery_destinations` when set.
- **Template variables that render**: `{alert_name}`, `{alert_status}`, `{alert_description}`, `{alert_start_time}`
  / `{alert_end_time}` (the query window, UTC), `{alert_trigger_time_str}`, `{alert_count}`, `{alert_agg_value}`,
  `{rows}` (each row rendered with the alert's `row_template`, joined by newlines, JSON-escaped inside a JSON body),
  and every key of the alert's `context_attributes` (`{runbook}`, `{slo}`, `{severity}`). `{alert_runbook_url}` does
  not render although `runbook_url` is a field: we keep the link in `context_attributes`.
- **Manual trigger** (`PATCH /api/v2/{org}/alerts/{id}/trigger`) sends a notification even for a disabled alert, with
  the row placeholders left unfilled: that is the test alert (a disabled alert with no rows in its template).
- **SSRF guard.** An HTTP destination to a private address (another container) is refused unless the host is in
  `ZO_SSRF_ALLOWED_HOSTS` (or `ZO_SSRF_ALLOWED_CIDRS`). api.telegram.org is public, so the live setup needs nothing;
  the throwaway test allowed only its fake Telegram's container name.
- **Root password policy.** v1.1.1 refuses to start with a weak `ZO_ROOT_USER_PASSWORD` (8+ characters with a lower,
  an upper, a digit and a special character), panicking with "backend job init failed: channel closed".
- **Export format** is the alert object minus the server's fields (`id`, `org_id`, `owner`, `last_*`, ...); the same
  object posts back to `POST /api/v2/{org}/alerts`. That is the repo's format: no schema of our own.

## Counters over a window

`max(value) - min(value)` per series misses the first increments of a process that started inside the window: its
first point already holds everything since the start. The SQL in `settleview_writer_dead_letters.json` (and the
other counter alerts) groups by `__hash__, start_time` and takes the baseline as 0 when the series' `start_time` is
within 90 s (one export interval plus slack) of its first point in the window, else the first point. Verified: a
process that dead-lettered 2 writes before its first export is counted (2); an older series with no new increment in
the window is not (0).

## Gotchas while testing on Windows

- Git Bash rewrites `-e ZO_DATA_DIR=/data` into a Windows path: `MSYS_NO_PATHCONV=1 docker run ...`.
- Python's text mode on Windows writes CRLF: edit repo files with `newline=""` / `newline="\n"`.
