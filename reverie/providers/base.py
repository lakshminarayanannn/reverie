"""Provider interface, structured-output helper, and the metered client used by stages."""

from __future__ import annotations

import json
import logging
import random
import re
import time
from collections.abc import Callable
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ValidationError

from reverie.core.budget import Budget

log = logging.getLogger("reverie.providers")


class Completion(BaseModel):
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str
    #: Provider-reported cost, when the provider knows it (claude_cli does).
    usd: float | None = None
    cached: bool = False


class ProviderError(RuntimeError):
    """A provider call failed permanently."""


class RetryableError(ProviderError):
    """Transient failure (rate limit, 5xx, network): safe to retry."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ProviderUnavailable(ProviderError):
    """No model is configured, or the configured one cannot be used here."""


@runtime_checkable
class Provider(Protocol):
    name: str
    #: True when calls leave this machine.
    is_cloud: bool

    def complete(self, *, model: str, system: str, messages: list[dict[str, Any]],
                 max_tokens: int, temperature: float = 0.0) -> Completion: ...


def with_retries(fn: Callable[[], Completion], *, max_retries: int = 4, base_delay: float = 1.0,
                 sleep: Callable[[float], None] = time.sleep) -> Completion:
    """Call ``fn`` retrying ``RetryableError`` with exponential backoff and jitter."""
    attempt = 0
    while True:
        try:
            return fn()
        except RetryableError as exc:
            if attempt >= max_retries:
                raise ProviderError(f"giving up after {attempt + 1} attempts: {exc}") from exc
            delay = exc.retry_after if exc.retry_after is not None else base_delay * (2 ** attempt)
            delay = min(delay, 60.0) * (1 + random.random() * 0.1)
            log.warning("retryable provider error (%s); retrying in %.1fs", exc, delay)
            sleep(delay)
            attempt += 1


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)

T = TypeVar("T", bound=BaseModel)


def extract_json(text: str) -> Any:
    """Parse the first JSON object in ``text`` (tolerates code fences and prose)."""
    candidates = [m.group(1) for m in _FENCE_RE.finditer(text)] + [text]
    for cand in candidates:
        cand = cand.strip()
        start = cand.find("{")
        if start < 0:
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(cand)):
            ch = cand[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(cand[start:i + 1])
                    except json.JSONDecodeError:
                        break
    raise ValueError("no JSON object found in model output")


class LLM:
    """What stages use: provider + budget + per-stage accounting + structured outputs.

    ``complete_json`` asks for JSON, validates with pydantic, and retries once with the
    validation error on failure.
    """

    def __init__(self, provider: Provider, budget: Budget, max_tokens: int = 8000,
                 max_retries: int = 4, sleep: Callable[[float], None] = time.sleep) -> None:
        self.provider = provider
        self.budget = budget
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self._sleep = sleep
        self.errors: list[str] = []

    def complete(self, *, stage: str, model: str, system: str, messages: list[dict[str, Any]],
                 max_tokens: int | None = None) -> Completion:
        self.budget.check()
        comp = with_retries(
            lambda: self.provider.complete(model=model, system=system, messages=messages,
                                           max_tokens=max_tokens or self.max_tokens),
            max_retries=self.max_retries, sleep=self._sleep,
        )
        self.budget.record(model=comp.model or model, input_tokens=comp.input_tokens,
                           output_tokens=comp.output_tokens, stage=stage, cached=comp.cached, usd=comp.usd)
        return comp

    def complete_json(self, *, stage: str, model: str, system: str, user: str, schema: type[T],
                      max_tokens: int | None = None) -> T:
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        comp = self.complete(stage=stage, model=model, system=system, messages=messages, max_tokens=max_tokens)
        try:
            return schema.model_validate(extract_json(comp.text))
        except (ValueError, ValidationError) as exc:
            err = str(exc)[:1500]
            log.info("stage %s: invalid JSON from model, retrying once: %s", stage, err)
            messages = messages + [
                {"role": "assistant", "content": comp.text},
                {"role": "user", "content": "Your reply did not validate against the required JSON schema:\n"
                                            f"{err}\nReply again with only the corrected JSON object."},
            ]
            comp2 = self.complete(stage=stage, model=model, system=system, messages=messages,
                                  max_tokens=max_tokens)
            try:
                return schema.model_validate(extract_json(comp2.text))
            except (ValueError, ValidationError) as exc2:
                raise ProviderError(f"stage {stage}: model output failed validation twice: {exc2}") from exc2
