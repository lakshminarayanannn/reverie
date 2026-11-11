"""Typed data models shared by every stage.

Two families live here:

* the canonical **session** format (``Session``, ``Event``, ``Outcome``) that every
  adapter produces, and
* the **memory** model (``Note``, ``MemoryFile``, ``MemoryRepo``) that mirrors an
  Agent Memory Repo on disk.

Memory files are parsed losslessly: every line that is not a reverie-managed note is
kept verbatim as a ``RawLine`` so that rewriting a file never drops user content or
unknown metadata keys.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator

SCHEMA_VERSION = "1"

EventType = Literal["user_msg", "assistant_msg", "tool_call", "tool_result", "system"]
OutcomeLabel = Literal["pass", "fail", "unknown"]
OutcomeSource = Literal["verifier", "tests", "edit_survival", "user_correction", "none"]
NoteStatus = Literal[
    "candidate", "verified", "active", "contested", "superseded", "stale", "rejected", "archived"
]
NoteKind = Literal["rule", "trap", "preference", "decision", "fact"]

NOTE_STATUSES: tuple[str, ...] = (
    "candidate", "verified", "active", "contested", "superseded", "stale", "rejected", "archived",
)
NOTE_KINDS: tuple[str, ...] = ("rule", "trap", "preference", "decision", "fact")
#: Statuses whose notes live in topic files / MEMORY.md (the "live" layer).
LIVE_STATUSES: frozenset[str] = frozenset({"verified", "active", "contested"})
#: Statuses whose notes are retired to the archive file.
RETIRED_STATUSES: frozenset[str] = frozenset({"superseded", "stale", "rejected", "archived"})

DEFAULT_TOOL_OUTPUT_LIMIT = 8000


def truncate_middle(text: str, limit: int = DEFAULT_TOOL_OUTPUT_LIMIT) -> str:
    """Keep the head and tail of ``text`` so it is at most ~``limit`` characters."""
    if limit <= 0 or len(text) <= limit:
        return text
    marker = f"\n...[truncated {len(text) - limit} chars]...\n"
    keep = max(limit - len(marker), 2)
    head = keep // 2
    tail = keep - head
    return text[:head] + marker + text[-tail:]


class Event(BaseModel):
    """One step of a session. ``idx`` is stable and used in evidence pointers."""

    model_config = ConfigDict(extra="ignore")

    idx: int = Field(ge=0)
    ts: str | None = None
    type: EventType
    text: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_output: str | None = None
    exit_code: int | None = None
    status: Literal["ok", "error"] | None = None
    #: The source format's own id for this event (e.g. a Claude Code uuid), if any.
    source_event_id: str | None = None

    def is_failure(self) -> bool:
        return self.status == "error" or (self.exit_code is not None and self.exit_code != 0)

    def command(self) -> str | None:
        """Shell command of a tool call, if this event is one."""
        if self.tool_input is None:
            return None
        for key in ("command", "cmd"):
            value = self.tool_input.get(key)
            if isinstance(value, str):
                return value
        return None

    def render(self, max_chars: int = 2000) -> str:
        """Human-readable one-block rendering used inside prompts and reports."""
        parts: list[str] = []
        if self.text:
            parts.append(self.text)
        if self.tool_name:
            parts.append(f"tool={self.tool_name}")
        if self.tool_input:
            cmd = self.command()
            if cmd is not None:
                parts.append(f"command: {cmd}")
            else:
                import json

                parts.append("input: " + json.dumps(self.tool_input, sort_keys=True)[:max_chars])
        if self.tool_output:
            parts.append("output: " + truncate_middle(self.tool_output, max_chars))
        if self.exit_code is not None:
            parts.append(f"exit_code={self.exit_code}")
        if self.status:
            parts.append(f"status={self.status}")
        return "\n".join(parts)


class Outcome(BaseModel):
    label: OutcomeLabel = "unknown"
    source: OutcomeSource = "none"
    detail: str | None = None


class Session(BaseModel):
    """A session in reverie's canonical format (``schema_version`` 1)."""

    model_config = ConfigDict(extra="ignore")

    schema_version: str = SCHEMA_VERSION
    session_id: str = Field(min_length=1)
    agent: str = "generic"
    started_at: str
    ended_at: str | None = None
    cwd: str | None = None
    events: list[Event] = Field(default_factory=list)
    outcome: Outcome = Field(default_factory=Outcome)

    @field_validator("schema_version")
    @classmethod
    def _known_version(cls, value: str) -> str:
        if value != SCHEMA_VERSION:
            raise ValueError(f"unsupported canonical schema_version {value!r}; expected {SCHEMA_VERSION!r}")
        return value

    def event(self, idx: int) -> Event | None:
        for ev in self.events:
            if ev.idx == idx:
                return ev
        return None

    def content_hash(self) -> str:
        """Hash of the session content; changes when a resumed session gains events."""
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()

    def text_bytes(self) -> int:
        total = 0
        for ev in self.events:
            for part in (ev.text, ev.tool_output, ev.command()):
                if part:
                    total += len(part.encode())
        return total


# ---------------------------------------------------------------------------
# Evidence pointers
# ---------------------------------------------------------------------------

_POINTER_RE = re.compile(r"^(?P<sid>[^#\s,;\]]+)#(?P<idx>\d+)$")


