"""On-disk response cache keyed by a hash of (provider, model, system, messages, params)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from reverie.core.fsutil import atomic_write_text
from reverie.providers.base import Completion, Provider


def cache_key(provider: str, model: str, system: str, messages: list[dict[str, Any]],
              max_tokens: int, temperature: float) -> str:
    payload = json.dumps(
        {"provider": provider, "model": model, "system": system, "messages": messages,
         "max_tokens": max_tokens, "temperature": temperature},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class CachedProvider:
    """Wraps a provider; cached hits come back with ``cached=True`` and cost nothing."""

    def __init__(self, inner: Provider, directory: Path) -> None:
        self.inner = inner
        self.directory = Path(directory)
        self.name = inner.name
        self.is_cloud = inner.is_cloud
        self.hits = 0
        self.misses = 0

    def _path(self, key: str) -> Path:
        return self.directory / key[:2] / f"{key}.json"

    def complete(self, *, model: str, system: str, messages: list[dict[str, Any]],
                 max_tokens: int, temperature: float = 0.0) -> Completion:
        key = cache_key(self.inner.name, model, system, messages, max_tokens, temperature)
        path = self._path(key)
        if path.is_file():
            try:
                comp = Completion.model_validate_json(path.read_text(encoding="utf-8"))
                self.hits += 1
                return comp.model_copy(update={"cached": True})
            except ValueError:
                path.unlink(missing_ok=True)
        comp = self.inner.complete(model=model, system=system, messages=messages,
                                   max_tokens=max_tokens, temperature=temperature)
        self.misses += 1
        atomic_write_text(path, comp.model_dump_json())
        return comp
