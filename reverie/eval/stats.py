"""Statistics for task evaluations: noise floor and paired bootstrap confidence intervals."""

from __future__ import annotations

import math
import random
from statistics import mean


def sample_sd(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def noise_floor(per_task_repeats: dict[str, list[float]]) -> float | None:
    """Mean per-task standard deviation across repeats (of the no-memory arm)."""
    sds = [sample_sd(v) for v in per_task_repeats.values() if len(v) >= 2]
    return mean(sds) if sds else None


def paired_bootstrap(arm: dict[str, float], base: dict[str, float], *, n_boot: int = 10_000,
                     seed: int = 0, alpha: float = 0.05) -> dict[str, float | int | None]:
    """Mean difference (arm - base) over shared tasks with a percentile bootstrap CI over tasks."""
    tasks = sorted(set(arm) & set(base))
    if not tasks:
        return {"n_tasks": 0, "mean_diff": None, "ci_low": None, "ci_high": None}
    diffs = [arm[t] - base[t] for t in tasks]
    rng = random.Random(seed)
    n = len(diffs)
    boots = sorted(mean(diffs[rng.randrange(n)] for _ in range(n)) for _ in range(n_boot))
    lo = boots[int((alpha / 2) * n_boot)]
    hi = boots[min(int((1 - alpha / 2) * n_boot), n_boot - 1)]
    return {"n_tasks": n, "mean_diff": round(mean(diffs), 4), "ci_low": round(lo, 4), "ci_high": round(hi, 4)}
