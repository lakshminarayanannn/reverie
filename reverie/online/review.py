"""Review workflow: list / show / approve / reject dream proposals, reject single notes.

Approving merges the dream branch (``--no-ff``) and adds its proposed guards to
``guards.toml``. Rejecting keeps the branch as ``rejected/<id>`` and records its sessions
as dreamed and its new notes' fingerprints as rejected, so the same proposal is not made
again.
"""

from __future__ import annotations

import json
import tomllib
from typing import Any

from reverie.core.fsutil import atomic_write_text
from reverie.core.schema import normalize_text
from reverie.core.store import MemoryStore, StoreError, git, parse_file, parse_note_line
from reverie.dream.organize import ARCHIVE_FILE, render_file
from reverie.dream.proposal import guards_to_toml


def _branch(run_id: str) -> str:
    return run_id if run_id.startswith("dream/") else f"dream/{run_id}"


def _run_id(branch: str) -> str:
    return branch.split("/", 1)[1]


def _require_ready(store: MemoryStore) -> str:
    store.require()
    if not store.is_clean():
        raise StoreError("memory repo has uncommitted changes; commit or discard them first")
    current = store.current_branch()
    if current.startswith("dream/"):
        raise StoreError(f"check out the default branch first (on {current})")
    return current


def list_proposals(store: MemoryStore) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for kind, pattern in (("pending", "dream/*"), ("rejected", "rejected/*")):
        for branch in store.branches(pattern):
            rid = _run_id(branch)
            text = store.show(branch, f".reverie/proposals/{rid}.md") or ""
            summary = ""
            lines = text.splitlines()
            for i, line in enumerate(lines):
                if line.startswith("| added"):
                    summary = lines[i + 2] if i + 2 < len(lines) else ""
                    break
            out.append({"id": rid, "status": kind, "branch": branch, "summary": summary})
    return out


def show_proposal(store: MemoryStore, run_id: str) -> str:
    rid = _run_id(_branch(run_id))
    for branch in (f"dream/{rid}", f"rejected/{rid}", store.current_branch()):
        text = store.show(branch, f".reverie/proposals/{rid}.md")
        if text is not None:
            return text
    raise StoreError(f"no proposal {run_id}")


def merge_guards(store: MemoryStore, new: list[dict[str, Any]]) -> int:
    path = store.root / "guards.toml"
    existing: list[dict[str, Any]] = []
    if path.exists():
        existing = [g for g in tomllib.loads(path.read_text(encoding="utf-8")).get("guard", []) if isinstance(g, dict)]
    have = {g.get("pattern") for g in existing}
    added = [g for g in new if g.get("pattern") not in have]
    if added:
        atomic_write_text(path, guards_to_toml(existing + added))
    return len(added)


def approve(store: MemoryStore, run_id: str, *, include_guards: bool = True) -> dict[str, Any]:
    base = _require_ready(store)
    branch = _branch(run_id)
    rid = _run_id(branch)
    if branch not in store.branches(branch):
        raise StoreError(f"no pending proposal {rid}")
    proc = git(store.root, "merge", "--no-ff", "-q", "-m", f"reverie: accept dream {rid}", branch, check=False)
    if proc.returncode != 0:
        git(store.root, "merge", "--abort", check=False)
        raise StoreError(f"could not merge {branch} into {base}: {proc.stderr.strip() or proc.stdout.strip()}")
    added = 0
    gpath = store.state_dir / "proposals" / f"{rid}.guards.toml"
    if include_guards and gpath.exists():
        props = tomllib.loads(gpath.read_text(encoding="utf-8")).get("guard", [])
        added = merge_guards(store, [g for g in props if isinstance(g, dict)])
        if added:
            store.commit_all(f"reverie: add {added} guard(s) from dream {rid}")
    git(store.root, "branch", "-q", "-D", branch)
    return {"approved": rid, "merged_into": base, "guards_added": added, "commit": store.head()}


def _added_note_texts(store: MemoryStore, base: str, branch: str) -> list[str]:
    diff = git(store.root, "diff", f"{base}...{branch}", "--", ".", ":!.reverie").stdout
    texts: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            note = parse_note_line(line[1:])
            if note is not None and note.status in ("verified", "active", "contested"):
                texts.append(note.text)
    return texts


def reject(store: MemoryStore, run_id: str) -> dict[str, Any]:
    base = _require_ready(store)
    branch = _branch(run_id)
    rid = _run_id(branch)
    if branch not in store.branches(branch):
        raise StoreError(f"no pending proposal {rid}")
    texts = _added_note_texts(store, base, branch)
    raw_state = store.show(branch, ".reverie/state.json")
    branch_state = json.loads(raw_state) if raw_state else {}
    state = store.read_state()
    state.setdefault("dreamed", {}).update(branch_state.get("dreamed", {}))
    fps = set(state.get("rejected_fingerprints", []))
    fps.update(normalize_text(t) for t in texts)
    state["rejected_fingerprints"] = sorted(fps)
    store.write_state(state)
    proposal = store.show(branch, f".reverie/proposals/{rid}.md")
    if proposal:
        atomic_write_text(store.state_dir / "proposals" / f"{rid}.rejected.md", proposal)
    store.commit_all(f"reverie: reject dream {rid}")
    git(store.root, "branch", "-q", "-m", branch, f"rejected/{rid}")
    return {"rejected": rid, "notes_blocked": len(texts), "kept_as": f"rejected/{rid}"}


def reject_note(store: MemoryStore, note_id: str, reason: str = "rejected by user") -> dict[str, Any]:
    """Retire one note now (status rejected, moved to the archive) and block it from returning."""
    _require_ready(store)
    repo = store.load()
    found = repo.find(note_id)
    if found is None:
        raise StoreError(f"no note {note_id}")
    path, note = found
    note.status = "rejected"
    note.extra["reason"] = reason
    src = repo.files[path]
    contents: dict[str, str | None] = {path: render_file(path, src, [n for n in src.notes() if n.id != note_id])}
    archive = repo.files.get(ARCHIVE_FILE)
    arch_notes = (archive.notes() if archive else []) + [note]
    contents[ARCHIVE_FILE] = render_file(ARCHIVE_FILE, archive, arch_notes,
                                         "# Archive (retired notes; not loaded by agents)")
    store.write_files(contents)
    state = store.read_state()
    fps = set(state.get("rejected_fingerprints", []))
    fps.add(normalize_text(note.text))
    state["rejected_fingerprints"] = sorted(fps)
    store.write_state(state)
    sha = store.commit_all(f"reverie: reject note {note_id}")
    return {"rejected_note": note_id, "from": path, "commit": sha}


def read_guards(store: MemoryStore) -> list[dict[str, Any]]:
    path = store.root / "guards.toml"
    if not path.exists():
        return []
    return [g for g in tomllib.loads(path.read_text(encoding="utf-8")).get("guard", []) if isinstance(g, dict)]


def set_guard_enabled(store: MemoryStore, guard_id: str, enabled: bool) -> dict[str, Any]:
    _require_ready(store)
    guards = read_guards(store)
    hit = [g for g in guards if g.get("id") == guard_id]
    if not hit:
        raise StoreError(f"no guard {guard_id}")
    for g in hit:
        g["enabled"] = enabled
    atomic_write_text(store.root / "guards.toml", guards_to_toml(guards))
    store.commit_all(f"reverie: {'enable' if enabled else 'disable'} guard {guard_id}")
    return {"guard": guard_id, "enabled": enabled}


def parse_memory_md(store: MemoryStore) -> Any:
    return parse_file("MEMORY.md", (store.root / "MEMORY.md").read_text(encoding="utf-8"))
