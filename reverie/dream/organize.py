"""Stage 6: placement decisions, topic assignment, and the write plan.

Writes are planned first (path -> full file contents) and then executed by one writer
per file. Nothing edits a file concurrently, and every non-managed line is preserved.
"""

from __future__ import annotations

import logging
import re

from reverie.core.budget import BudgetExceeded
from reverie.core.schema import LIVE_STATUSES, MemoryFile, MemoryRepo, Note, NoteLine, RawLine
from reverie.core.store import ENTRY_FILE, INDEX_HEADING, link_target, links_in, parse_file, path_to_link
from reverie.dream.models import (
    InstructionEdit,
    OrganizeOut,
    PlacementRecord,
    VerifiedCandidate,
)
from reverie.dream.prompts import load_prompt
from reverie.providers.base import LLM, ProviderError

log = logging.getLogger("reverie.dream.organize")

ARCHIVE_FILE = "archive.md"
_TOPIC_RE = re.compile(r"^topics/(?:[a-z0-9][a-z0-9-]{0,40}/)?[a-z0-9][a-z0-9-]{0,60}\.md$")
DEFAULT_TOPICS = {"rule": "topics/rules.md", "trap": "topics/traps.md", "preference": "topics/preferences.md",
                  "decision": "topics/decisions.md", "fact": "topics/facts.md", None: "topics/notes.md"}


def sanitize_topic(raw: str) -> str | None:
    topic = raw.strip().lower().replace(" ", "-").replace("_", "-")
    if not topic.startswith("topics/"):
        topic = "topics/" + topic.lstrip("/")
    if not topic.endswith(".md"):
        topic += ".md"
    return topic if _TOPIC_RE.match(topic) and ".." not in topic else None


def placements(survivors: list[VerifiedCandidate], dropped: list[VerifiedCandidate], placed: dict[str, str],
               guard_note_ids: set[str], notes: dict[str, Note] | None = None,
               ) -> tuple[list[PlacementRecord], list[InstructionEdit]]:
    """One placement decision per candidate, as recorded in PROPOSAL.md."""
    records: list[PlacementRecord] = []
    edits: list[InstructionEdit] = []
    for vc in survivors:
        c = vc.candidate
        nid = placed.get(c.cid)
        if c.placement == "instruction_edit":
            edits.append(InstructionEdit(text=c.text, evidence=c.evidence))
            records.append(PlacementRecord(cid=c.cid, text=c.text, placement="instruction_edit", note_id=nid,
                                           reason="stable convention for the agent's own instruction file "
                                                  "(proposed only, never applied automatically; kept as a memory "
                                                  "note until you apply it)"))
        elif c.placement == "evidence_only":
            records.append(PlacementRecord(cid=c.cid, text=c.text, placement="evidence_only",
                                           reason="true but not worth loading; kept in the experience store"))
        elif nid is None:
            records.append(PlacementRecord(cid=c.cid, text=c.text, placement="nowhere",
                                           reason="superseded or rejected during reconciliation"))
        elif notes is not None and nid in notes and notes[nid].status not in LIVE_STATUSES:
            records.append(PlacementRecord(cid=c.cid, text=c.text, placement="nowhere", note_id=nid,
                                           reason=f"note became {notes[nid].status} later in this dream"))
        elif nid in guard_note_ids:
            records.append(PlacementRecord(cid=c.cid, text=c.text, placement="guard_proposal", note_id=nid,
                                           reason="verified trap with a validated command pattern (also a note)"))
        else:
            records.append(PlacementRecord(cid=c.cid, text=c.text, placement="memory_note", note_id=nid))
    for vc in dropped:
        records.append(PlacementRecord(cid=vc.candidate.cid, text=vc.candidate.text, placement="nowhere",
                                       reason=vc.dropped or "dropped"))
    return records, edits


def assign_topics(new_notes: list[Note], existing_topics: list[str], llm: LLM | None, model: str,
                  purpose: str, errors: list[str]) -> dict[str, str]:
    out = {n.id: DEFAULT_TOPICS.get(n.kind, DEFAULT_TOPICS[None]) for n in new_notes}
    if not new_notes or llm is None:
        return out
    system = load_prompt("organize").render(purpose=purpose)
    lines = ["Existing topic files: " + (", ".join(existing_topics) or "(none)"), "", "Notes to file:"]
    lines += [f'- id={n.id} kind={n.kind}: "{n.text}"' for n in new_notes]
    try:
        res = llm.complete_json(stage="organize", model=model, system=system, user="\n".join(lines),
                                schema=OrganizeOut, max_tokens=4000)
    except (ProviderError, BudgetExceeded) as exc:
        errors.append(f"organize: {exc} (fell back to topics by kind)")
        return out
    for a in res.assignments:
        topic = sanitize_topic(a.topic)
        if a.id in out and topic is not None:
            out[a.id] = topic
    return out


def _title(path: str) -> str:
    stem = path.rsplit("/", 1)[-1].removesuffix(".md")
    return stem.replace("-", " ").strip().capitalize() or "Notes"


