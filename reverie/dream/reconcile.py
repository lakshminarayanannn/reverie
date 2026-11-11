"""Stage 5: reconcile verified candidates with existing memory.

Candidates are processed in chronological order of their session, each against the
current note set (existing notes plus notes added earlier in this dream), so a
contradiction inside one batch of sessions is handled the same way as one across dreams.

* exact or near duplicates merge deterministically, keeping all sources;
* overlapping notes go to the model, which names the relation (duplicate / refines /
  contradicts / updates / unrelated); the *resolution* is decided by evidence:
  newer verified evidence wins and the old note is retired (kept in the archive and in
  git history); equal or unknown recency leaves both ``contested``.
"""

from __future__ import annotations

import logging
import re

from pydantic import BaseModel, Field

from reverie.core.budget import BudgetExceeded
from reverie.core.schema import LIVE_STATUSES, Note, NoteStatus, new_note_id, normalize_text, today
from reverie.dream.models import Change, ReconcileOut, VerifiedCandidate
from reverie.dream.prompts import load_prompt
from reverie.providers.base import LLM, ProviderError

log = logging.getLogger("reverie.dream.reconcile")

_STOP = ("the a an and or of to in on for with is are was were be been it this that these those use uses "
         "used using when then than not no from by as at into instead before after should must always never "
         "run runs")
STOPWORDS = frozenset(_STOP.split())
NEAR_DUP = 0.85
RELATED = 0.2


def tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[\s]+", normalize_text(text)) if len(t) >= 3 and t not in STOPWORDS}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


