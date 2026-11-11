"""Stage 2: select sessions to dream, notes to re-check, and chunk transcripts for the model."""

from __future__ import annotations

from reverie.core.schema import Event, MemoryRepo, Session
from reverie.dream.ingest import IngestResult
from reverie.dream.models import Chunk, Selection

DELIM_OPEN = "<<<TRANSCRIPT"
DELIM_CLOSE = "<<<END TRANSCRIPT>>>"


def state_key(session: Session) -> str:
    return f"{session.agent}/{session.session_id}"


def _escape(text: str) -> str:
    # Data must not be able to close or open our delimiters.
    return text.replace("<<<", "‹‹‹").replace(">>>", "›››")


def render_event(ev: Event, tainted: bool = False, max_chars: int = 4000) -> str:
    label: str = str(ev.type)
    if ev.tool_name:
        label += f" {ev.tool_name}"
    if ev.status:
        label += f" status={ev.status}"
    if ev.exit_code is not None:
        label += f" exit={ev.exit_code}"
    if tainted:
        label += " [possible-injection]"
    body = ev.text or ""
    if ev.type == "tool_call":
        body = ev.render(max_chars)
    elif ev.type == "tool_result":
        body = ev.tool_output or ev.text or ""
    if len(body) > max_chars:
        body = body[: max_chars // 2] + "\n...[cut]...\n" + body[-max_chars // 2:]
    return f"[#{ev.idx} {label}] {_escape(body)}"


def render_transcript(session: Session, events: list[Event], tainted: set[int], note: str = "") -> str:
    header = (f'{DELIM_OPEN} session="{session.session_id}" agent="{session.agent}" '
              f'started="{session.started_at}" outcome="{session.outcome.label}"{note}>>>')
    lines = [header]
    lines.extend(render_event(ev, ev.idx in tainted) for ev in events)
    lines.append(DELIM_CLOSE)
    return "\n".join(lines)


def chunk_session(session: Session, tainted: set[int], max_chars: int, overlap: int) -> list[Chunk]:
    """Split a session into chunks of roughly ``max_chars``, overlapping by ``overlap`` events."""
    rendered = [(ev, render_event(ev, ev.idx in tainted)) for ev in session.events]
    if not rendered:
        return []
    chunks: list[Chunk] = []
    start = 0
    while start < len(rendered):
        size = 0
        end = start
        while end < len(rendered) and (end == start or size + len(rendered[end][1]) <= max_chars):
            size += len(rendered[end][1]) + 1
            end += 1
        events = [ev for ev, _ in rendered[start:end]]
        part = "" if (start == 0 and end == len(rendered)) else f" part=\"events {events[0].idx}-{events[-1].idx}\""
        chunks.append(Chunk(session_id=session.session_id, start_idx=events[0].idx, end_idx=events[-1].idx,
                            text=render_transcript(session, events, tainted, part), last=end >= len(rendered)))
        if end >= len(rendered):
            break
        start = max(end - overlap, start + 1)
    return chunks


def select(ingested: IngestResult, state: dict[str, object], repo: MemoryRepo, *, chunk_chars: int,
           overlap: int, recheck_after: int) -> Selection:
    dreamed = state.get("dreamed") if isinstance(state.get("dreamed"), dict) else {}
    assert isinstance(dreamed, dict)
    sel = Selection()
    selected_ids: set[str] = set()
    for s in ingested.sessions:
        if dreamed.get(state_key(s)) == s.content_hash():
            sel.already_dreamed.append(s.session_id)
            continue
        sel.sessions.append(s.session_id)
        selected_ids.add(s.session_id)
        sel.chunks.extend(chunk_session(s, set(ingested.tainted.get(s.session_id, [])), chunk_chars, overlap))

    note_state = state.get("notes") if isinstance(state.get("notes"), dict) else {}
    assert isinstance(note_state, dict)
    dream_count = int(state.get("dream_count", 0) or 0)  # type: ignore[call-overload]
    for _, note in repo.all_notes():
        if note.status not in ("verified", "active", "contested"):
            continue
        cited = set(note.source_sessions()) & selected_ids
        meta = note_state.get(note.id, {}) if isinstance(note_state.get(note.id), dict) else {}
        last = int(meta.get("last_verified_dream", 0) or 0)
        if cited or note.status == "contested" or dream_count - last >= recheck_after:
            sel.recheck_note_ids.append(note.id)
    return sel
