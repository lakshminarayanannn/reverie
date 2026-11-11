"""Token and cost accounting with hard per-run limits."""

from __future__ import annotations

from dataclasses import dataclass, field

# USD per million tokens (input, output). Anthropic first-party list prices as published
# in the claude-api reference (cached 2026-10-06). Unknown models cost 0 and are flagged.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "mock": (0.0, 0.0),
}


class BudgetExceeded(RuntimeError):
    """Raised when a call would exceed (or has exceeded) the run budget."""


def estimate_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    price = PRICES_PER_MTOK.get(model)
    if price is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


@dataclass
class Budget:
    """Tracks spend. ``max_tokens`` / ``max_usd`` of ``None`` mean unlimited."""

    max_tokens: int | None = None
    max_usd: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
    calls: int = 0
    cached_calls: int = 0
    unpriced_models: set[str] = field(default_factory=set)
    by_stage: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def check(self) -> None:
        """Raise if the budget is already spent (called before every model call)."""
        if self.max_tokens is not None and self.tokens >= self.max_tokens:
            raise BudgetExceeded(f"token budget spent: {self.tokens} >= {self.max_tokens}")
        if self.max_usd is not None and self.usd >= self.max_usd:
            raise BudgetExceeded(f"USD budget spent: {self.usd:.4f} >= {self.max_usd:.4f}")

    def record(self, *, model: str, input_tokens: int, output_tokens: int, stage: str = "other",
               cached: bool = False, usd: float | None = None) -> None:
        self.calls += 1
        stage_rec = self.by_stage.setdefault(stage, {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                                                     "usd": 0.0, "cached_calls": 0})
        stage_rec["calls"] += 1
        if cached:
            self.cached_calls += 1
            stage_rec["cached_calls"] += 1
            return
        cost = usd if usd is not None else estimate_usd(model, input_tokens, output_tokens)
        if cost is None:
            self.unpriced_models.add(model)
            cost = 0.0
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.usd += cost
        stage_rec["input_tokens"] += input_tokens
        stage_rec["output_tokens"] += output_tokens
        stage_rec["usd"] += cost

    def summary(self) -> dict[str, object]:
        return {
            "calls": self.calls,
            "cached_calls": self.cached_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "usd": round(self.usd, 6),
            "max_tokens": self.max_tokens,
            "max_usd": self.max_usd,
            "unpriced_models": sorted(self.unpriced_models),
            "by_stage": self.by_stage,
        }