def render_file(path: str, original: MemoryFile | None, notes: list[Note], header: str | None = None) -> str:
    """Rewrite ``path`` with exactly ``notes`` as its managed notes, keeping every raw line."""
    lines: list[RawLine | NoteLine] = []
    wanted = {n.id: n for n in notes}
    placed: set[str] = set()
    if original is not None:
        for line in original.lines:
            if isinstance(line, NoteLine):
                n = wanted.get(line.note.id)
                if n is not None and n.id not in placed:
                    lines.append(NoteLine(note=n))
                    placed.add(n.id)
            else:
                lines.append(line)
    else:
        lines.append(RawLine(text=header or f"# {_title(path)}"))
        lines.append(RawLine(text=""))
    # append new notes before trailing blank lines
    tail: list[RawLine | NoteLine] = []
    while lines and isinstance(lines[-1], RawLine) and not lines[-1].text.strip():
        tail.insert(0, lines.pop())
    for n in notes:
        if n.id not in placed:
            lines.append(NoteLine(note=n))
    return MemoryFile(path=path, lines=lines + tail).render()


def build_memory_md(original: MemoryFile, promoted: list[Note], topic_files: list[str],
                    existing_paths: set[str]) -> str:
    """MEMORY.md: unmanaged lines kept, managed notes = ``promoted``, Index regenerated."""
    pre: list[RawLine | NoteLine] = []
    post: list[RawLine | NoteLine] = []
    index_lines: list[str] = []
    section = "pre"
    for line in original.lines:
        if isinstance(line, RawLine) and line.text.strip() == INDEX_HEADING:
            section = "index"
            continue
        if section == "index" and isinstance(line, RawLine) and line.text.startswith("#"):
            section = "post"
        if isinstance(line, NoteLine):
            continue  # managed notes are re-placed below
        if section == "pre":
            pre.append(line)
        elif section == "index":
            if line.text.strip():
                index_lines.append(line.text)
        else:
            post.append(line)
    while pre and isinstance(pre[-1], RawLine) and not pre[-1].text.strip():
        pre.pop()
    if not pre:
        pre = [RawLine(text="# Memory")]
    body: list[RawLine | NoteLine] = list(pre)
    if promoted:
        body.append(RawLine(text=""))
        body.extend(NoteLine(note=n) for n in promoted)
    body.append(RawLine(text=""))
    body.append(RawLine(text=INDEX_HEADING))
    # keep user index lines whose links resolve (or that have no links); drop dangling ones
    linked: set[str] = set()
    for text in index_lines:
        targets = [link_target(t) for t in links_in(text)]
        if targets and not all(t in existing_paths for t in targets):
            continue
        linked.update(targets)
        body.append(RawLine(text=text))
    for path in topic_files:
        if path not in linked:
            body.append(RawLine(text=f"- [[{path_to_link(path)}]]"))
    if post:
        body.append(RawLine(text=""))
        body.extend(post)
    return MemoryFile(path=ENTRY_FILE, lines=body).render()


def fix_dangling_links(contents: dict[str, str | None], all_paths: set[str]) -> tuple[dict[str, str | None], int]:
    """Rewrite ``[[x]]`` links whose target is missing: to a unique same-named file, else plain text."""
    by_name: dict[str, list[str]] = {}
    for p in all_paths:
        by_name.setdefault(p.rsplit("/", 1)[-1], []).append(p)
    fixed = 0
    out: dict[str, str | None] = {}
    for path, text in contents.items():
        if text is None:
            out[path] = None
            continue

        def repl(m: re.Match[str]) -> str:
            nonlocal fixed
            target = link_target(m.group(1))
            if target in all_paths:
                return m.group(0)
            fixed += 1
            candidates = by_name.get(target.rsplit("/", 1)[-1], [])
            if len(candidates) == 1:
                return f"[[{path_to_link(candidates[0])}]]"
            return m.group(1)

        out[path] = re.sub(r"\[\[([^\[\]]+)\]\]", repl, text)
    return out, fixed


def plan_writes(repo: MemoryRepo, notes: dict[str, Note], paths: dict[str, str],
                removed: set[str]) -> dict[str, str | None]:
    """Full contents for every file that holds managed notes (None = delete an emptied reverie file)."""
    by_file: dict[str, list[Note]] = {}
    for nid, note in notes.items():
        if nid in removed:
            continue
        by_file.setdefault(paths[nid], []).append(note)
    contents: dict[str, str | None] = {}
    touched = set(by_file) | {p for p, f in repo.files.items() if f.notes()}
    for path in sorted(touched):
        if path == ENTRY_FILE:
            continue
        file_notes = by_file.get(path, [])
        original = repo.files.get(path)
        if not file_notes and original is not None and not any(
                isinstance(line, RawLine) and line.text.strip() and not line.text.startswith("# ")
                for line in original.lines):
            contents[path] = None  # only a heading was left: remove the file
            continue
        file_notes.sort(key=lambda n: (n.added, n.id))
        header = "# Archive (retired notes; not loaded by agents)" if path == ARCHIVE_FILE else None
        contents[path] = render_file(path, original, file_notes, header)
    return contents


def topic_files_with_live_notes(notes: dict[str, Note], paths: dict[str, str], removed: set[str]) -> list[str]:
    out = {paths[nid] for nid, n in notes.items()
           if nid not in removed and n.status in LIVE_STATUSES and paths[nid] not in (ENTRY_FILE, ARCHIVE_FILE)}
    return sorted(out)


def parse_contents(contents: dict[str, str | None]) -> dict[str, MemoryFile]:
    return {p: parse_file(p, t) for p, t in contents.items() if t is not None}
