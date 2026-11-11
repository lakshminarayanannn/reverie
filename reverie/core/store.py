"""Agent Memory Repo storage: lossless parse/render, atomic writes, git, and a dream lock.

Format follows the Agent Memory Repo spec (github.com/AgentMemoryRepo/agentmemoryrepo,
SPEC.md at commit 8798cb2): ``MEMORY.md`` at the root with an ``## Index`` of
``[[path]]`` links, one bullet per entry, metadata as ``[key: value; key: value]`` at
the end of the line. Keys are open, so reverie's extra keys stay spec-compatible.

Only bullets that carry an ``id`` key are *managed* notes. Every other line is kept
verbatim, which is how user-written content and unknown metadata survive rewrites.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from reverie.core.fsutil import atomic_write_json, atomic_write_text
from reverie.core.schema import (
    NOTE_KINDS,
    NOTE_STATUSES,
    MemoryFile,
    MemoryRepo,
    Note,
    NoteLine,
    RawLine,
)

ENTRY_FILE = "MEMORY.md"
INDEX_HEADING = "## Index"
STATE_DIR = ".reverie"
DEFAULT_MEMORY_MD = "# Memory\n\n## Index\n"
GITIGNORE = ".reverie/usage.jsonl\n.reverie/runs/\n.reverie/*.lock\n"

_BULLET_RE = re.compile(
    r"^(?P<indent>\s*)[-*] (?P<text>.*?)\s*(?<!\[)\[(?P<meta>[A-Za-z_][\w-]*\s*:[^\[\]]*)\]\s*$"
)
_LINK_RE = re.compile(r"\[\[([^\[\]]+)\]\]")


class StoreError(RuntimeError):
    """A memory-repo operation failed (bad path, dirty tree, git failure)."""


class LockHeld(StoreError):
    """Another dream holds the repo lock."""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_metadata(raw: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    for part in raw.split(";"):
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        key = key.strip()
        if key:
            meta[key] = value.strip()
    return meta


def parse_note_line(line: str) -> Note | None:
    """Parse a managed note line; ``None`` if the line is not one."""
    match = _BULLET_RE.match(line)
    if not match:
        return None
    meta = parse_metadata(match["meta"])
    if "id" not in meta:
        return None
    text = match["text"]
    if text.endswith("] ."):
        text = text[:-2]
    kind = meta.pop("kind", None)
    status = meta.pop("status", "verified")
    extra: dict[str, str] = {}
    if kind is not None and kind not in NOTE_KINDS:
        extra["kind"] = kind
        kind = None
    if status not in NOTE_STATUSES:
        extra["status"] = status
        status = "verified"
    sources = [s.strip() for s in meta.pop("source", "").split(",") if s.strip()]
    note_id = meta.pop("id")
    added = meta.pop("added", None)
    verified = meta.pop("verified", None)
    quote = meta.pop("quote", "false").lower() == "true"
    extra.update(meta)
    try:
        kwargs: dict[str, Any] = {
            "id": note_id, "text": text, "kind": kind, "status": status, "sources": sources,
            "verified": verified, "quote": quote, "extra": extra, "indent": match["indent"],
        }
        if added:
            kwargs["added"] = added
        note = Note(**kwargs)
        note.remember_origin(line)
        return note
    except ValidationError:
        return None


def parse_file(path: str, text: str) -> MemoryFile:
    lines: list[RawLine | NoteLine] = []
    body = text[:-1] if text.endswith("\n") else text
    for line in body.split("\n") if body else []:
        note = parse_note_line(line)
        lines.append(NoteLine(note=note) if note is not None else RawLine(text=line))
    return MemoryFile(path=path, lines=lines)


def links_in(text: str) -> list[str]:
    return [m.strip() for m in _LINK_RE.findall(text)]


def link_target(link: str) -> str:
    """Map a ``[[path]]`` link to the file path it names (``.md`` omitted for Markdown)."""
    link = link.split("|", 1)[0].strip()
    return link if "." in Path(link).name else link + ".md"


def path_to_link(path: str) -> str:
    return path[:-3] if path.endswith(".md") else path


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "reverie")
    env.setdefault("GIT_AUTHOR_EMAIL", "reverie@localhost")
    env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
    env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=False
    )
    if check and proc.returncode != 0:
        raise StoreError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class MemoryStore:
    """A memory repository on disk."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve()

    # -- lifecycle ---------------------------------------------------------

    @property
    def state_dir(self) -> Path:
        return self.root / STATE_DIR

    def exists(self) -> bool:
        return (self.root / ENTRY_FILE).is_file() and (self.root / ".git").exists()

    def init(self) -> None:
        """Create a memory repo (idempotent on an existing one)."""
        if self.exists():
            return
        if self.root.exists() and any(self.root.iterdir()) and not (self.root / ".git").exists():
            raise StoreError(f"{self.root} is a non-empty directory that is not a memory repo")
        self.root.mkdir(parents=True, exist_ok=True)
        if not (self.root / ".git").exists():
            git(self.root, "init", "-q", "-b", "main")
        if not (self.root / ENTRY_FILE).exists():
            atomic_write_text(self.root / ENTRY_FILE, DEFAULT_MEMORY_MD)
        atomic_write_text(self.root / ".gitignore", GITIGNORE)
        git(self.root, "add", ENTRY_FILE, ".gitignore")
        git(self.root, "commit", "-q", "-m", "Create memory repo")

    def require(self) -> None:
        if not self.exists():
            raise StoreError(f"{self.root} is not a memory repo (no MEMORY.md + .git); run `reverie init`")
        self.ensure_local_excludes()

    def ensure_local_excludes(self) -> None:
        """Keep run artifacts, usage logs and locks out of git even in repos reverie did not create.

        Uses ``.git/info/exclude`` (local, never committed, never makes the tree dirty).
        """
        git_dir = self.root / ".git"
        if not git_dir.is_dir():
            return
        exclude = git_dir / "info" / "exclude"
        current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        missing = [line for line in GITIGNORE.splitlines() if line and line not in current.splitlines()]
        if missing:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            sep = "" if not current or current.endswith("\n") else "\n"
            atomic_write_text(exclude, current + sep + "# reverie\n" + "\n".join(missing) + "\n")

    # -- reading ------------------------------------------------------------

    def iter_files(self) -> Iterator[Path]:
        for path in sorted(self.root.rglob("*")):
            rel = path.relative_to(self.root)
            if rel.parts and rel.parts[0] in (".git", STATE_DIR):
                continue
            if path.is_file():
                yield path

    def load(self) -> MemoryRepo:
        repo = MemoryRepo()
        for path in self.iter_files():
            rel = path.relative_to(self.root).as_posix()
            if rel == ".gitignore":
                continue
            if path.suffix == ".md":
                repo.files[rel] = parse_file(rel, path.read_text(encoding="utf-8", errors="replace"))
            else:
                repo.other_files.append(rel)
        if ENTRY_FILE not in repo.files:
            repo.files[ENTRY_FILE] = parse_file(ENTRY_FILE, DEFAULT_MEMORY_MD)
        return repo

    # -- writing ------------------------------------------------------------

    def write_files(self, contents: dict[str, str | None]) -> list[str]:
        """Write planned contents (``None`` deletes). One writer per file, sequentially."""
        changed: list[str] = []
        for rel in sorted(contents):
            target = (self.root / rel).resolve()
            if self.root not in target.parents:
                raise StoreError(f"refusing to write outside the memory repo: {rel}")
            value = contents[rel]
            if value is None:
                if target.exists():
                    target.unlink()
                    changed.append(rel)
                continue
            old = target.read_text(encoding="utf-8") if target.exists() else None
            if old != value:
                atomic_write_text(target, value)
                changed.append(rel)
        return changed

    def read_state(self) -> dict[str, Any]:
        path = self.state_dir / "state.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StoreError(f"corrupt {path}: {exc}") from exc
        return data if isinstance(data, dict) else {}

    def write_state(self, state: dict[str, Any]) -> None:
        atomic_write_json(self.state_dir / "state.json", state)

    # -- git ----------------------------------------------------------------

    def current_branch(self) -> str:
        return git(self.root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    def head(self) -> str:
        return git(self.root, "rev-parse", "HEAD").stdout.strip()

    def is_clean(self) -> bool:
        return git(self.root, "status", "--porcelain").stdout.strip() == ""

    def dirty_paths(self) -> list[str]:
        out = git(self.root, "status", "--porcelain").stdout.splitlines()
        return [line[3:] for line in out if line.strip()]

    def commit_all(self, message: str) -> str | None:
        """Stage every change (respecting .gitignore) and commit; return the sha or None."""
        git(self.root, "add", "-A")
        if git(self.root, "diff", "--cached", "--quiet", check=False).returncode == 0:
            return None
        git(self.root, "commit", "-q", "-m", message)
        return self.head()

    def branches(self, pattern: str) -> list[str]:
        out = git(self.root, "branch", "--list", pattern, "--format=%(refname:short)").stdout
        return sorted(line.strip() for line in out.splitlines() if line.strip())

    def default_branch(self) -> str:
        state = self.read_state()
        name = state.get("default_branch")
        return str(name) if name else "main"

    def show(self, rev: str, path: str) -> str | None:
        proc = git(self.root, "show", f"{rev}:{path}", check=False)
        return proc.stdout if proc.returncode == 0 else None

    def rollback(self, rev: str) -> str | None:
        """Restore the tracked tree of ``rev`` as a new commit (history is kept)."""
        git(self.root, "rev-parse", "--verify", f"{rev}^{{commit}}")
        if not self.is_clean():
            raise StoreError("memory repo has uncommitted changes; commit or discard them first")
        git(self.root, "restore", f"--source={rev}", "--staged", "--worktree", ":/")
        return self.commit_all(f"reverie: rollback memory to {rev}")

    # -- lock ---------------------------------------------------------------

    @property
    def lock_path(self) -> Path:
        return self.root / ".git" / "reverie-dream.lock"

    @contextmanager
    def lock(self, stale_after_s: float = 6 * 3600) -> Iterator[None]:
        """Exclusive dream lock; a lock from a dead local process or older than ``stale_after_s`` is broken."""
        path = self.lock_path
        info = {"pid": os.getpid(), "host": socket.gethostname(), "ts": time.time()}
        for _ in range(2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._lock_is_stale(path, stale_after_s):
                    path.unlink(missing_ok=True)
                    continue
                raise LockHeld(f"another dream holds {path}; remove it if no dream is running") from None
            with os.fdopen(fd, "w") as handle:
                json.dump(info, handle)
            break
        else:  # pragma: no cover - only reachable in a race
            raise LockHeld(f"could not acquire {path}")
        try:
            yield
        finally:
            path.unlink(missing_ok=True)

    @staticmethod
    def _lock_is_stale(path: Path, stale_after_s: float) -> bool:
        try:
            info = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return True
        if time.time() - float(info.get("ts", 0)) > stale_after_s:
            return True
        if info.get("host") == socket.gethostname():
            pid = int(info.get("pid", 0))
            return pid > 0 and not _pid_alive(pid)
        return False


def _pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    if os.name == "nt":  # os.kill(pid, 0) would terminate the process on Windows
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
