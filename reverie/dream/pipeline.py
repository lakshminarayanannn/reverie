"""Dream orchestration: ingest -> select -> extract -> verify -> reconcile -> organize ->
promote -> prune -> emit.

The dream makes no tool calls: it works only from transcript text. Every intermediate
artifact is written to ``<runs dir>/<run id>/``. Memory changes are committed on a new
branch ``dream/<run id>`` and merged only with ``auto_accept``.
"""

from __future__ import annotations

import json
import logging
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from reverie.core.budget import Budget
from reverie.core.config import ReverieConfig, experience_db_path
from reverie.core.experience import ExperienceStore
from reverie.core.fsutil import atomic_write_json, atomic_write_text
from reverie.core.schema import (
    LIVE_STATUSES,
    RETIRED_STATUSES,
    MemoryFile,
    Note,
    Session,
    normalize_text,
    parse_pointers,
)
from reverie.core.store import ENTRY_FILE, MemoryStore, StoreError, git, parse_file
from reverie.dream import guards as guards_mod
from reverie.dream.extract import ExtractResult, extract
from reverie.dream.ingest import IngestResult, ingest
from reverie.dream.models import Change, VerifiedCandidate
from reverie.dream.organize import (
    ARCHIVE_FILE,
    DEFAULT_TOPICS,
    assign_topics,
    build_memory_md,
    fix_dangling_links,
    placements,
    plan_writes,
    topic_files_with_live_notes,
)
from reverie.dream.promote import eligible, fit_to_cap, judge_general, rank_key
from reverie.dream.prompts import load_purpose, prompt_versions
from reverie.dream.proposal import guards_to_toml, render_proposal
from reverie.dream.prune import prune
from reverie.dream.reconcile import ReconcileResult, reconcile
from reverie.dream.select import select, state_key
from reverie.dream.verify import (
    VerifyResult,
    apply_rechecks,
    check_note_text,
    evidence_events,
    recheck_notes,
    verify,
)
from reverie.eval.metrics import memory_metrics
from reverie.online.usage import read_usage, usage_stats
from reverie.providers import LLM, Provider, ProviderUnavailable, build_provider

log = logging.getLogger("reverie.dream")

MARKER = "reverie-dream-inprogress.json"
STATE_VERSION = 1


class PendingProposal(StoreError):
    """A previous dream's proposal has not been approved or rejected yet."""


class DreamOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sessions: Path
    memory: Path
    purpose: Path | None = None
    budget_usd: float | None = None
    auto_accept: bool = False
    use_cache: bool = True
    allow_cloud: bool = False
    adapter: str = "auto"
    runs_dir: Path | None = None
    run_id: str | None = None


class DreamReport(BaseModel):
    run_id: str
    status: str = "ok"  # ok | no_changes | partial_budget
    llm_available: bool = True
    provider: str = "none"
    branch: str | None = None
    commit: str | None = None
    merged: bool = False
    run_dir: str = ""
    counts: dict[str, int] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


