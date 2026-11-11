"""Anthropic Messages API provider (official ``anthropic`` SDK).

The API key is read only from ``ANTHROPIC_API_KEY`` and is never logged. SDK-level
retries are disabled (``max_retries=0``) so reverie's own backoff and budget accounting
see every attempt. Sampling parameters are not sent: current Claude models reject
``temperature`` (the interface keeps the argument for other providers).
"""

from __future__ import annotations

import os
from typing import Any

from reverie.providers.base import Completion, ProviderError, ProviderUnavailable, RetryableError


class AnthropicProvider:
    name = "anthropic"
    is_cloud = True

    def __init__(self, *, effort: str | None = None, timeout_s: float = 300.0) -> None:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ProviderUnavailable(
                "provider 'anthropic' needs ANTHROPIC_API_KEY in the environment (it is never read from "
                "config files)")
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ProviderUnavailable("the 'anthropic' package is not installed") from exc
        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=key, max_retries=0, timeout=timeout_s)
        self.effort = effort

    def complete(self, *, model: str, system: str, messages: list[dict[str, Any]],
                 max_tokens: int, temperature: float = 0.0) -> Completion:
        anthropic = self._anthropic
        kwargs: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "system": system,
                                  "messages": messages}
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        try:
            resp = self._client.messages.create(**kwargs)
        except anthropic.RateLimitError as exc:
            retry_after = _retry_after(exc)
            raise RetryableError("rate limited (429)", retry_after) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500 or exc.status_code in (408, 409):
                raise RetryableError(f"server error {exc.status_code}") from exc
            raise ProviderError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise RetryableError(f"connection error: {type(exc).__name__}") from exc
        if resp.stop_reason == "refusal":
            raise ProviderError("model declined the request (stop_reason=refusal)")
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        return Completion(text=text, input_tokens=resp.usage.input_tokens,
                          output_tokens=resp.usage.output_tokens, model=resp.model or model)


def _retry_after(exc: Any) -> float | None:
    try:
        value = exc.response.headers.get("retry-after")
        return float(value) if value is not None else None
    except (AttributeError, TypeError, ValueError):
        return None
