"""Order-book features from the per-second book aggregates. Not backfillable.

Sources: order-flow imbalance from Cont, Kukanov & Stoikov (2014), "The price impact of order book events",
J. Financial Econometrics 12(1), section 2.1 (e_n, OFI_k) and eq. 3 (impact inversely proportional to average
depth, hence the depth normalization). Microprice is the first-order "weighted mid" of Stoikov (2018),
"The micro-price", Quantitative Finance 18(12); the calibrated adjustment g(I, S) is not modelled.
"""

from __future__ import annotations

from collections.abc import Sequence

from btc_signal_engine.domain.features import OFI_WINDOWS
from btc_signal_engine.domain.models.events import BookSecond

BOOK_FEATURES = ("spread", "book_imbalance_10", "depth_bid_10", "depth_ask_10", "microprice_bps")
OFI_LOOKBACK_S = max(OFI_WINDOWS) + 1
"""Book seconds ofi_features reads: the widest window plus the second before it (the first change's reference)."""


def book_features(book: BookSecond | None) -> dict[str, float | None]:
    if book is None:
        return dict.fromkeys(BOOK_FEATURES)
    return {
        "spread": float(book.spread),
        "book_imbalance_10": float(book.imbalance_10),
        "depth_bid_10": float(book.bid_depth_10),
        "depth_ask_10": float(book.ask_depth_10),
        "microprice_bps": microprice_bps(book),
    }


def microprice_bps(book: BookSecond) -> float | None:
    """(weighted mid - mid) / mid in basis points; the weight on the ask is the bid's share of best-level size."""
    if book.bid_size is None or book.ask_size is None or book.bid_size + book.ask_size <= 0:
        return None
    imb = book.bid_size / (book.bid_size + book.ask_size)
    micro = imb * book.ask + (1 - imb) * book.bid
    mid = book.mid
    return float((micro - mid) / mid * 1e4)


def _sized(b: BookSecond | None) -> bool:
    return b is not None and b.bid_size is not None and b.ask_size is not None


def ofi_event(prev: BookSecond, cur: BookSecond) -> float:
    """CKS e_n between two consecutive best-level states (BTC)."""
    e = 0.0
    if cur.bid >= prev.bid:
        e += cur.bid_size
    if cur.bid <= prev.bid:
        e -= prev.bid_size
    if cur.ask <= prev.ask:
        e -= cur.ask_size
    if cur.ask >= prev.ask:
        e += prev.ask_size
    return e


def ofi_features(books: Sequence[BookSecond | None]) -> dict[str, float | None]:
    """`books[i]` is the book second at T - OFI_LOOKBACK_S + i (None if missing); the last is the second before T.

    Each second s with sized books at s and s-1 contributes e_s; ofi_W sums e_s over the last W seconds and
    divides by the mean best-level depth (bid_size + ask_size)/2 of those seconds. A window where fewer than
    half its seconds have such a pair is unavailable.
    """
    n = len(books)
    events: list[float | None] = [None] * n
    depth: list[float] = [0.0] * n
    for i in range(1, n):
        cur, prev = books[i], books[i - 1]
        if _sized(cur) and _sized(prev):
            events[i] = ofi_event(prev, cur)
            depth[i] = (cur.bid_size + cur.ask_size) / 2
    out: dict[str, float | None] = {}
    for w in OFI_WINDOWS:
        total = depth_sum = 0.0
        pairs = 0
        for i in range(n - w, n):
            if events[i] is not None:
                total += events[i]
                depth_sum += depth[i]
                pairs += 1
        ok = pairs >= (w + 1) // 2 and depth_sum > 0
        out[f"ofi_{w}"] = float(total / (depth_sum / pairs)) if ok else None
    return out
