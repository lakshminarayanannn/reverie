"""Canonical format passthrough (``schema_version`` "1").

Two layouts are accepted in a ``.jsonl``/``.json`` file:

1. **Whole-session lines**: each line is a full session object with an ``events`` array.
2. **Streamed**: a header line (session fields, no ``events``) followed by one event
   object per line. This supports incomplete/resumed sessions and out-of-order events
   (events are sorted by ``idx``; a repeated ``idx`` keeps the first and is counted).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from reverie.adapters.base import Adapter, AdapterReport, UnsupportedFormatVersion, first_json_line
from reverie.core.schema import SCHEMA_VERSION, Event, Session


class GenericJsonlAdapter(Adapter):
    name = "generic"
    supported_versions = f"reverie canonical schema_version {SCHEMA_VERSION}"

    def detect(self, path: Path) -> bool:
        first = first_json_line(path)
        return bool(first and "schema_version" in first and "session_id" in first)

    def read(self, path: Path) -> tuple[list[Session], AdapterReport]:
        report = AdapterReport(adapter=self.name, files_read=1)
        text = path.read_text(encoding="utf-8", errors="replace")
        records: list[dict[str, Any]] = []
        stripped = text.strip()
        if path.suffix == ".json" or (stripped.startswith("{") and "\n" not in stripped):
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}: invalid JSON: {exc}") from exc
            records = value if isinstance(value, list) else [value]
        else:
            for n, line in enumerate(text.splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    report.skip("corrupt_line")
                    report.errors.append(f"{path}:{n}: corrupt JSON line skipped")
                    continue
                if isinstance(value, dict):
                    records.append(value)
                else:
                    report.skip("non_object_line")
        if not records:
            raise ValueError(f"{path}: no JSON records")

        sessions: list[Session] = []
        current: dict[str, Any] | None = None
        pending: list[dict[str, Any]] = []

        def flush() -> None:
            nonlocal current, pending
            if current is None:
                return
            data = dict(current)
            data["events"] = list(data.get("events") or []) + pending
            sessions.append(self._build(path, data, report))
            current, pending = None, []

        for rec in records:
            if "session_id" in rec and "schema_version" in rec:
                flush()
                version = str(rec.get("schema_version"))
                if version != SCHEMA_VERSION:
                    raise UnsupportedFormatVersion(
                        f"{path}: canonical schema_version {version!r} is not supported "
                        f"(supported: {self.supported_versions})")
                if version not in report.format_versions:
                    report.format_versions.append(version)
                current = rec
            elif "idx" in rec and "type" in rec:
                if current is None:
                    report.skip("event_before_header")
                    continue
                pending.append(rec)
            else:
                report.skip("unrecognized_record")
        flush()
        report.sessions = len(sessions)
        return sessions, report

    @staticmethod
    def _build(path: Path, data: dict[str, Any], report: AdapterReport) -> Session:
        events: list[Event] = []
        seen: set[int] = set()
        raw_events = data.pop("events", [])
        for raw in raw_events:
            try:
                ev = Event.model_validate(raw)
            except ValidationError:
                report.skip("invalid_event")
                continue
            if ev.idx in seen:
                report.skip("duplicate_idx")
                continue
            seen.add(ev.idx)
            events.append(ev)
        events.sort(key=lambda e: e.idx)
        report.events_read += len(events)
        try:
            return Session.model_validate({**data, "events": [e.model_dump() for e in events]})
        except ValidationError as exc:
            raise ValueError(f"{path}: invalid session header: {exc.errors()[0]['msg']}") from exc


def write_session(session: Session, path: Path) -> None:
    """Write a session as a single whole-session JSONL line (atomic)."""
    from reverie.core.fsutil import atomic_write_text

    atomic_write_text(path, session.model_dump_json(exclude_none=False) + "\n")
