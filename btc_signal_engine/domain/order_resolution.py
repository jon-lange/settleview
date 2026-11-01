"""One rule for an order whose answer was lost (Kalshi event contracts, Polymarket US, Kalshi perps).

The rule (review fix plan 2026-10-31, P0 #2; docs/solutions/2026-10-31-lost-order-answers.md):

    An order whose answer was lost stays UNKNOWN until fills or activities prove its outcome. A list miss is never
    proof of no fill. On a late fill the saved protection is placed, or the position is marked unprotected and the
    user is alerted. Committed risk (the approved maximum) is held until the outcome is confirmed.

How the venues prove it (the order lists lag placements and the positions lists lag our own fills, by seconds):

* FILLED: the order itself is found (by client_order_id in the order lists, or by its id from a fill / activity of
  ours since it was sent), or, where the venue has no ids on its trades (Polymarket US activities), trades of the
  account in that market since the send that no other order of ours explains, at prices the order could have filled
  at. The booked price is the order's real average fill price (fees from its fills, else the venue's fee formula).
* NOT FILLED: only when BOTH (1) the venue's fills / activities since the send show nothing that could be this order
  and (2) the positions list holds no more on that side than our own records explain. Then it never reached the
  venue, or reached it and filled nothing (an IOC that found no offer).
* Anything else (a read failed, or the position shows contracts no record of ours explains): still UNKNOWN. It keeps
  its approved maximum committed, blocks new orders on that market as before, is checked again every
  PROOF_EVERY_MS, and is surfaced to the user as an unresolved order.

These helpers are pure: the adapters and services do the reads.
"""

from __future__ import annotations

from collections.abc import Iterable

FIND_GIVE_UP_MS = 60_000  # after this a list miss stops being "maybe in flight": the fills / positions are checked
PROOF_EVERY_MS = 10_000  # how often that check runs again while the order stays unknown
TS_SLACK_MS = 1_000  # venue fill times are second-granular (Kalshi min_ts) and clocks differ: look a little earlier
EPS = 1e-9

FILLED, NOT_FILLED, UNKNOWN = "filled", "not_filled", "unknown"

# A 4xx that certainly means "this request had no effect" (the venue validated and refused it). Any other 4xx (408
# request timeout, 409 handled separately, 425, 499 client closed, ...) may have been acted on: the outcome is unknown.
DEFINITIVE_4XX = frozenset({400, 401, 403, 404, 422})


def definitive_status(status: int | None) -> bool:
    """True when an HTTP answer to a write proves the write did not happen."""
    return status in DEFINITIVE_4XX


def candidate_orders(fills: Iterable[tuple[str | None, int | None, str | None]], *, since_ms: int, book_side: str,
                     known: Iterable[str] = ()) -> list[str]:
    """Order ids (oldest first, each once) of fills `(order_id, ts_ms, book_side)` at or after `since_ms` (less
    TS_SLACK_MS) on `book_side` ("bid" bought YES / opened long; "ask" sold YES / opened short) whose order is not in
    `known`. Each candidate is then read by id and accepted only if its client_order_id is the lost order's."""
    skip = set(known)
    out: list[str] = []
    for order_id, ts, side in sorted(fills, key=lambda f: f[1] or 0):
        if not order_id or order_id in skip or order_id in out or side != book_side:
            continue
        if ts is not None and ts < since_ms - TS_SLACK_MS:
            continue
        out.append(order_id)
    return out


def excess_on_side(on_exchange: float | None, ours: float) -> float | None:
    """Contracts the venue shows on our side beyond what our own records hold (None when unreadable). Positive means
    a fill we cannot account for: possibly the lost order's."""
    if on_exchange is None:
        return None
    return round(max(float(on_exchange), 0.0) - max(float(ours), 0.0), 8)


def decide(evidence: dict | None, excess: float | None) -> str:
    """FILLED, NOT_FILLED or UNKNOWN from the venue's fills/activities evidence ({filled, ...} or None when it could
    not be read) and the position excess (excess_on_side; None when unreadable)."""
    if evidence is None:
        return UNKNOWN
    if float(evidence.get("filled") or 0.0) > EPS:
        return FILLED
    if excess is None or excess > EPS:
        return UNKNOWN
    return NOT_FILLED
