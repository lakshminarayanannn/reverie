"""Memory-quality metrics computed on every dream (no task runs needed)."""

from __future__ import annotations

from collections import Counter
from typing import Any

from reverie.core.schema import LIVE_STATUSES, MemoryFile
from reverie.core.store import ENTRY_FILE, link_target, links_in


def dangling_links(files: dict[str, MemoryFile], other_files: set[str]) -> list[str]:
    paths = set(files) | other_files
    out: list[str] = []
    for path, f in files.items():
        for link in links_in(f.render()):
            if link_target(link) not in paths:
                out.append(f"{path}: [[{link}]]")
    return out


def memory_metrics(files: dict[str, MemoryFile], other_files: set[str], corpus_bytes: int,
                   max_lines: int, max_bytes: int) -> dict[str, Any]:
    statuses: Counter[str] = Counter()
    for f in files.values():
        for n in f.notes():
            statuses[n.status] += 1
    entry = files.get(ENTRY_FILE)
    entry_text = entry.render() if entry else ""
    total = sum(len(f.render().encode()) for f in files.values())
    live_bytes = sum(len(f.render().encode()) for p, f in files.items() if p != "archive.md")
    return {
        "memory_bytes": total,
        "live_memory_bytes": live_bytes,
        "corpus_bytes_redacted": corpus_bytes,
        "memory_to_corpus_ratio": round(total / corpus_bytes, 4) if corpus_bytes else None,
        "memory_md": {"lines": entry_text.count("\n"), "bytes": len(entry_text.encode()),
                      "max_lines": max_lines, "max_bytes": max_bytes,
                      "within_cap": entry_text.count("\n") <= max_lines and len(entry_text.encode()) <= max_bytes},
        "notes_by_status": dict(sorted(statuses.items())),
        "live_notes": sum(v for k, v in statuses.items() if k in LIVE_STATUSES),
        "contested": statuses.get("contested", 0),
        "dangling_links": len(dangling_links(files, other_files)),
    }
