# The perps View paid a database round trip per event-contract env on Postgres

**Problem.** `test_computing_the_view_with_an_open_position_takes_under_5_ms` failed only on the Postgres CI job:
median 5.79 ms, max 6.58 ms. The median was already in place
(docs/solutions/2026-11-01-ci-timing-budgets.md), and a max that close to the median means a steady cost, not a stall.

**Cause.** The View reads today's committed total fresh on every call. That is deliberate: other services' stakes
count, so it is not memoised like the kill switch and limits. The total is `CommittedToday.total`, which calls each
source. The event-contract source, `TradingService.committed_by_env`, called `exposure(env, now)` once for each of
its envs (`self.gateways` always has all three mode envs: demo, shadow, prod), and each call ran its own
`trading_brackets` select. So one View made 4 selects (perp_orders plus 3 bracket reads). Profiled in the dev
container, those selects were about 97% of the View's time: 0.65 ms on SQLite (in process) and 2.7 ms on a local
Postgres 17. Each Postgres select also checks out a pooled connection and pings it, so it costs more than one round
trip. In production every event-contract service is a source, so the reads grow as 3 x services, and they run about
20 times a second while the screen is open, plus on every limits check.

**Fix.** `committed_by_env` makes one select of this strategy's brackets since the start of the user's day, merges
the watched in-memory brackets the same way `_env_brackets` does, and sums `committed()` per env. The figures are
the same as `exposure(env, now)["day"]`: tests/trading/test_user_day.py checks that it is one select and gives equal
figures across envs, strategies and the day boundary. A View is now 2 selects, about 2.1 ms on local Postgres. The
5 ms budget is unchanged, and the total is still read fresh on every View.

**How to avoid.** On the View's hot path, count selects, not just milliseconds. An in-process SQLite store hides a
per-item read loop that Postgres turns into round trips.