class ReconcileResult(BaseModel):
    notes: dict[str, Note] = Field(default_factory=dict)
    changes: list[Change] = Field(default_factory=list)
    #: candidate id -> note id it became / merged into.
    placed: dict[str, str] = Field(default_factory=dict)
    new_ids: list[str] = Field(default_factory=list)
    contested_pairs: list[tuple[str, str]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    budget_exhausted: bool = False


def _order_key(v: VerifiedCandidate) -> tuple[str, str, int, str]:
    """Chronological: session start, then position of the first cited event in the session."""
    c = v.candidate
    first = min((int(e.split("#")[1]) for e in c.evidence if "#" in e and e.split("#")[1].isdigit()), default=0)
    return (c.session_started, c.session_id, first, c.cid)


def note_recency(note: Note, session_times: dict[str, str]) -> str:
    times = [session_times.get(sid, "") for sid in note.source_sessions()]
    return max(times) if times else ""


def reconcile(survivors: list[VerifiedCandidate], existing: dict[str, Note], session_times: dict[str, str],
              llm: LLM | None, model: str, rejected: set[str],
              outcomes: dict[str, str] | None = None) -> ReconcileResult:
    res = ReconcileResult(notes={k: v.model_copy(deep=True) for k, v in existing.items()})
    res_outcomes = outcomes or {}
    ordered = sorted(survivors, key=_order_key)
    for vc in ordered:
        cand = vc.candidate
        fingerprint = normalize_text(cand.text)
        if fingerprint in rejected:
            res.changes.append(Change(action="dropped", text=cand.text, evidence=cand.evidence,
                                      detail="previously rejected by the user"))
            continue
        live = {nid: n for nid, n in res.notes.items() if n.status in LIVE_STATUSES}
        ctoks = tokens(cand.text)
        # 1) deterministic duplicates
        dup = next((n for n in live.values() if normalize_text(n.text) == fingerprint), None)
        if dup is None:
            dup = next((n for n in live.values() if jaccard(tokens(n.text), ctoks) >= NEAR_DUP), None)
        if dup is not None:
            _merge(res, dup, vc, "duplicate (deterministic)", outcomes=res_outcomes)
            continue
        # 2) model-judged relation with overlapping notes
        related = sorted(((overlap(tokens(n.text), ctoks), n) for n in live.values()),
                         key=lambda x: (-x[0], x[1].id))
        related = [(s, n) for s, n in related if s >= RELATED][:5]
        out: ReconcileOut | None = None
        if related and llm is not None and not res.budget_exhausted:
            try:
                out = _ask(llm, model, vc, [n for _, n in related], session_times)
            except BudgetExceeded:
                res.budget_exhausted = True
            except ProviderError as exc:
                res.errors.append(f"reconcile {cand.cid}: {exc}")
        target = res.notes.get(out.target_id) if out and out.target_id else None
        if out is None or out.relation == "unrelated" or target is None or target.status not in LIVE_STATUSES:
            _add(res, vc)
            continue
        if out.relation == "duplicate":
            _merge(res, target, vc, "duplicate (model)", outcomes=res_outcomes)
        elif out.relation == "refines":
            old_text = target.text
            limit = max(int(1.3 * max(len(old_text), len(cand.text))), 240)
            if out.merged_text and len(out.merged_text) <= limit:
                target.text = out.merged_text
                _merge(res, target, vc, "refined", changed_from=old_text, outcomes=res_outcomes)
            else:
                why = "refines (merged text too long; kept existing wording)" if out.merged_text else "refines"
                _merge(res, target, vc, why, outcomes=res_outcomes)
        else:
            _resolve(res, target, vc, out, session_times)
    return res


def _ask(llm: LLM, model: str, vc: VerifiedCandidate, related: list[Note], times: dict[str, str]) -> ReconcileOut:
    system = load_prompt("reconcile").render()
    new_time = vc.candidate.session_started
    lines = [f'New note (newest evidence {new_time or "unknown"}):\n"{vc.candidate.text}"', "", "Existing notes:"]
    for n in related:
        lines.append(f'- id={n.id} status={n.status} newest evidence {note_recency(n, times) or "unknown"}: "{n.text}"')
    return llm.complete_json(stage="reconcile", model=model, system=system, user="\n".join(lines),
                             schema=ReconcileOut, max_tokens=1500)


def _new_note(vc: VerifiedCandidate, existing: dict[str, Note]) -> Note:
    cand = vc.candidate
    nid = new_note_id(cand.text, cand.evidence[0])
    salt = 0
    while nid in existing:
        salt += 1
        nid = new_note_id(cand.text, f"{cand.evidence[0]}#{salt}")
    extra: dict[str, str] = {}
    if vc.contrast:
        extra["contrast"] = "true"
    return Note(id=nid, text=cand.text, kind=cand.kind, status="verified", sources=list(cand.evidence),
                added=today(), verified=today(), quote=cand.verbatim, extra=extra)


def _add(res: ReconcileResult, vc: VerifiedCandidate) -> Note:
    note = _new_note(vc, res.notes)
    res.notes[note.id] = note
    res.new_ids.append(note.id)
    res.placed[vc.candidate.cid] = note.id
    res.changes.append(Change(action="added", note_id=note.id, text=note.text, evidence=note.sources,
                              detail=f"verified by {vc.method}" + ("; pass/fail contrast" if vc.contrast else "")))
    return note


def _merge(res: ReconcileResult, target: Note, vc: VerifiedCandidate, why: str, changed_from: str | None = None,
           outcomes: dict[str, str] | None = None) -> None:
    before = len(target.source_sessions())
    target.add_sources(vc.candidate.evidence)
    target.verified = today()
    labels = {(outcomes or {}).get(sid) for sid in target.source_sessions()}
    if vc.contrast or {"pass", "fail"} <= labels:
        target.extra["contrast"] = "true"
    if changed_from is not None:
        target.quote = False  # merged wording is no longer a verbatim quote
    if len(target.source_sessions()) >= 2 and target.status == "verified" and target.id not in res.new_ids:
        target.status = "active"
    res.placed[vc.candidate.cid] = target.id
    action = "changed" if changed_from is not None else "merged"
    detail = why + (f"; was: {changed_from}" if changed_from else "") + \
        f"; sessions {before}->{len(target.source_sessions())}"
    res.changes.append(Change(action=action, note_id=target.id, text=target.text, evidence=vc.candidate.evidence,
                              detail=detail))


def _resolve(res: ReconcileResult, old: Note, vc: VerifiedCandidate, out: ReconcileOut,
             times: dict[str, str]) -> None:
    new_time = vc.candidate.session_started
    old_time = note_recency(old, times)
    if new_time and old_time and new_time != old_time:
        newer_is_new = new_time > old_time
    elif out.stronger in ("new", "old"):
        newer_is_new = out.stronger == "new"
    else:
        newer_is_new = None
    retired_status: NoteStatus = "superseded" if out.relation == "contradicts" else "stale"
    if newer_is_new is True:
        note = _add(res, vc)
        note.extra["replaces"] = old.id
        old.status = retired_status
        old.extra["superseded_by"] = note.id
        res.changes.append(Change(action=retired_status, note_id=old.id, text=old.text, evidence=old.sources,
                                  detail=f"{out.relation} by newer evidence in {note.id}: {out.reason}"))
    elif newer_is_new is False:
        res.changes.append(Change(action="dropped", text=vc.candidate.text, evidence=vc.candidate.evidence,
                                  detail=f"{out.relation} existing note {old.id}, which has newer evidence"))
    else:
        note = _add(res, vc)
        for a, b in ((note, old), (old, note)):
            a.status = "contested"
            a.extra["contested_by"] = b.id
        res.contested_pairs.append((old.id, note.id))
        res.changes.append(Change(action="contested", note_id=old.id, text=old.text, evidence=old.sources,
                                  detail=f"conflicts with {note.id}; evidence cannot decide: {out.reason}"))
