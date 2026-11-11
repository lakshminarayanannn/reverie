"""Stage 4: verify candidates against only their cited evidence.

Deterministic checks run first and never need a model:

* evidence flagged as possible prompt injection -> dropped;
* destructive advice without user-message evidence -> dropped;
* re-derivable state (only read-only observations as evidence) -> dropped;
* verbatim / critical values -> checked by exact string match, not by a model.

Everything else gets a clean-context model call that sees the candidate and its cited
events only.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from pydantic import BaseModel, Field

from reverie.core.budget import BudgetExceeded
from reverie.core.schema import Event, EvidencePointer, Note, Session, parse_pointers, today
from reverie.dream.ingest import IngestResult
from reverie.dream.models import Candidate, VerifiedCandidate, VerifyOut
from reverie.dream.prompts import load_prompt
from reverie.dream.select import render_transcript
from reverie.providers.base import LLM, ProviderError

log = logging.getLogger("reverie.dream.verify")

SessionLookup = Callable[[str], Session | None]

DANGEROUS = re.compile(
    r"(?i)(\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r|\bmkfs\b|\bdd\s+if=|chmod\s+-R\s+777|"
    r"curl[^|\n]*\|\s*(sudo\s+)?(ba)?sh|wget[^|\n]*\|\s*(sudo\s+)?(ba)?sh|git\s+push\s+(-f|--force)|"
    r"\bdrop\s+(table|database)\b|--no-verify|disable\s+(the\s+)?(tests?|ci|checks?))")
READ_ONLY_TOOLS = {"read", "glob", "grep", "ls", "notebookread", "list", "view"}
READ_ONLY_CMD = re.compile(
    r"^\s*(ls|find|tree|cat|head|tail|pwd|wc|du|stat|file|git\s+(status|log|branch|show|diff|ls-files))\b")
_BACKTICK = re.compile(r"`([^`]+)`")


def evidence_events(ptrs: list[EvidencePointer], lookup: SessionLookup) -> list[tuple[Session, Event]]:
    out: list[tuple[Session, Event]] = []
    for p in ptrs:
        s = lookup(p.session_id)
        ev = s.event(p.idx) if s else None
        if s is not None and ev is not None:
            out.append((s, ev))
    return out


def paired_call(session: Session, ev: Event) -> Event | None:
    """The tool_call that produced a tool_result (nearest earlier call)."""
    if ev.type != "tool_result":
        return None
    for prev in reversed([e for e in session.events if e.idx < ev.idx]):
        if prev.type == "tool_call":
            return prev
    return None


def _is_read_only(session: Session, ev: Event) -> bool:
    call = ev if ev.type == "tool_call" else paired_call(session, ev)
    if call is None or ev.type not in ("tool_call", "tool_result"):
        return False
    if (call.tool_name or "").lower() in READ_ONLY_TOOLS:
        return True
    cmd = call.command()
    return bool(cmd and READ_ONLY_CMD.match(cmd) and "&&" not in cmd and ";" not in cmd)


def is_rederivable(cand: Candidate, evs: list[tuple[Session, Event]]) -> bool:
    if cand.kind != "fact" or cand.critical or not evs:
        return False
    return all(_is_read_only(s, e) for s, e in evs)


def evidence_text(evs: list[tuple[Session, Event]]) -> str:
    parts: list[str] = []
    for s, e in evs:
        call = paired_call(s, e)
        if call is not None:
            parts.append(call.render(100_000))
        parts.append(e.render(100_000))
    return "\n".join(parts)


def verbatim_ok(text: str, evidence: str) -> bool:
    spans = _BACKTICK.findall(text)
    norm_ev = " ".join(evidence.split())
    if spans:
        return all(span in evidence or " ".join(span.split()) in norm_ev for span in spans)
    return " ".join(text.split()) in norm_ev


def has_prose_claim(text: str) -> bool:
    """True when the text claims more than its quoted values (needs a semantic check)."""
    from reverie.dream.reconcile import tokens

    return len(tokens(_BACKTICK.sub(" ", text))) >= 3


def has_contrast(sessions: list[Session]) -> bool:
    labels = {s.outcome.label for s in sessions}
    return "pass" in labels and "fail" in labels


def rule_check(cand: Candidate, evs: list[tuple[Session, Event]], ingested: IngestResult) -> str | None:
    """Return a drop reason from deterministic rules, or None."""
    if not evs:
        return "evidence_not_found"
    for s, e in evs:
        if ingested.is_tainted(s.session_id, e.idx):
            return "evidence_tainted (possible prompt injection)"
        call = paired_call(s, e)
        if call is not None and ingested.is_tainted(s.session_id, call.idx):
            return "evidence_tainted (possible prompt injection)"
    if DANGEROUS.search(cand.text) and not any(e.type == "user_msg" for _, e in evs):
        return "destructive_advice_without_user_evidence"
    if cand.command_pattern and DANGEROUS.search(cand.replacement or ""):
        return "destructive_replacement"
    if is_rederivable(cand, evs):
        return "rederivable_from_environment"
    return None


class VerifyResult(BaseModel):
    verified: list[VerifiedCandidate] = Field(default_factory=list)
    rechecked: dict[str, str] = Field(default_factory=dict)  # note id -> verdict
    errors: list[str] = Field(default_factory=list)
    budget_exhausted: bool = False

    def survivors(self) -> list[VerifiedCandidate]:
        return [v for v in self.verified if v.dropped is None]


def llm_verdict(llm: LLM, model: str, text: str, evs: list[tuple[Session, Event]]) -> VerifyOut:
    system = load_prompt("verify").render()
    blocks = []
    for s, e in evs:
        shown = [x for x in (paired_call(s, e), e) if x is not None]
        blocks.append(render_transcript(s, shown, set(), note=' excerpt="cited evidence only"'))
    user = f"Candidate note:\n\"{text}\"\n\nCited evidence:\n\n" + "\n\n".join(blocks)
    return llm.complete_json(stage="verify", model=model, system=system, user=user, schema=VerifyOut,
                             max_tokens=1500)


def verify(candidates: list[Candidate], ingested: IngestResult, lookup: SessionLookup, llm: LLM | None,
           model: str) -> VerifyResult:
    result = VerifyResult()
    for cand in candidates:
        evs = evidence_events(parse_pointers(cand.evidence), lookup)
        sessions = list({s.session_id: s for s, _ in evs}.values())
        vc = VerifiedCandidate(candidate=cand, verdict="unsupported", contrast=has_contrast(sessions))
        reason = rule_check(cand, evs, ingested)
        if reason is not None:
            vc.method, vc.dropped, vc.reason = "rule", reason, reason
            result.verified.append(vc)
            continue
        if cand.verbatim or cand.critical:
            ok = verbatim_ok(cand.text, evidence_text(evs))
            vc.method = "string_match"
            vc.verdict = "supported" if ok else "unsupported"
            vc.reason = "verbatim values found in evidence" if ok else "verbatim values not found in evidence"
            if not ok:
                vc.dropped = "unsupported (verbatim mismatch)"
                result.verified.append(vc)
                continue
            vc.candidate = cand.model_copy(update={"verbatim": True})
            if not has_prose_claim(cand.text) or llm is None:
                # A pure quote is fully checked by the string match. Prose around quotes without a model
                # stays string-match-only and is reported as such.
                result.verified.append(vc)
                continue
        if llm is None:
            vc.dropped, vc.reason = "no_model_for_verification", "no model configured"
            result.verified.append(vc)
            continue
        try:
            out = llm_verdict(llm, model, cand.text, evs)
        except BudgetExceeded:
            result.budget_exhausted = True
            vc.dropped, vc.reason = "budget_exhausted", "budget exhausted before verification"
            result.verified.append(vc)
            break
        except ProviderError as exc:
            result.errors.append(f"verify {cand.cid}: {exc}")
            vc.dropped, vc.reason = "verify_error", str(exc)
            result.verified.append(vc)
            continue
        quoted = vc.method == "string_match"
        vc.verdict, vc.reason = out.verdict, out.reason
        vc.method = "string_match+llm" if quoted else "llm"
        if out.verdict == "unsupported" and out.supported_text:
            try:
                _try_trim(vc, out.supported_text, evs, llm, model)
            except BudgetExceeded:
                result.budget_exhausted = True
        if vc.verdict != "supported":
            vc.dropped = vc.verdict
        result.verified.append(vc)
    if llm is not None and not result.budget_exhausted:
        try:
            contrast_retry(result, lookup, llm, model)
        except BudgetExceeded:
            result.budget_exhausted = True
    return result


def _try_trim(vc: VerifiedCandidate, trimmed: str, evs: list[tuple[Session, Event]], llm: LLM, model: str) -> None:
    """Keep the part of a claim the evidence shows, after re-verifying it (the model must not invent)."""
    trimmed = " ".join(trimmed.split())
    cand = vc.candidate
    if not trimmed or trimmed == cand.text or len(trimmed) > len(cand.text) * 1.1:
        return
    if (cand.verbatim or cand.critical) and not verbatim_ok(trimmed, evidence_text(evs)):
        return
    try:
        again = llm_verdict(llm, model, trimmed, evs)
    except ProviderError:
        return
    if again.verdict == "supported":
        vc.candidate = cand.model_copy(update={"text": trimmed})
        vc.verdict, vc.method = "supported", "llm_trimmed"
        vc.reason = f"trimmed to the supported part; was: {cand.text}"


def contrast_retry(result: VerifyResult, lookup: SessionLookup, llm: LLM, model: str) -> None:
    """Re-verify an unsupported candidate together with a similar candidate from a session with the
    opposite outcome: a fail/pass contrast is only visible across sessions."""
    from reverie.dream.reconcile import overlap, tokens

    def label(vc: VerifiedCandidate) -> str | None:
        s = lookup(vc.candidate.session_id)
        return s.outcome.label if s and s.outcome.label in ("pass", "fail") else None

    for vc in result.verified:
        if vc.dropped not in ("unsupported",) or vc.method == "rule" or label(vc) is None:
            continue
        mine = tokens(vc.candidate.text)
        partners = [p for p in result.verified if p is not vc and label(p) not in (None, label(vc))
                    and overlap(tokens(p.candidate.text), mine) >= 0.3]
        for partner in partners:
            evidence = list(dict.fromkeys(vc.candidate.evidence + partner.candidate.evidence))
            evs = evidence_events(parse_pointers(evidence), lookup)
            try:
                out = llm_verdict(llm, model, vc.candidate.text, evs)
            except ProviderError as exc:
                result.errors.append(f"contrast retry {vc.candidate.cid}: {exc}")
                break
            if out.verdict == "supported":
                vc.candidate = vc.candidate.model_copy(update={"evidence": evidence})
                vc.verdict, vc.dropped, vc.contrast, vc.method = "supported", None, True, "llm_contrast"
                vc.reason = f"supported by the pass/fail contrast with {partner.candidate.session_id}: {out.reason}"
                break


def recheck_notes(notes: list[Note], lookup: SessionLookup, llm: LLM | None, model: str,
                  ingested: IngestResult, result: VerifyResult) -> None:
    """Re-verify existing notes against their sources; sets ``result.rechecked``."""
    for note in notes:
        evs = evidence_events(parse_pointers(note.sources), lookup)
        if not evs:
            result.rechecked[note.id] = "sources_unavailable"
            continue
        if any(ingested.is_tainted(s.session_id, e.idx) for s, e in evs):
            result.rechecked[note.id] = "contradicted"
            continue
        try:
            result.rechecked[note.id] = check_note_text(note.text, note.quote, evs, llm, model)
        except BudgetExceeded:
            result.budget_exhausted = True
            return
        except ProviderError as exc:
            result.errors.append(f"recheck {note.id}: {exc}")
            result.rechecked[note.id] = "not_checked"


def check_note_text(text: str, quote: bool, evs: list[tuple[Session, Event]], llm: LLM | None, model: str) -> str:
    """Verdict for stored text: quoted values by string match, any prose claim by the model."""
    if quote:
        if not verbatim_ok(text, evidence_text(evs)):
            return "unsupported"
        if not has_prose_claim(text):
            return "supported"
    if llm is None:
        return "supported" if quote else "not_checked"
    return llm_verdict(llm, model, text, evs).verdict


def apply_rechecks(notes: dict[str, Note], rechecked: dict[str, str]) -> list[tuple[str, str]]:
    """Update note statuses from recheck verdicts; returns (note id, new status) changes."""
    changes: list[tuple[str, str]] = []
    for note_id, verdict in rechecked.items():
        note = notes.get(note_id)
        if note is None:
            continue
        if verdict == "supported":
            note.verified = today()
            if note.status == "contested" and "contested_by" not in note.extra:
                note.status = "verified"
                changes.append((note_id, "verified"))
        elif verdict in ("unsupported", "contradicted") and note.status != "contested":
            note.status = "contested"
            note.extra["reason"] = f"re-verification: {verdict}"
            changes.append((note_id, "contested"))
    return changes
