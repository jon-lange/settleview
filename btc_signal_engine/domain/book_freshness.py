"""When a cached order book may still be served: one rule for every venue's book (pure; no clock, no I/O).

Why (docs/solutions/2026-10-31-pm-quiet-book-vs-dead-book-feed.md): a venue sends a book update only when the book
changes, so "the last book message is at most 2 s old" alone calls a valid QUIET book stale; and on 2026-10-31 13:59
Polymarket stopped every book message while trades kept coming on the same live connections, so "the connection
is alive" (or "some message arrived": a ticker, a heartbeat) alone does not mean the book is current. The same
failure was possible on Kalshi perps, whose ticker channel sends a message every second even when nothing changes
(review fix plan 2026-10-31, B3#8). So freshness is judged by BOOK updates, and a quiet book is served only while the
trade feed vouches that nothing traded after it.

`unservable_reason` answers None when the book record is servable, else why not, in plain English:
  (a) FRESH: the record arrived within `max_age_ms` (2 s); or
  (b) QUIET, all of: the connection is alive (any message on it within `conn_live_ms`, 15 s); no print arrived after
      the record (a print moves or consumes the book, so a book older than a print is outdated; a print without a
      following book update within `grace_ms`, 2 s, is a broken book feed); the trade subscription has covered the
      market since before the record arrived (so a print after it could not have been missed); and the record is at
      most `quiet_max_ms` (30 s) old.
Message order is compared by a per-connection message number, never by timestamps ("a print after this book" is
exact). The caller checks first that the record's connection is up and still subscribed.
"""

from __future__ import annotations

MAX_AGE_MS = 2_000  # a book this recent is servable outright
QUIET_MAX_MS = 30_000  # a quiet book is never served older than this
CONN_LIVE_MS = 15_000  # a connection with no message at all (trade, book, ticker, heartbeat) for this long is not alive
TRADE_GRACE_MS = 2_000  # a print not followed by a book update within this means the book feed is broken


def unservable_reason(*, now_ms: int, book_ms: int, book_seq: int, conn_last_ms: int | None,
                      last_trade_seq: int | None, unbooked_ms: int | None, covered_since_ms: int | None,
                      what: str = "book", conn: str = "the connection", sub_on: str | None = None,
                      max_age_ms: int = MAX_AGE_MS,
                      quiet_max_ms: int = QUIET_MAX_MS, conn_live_ms: int = CONN_LIVE_MS,
                      grace_ms: int = TRADE_GRACE_MS) -> str | None:
    """None when the cached book (`what`, received at local `book_ms` as message number `book_seq`) may be served at
    `now_ms`, else why not (the module docstring).

    `conn_last_ms`: the newest message of any kind on its connection. `last_trade_seq`: the message number of the
    market's newest print (None: none seen). `unbooked_ms`: when the first print since the market's last book update
    arrived (None: a book update followed every print). `covered_since_ms`: every print for the market since this
    local time was received (None: the trade subscription is not confirmed on this connection). `conn` / `sub_on`
    name the connection in the reasons (`sub_on` defaults to `conn`)."""
    age = now_ms - book_ms
    if age <= max_age_ms:
        return None  # (a) fresh
    silent = None if conn_last_ms is None else now_ms - conn_last_ms
    if silent is None or silent > conn_live_ms:
        return (f"{conn} silent for {silent / 1000:.1f} s (alive means a message within {conn_live_ms // 1000} s)"
                if silent is not None else f"{conn} has sent nothing")
    if last_trade_seq is not None and last_trade_seq > book_seq:
        if unbooked_ms is None:  # a newer book update followed the print; this older record did not
            return f"a trade arrived after this {what}, which no update of its own followed"
        since = now_ms - unbooked_ms
        if since > grace_ms:
            return (f"book feed broken: trades for {since / 1000:.1f} s without a book update (grace "
                    f"{grace_ms // 1000} s)")
        return "a trade arrived after the last book update; waiting for its book update"
    if covered_since_ms is None or covered_since_ms > book_ms:
        return (f"its trade subscription on {sub_on or conn} has not covered the time since this {what} arrived (a quiet "
                f"book is served only while a print would show it is outdated)")
    if age > quiet_max_ms:
        return f"the {what} is {age / 1000:.1f} s old (quiet limit {quiet_max_ms // 1000} s)"
    return None  # (b) quiet but current
