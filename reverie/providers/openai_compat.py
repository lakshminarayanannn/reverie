"""Any OpenAI-compatible ``/chat/completions`` endpoint (including local Ollama).

Uses only the standard library. The key (optional for local servers) is read from the
environment variable named in config, never from the config file itself.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from reverie.providers.base import Completion, ProviderError, ProviderUnavailable, RetryableError

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(self, *, base_url: str | None, api_key_env: str = "OPENAI_API_KEY",
                 timeout_s: float = 300.0) -> None:
        if not base_url:
            raise ProviderUnavailable("provider 'openai_compat' needs [llm] base_url, e.g. "
                                      "http://localhost:11434/v1 for Ollama")
        self.base_url = base_url.rstrip("/")
        self.api_key = os.environ.get(api_key_env)
        self.timeout_s = timeout_s
        host = urlparse(self.base_url).hostname or ""
        self.is_cloud = host not in LOCAL_HOSTS

    def complete(self, *, model: str, system: str, messages: list[dict[str, Any]],
                 max_tokens: int, temperature: float = 0.0) -> Completion:
        body = {"model": model, "max_tokens": max_tokens, "temperature": temperature,
                "messages": [{"role": "system", "content": system}, *messages]}
        req = urllib.request.Request(
            self.base_url + "/chat/completions", data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or exc.code >= 500:
                raise RetryableError(f"HTTP {exc.code}") from exc
            raise ProviderError(f"HTTP {exc.code} from {self.base_url}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise RetryableError(f"connection error: {exc}") from exc
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("unexpected response shape from OpenAI-compatible endpoint") from exc
        usage = data.get("usage") or {}
        return Completion(text=text, input_tokens=int(usage.get("prompt_tokens") or 0),
                          output_tokens=int(usage.get("completion_tokens") or 0), model=data.get("model") or model)
