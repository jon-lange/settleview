# A clean `docker compose stop` was an instant exit 143; a database outage stalled the loop

Found while testing the database-resilience and hardening work on a throwaway compose stack (2026-10-31).

**Problem 1.** `docker compose stop app` returned in 0 s with exit code 143 and no shutdown lines: no writer flush,
no agent or perps drain, nothing dead-lettered. Every deploy since the move to Docker stopped the app this way; the
60 s `stop_grace_period` was never used.

**Cause.** uvicorn's `Server.capture_signals()` installs its own SIGTERM handler while it serves, and on exit restores
the previous handler and **re-raises** the captured signal (`signal.raise_signal`). The previous handler was Python's
default, so the re-raised SIGTERM killed the process right after uvicorn returned, before the app's `finally` blocks
ran. Not caused by the init process (docker-init or tini both forward SIGTERM correctly).

**Fix.** `Runtime._handle_sigterm` installs a SIGTERM handler around the supervised run that asks the supervisor to
stop (on the loop, `call_soon_threadsafe`); uvicorn restores that handler and its re-raise lands there. Measured
after: exit 0, `SIGTERM: stopping gracefully`, `process exiting`. Test: `test_sigterm_is_a_graceful_stop_not_an_instant_kill`.

**Problem 2.** With Postgres stopped for 78 s, the writer now held every write (566) and committed them all once it was
back, but the event loop stalled ~3.8 s at a time: `settle_due` in the decision loop and the periodic Kalshi sync made
synchronous store reads on the loop thread, each waiting for a DNS / connect failure, and the raised error restarted the
decision loop.

**Fix.** `settle_due` runs in a worker thread and a failure waits for the next pass. Still on the loop (owned
elsewhere, listed in the review plan P1 "Event loop and database"): the periodic Kalshi settled sync, and
`/api/health` reads the `gaps` table, so the healthcheck fails while the database is down (fine for compose, which
never restarts an unhealthy container; a cloud liveness probe would restart the app for a database outage).

**How to avoid.** Test a stop and a dependency outage on a real stack, not only in unit tests: a unit test calling
`run()` with a fake server never sees uvicorn's signal handling, and a SQLite test database is never "down". For any
library that captures signals, check what it does on exit (restore and re-raise is common).
