"""Stage 7: choose the few notes every session needs for ``MEMORY.md``, within the cap."""

from __future__ import annotations

from reverie.core.budget import BudgetExceeded
from reverie.core.schema import Note
from reverie.dream.models import PromoteOut
from reverie.dream.prompts import load_prompt
from reverie.providers.base import LLM, ProviderError

KIND_PRIORITY = {"trap": 0, "rule": 1, "preference": 2, "decision": 3, "fact": 4, None: 5}
PROMOTABLE_KINDS = {"trap", "rule", "preference", "decision"}


def eligible(note: Note, min_sessions: int) -> bool:
    return (note.status in ("verified", "active") and len(note.source_sessions()) >= min_sessions
            and note.kind in PROMOTABLE_KINDS)


def rank_key(note: Note) -> tuple[int, int, int, str, str]:
    return (-len(note.source_sessions()), 0 if note.extra.get("contrast") == "true" else 1,
            KIND_PRIORITY.get(note.kind, 5), note.added, note.id)


def judge_general(candidates: list[Note], llm: LLM | None, model: str, purpose: str,
                  errors: list[str]) -> set[str]:
    """Ask the model which eligible notes are general across tasks; without a model, all are."""
    ids = {n.id for n in candidates}
    if llm is None or not candidates:
        return ids
    system = load_prompt("promote").render(purpose=purpose)
    user = "Notes:\n" + "\n".join(f'- id={n.id} kind={n.kind}: "{n.text}"' for n in candidates)
    try:
        out = llm.complete_json(stage="promote", model=model, system=system, user=user, schema=PromoteOut,
                                max_tokens=2000)
    except (ProviderError, BudgetExceeded) as exc:
        errors.append(f"promote: {exc} (fell back to deterministic criteria)")
        return ids
    return ids & set(out.general)


def fit_to_cap(ranked: list[Note], base_lines: int, base_bytes: int, max_lines: int, max_bytes: int) -> list[Note]:
    """Greedy fill: take notes in rank order while MEMORY.md stays within both caps."""
    chosen: list[Note] = []
    lines, size = base_lines + 1, base_bytes + 1  # +1 for the blank line before notes
    for n in ranked:
        line = n.render()
        cost = len(line.encode()) + 1
        if lines + 1 > max_lines or size + cost > max_bytes:
            continue
        chosen.append(n)
        lines += 1
        size += cost
    return chosen
