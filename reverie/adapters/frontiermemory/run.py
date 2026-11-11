"""Benchmark-mode dream: evidence corpus in, a FrontierMemory-valid ``/memory`` folder out.

The output follows ``frontier_memory/memory.py``: ``MEMORY.md`` at the top and not empty,
UTF-8 regular files only, no names starting with a dot, at most 8 MiB. reverie's own
bookkeeping (``.git``, ``.reverie``) and the archive of retired notes are not exported.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

from reverie.adapters.frontiermemory.convert import convert_corpus
from reverie.adapters.generic_jsonl import write_session
from reverie.core.config import ReverieConfig
from reverie.core.store import MemoryStore
from reverie.dream.pipeline import DreamOptions, DreamReport, run_dream
from reverie.dream.prompts import PURPOSE_DIR
from reverie.providers.base import Provider

MAX_BYTES = 8 * 1024 * 1024
EXCLUDE = {"archive.md", "guards.toml"}


def check_memory(root: Path, max_bytes: int = MAX_BYTES) -> dict[str, Any]:
    """Same rules as FrontierMemory's memory format checker."""
    errors: list[str] = []
    if not root.is_dir() or root.is_symlink():
        return {"ok": False, "errors": [f"{root} is not a folder"], "files": []}
    total = 0
    files = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            errors.append(f"{rel.as_posix()}: names must not start with a dot")
            continue
        mode = os.lstat(path).st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            errors.append(f"{rel.as_posix()}: not a regular file")
            continue
        data = path.read_bytes()
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            errors.append(f"{rel.as_posix()}: not UTF-8 text")
        total += len(data)
        files.append({"path": rel.as_posix(), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    entry = root / "MEMORY.md"
    if not entry.is_file() or entry.is_symlink():
        errors.append("MEMORY.md is missing at the top of the folder")
    elif not entry.read_text(errors="replace").strip():
        errors.append("MEMORY.md is empty")
    if total > max_bytes:
        errors.append(f"total size {total} bytes is over the {max_bytes}-byte limit")
    return {"ok": not errors, "errors": errors, "total_bytes": total, "file_count": len(files), "files": files}


def export_memory(repo: Path, out: Path) -> list[str]:
    out.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for path in sorted(Path(repo).rglob("*")):
        rel = path.relative_to(repo)
        if any(p.startswith(".") for p in rel.parts) or not path.is_file() or rel.as_posix() in EXCLUDE:
            continue
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        written.append(rel.as_posix())
    return written


def benchmark_dream(evidence: Path, out: Path, cfg: ReverieConfig, *, provider: Provider | None = None,
                    purpose: Path | None = None, work: Path | None = None) -> dict[str, Any]:
    work = Path(work or tempfile.mkdtemp(prefix="reverie-fm-"))
    sessions, conv = convert_corpus(evidence)
    sess_dir = work / "sessions"
    for s in sessions:
        write_session(s, sess_dir / f"{s.session_id}.jsonl")
    store = MemoryStore(work / "memory")
    store.init()
    report: DreamReport = run_dream(
        DreamOptions(sessions=sess_dir, memory=store.root, purpose=purpose or PURPOSE_DIR / "benchmark.md",
                     auto_accept=True, allow_cloud=True, runs_dir=work / "runs"),
        cfg, provider=provider, notify=lambda m: print(m, flush=True))
    md = store.root / "MEMORY.md"
    if not md.read_text().strip() or md.read_text().strip() == "# Memory\n\n## Index".strip():
        # never hand the task agent an empty entry point
        md.write_text("# Memory\n\nNo durable lessons were extracted from the evidence corpus.\n\n## Index\n")
    files = export_memory(store.root, out)
    check = check_memory(out)
    return {"convert": conv.model_dump(), "dream": report.model_dump(exclude={"metrics"}),
            "metrics": report.metrics, "exported": files, "check": check, "work": str(work)}
