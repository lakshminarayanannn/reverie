"""Stage 3: extract candidate notes from each transcript chunk."""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from reverie.core.budget import BudgetExceeded
from reverie.core.schema import EvidencePointer, Session
from reverie.dream.models import Candidate, Chunk, ExtractOut
from reverie.dream.prompts import load_prompt
from reverie.providers.base import LLM, ProviderError

log = logging.getLogger("reverie.dream.extract")


class ExtractResult(BaseModel):
    candidates: list[Candidate] = Field(default_factory=list)
    #: Sessions whose every chunk was processed (safe to mark dreamed).
    completed_sessions: list[str] = Field(default_factory=list)
    dropped: list[dict[str, str]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    budget_exhausted: bool = False


def clean_evidence(raw: list[str], session: Session, chunk: Chunk) -> list[str]:
    """Keep only pointers to real events of this session (inside this chunk)."""
    out: list[str] = []
    for item in raw:
        try:
            ptr = EvidencePointer.parse(item)
        except ValueError:
            continue
        if ptr.session_id != session.session_id:
            continue
        if not (chunk.start_idx <= ptr.idx <= chunk.end_idx) or session.event(ptr.idx) is None:
            continue
        if str(ptr) not in out:
            out.append(str(ptr))
    return out


def extract(chunks: list[Chunk], sessions: dict[str, Session], llm: LLM, model: str, purpose: str) -> ExtractResult:
    prompt = load_prompt("extract")
    system = prompt.render(purpose=purpose)
    result = ExtractResult()
    failed_sessions: set[str] = set()
    seen: set[str] = set()
    for chunk in chunks:
        session = sessions[chunk.session_id]
        user = ("Propose candidate memory notes from this session. Cite evidence as "
                f"\"{session.session_id}#<idx>\".\n\n{chunk.text}")
        try:
            out = llm.complete_json(stage="extract", model=model, system=system, user=user, schema=ExtractOut)
        except BudgetExceeded:
            result.budget_exhausted = True
            failed_sessions.add(chunk.session_id)
            break
        except ProviderError as exc:
            msg = f"extract {chunk.session_id} events {chunk.start_idx}-{chunk.end_idx}: {exc}"
            log.error(msg)
            result.errors.append(msg)
            failed_sessions.add(chunk.session_id)
            continue
        for cand in out.candidates:
            evidence = clean_evidence(cand.evidence, session, chunk)
            if not evidence:
                result.dropped.append({"text": cand.text, "reason": "no_valid_evidence"})
                continue
            cid = Candidate.make_id(session.session_id, cand.text)
            if cid in seen:  # overlapping chunks propose the same note twice
                continue
            seen.add(cid)
            result.candidates.append(Candidate(
                **{**cand.model_dump(), "evidence": evidence}, cid=cid, session_id=session.session_id,
                session_started=session.started_at,
            ))
        if chunk.last and chunk.session_id not in failed_sessions:
            result.completed_sessions.append(chunk.session_id)
    return result
