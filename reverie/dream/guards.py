"""Guard proposals: turn verified trap notes into PreToolUse checks, validated against the corpus.

A guard is proposed only when all of these hold (deterministically, no model):

* it comes from a non-contested trap note with a command regex and a replacement;
* the regex compiles, is short, and does not match its own replacement;
* in at least ``min_sessions`` sessions a shell command matching the regex failed;
* the regex matches no command that succeeded anywhere in the corpus (it would block
  real work otherwise);
* none of that evidence is flagged as possible prompt injection.
"""

from __future__ import annotations

import hashlib
import re

from reverie.core.schema import Note, Session
from reverie.dream.ingest import IngestResult
from reverie.dream.models import GuardProposal

GUARD_PREFIX = "reverie guard (automated; may be outdated):"


def _commands(session: Session) -> list[tuple[int, str, bool | None]]:
    """(idx, command, failed?) for each shell tool call; failure read from the next tool_result."""
    out: list[tuple[int, str, bool | None]] = []
    events = session.events
    for i, ev in enumerate(events):
        if ev.type != "tool_call":
            continue
        cmd = ev.command()
        if not cmd:
            continue
        failed: bool | None = None
        if ev.exit_code is not None or ev.status is not None:
            failed = ev.is_failure()
        else:
            for nxt in events[i + 1:]:
                if nxt.type == "tool_call":
                    break
                if nxt.type == "tool_result":
                    failed = nxt.is_failure()
                    break
        out.append((ev.idx, cmd, failed))
    return out


def choose_hint(hints: list[tuple[str, str]]) -> tuple[str, str]:
    """Several sessions proposed a guard for one note: most common pattern, shortest replacement."""
    from collections import Counter

    pattern = Counter(p for p, _ in hints).most_common(1)[0][0]
    replacement = min((r for _, r in hints), key=lambda r: (len(r), r))
    return pattern, replacement


def guard_message(pattern: str, replacement: str, note_id: str) -> str:
    return (f"{GUARD_PREFIX} this command matches the pattern `{pattern}`, which failed in earlier sessions "
            f"(memory note {note_id}). Earlier sessions succeeded with: `{replacement}`.")


def validate_guard(pattern: str, replacement: str, ingested: IngestResult,
                   min_sessions: int) -> tuple[list[str], str | None]:
    """Return (sessions where a matching command failed, rejection reason or None)."""
    if not pattern or len(pattern) > 200:
        return [], "pattern missing or too long"
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return [], f"invalid regex: {exc}"
    if not replacement.strip():
        return [], "missing replacement"
    if rx.search(replacement):
        return [], "pattern matches its own replacement"
    failing: list[str] = []
    for s in ingested.sessions:
        tainted = set(ingested.tainted.get(s.session_id, []))
        for idx, cmd, failed in _commands(s):
            if not rx.search(cmd):
                continue
            if failed is False:
                return [], f"pattern matches a command that succeeded ({s.session_id}#{idx})"
            if failed and idx not in tainted and s.session_id not in failing:
                failing.append(s.session_id)
    if len(failing) < min_sessions:
        return failing, f"matching command failed in {len(failing)} session(s); need {min_sessions}"
    return failing, None


def propose_guards(notes: list[Note], hints: dict[str, tuple[str, str]], ingested: IngestResult,
                   min_sessions: int, existing_patterns: set[str]) -> tuple[list[GuardProposal], list[dict[str, str]]]:
    """``hints``: note id -> (command_pattern, replacement) from extraction."""
    proposals: list[GuardProposal] = []
    rejected: list[dict[str, str]] = []
    for note in notes:
        if note.id not in hints or note.kind != "trap" or note.status not in ("verified", "active"):
            continue
        pattern, replacement = hints[note.id]
        if pattern in existing_patterns or any(p.pattern == pattern for p in proposals):
            continue
        sessions, reason = validate_guard(pattern, replacement, ingested, min_sessions)
        if reason is not None:
            rejected.append({"note_id": note.id, "pattern": pattern, "reason": reason})
            continue
        gid = "g-" + hashlib.sha256(pattern.encode()).hexdigest()[:6]
        proposals.append(GuardProposal(id=gid, note_id=note.id, pattern=pattern, replacement=replacement,
                                       message=guard_message(pattern, replacement, note.id),
                                       evidence_sessions=sessions))
    return proposals, rejected