class EvidencePointer(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str
    idx: int

    @classmethod
    def parse(cls, raw: str) -> EvidencePointer:
        match = _POINTER_RE.match(raw.strip())
        if not match:
            raise ValueError(f"not an evidence pointer (want session_id#idx): {raw!r}")
        return cls(session_id=match["sid"], idx=int(match["idx"]))

    def __str__(self) -> str:
        return f"{self.session_id}#{self.idx}"


def parse_pointers(raw: list[str]) -> list[EvidencePointer]:
    out: list[EvidencePointer] = []
    for item in raw:
        try:
            out.append(EvidencePointer.parse(item))
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------------------
# Memory model
# ---------------------------------------------------------------------------

#: Metadata keys reverie writes, in output order. Unknown keys keep their own order after these.
KNOWN_META_ORDER: tuple[str, ...] = (
    "id", "kind", "status", "source", "added", "verified", "quote", "contrast", "replaces", "reason",
)


def new_note_id(text: str, salt: str = "") -> str:
    digest = hashlib.sha256((normalize_text(text) + "\0" + salt).encode()).hexdigest()
    return "n-" + digest[:6]


def normalize_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[`'\"*_]", "", text)
    text = re.sub(r"[^a-z0-9./:=@+\- ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def today() -> str:
    return date.today().isoformat()


class Note(BaseModel):
    """One memory entry: a single bullet line with ``[key: value; ...]`` metadata."""

    model_config = ConfigDict(validate_assignment=True)

    id: str
    text: str
    kind: NoteKind | None = None
    status: NoteStatus = "verified"
    sources: list[str] = Field(default_factory=list)
    added: str = Field(default_factory=today)
    verified: str | None = None
    quote: bool = False
    #: Unknown / extra metadata keys preserved verbatim and in order.
    extra: dict[str, str] = Field(default_factory=dict)
    #: Indentation prefix of the bullet (kept for round-trips).
    indent: str = ""
    #: (original line, canonical render at parse time): unchanged notes re-render byte-identically.
    _origin: tuple[str, str] | None = PrivateAttr(default=None)

    def remember_origin(self, line: str) -> None:
        self._origin = (line, self._canonical())

    @field_validator("text")
    @classmethod
    def _one_line(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("note text must not be empty")
        return value

    def source_sessions(self) -> list[str]:
        out: list[str] = []
        for src in self.sources:
            sid = src.split("#", 1)[0]
            if sid not in out:
                out.append(sid)
        return out

    def add_sources(self, more: list[str]) -> None:
        merged = list(self.sources)
        for src in more:
            if src not in merged:
                merged.append(src)
        self.sources = merged

    def metadata(self) -> dict[str, str]:
        meta: dict[str, str] = {"id": self.id}
        if self.kind:
            meta["kind"] = self.kind
        meta["status"] = self.status
        if self.sources:
            meta["source"] = ", ".join(self.sources)
        meta["added"] = self.added
        if self.verified:
            meta["verified"] = self.verified
        if self.quote:
            meta["quote"] = "true"
        for key, value in self.extra.items():
            if key not in meta:
                meta[key] = value
        return meta

    def render(self) -> str:
        canonical = self._canonical()
        if self._origin is not None and self._origin[1] == canonical:
            return self._origin[0]
        return canonical

    def _canonical(self) -> str:
        meta = "; ".join(f"{k}: {v}" for k, v in self.metadata().items())
        text = self.text
        # A trailing "[...]" in the text itself would be mistaken for metadata; escape it.
        if text.endswith("]"):
            text = text + " ."
        return f"{self.indent}- {text} [{meta}]"

    def render_compact(self, provenance: bool = True) -> str:
        """Short form used for injection: text, then where it came from and when it was last checked."""
        if not provenance:
            return f"- {self.text} ({self.id})"
        n = len(self.source_sessions())
        when = self.verified or self.added
        return (f"- {self.text} (from {n} past session{'s' if n != 1 else ''} here, checked against their "
                f"transcripts {when}; {self.id})")


class RawLine(BaseModel):
    """A line of a memory file that reverie does not manage; kept verbatim."""

    text: str


class NoteLine(BaseModel):
    note: Note


Line = Annotated[RawLine | NoteLine, Field(union_mode="left_to_right")]


class MemoryFile(BaseModel):
    """A Markdown file of a memory repo, parsed into raw lines and notes."""

    path: str
    lines: list[RawLine | NoteLine] = Field(default_factory=list)

    def notes(self) -> list[Note]:
        return [line.note for line in self.lines if isinstance(line, NoteLine)]

    def render(self) -> str:
        out = [line.note.render() if isinstance(line, NoteLine) else line.text for line in self.lines]
        text = "\n".join(out)
        return text + "\n" if text and not text.endswith("\n") else text


class MemoryRepo(BaseModel):
    """All Markdown files of a memory repo keyed by POSIX path relative to the root."""

    files: dict[str, MemoryFile] = Field(default_factory=dict)
    #: Non-Markdown files (paths only); reverie never edits these.
    other_files: list[str] = Field(default_factory=list)

    def all_notes(self) -> list[tuple[str, Note]]:
        out: list[tuple[str, Note]] = []
        for path in sorted(self.files):
            for note in self.files[path].notes():
                out.append((path, note))
        return out

    def find(self, note_id: str) -> tuple[str, Note] | None:
        for path, note in self.all_notes():
            if note.id == note_id:
                return path, note
        return None

    def total_bytes(self) -> int:
        return sum(len(f.render().encode()) for f in self.files.values())
