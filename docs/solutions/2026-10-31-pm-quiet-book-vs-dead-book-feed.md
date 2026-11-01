# Polymarket: a quiet book is not a stale book, and a live socket is not a live book

Two lessons from one Saturday (2026-10-31, Polymarket US BTC Up/Down 5m / 15m / 1h).

## 1. Quiet books read as stale; the trading feed flapped

**Problem.** The Polymarket trading feed went `stream_down` / `stream_up` about 600 times in 4 hours (dozens per 2
minutes after the Postgres cut-over). Every flip wrote a `trading_audit` row and a log line and forced REST polling,
and `/api/health` stayed `degraded` (docs/plans/2026-10-31-1400-infra-observability-evaluation.md, A11).

**Cause.** The markets WebSocket sends a book / top message only when the book changes. On a quiet market, 2 to 18 s
between messages is normal. The cache called anything older than `MAX_AGE_MS` (2 s) stale. That limit was set on
2026-10-29, from a weekday session that streamed about 10 messages a second. The feed's health used the same 2 s rule
with no hysteresis.

**Fix** (`adapters/polymarket_us/market_data.py`, "Servable"; docs/operations.md, "Polymarket book freshness"). A book
or top within 2 s is still servable as before. An older one is servable on the quiet path when:
- its connection has sent anything (trades and heartbeats count) within 15 s;
- the market's TRADE subscription has covered it since the book arrived;
- no print has come after the book;
- the book is at most 30 s old.

The trading feed (`adapters/polymarket_us/stream.py`) goes down at once on a dead connection or a lost subscription,
and only after 10 s of an unservable book otherwise. It comes back after 5 s with no problem. Play and the agents take
the cache's verdict (`servable`) instead of their own 5 s age limit, so every path agrees on one rule.

## 2. Trades kept coming, books stopped: the 13:59 outage

**Problem.** From 13:59:56 to about 14:45 CDT Polymarket sent no book messages at all, neither MARKET_DATA nor
MARKET_DATA_LITE (checked with fresh connections), while TRADE messages kept arriving (333 trades in 5 minutes). The
quotes recorder (`rec-pm-quotes`) kept its connection and its subscriptions and recorded nothing for about 45
minutes. Its compose healthcheck went unhealthy, but plain Docker does not restart an unhealthy container.

**Cause.** Both the cache and the recorder took "the connection is alive" to mean "the book is current". A live
socket that carries trades proves the opposite here: the market traded and the book did not move.

**Fix.**
- Cache: a print after the last book update refuses the quiet path at once. The 2 s rule still covers the first
  2 s, exactly as before. If no book update follows within 2 s, the reason reads "book feed broken". The 13:59
  pattern is refused within 2 s of the last book and stays refused until books return. The feed goes down once and
  comes back up once (`tests/polymarket_us/test_book_freshness.py`).
- Recorder (`adapters/polymarket_us/recorders.py`):
  - trades with no quote for 60 s on a lane: reconnect, with a WARNING;
  - three such reconnects in a row: a venue outage, said once, then a doubling wait up to 120 s, trades still
    recorded;
  - no quote at all for 10 minutes without that evidence: exit 75, so the restart policy acts.
- App: the `data` alert compares the newest recorded print with the newest quote and says "Polymarket is sending
  trades but no quotes".

## How to avoid

- Do not judge a feed that sends on change by the age of its last message alone. Ask whether anything could have
  changed it: here, a print.
- Liveness ("a socket is open") and currency ("the data is current") are two checks. Test both, and test the case
  where one holds and the other does not.
- A healthcheck that nobody acts on is only a report. If the process can tell it is wedged, it should exit
  non-zero itself.
- Limits that came from one session's traffic (2 s on a weekday) need a check on quiet days before they gate entries.
