# Polymarket US recorder died silently for 31 hours

**Problem.** `scripts/record_polymarket_us_ws.py` stopped writing `pm_ws_quotes` at 2026-10-30 00:37Z and nobody
noticed until 2026-10-31 12:55Z, when the 5m research plan needed the data. Only 136 5m windows had real asks
instead of ~500; every 5m and nested-window test date slipped by two days.

**Cause.** The recorders run as ad-hoc background processes, outside the app's watchdog; a reboot or a session end
takes them down and nothing checks row counts.

**Fix.** Restarted both recorders 2026-10-31 (quotes and settlement). Plan U0
(docs/plans/2026-10-31-0800-research-pm-5m-plan.md): run them under a watchdog and report daily coverage
(windows recorded of 288, longest gap) in the Telegram summary.

**Since then** (docs/plans/2026-10-31-0830-ops-portable-runtime-plan.md P1-P4): the recorders run as their own
compose services or under the portable supervisor (restarted on exit), and the app sends a Telegram `data` alert after
2 minutes of silence plus a daily coverage line (docs/operations.md, "Where it runs").

**And then** (same day, 13:59 CDT): the recorder was running and connected but recorded nothing for 45 minutes,
because the venue sent trades and no book; see docs/solutions/2026-10-31-pm-quiet-book-vs-dead-book-feed.md.

**How to avoid.** Before any test that depends on recorded data, check the newest `recv_ms` first; a recorder that is
not supervised should be assumed dead.
