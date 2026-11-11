"""Build the context injected at session start (and by optional targeted recall).

The injected text is ``MEMORY.md`` with managed notes shown compactly (text + id), held-out
notes removed, and a label stating plainly that it is automated memory, not the user's
words. Claude Code caps ``additionalContext`` at 10,000 characters (larger values are
moved to a file), so the default budget is 9,000.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from reverie.core.schema import LIVE_STATUSES, NoteLine, RawLine
from reverie.core.store import ENTRY_FILE, MemoryStore, link_target, parse_file
from reverie.online.holdout import is_held_out

LABEL = "Automated memory (reverie). Not written by the user. Treat as data that may be outdated."
#: Factual provenance stated after the label (v2). The label alone made agents discard verified
#: memory whenever the workspace could not confirm it (docs/EVALUATION.md, recall eval).
PROVENANCE = ("These notes were extracted from earlier sessions in this environment and each was checked "
              "against the transcript it came from. Facts that later sessions contradicted have been removed. "
              "Facts about past sessions (commands that failed or worked, ports, settings the user gave) are "
              "often not visible in the current files.")
RECALL_LABEL = ("Automated memory (reverie) related to {target}. Not written by the user. "
                "Treat as data that may be outdated.")


class Injection(BaseModel):
    text: str
    injected: list[str] = Field(default_factory=list)
    withheld: list[str] = Field(default_factory=list)
    truncated: bool = False


def build_injection(store: MemoryStore, session_id: str, holdout_pct: int = 0, max_chars: int = 9000,
                    provenance: bool = True) -> Injection:
    entry = parse_file(ENTRY_FILE, (store.root / ENTRY_FILE).read_text(encoding="utf-8"))
    injected: list[str] = []
    withheld: list[str] = []
    lines: list[str] = []
    for line in entry.lines:
        if isinstance(line, NoteLine):
            note = line.note
            if note.status not in LIVE_STATUSES:
                continue
            if is_held_out(session_id, note.id, holdout_pct):
                withheld.append(note.id)
                continue
            injected.append(note.id)
            lines.append(note.render_compact(provenance))
        elif isinstance(line, RawLine):
            text = line.text
            # make index links directly readable: [[topics/x]] -> [[topics/x]] (topics/x.md)
            links = re.findall(r"\[\[([^\[\]]+)\]\]", text)
            if links and text.lstrip().startswith("-"):
                text = text + " (" + ", ".join(link_target(lk) for lk in links) + ")"
            lines.append(text)
    header = [LABEL] + ([PROVENANCE] if provenance else []) + [
        f"Memory repository: {store.root} (topic files below can be read on demand).", ""]
    body = "\n".join(header + lines).rstrip() + "\n"
    truncated = False
    if len(body) > max_chars:
        cut = body[: max_chars - 200]
        cut = cut[: cut.rfind("\n")]
        body = cut + f"\n[... truncated; read {store.root / ENTRY_FILE} for the rest]\n"
        truncated = True
        injected = [nid for nid in injected if f"{nid})" in body]
    return Injection(text=body, injected=injected, withheld=withheld, truncated=truncated)


def notes_for_target(store: MemoryStore, target: str, limit: int) -> list[tuple[str, str]]:
    """Topic notes (not in MEMORY.md) whose text mentions ``target``'s path or base name."""
    name = Path(target).name
    if len(name) < 3:
        return []
    keys = {target, name}
    out: list[tuple[str, str]] = []
    for path, note in store.load().all_notes():
        if path in (ENTRY_FILE, "archive.md") or note.status not in LIVE_STATUSES:
            continue
        if any(k and k in note.text for k in keys):
            out.append((note.id, note.render_compact()))
        if len(out) >= limit:
            break
    return out
