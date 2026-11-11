"""Deterministic mock provider for tests and demos.

Responses come from (in order): an exact prompt-hash table, then a rule function.
Every call is recorded (``self.calls``) so tests can assert what was sent, for example
that no secret ever reaches a prompt. A mock is never presented as real learning: runs
using it are labeled ``provider: mock`` in PROPOSAL.md and metrics.json.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from reverie.providers.base import Completion, ProviderError

Rule = Callable[[str, str, list[dict[str, Any]]], str | None]


def prompt_hash(system: str, messages: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps({"system": system, "messages": messages}, sort_keys=True).encode()).hexdigest()


class MockProvider:
    name = "mock"
    is_cloud = False

    def __init__(self, rule: Rule | None = None, table: dict[str, str] | None = None) -> None:
        self.rule = rule
        self.table = dict(table or {})
        self.calls: list[dict[str, Any]] = []

    @classmethod
    def from_file(cls, path: Path) -> MockProvider:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        rule: Rule | None = None
        if "script" in data:
            from reverie.providers.scripted import ScriptedOracle

            rule = ScriptedOracle(data["script"])
        return cls(rule=rule, table=data.get("by_hash", {}))

    def complete(self, *, model: str, system: str, messages: list[dict[str, Any]],
                 max_tokens: int, temperature: float = 0.0) -> Completion:
        self.calls.append({"model": model, "system": system, "messages": messages})
        key = prompt_hash(system, messages)
        text: str | None = self.table.get(key)
        if text is None and self.rule is not None:
            text = self.rule(model, system, messages)
        if text is None:
            raise ProviderError(f"mock provider has no response for prompt {key[:12]}")
        in_tokens = (len(system) + sum(len(str(m.get("content", ""))) for m in messages)) // 4
        return Completion(text=text, input_tokens=in_tokens, output_tokens=len(text) // 4, model="mock")

    def all_prompt_text(self) -> str:
        parts: list[str] = []
        for call in self.calls:
            parts.append(call["system"])
            for msg in call["messages"]:
                parts.append(str(msg.get("content", "")))
        return "\n".join(parts)
