"""SDK client construction. Keys come only from settings (`SecretStr`), are handed straight to the SDK and are never
logged, returned or put in an error. The public client (no keys) is used for public market reads (least privilege)."""

from __future__ import annotations

from typing import Any

from polymarket_us import AsyncPolymarketUS

TIMEOUT_S = 10.0  # an order answer slower than this is treated as unknown and reconciled (never resent)


def sdk_client(key_id: str | None, secret: Any | None, *, timeout: float = TIMEOUT_S) -> AsyncPolymarketUS:
    """Authenticated client when both key parts are set (secret: a pydantic SecretStr or str), else public."""
    raw = secret.get_secret_value() if hasattr(secret, "get_secret_value") else secret
    if key_id and raw:
        return AsyncPolymarketUS(key_id=key_id, secret_key=raw, timeout=timeout)
    return AsyncPolymarketUS(timeout=timeout)
