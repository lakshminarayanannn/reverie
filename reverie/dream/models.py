"""Typed objects passed between dream stages."""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, Field

from reverie.core.schema import Note, NoteKind

Placement = Literal["memory_note", "instruction_edit", "evidence_only", "guard_proposal", "nowhere"]
Verdict = Literal["supported", "unsupported", "contradicted"]


class Chunk(BaseModel):
    session_id: str
    start_idx: int
    end_idx: int
    text: str
    #: True when this chunk ends the session (used to mark sessions fully dreamed).
    last: bool = True


class Selection(BaseModel):
    sessions: list[str] = Field(default_factory=list)
    already_dreamed: list[str] = Field(default_factory=list)
    recheck_note_ids: list[str] = Field(default_factory=list)
    chunks: list[Chunk] = Field(default_factory=list)


class CandidateOut(BaseModel):
    """Schema the extract prompt asks the model for."""

    text: str
    kind: NoteKind = "fact"
    evidence: list[str] = Field(default_factory=list)
    outcome_support: str | None = None
    verbatim: bool = False
    critical: bool = False
    command_pattern: str | None = None
    replacement: str | None = None
    placement: Literal["memory_note", "instruction_edit", "evidence_only"] = "memory_note"


class ExtractOut(BaseModel):
    candidates: list[CandidateOut] = Field(default_factory=list)


class Candidate(CandidateOut):
    cid: str
    session_id: str
    session_started: str = ""

    @staticmethod
    def make_id(session_id: str, text: str) -> str:
        return "c-" + hashlib.sha256(f"{session_id}\0{text}".encode()).hexdigest()[:8]


class VerifyOut(BaseModel):
    verdict: Verdict
    reason: str = ""
    supported_text: str | None = None


class VerifiedCandidate(BaseModel):
    candidate: Candidate
    verdict: Verdict
    reason: str = ""
    method: Literal["string_match", "string_match+llm", "llm", "llm_trimmed", "llm_contrast", "rule"] = "llm"
    contrast: bool = False
    #: Set when the candidate is dropped (and why); ``None`` means it survives.
    dropped: str | None = None


class ReconcileOut(BaseModel):
    relation: Literal["duplicate", "refines", "contradicts", "updates", "unrelated"]
    target_id: str | None = None
    merged_text: str | None = None
    stronger: Literal["new", "old", "unclear"] = "unclear"
    reason: str = ""


class Change(BaseModel):
    """One entry in PROPOSAL.md."""

    action: Literal["added", "merged", "changed", "contested", "superseded", "stale", "removed",
                    "promoted", "demoted", "rechecked", "dropped"]
    note_id: str | None = None
    text: str
    evidence: list[str] = Field(default_factory=list)
    detail: str = ""


class GuardProposal(BaseModel):
    id: str
    note_id: str
    pattern: str
    replacement: str
    mode: Literal["warn", "block"] = "warn"
    message: str
    evidence_sessions: list[str] = Field(default_factory=list)


class InstructionEdit(BaseModel):
    target: str = "CLAUDE.md"
    text: str
    evidence: list[str] = Field(default_factory=list)


class PlacementRecord(BaseModel):
    cid: str
    text: str
    placement: Placement
    reason: str = ""
    note_id: str | None = None


class OrganizeOut(BaseModel):
    class Assignment(BaseModel):
        id: str
        topic: str

    assignments: list[Assignment] = Field(default_factory=list)


class PromoteOut(BaseModel):
    general: list[str] = Field(default_factory=list)


class Layout(BaseModel):
    """Where every note lives after a dream: note id -> file path, plus the notes themselves."""

    notes: dict[str, Note] = Field(default_factory=dict)
    paths: dict[str, str] = Field(default_factory=dict)
    #: Notes removed entirely this dream (still in git history).
    removed: list[str] = Field(default_factory=list)
