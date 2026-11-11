"""Convert a FrontierMemory evidence corpus into canonical sessions.

Per the benchmark rules (github.com/letta-ai/frontiermemory ``docs/rules.md``), the memory
agent sees only ``/evidence/inputs/``: the trajectories, verbatim, each ending with its
verifier result, ``PASS`` or ``FAIL``. The released corpus is gated and was not read here.
The benchmark's own oracle (``dream_task/solution/solve.sh``) counts verdicts with
``grep 'Verifier result.*PASS'``, so that framing is matched first; the bundled smoke
corpus is Claude Code JSONL without a verdict line.

Strategy (and its limits, see docs/LIMITATIONS.md):

* Claude Code transcripts go through the Claude Code adapter.
* Any other JSON-lines trajectory becomes one ``system`` event per record (nothing is
  dropped silently), so the dream can still read it.
* The verdict is the last standalone ``PASS``/``FAIL`` token found in the final records
  (or in keys such as ``verifier_result``/``reward``), mapped to ``outcome.label`` with
  ``source: verifier``. If none is found the label stays ``unknown`` and is counted.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from reverie.adapters.base import AdapterReport, UnsupportedFormatVersion
from reverie.adapters.claude_code import ClaudeCodeAdapter
from reverie.core.schema import Event, Outcome, Session

_VERDICT = re.compile(r"(?<![A-Za-z])(PASS|FAIL)(?![A-Za-z])")
_VERIFIER_LINE = re.compile(r"Verifier result.*?(PASS|FAIL)")


class ConvertReport(BaseModel):
    files: int = 0
    sessions: int = 0
    claude_code: int = 0
    generic: int = 0
    labels: dict[str, int] = Field(default_factory=dict)
    adapter: AdapterReport = Field(default_factory=lambda: AdapterReport(adapter="frontiermemory"))


def _records(path: Path) -> list[Any]:
    out: list[Any] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append(line)
    return out


def detect_verdict(records: list[Any], tail: int = 5) -> str:
    for rec in reversed(records):
        text = rec if isinstance(rec, str) else json.dumps(rec)
        m = _VERIFIER_LINE.findall(text)
        if m:
            return str(m[-1]).lower()
    for rec in reversed(records[-tail:]):
        if isinstance(rec, dict):
            for key in ("verifier_result", "verdict", "result", "reward", "status"):
                val = rec.get(key)
                if isinstance(val, str) and val.strip().upper() in ("PASS", "FAIL"):
                    return val.strip().lower()
                if key == "reward" and isinstance(val, int | float):
                    return "pass" if val > 0 else "fail"
            text = json.dumps(rec)
        else:
            text = str(rec)
        found = _VERDICT.findall(text)
        if found:
            return str(found[-1]).lower()
    return "unknown"


def convert_file(path: Path, report: ConvertReport) -> list[Session]:
    records = _records(path)
    label = detect_verdict(records)
    sessions: list[Session]
    adapter = ClaudeCodeAdapter()
    if adapter.detect(path):
        try:
            sessions, sub = adapter.read(path)
            report.adapter.merge(sub)
            report.claude_code += 1
        except (UnsupportedFormatVersion, ValueError) as exc:
            report.adapter.errors.append(f"{path.name}: {exc}; read generically")
            sessions = [_generic(path, records, report)]
    else:
        sessions = [_generic(path, records, report)]
    out: list[Session] = []
    for i, s in enumerate(sessions):
        # One trajectory file is one attempt: name sessions after the file so evidence pointers are stable.
        sid = path.stem if len(sessions) == 1 else f"{path.stem}-{i}"
        outcome = Outcome.model_validate({"label": label, "source": "verifier" if label != "unknown" else "none"})
        out.append(s.model_copy(update={"session_id": sid, "outcome": outcome, "agent": "frontiermemory"}))
    report.labels[label] = report.labels.get(label, 0) + 1
    return out


def _generic(path: Path, records: list[Any], report: ConvertReport) -> Session:
    events = []
    for i, rec in enumerate(records):
        text = rec if isinstance(rec, str) else json.dumps(rec, ensure_ascii=False)
        events.append(Event(idx=i, type="system", text=text[:20_000]))
    report.generic += 1
    report.adapter.events_read += len(events)
    return Session(session_id=path.stem, agent="frontiermemory", started_at="", events=events)


def convert_corpus(inputs: Path) -> tuple[list[Session], ConvertReport]:
    report = ConvertReport()
    sessions: list[Session] = []
    files = sorted(p for p in Path(inputs).rglob("*") if p.is_file() and not p.name.startswith("."))
    for i, path in enumerate(files):
        report.files += 1
        try:
            got = convert_file(path, report)
        except (OSError, UnicodeDecodeError) as exc:
            report.adapter.files_failed += 1
            report.adapter.errors.append(f"{path.name}: {exc}")
            continue
        # Trajectories have no reliable timestamps relative to each other; order by file name.
        for s in got:
            sessions.append(s.model_copy(update={"started_at": s.started_at or f"0000-00-00T{i:06d}"}))
    report.sessions = len(sessions)
    return sessions, report
