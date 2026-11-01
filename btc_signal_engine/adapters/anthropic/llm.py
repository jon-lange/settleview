"""AnthropicLlm: the launch-time key check (`ports.ApiKeyCheck`, R16) on the official `anthropic` SDK (KTD10).

The agent's model loop runs on the Claude Agent SDK (adapters/agent_sdk); this client only proves the key works with
one cheap `models.list` call, which spends no model tokens.
- One `AsyncAnthropic` client, built once from the key passed in (the app's `SecretStr` setting); an empty key is an
  error, never a fallback to the environment, and so is the base URL (always api.anthropic.com, never an inherited
  ANTHROPIC_BASE_URL). 60 s timeout (5 s to connect) and the SDK's two retries.
- Errors: 401 -> LlmInvalidKey; 400 / 404 -> LlmBadRequest with the API's message; anything else the SDK gives up on
  (429, 529, 5xx, timeouts, connection, other statuses) -> LlmUnavailable. The key never appears in a message.
"""

from __future__ import annotations

from typing import Any

import anthropic

from btc_signal_engine.application.ports import LlmBadRequest, LlmInvalidKey, LlmUnavailable

TIMEOUT = anthropic.Timeout(60.0, connect=5.0)
MAX_RETRIES = 2
# Always the real API: an ANTHROPIC_BASE_URL inherited from the environment is never honoured.
BASE_URL = "https://api.anthropic.com"


def _why(e: anthropic.APIError) -> str:
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
    return str(getattr(e, "message", None) or e.__class__.__name__)


def _mapped(e: anthropic.APIError) -> Exception:
    if isinstance(e, anthropic.AuthenticationError):
        return LlmInvalidKey(f"The Anthropic API key was rejected: {_why(e)}")
    if isinstance(e, (anthropic.BadRequestError, anthropic.NotFoundError)):
        return LlmBadRequest(f"The model rejected the request: {_why(e)}")
    if isinstance(e, anthropic.APITimeoutError):
        return LlmUnavailable("The model did not answer in time (after retries).")
    if isinstance(e, anthropic.APIConnectionError):
        return LlmUnavailable("Could not reach the Anthropic API (after retries).")
    status = getattr(e, "status_code", None)
    return LlmUnavailable(f"The Anthropic API answered {status or 'an error'}: {_why(e)}")


class AnthropicLlm:
    def __init__(self, api_key: str, *, client: Any = None) -> None:
        """`client` replaces the SDK client (tests); otherwise one AsyncAnthropic is built from `api_key`."""
        if client is None:
            if not isinstance(api_key, str) or not api_key.strip():
                raise ValueError("an Anthropic API key is required")
            client = anthropic.AsyncAnthropic(api_key=api_key, base_url=BASE_URL, timeout=TIMEOUT,
                                              max_retries=MAX_RETRIES)
        self._client = client

    async def check_key(self) -> None:
        try:
            await self._client.models.list(limit=1)
        except anthropic.APIError as e:
            raise _mapped(e) from None
