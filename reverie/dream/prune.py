"""Stage 8: retire notes that are stale, contradicted, unread, or ineffective.

Retired notes move to ``archive.md`` (not linked from the index, so agents do not load
them) and are deleted from it after ``archive_retention_dreams`` dreams. Deleted notes
stay in git history; the evidence they came from is never deleted.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from reverie.core.schema import RETIRED_STATUSES, Note
from reverie.dream.models import Change


class UsageStats(BaseModel):
    """Per-note online usage aggregated from ``.reverie/usage.jsonl``."""

    sessions_seen: int = 0
    last_seen_dream: dict[str, int] = Field(default_factory=dict)  # note id -> dream number of last injection/read
    ineffective: dict[str, str] = Field(default_factory=dict)  # note id -> reason (holdout)


def prune(notes: dict[str, Note], dream_no: int, usage: UsageStats, note_state: dict[str, dict[str, int]],
          *, unread_dreams: int, unread_min_sessions: int, archive_retention: int,
          promoted_ids: set[str]) -> tuple[set[str], list[Change]]:
    """Mutates statuses; returns (note ids to delete entirely, changes)."""
    changes: list[Change] = []
    removed: set[str] = set()
    for nid, note in notes.items():
        if note.status in ("verified", "active") and nid in usage.ineffective:
            note.status = "stale"
            note.extra["reason"] = usage.ineffective[nid]
            changes.append(Change(action="stale", note_id=nid, text=note.text, evidence=note.sources,
                                  detail=usage.ineffective[nid]))
            continue
        if (note.status in ("verified", "active") and usage.sessions_seen >= unread_min_sessions
                and nid not in promoted_ids):
            first = note_state.get(nid, {}).get("first_dream", dream_no)
            last = usage.last_seen_dream.get(nid, first)
            if dream_no - last >= unread_dreams:
                note.status = "stale"
                note.extra["reason"] = f"not injected or read for {dream_no - last} dreams"
                changes.append(Change(action="stale", note_id=nid, text=note.text, evidence=note.sources,
                                      detail=note.extra["reason"]))
                continue
        if note.status in RETIRED_STATUSES:
            retired = int(note.extra.get("retired_dream", "0") or 0)
            if not retired:
                note.extra["retired_dream"] = str(dream_no)
            elif dream_no - retired >= archive_retention:
                removed.add(nid)
                changes.append(Change(action="removed", note_id=nid, text=note.text, evidence=note.sources,
                                      detail=f"archived for {dream_no - retired} dreams; kept in git history"))
    return removed, changes
