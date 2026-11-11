"""Stage 1: ingest and govern.

Loads sessions through adapters, truncates long tool output, redacts secrets, flags
events that look like prompt injection ("tainted"), and imports everything into the
experience store. Nothing downstream ever sees unredacted text.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from reverie.adapters import AdapterReport, load_sessions
from reverie.core.config import ReverieConfig
from reverie.core.experience import ExperienceStore
from reverie.core.redact import Redactor
from reverie.core.schema import Session, truncate_middle

_INJECTION_PATTERNS = [
    re.compile(r"(?i)\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|earlier|all|your)\b"
               r".{0,20}\b(instructions?|prompts?|rules|directives)\b"),
    re.compile(r"(?i)\b(add|write|append|insert|save|put|record|store)\b.{0,80}\b(to|into|in)\b.{0,20}"
               r"(MEMORY\.md|CLAUDE\.md|AGENTS\.md|guards?\.toml|your memory|the memory|memory file)"),
    re.compile(r"(?i)\b(new|updated|real)\s+(system\s+)?instructions?\s*:"),
    re.compile(r"(?i)</?\s*(system|instructions?|assistant)\s*>"),
    re.compile(r"(?i)\bnote to (the )?(ai|assistant|agent|llm|model)\b"),
    re.compile(r"(?i)\b(create|add|install)\b.{0,40}\bguard\b.{0,60}\b(block|deny|forbid)"),
]


def injection_reasons(text: str | None) -> list[str]:
    if not text:
        return []
    return [p.pattern[:40] for p in _INJECTION_PATTERNS if p.search(text)]


class IngestResult(BaseModel):
    sessions: list[Session] = Field(default_factory=list)
    adapter_report: AdapterReport
    redactions: dict[str, int] = Field(default_factory=dict)
    corpus_bytes: int = 0
    #: session_id -> event idx list flagged as possible prompt injection.
    tainted: dict[str, list[int]] = Field(default_factory=dict)
    merged_duplicates: int = 0

    def is_tainted(self, session_id: str, idx: int) -> bool:
        return idx in self.tainted.get(session_id, [])

    def by_id(self) -> dict[str, Session]:
        return {s.session_id: s for s in self.sessions}


def _merge_duplicates(sessions: list[Session]) -> tuple[list[Session], int]:
    """Same (agent, session_id) from several files: keep the most complete copy."""
    best: dict[tuple[str, str], Session] = {}
    order: list[tuple[str, str]] = []
    merged = 0
    for s in sessions:
        key = (s.agent, s.session_id)
        if key not in best:
            best[key] = s
            order.append(key)
            continue
        merged += 1
        if len(s.events) > len(best[key].events):
            best[key] = s
    return [best[k] for k in order], merged


def ingest(path: Path, cfg: ReverieConfig, adapter: str = "auto",
           extra_sessions: list[Session] | None = None) -> IngestResult:
    raw, report = load_sessions(path, adapter=adapter)
    if extra_sessions:
        raw.extend(extra_sessions)
    raw, merged = _merge_duplicates(raw)
    return govern(raw, report, cfg, merged)


def govern(raw: list[Session], report: AdapterReport, cfg: ReverieConfig, merged: int = 0) -> IngestResult:
    redactor = Redactor(entropy_threshold=cfg.dream.entropy_threshold)
    limit = cfg.dream.tool_output_limit
    out: list[Session] = []
    tainted: dict[str, list[int]] = {}
    corpus = 0
    for s in raw:
        events = [e.model_copy(update={"tool_output": truncate_middle(e.tool_output, limit)})
                  if e.tool_output else e for e in s.events]
        red = redactor.redact_session(s.model_copy(update={"events": events}))
        for ev in red.events:
            if ev.type in ("tool_result", "system") or (ev.type == "tool_call" and ev.tool_input):
                if injection_reasons(ev.tool_output) or injection_reasons(ev.text):
                    tainted.setdefault(red.session_id, []).append(ev.idx)
        corpus += red.text_bytes()
        out.append(red)
    out.sort(key=lambda s: (s.started_at or "", s.session_id))
    return IngestResult(sessions=out, adapter_report=report, redactions=dict(redactor.counts),
                        corpus_bytes=corpus, tainted=tainted, merged_duplicates=merged)


def import_into_store(store: ExperienceStore, path: Path, cfg: ReverieConfig,
                      adapter: str = "auto") -> dict[str, Any]:
    """Import files under ``path``; files unchanged since the last import are skipped."""
    from reverie.adapters.base import session_files

    summary: dict[str, Any] = {"sessions": 0, "sessions_new": 0, "events_new": 0, "events_existing": 0,
                               "files_unchanged": 0, "secrets_redacted": 0, "errors": [],
                               "events_skipped": {}}
    files = session_files(path) if path.exists() else []
    if not path.exists():
        raise FileNotFoundError(f"path does not exist: {path}")
    for file in files:
        if store.file_unchanged(file):
            summary["files_unchanged"] += 1
            continue
        result = ingest(file, cfg, adapter=adapter)
        summary["errors"].extend(result.adapter_report.errors)
        for k, v in result.adapter_report.events_skipped.items():
            summary["events_skipped"][k] = summary["events_skipped"].get(k, 0) + v
        summary["secrets_redacted"] += sum(result.redactions.values())
        for session in result.sessions:
            r = store.import_session(session, source_path=str(file))
            summary["sessions"] += 1
            summary["sessions_new"] += r.sessions_new
            summary["events_new"] += r.events_new
            summary["events_existing"] += r.events_existing
        if not result.adapter_report.files_failed:
            store.checkpoint(file)
    return summary