def new_run_id(now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    return now.strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


def recover_interrupted(store: MemoryStore) -> str | None:
    """Undo a dream that died mid-write. Returns a description of what was done."""
    marker = store.root / ".git" / MARKER
    if not marker.exists():
        return None
    try:
        info = json.loads(marker.read_text())
    except json.JSONDecodeError:
        info = {}
    base = info.get("base_branch") or store.default_branch()
    branch = info.get("branch")
    git(store.root, "checkout", "-q", "-f", base, check=False)
    git(store.root, "clean", "-q", "-fd", "--", ".", ":!.reverie/usage.jsonl", check=False)
    if branch:
        git(store.root, "branch", "-q", "-D", branch, check=False)
    marker.unlink(missing_ok=True)
    msg = f"recovered from an interrupted dream: restored {base}" + (f", deleted {branch}" if branch else "")
    log.warning(msg)
    return msg


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


def pending_dreamed(store: MemoryStore) -> dict[str, str]:
    """Sessions already covered by proposals awaiting review (so they are not re-proposed)."""
    out: dict[str, str] = {}
    for branch in store.branches("dream/*"):
        text = store.show(branch, ".reverie/state.json")
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        out.update(data.get("dreamed", {}))
    return out


def load_guards(store: MemoryStore) -> list[dict[str, Any]]:
    path = store.root / "guards.toml"
    if not path.exists():
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise StoreError(f"invalid guards.toml: {exc}") from exc
    guards = data.get("guard", [])
    return [g for g in guards if isinstance(g, dict)]


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def run_dream(opts: DreamOptions, cfg: ReverieConfig, *, provider: Provider | None = None,
              notify: Callable[[str], None] = print,
              fail_hook: Callable[[str], None] | None = None) -> DreamReport:
    """Run one dream. ``provider`` overrides config (tests); ``fail_hook(stage)`` lets tests inject crashes."""
    store = MemoryStore(opts.memory)
    store.require()
    run_id = opts.run_id or new_run_id()
    report = DreamReport(run_id=run_id)
    recovered = recover_interrupted(store)
    if recovered:
        report.warnings.append(recovered)
    with store.lock():
        if not store.is_clean():
            raise StoreError("memory repo has uncommitted changes: " + ", ".join(store.dirty_paths()[:5]))
        state = store.read_state()
        base_branch = store.current_branch()
        if base_branch.startswith("dream/"):
            raise StoreError(f"currently on proposal branch {base_branch}; check out the default branch first")
        pending = store.branches("dream/*")
        if pending:
            raise PendingProposal(
                f"proposal(s) awaiting review: {', '.join(pending)}; run `reverie proposals approve|reject <id>` "
                "first (or dream with --auto-accept)")
        state.setdefault("default_branch", base_branch)
        return _run_locked(opts, cfg, store, state, base_branch, report, provider, notify, fail_hook)


def _hook(fail_hook: Callable[[str], None] | None, stage: str) -> None:
    if fail_hook is not None:
        fail_hook(stage)


def _run_locked(opts: DreamOptions, cfg: ReverieConfig, store: MemoryStore, state: dict[str, Any],
                base_branch: str, report: DreamReport, provider: Provider | None,
                notify: Callable[[str], None], fail_hook: Callable[[str], None] | None) -> DreamReport:
    run_id = report.run_id
    runs_root = opts.runs_dir or (Path(cfg.dream.runs_dir) if cfg.dream.runs_dir else store.state_dir / "runs")
    run_dir = Path(runs_root) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    report.run_dir = str(run_dir)
    purpose = load_purpose(opts.purpose or (Path(cfg.dream.purpose_file) if cfg.dream.purpose_file else None))
    budget = Budget(max_tokens=cfg.budget.max_tokens,
                    max_usd=opts.budget_usd if opts.budget_usd is not None else cfg.budget.max_usd)

    # 1. ingest & govern ------------------------------------------------------------
    ingested = ingest(opts.sessions, cfg, adapter=opts.adapter)
    report.warnings.extend(ingested.adapter_report.errors)
    exp = ExperienceStore(experience_db_path(cfg))
    try:
        for s in ingested.sessions:
            exp.import_session(s, source_path=str(opts.sessions))
        if cfg.store.retention_days:
            from datetime import timedelta

            cutoff = (datetime.now(UTC) - timedelta(days=cfg.store.retention_days)).isoformat()
            dropped = exp.apply_retention(cutoff)
            if dropped:
                report.warnings.append(f"retention: removed {dropped} session(s) older than "
                                       f"{cfg.store.retention_days} days from the experience store")
        _write_json(run_dir / "ingest.json", {
            "adapter": ingested.adapter_report.model_dump(), "redactions": ingested.redactions,
            "corpus_bytes": ingested.corpus_bytes, "tainted": ingested.tainted,
            "sessions": [s.session_id for s in ingested.sessions]})
        _hook(fail_hook, "ingest")
        by_id = ingested.by_id()

        def lookup(sid: str) -> Session | None:
            return by_id.get(sid) or exp.find_session(sid)

        return _dream(opts, cfg, store, state, base_branch, report, provider, notify, fail_hook, run_dir,
                      purpose, budget, ingested, lookup)
    finally:
        exp.close()


def _dream(opts: DreamOptions, cfg: ReverieConfig, store: MemoryStore, state: dict[str, Any], base_branch: str,
           report: DreamReport, provider: Provider | None, notify: Callable[[str], None],
           fail_hook: Callable[[str], None] | None, run_dir: Path, purpose: str, budget: Budget,
           ingested: IngestResult, lookup: Callable[[str], Session | None]) -> DreamReport:
    run_id = report.run_id
    repo = store.load()
    notes: dict[str, Note] = {}
    paths: dict[str, str] = {}
    for path, note in repo.all_notes():
        if note.id in notes:
            report.warnings.append(f"duplicate note id {note.id} in {path}; keeping the first")
            continue
        notes[note.id] = note
        paths[note.id] = path
    dream_no = int(state.get("dream_count", 0) or 0) + 1
    note_state: dict[str, dict[str, Any]] = state.setdefault("notes", {})

    # 2. select ------------------------------------------------------------------------
    sel_state = dict(state)
    sel_state["dreamed"] = {**pending_dreamed(store), **state.get("dreamed", {})}
    selection = select(ingested, sel_state, repo, chunk_chars=cfg.dream.chunk_chars,
                       overlap=cfg.dream.chunk_overlap_events, recheck_after=cfg.dream.recheck_after_dreams)
    _write_json(run_dir / "selection.json", selection.model_dump(exclude={"chunks"}) |
                {"chunks": [c.model_dump(exclude={"text"}) for c in selection.chunks]})

    # LLM ------------------------------------------------------------------------------------
    llm: LLM | None = None
    try:
        prov = provider or build_provider(cfg, use_cache=opts.use_cache, allow_cloud=opts.allow_cloud)
        report.provider = prov.name
        llm = LLM(prov, budget, max_tokens=cfg.llm.max_tokens, max_retries=cfg.llm.max_retries)
        if prov.is_cloud and (selection.chunks or selection.recheck_note_ids):
            chars = sum(len(c.text) for c in selection.chunks)
            notify(f"reverie: about to send data to model provider '{prov.name}' "
                   f"(extract={cfg.llm.extract_model}, verify={cfg.llm.verify_model}, "
                   f"reconcile={cfg.llm.reconcile_model}): {len(selection.chunks)} redacted transcript excerpt(s) "
                   f"(~{chars} chars) from {len(selection.sessions)} session(s), plus up to "
                   f"{len(selection.recheck_note_ids) + len(notes)} memory note(s) and their cited evidence. "
                   f"Secrets redacted: {sum(ingested.redactions.values())}. Budget cap: "
                   f"${budget.max_usd if budget.max_usd is not None else 'none'}.")
    except ProviderUnavailable as exc:
        report.llm_available = False
        report.provider = "none"
        report.warnings.append(f"LLM stages unavailable: {exc}")

    models = {"extract": cfg.llm.extract_model, "verify": cfg.llm.verify_model,
              "reconcile": cfg.llm.reconcile_model} if llm else {}
    changes: list[Change] = []
    errors: list[str] = []
    budget_hit = False

    # 3. extract -------------------------------------------------------------------------
    ext = ExtractResult()
    if llm is not None and selection.chunks:
        ext = extract(selection.chunks, ingested.by_id(), llm, cfg.llm.extract_model, purpose)
        errors.extend(ext.errors)
        budget_hit |= ext.budget_exhausted
    elif llm is None:
        # Without a model nothing is extracted; sessions stay un-dreamed so a later run with a model sees them.
        ext.completed_sessions = []
    _write_json(run_dir / "candidates.json", {"candidates": [c.model_dump() for c in ext.candidates],
                                              "dropped": ext.dropped})
    _hook(fail_hook, "extract")

    # 4. verify ----------------------------------------------------------------------------
    ver = VerifyResult()
    if ext.candidates:
        ver = verify(ext.candidates, ingested, lookup, llm, cfg.llm.verify_model)
        errors.extend(ver.errors)
        budget_hit |= ver.budget_exhausted
    recheck = [notes[i] for i in selection.recheck_note_ids if i in notes]
    if recheck and not budget_hit:
        recheck_notes(recheck, lookup, llm, cfg.llm.verify_model, ingested, ver)
        budget_hit |= ver.budget_exhausted
    _write_json(run_dir / "verified.json", {"verified": [v.model_dump() for v in ver.verified],
                                            "rechecked": ver.rechecked})
    _hook(fail_hook, "verify")

    # 5. reconcile -------------------------------------------------------------------------
    survivors = ver.survivors()
    session_times = {s.session_id: s.started_at for s in ingested.sessions}
    for n in notes.values():
        for sid in n.source_sessions():
            if sid not in session_times:
                s = lookup(sid)
                session_times[sid] = s.started_at if s else ""
    rejected = set(state.get("rejected_fingerprints", []))
    outcomes: dict[str, str] = {s.session_id: str(s.outcome.label) for s in ingested.sessions}
    rec: ReconcileResult = reconcile(survivors, notes, session_times, llm, cfg.llm.reconcile_model, rejected,
                                     outcomes=outcomes)
    errors.extend(rec.errors)
    budget_hit |= rec.budget_exhausted
    notes = rec.notes
    changes.extend(rec.changes)
    for nid, status in apply_rechecks(notes, ver.rechecked):
        n = notes[nid]
        changes.append(Change(action="contested" if status == "contested" else "rechecked", note_id=nid,
                              text=n.text, evidence=n.sources, detail=f"re-verification: {ver.rechecked[nid]}"))
    for nid, verdict in ver.rechecked.items():
        if verdict == "supported":
            note_state.setdefault(nid, {})["last_verified_dream"] = dream_no
    _write_json(run_dir / "reconcile.json", rec.model_dump())
    _hook(fail_hook, "reconcile")

    # guards ----------------------------------------------------------------------------------
    all_hints: dict[str, list[tuple[str, str]]] = {}
    for vc in survivors:
        c = vc.candidate
        placed_id = rec.placed.get(c.cid)
        if placed_id and c.command_pattern and c.replacement:
            all_hints.setdefault(placed_id, []).append((c.command_pattern, c.replacement))
    hints = {nid: guards_mod.choose_hint(h) for nid, h in all_hints.items()}
    existing_guards = load_guards(store)
    guard_props, guard_rejects = guards_mod.propose_guards(
        [n for n in notes.values() if n.status in LIVE_STATUSES], hints, ingested, cfg.dream.guard_min_sessions,
        {str(g.get("pattern")) for g in existing_guards})

    # 6. organize --------------------------------------------------------------------------
    new_notes = [notes[i] for i in rec.new_ids if notes[i].status in LIVE_STATUSES]
    topics = assign_topics(new_notes, sorted({p for p in paths.values() if p.startswith("topics/")}),
                           llm if not budget_hit else None, cfg.llm.reconcile_model, purpose, errors)
    for nid in rec.new_ids:
        paths[nid] = topics.get(nid) or DEFAULT_TOPICS.get(notes[nid].kind) or DEFAULT_TOPICS[None]
        note_state.setdefault(nid, {}).update({"first_dream": dream_no, "last_verified_dream": dream_no,
                                               "home": paths[nid], "run": run_id, "prompts": prompt_versions()})
    _hook(fail_hook, "organize")

    # 8. prune (before promotion so retired notes are never promoted) -------------------------
    usage_records = read_usage(store.root)
    ustats = usage_stats(usage_records, outcomes, cfg.dream.holdout_min_sessions)
    removed, prune_changes = prune(
        notes, dream_no, ustats, note_state, unread_dreams=cfg.dream.unread_prune_dreams,
        unread_min_sessions=cfg.dream.unread_prune_min_sessions,
        archive_retention=cfg.dream.archive_retention_dreams,
        promoted_ids={nid for nid, p in paths.items() if p == ENTRY_FILE})
    changes.extend(prune_changes)
    for nid, n in notes.items():
        if n.status in RETIRED_STATUSES:
            if paths.get(nid) != ARCHIVE_FILE and nid not in removed:
                note_state.setdefault(nid, {}).setdefault("home", paths.get(nid, ""))
            paths[nid] = ARCHIVE_FILE
            n.extra.setdefault("retired_dream", str(dream_no))

    # 7. promote / demote ---------------------------------------------------------------------
    entry = repo.files[ENTRY_FILE]
    base_md = build_memory_md(entry, [], [], set())
    base_lines, base_bytes = base_md.count("\n"), len(base_md.encode())
    index_files = topic_files_with_live_notes(notes, {**paths, **{
        nid: note_state.get(nid, {}).get("home") or DEFAULT_TOPICS.get(notes[nid].kind, DEFAULT_TOPICS[None])
        for nid, p in paths.items() if p == ENTRY_FILE}}, removed)
    index_cost_lines = len(index_files)
    index_cost_bytes = sum(len(f"- [[{p}]]\n") for p in index_files)
    cands = sorted([n for nid, n in notes.items()
                    if nid not in removed and eligible(n, cfg.dream.promote_min_sessions)], key=rank_key)
    general = judge_general(cands, llm if not budget_hit else None, cfg.llm.reconcile_model, purpose, errors)
    ranked = [n for n in cands if n.id in general]
    promoted = fit_to_cap(ranked, base_lines + index_cost_lines, base_bytes + index_cost_bytes,
                          cfg.dream.memory_md_max_lines, cfg.dream.memory_md_max_bytes)
    promoted_ids = {n.id for n in promoted}
    for nid, n in notes.items():
        if nid in removed:
            continue
        if nid in promoted_ids:
            if paths.get(nid) != ENTRY_FILE:
                note_state.setdefault(nid, {}).setdefault("home", paths.get(nid))
                changes.append(Change(action="promoted", note_id=nid, text=n.text, evidence=n.sources,
                                      detail=f"cited by {len(n.source_sessions())} sessions"))
                paths[nid] = ENTRY_FILE
            if n.status == "verified":
                n.status = "active"
        elif paths.get(nid) == ENTRY_FILE:
            home = note_state.get(nid, {}).get("home") or DEFAULT_TOPICS.get(n.kind, DEFAULT_TOPICS[None])
            if home == ENTRY_FILE:
                home = DEFAULT_TOPICS.get(n.kind, DEFAULT_TOPICS[None])
            paths[nid] = home if n.status not in RETIRED_STATUSES else ARCHIVE_FILE
            changes.append(Change(action="demoted", note_id=nid, text=n.text, evidence=n.sources,
                                  detail=f"moved to {paths[nid]}"))

    # size guard: memory must not bloat past the corpus ---------------------------------
    corpus_total = int(state.get("corpus_bytes", 0) or 0) + sum(
        s.text_bytes() for s in ingested.sessions if s.session_id in set(ext.completed_sessions))
    contents = _plan(repo, notes, paths, removed, promoted)
    live_bytes = sum(len((t or "").encode()) for p, t in contents.items() if p != ARCHIVE_FILE)
    limit = cfg.dream.max_memory_to_corpus_ratio * max(corpus_total, 1)
    if live_bytes > max(limit, 10_000):
        for nid in sorted(rec.new_ids, key=lambda i: rank_key(notes[i]), reverse=True):
            if live_bytes <= max(limit, 10_000):
                break
            if nid in promoted_ids or notes[nid].status not in LIVE_STATUSES:
                continue
            removed.add(nid)
            changes.append(Change(action="dropped", note_id=nid, text=notes[nid].text, evidence=notes[nid].sources,
                                  detail="memory size limit (max_memory_to_corpus_ratio)"))
            contents = _plan(repo, notes, paths, removed, promoted)
            live_bytes = sum(len((t or "").encode()) for p, t in contents.items() if p != ARCHIVE_FILE)
        report.warnings.append(f"memory size limit reached: live memory {live_bytes} bytes vs corpus {corpus_total}")

    md = contents.get(ENTRY_FILE) or ""
    if md.count("\n") > cfg.dream.memory_md_max_lines or len(md.encode()) > cfg.dream.memory_md_max_bytes:
        report.warnings.append("MEMORY.md exceeds its cap because of content reverie does not manage "
                               "(user-written lines or index entries); trim it by hand")
    _hook(fail_hook, "promote")

    # placements & proposal ----------------------------------------------------------------
    dropped = [v for v in ver.verified if v.dropped is not None]
    guard_note_ids = {g.note_id for g in guard_props}
    place, edits = placements(survivors, dropped, rec.placed, guard_note_ids, notes)
    _write_json(run_dir / "plan.json", {"placements": [p.model_dump() for p in place],
                                        "paths": paths, "removed": sorted(removed),
                                        "guards": [g.model_dump() for g in guard_props],
                                        "rejected_guards": guard_rejects})

    # state ---------------------------------------------------------------------------------
    new_state = json.loads(json.dumps(state))
    dreamed = new_state.setdefault("dreamed", {})
    for s in ingested.sessions:
        if s.session_id in set(ext.completed_sessions):
            dreamed[state_key(s)] = s.content_hash()
    new_state["notes"] = {k: v for k, v in note_state.items() if k in notes and k not in removed}
    new_state["corpus_bytes"] = corpus_total
    new_state["version"] = STATE_VERSION

    memory_changed = any((repo.files[p].render() if p in repo.files else None) != t for p, t in contents.items())
    state_changed = dreamed != state.get("dreamed", {}) or bool(guard_props)
    if budget_hit:
        report.status = "partial_budget"
        report.warnings.append("budget exhausted: partial progress saved; unfinished sessions will be dreamed "
                               "next time")
    report.counts = _counts(changes)
    report.errors = errors

    files_after = {p: f for p, f in repo.files.items()}
    for p, t in contents.items():
        if t is None:
            files_after.pop(p, None)
        else:
            files_after[p] = parse_file(p, t)
    report.metrics = _metrics(files_after, set(repo.other_files), corpus_total, cfg, ingested, budget,
                              usage_records, notes, removed, llm, lookup, run_id)

    if not memory_changed and not state_changed:
        report.status = "no_changes" if not budget_hit else report.status
        _finish_run_dir(run_dir, report, None)
        return report

    new_state["dream_count"] = dream_no
    new_state["last_run"] = run_id
    proposal = render_proposal(
        run_id=run_id, provider=report.provider, models=models, changes=changes, placements=place,
        guards=guard_props, rejected_guards=guard_rejects, edits=edits, contested=rec.contested_pairs,
        budget=budget.summary(), warnings=report.warnings, errors=errors, sessions=ext.completed_sessions,
        adapter_summary=ingested.adapter_report.model_dump())
    report.metrics["budget"] = budget.summary()
    _emit(store, base_branch, run_id, contents, new_state, proposal, guard_props, report, fail_hook,
          opts.auto_accept)
    _finish_run_dir(run_dir, report, proposal)
    return report


def _plan(repo: Any, notes: dict[str, Note], paths: dict[str, str], removed: set[str],
          promoted: list[Note]) -> dict[str, str | None]:
    contents = plan_writes(repo, notes, paths, removed | {n.id for n in promoted})
    topic_files = topic_files_with_live_notes(notes, paths, removed)
    existing = set(repo.files) | set(repo.other_files) | {p for p, t in contents.items() if t is not None}
    existing -= {p for p, t in contents.items() if t is None}
    contents[ENTRY_FILE] = build_memory_md(repo.files[ENTRY_FILE], [n for n in promoted if n.id not in removed],
                                           topic_files, existing)
    fixed, _ = fix_dangling_links(contents, existing)
    return fixed


def _counts(changes: list[Change]) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in changes:
        out[c.action] = out.get(c.action, 0) + 1
    return out


def _metrics(files: dict[str, MemoryFile], other: set[str], corpus: int, cfg: ReverieConfig,
             ingested: IngestResult, budget: Budget, usage: list[dict[str, Any]], notes: dict[str, Note],
             removed: set[str], llm: LLM | None, lookup: Callable[[str], Session | None],
             run_id: str) -> dict[str, Any]:
    from reverie.online.usage import success_levels

    m = memory_metrics(files, other, corpus, cfg.dream.memory_md_max_lines, cfg.dream.memory_md_max_bytes)
    m["secrets_redacted"] = dict(ingested.redactions)
    m["secrets_redacted_total"] = sum(ingested.redactions.values())
    m["tainted_events"] = sum(len(v) for v in ingested.tainted.values())
    live = {nid for nid, n in notes.items() if nid not in removed and n.status in LIVE_STATUSES}
    m["success_levels"] = success_levels(usage, live)
    m["unsupported_claim_rate"] = _unsupported_rate(notes, live, cfg.dream.unsupported_sample_size, llm, lookup,
                                                    cfg.llm.verify_model, run_id)
    m["budget"] = budget.summary()
    return m


def _unsupported_rate(notes: dict[str, Note], live: set[str], sample: int, llm: LLM | None,
                      lookup: Callable[[str], Session | None], model: str, run_id: str) -> dict[str, Any]:
    import hashlib

    ids = sorted(live, key=lambda i: hashlib.sha256(i.encode()).hexdigest())[:sample]
    if not ids:
        return {"checked": 0, "unsupported": 0, "rate": None}
    checked = unsupported = 0
    skipped = 0
    for nid in ids:
        n = notes[nid]
        evs = evidence_events(parse_pointers(n.sources), lookup)
        if not evs:
            skipped += 1
            continue
        try:
            verdict = check_note_text(n.text, n.quote, evs, llm, model)
        except Exception as exc:  # budget or provider failure: report, do not fail the dream
            log.warning("unsupported-claim sampling stopped: %s", exc)
            break
        if verdict == "not_checked":
            skipped += 1
            continue
        ok = verdict == "supported"
        checked += 1
        unsupported += 0 if ok else 1
    return {"checked": checked, "unsupported": unsupported, "skipped": skipped,
            "rate": round(unsupported / checked, 4) if checked else None}


def _emit(store: MemoryStore, base_branch: str, run_id: str, contents: dict[str, str | None],
          new_state: dict[str, Any], proposal: str, guard_props: list[Any], report: DreamReport,
          fail_hook: Callable[[str], None] | None, auto_accept: bool) -> None:
    branch = f"dream/{run_id}"
    marker = store.root / ".git" / MARKER
    atomic_write_json(marker, {"branch": branch, "base_branch": base_branch, "run_id": run_id})
    try:
        git(store.root, "checkout", "-q", "-b", branch)
        store.write_files(contents)
        _hook(fail_hook, "write")
        store.write_state(new_state)
        prop_dir = store.state_dir / "proposals"
        atomic_write_text(prop_dir / f"{run_id}.md", proposal)
        if guard_props:
            atomic_write_text(prop_dir / f"{run_id}.guards.toml",
                              guards_to_toml([{**g.model_dump(), "enabled": True} for g in guard_props]))
        sha = store.commit_all(f"reverie dream {run_id}\n\n" + _commit_summary(report))
        git(store.root, "checkout", "-q", base_branch)
    except BaseException:
        git(store.root, "checkout", "-q", "-f", base_branch, check=False)
        git(store.root, "clean", "-q", "-fd", "--", ".", ":!.reverie/usage.jsonl", check=False)
        git(store.root, "branch", "-q", "-D", branch, check=False)
        marker.unlink(missing_ok=True)
        raise
    marker.unlink(missing_ok=True)
    report.branch, report.commit = branch, sha
    if auto_accept:
        from reverie.online.review import approve

        approve(store, run_id)
        report.merged = True


def _commit_summary(report: DreamReport) -> str:
    return ", ".join(f"{k}: {v}" for k, v in sorted(report.counts.items())) or "maintenance only"


def _finish_run_dir(run_dir: Path, report: DreamReport, proposal: str | None) -> None:
    if proposal is not None:
        atomic_write_text(run_dir / "PROPOSAL.md", proposal)
    _write_json(run_dir / "metrics.json", report.metrics)
    _write_json(run_dir / "report.json", report.model_dump())


def _write_json(path: Path, data: Any) -> None:
    atomic_write_json(path, data)


def rejected_fingerprints_for(notes: list[Note]) -> list[str]:
    return [normalize_text(n.text) for n in notes]


__all__ = ["DreamOptions", "DreamReport", "VerifiedCandidate", "recover_interrupted", "run_dream"]
