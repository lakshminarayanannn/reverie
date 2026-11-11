"""Claude Code transcript adapter.

Source format (verified against real transcripts written by Claude Code 2.1.319,
``~/.claude/projects/<project-key>/<session-id>.jsonl``, and the hooks
reference at https://code.claude.com/docs/en/hooks):

* one JSON record per line; each record has a ``type``;
* ``user`` / ``assistant`` records carry ``message.content``: a string, or a list of
  blocks of type ``text``, ``thinking``, ``tool_use`` (``id``, ``name``, ``input``) or
  ``tool_result`` (``tool_use_id``, ``content``, ``is_error``);
* records carry ``sessionId``, ``uuid``, ``timestamp``, ``cwd`` and ``version`` (the CLI
  version, e.g. ``"2.1.319"``);
* other record types (``attachment``, ``last-prompt``, ``queue-operation``,
  ``tools-used``, ``atis-latch``, ``summary``, ``system``, ...) are metadata.

Supported: CLI major version 1 and 2. Anything else raises ``UnsupportedFormatVersion``.
Every record or block that does not become an event is counted with a reason.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from reverie.adapters.base import Adapter, AdapterReport, UnsupportedFormatVersion, first_json_line
from reverie.core.schema import Event, Session

SUPPORTED_MAJOR_VERSIONS = (1, 2)
METADATA_TYPES = {
    "attachment", "last-prompt", "queue-operation", "tools-used", "atis-latch", "summary",
    "file-history-snapshot", "progress", "custom-title", "tag", "agent-name",
}


def _major(version: str) -> int | None:
    try:
        return int(version.split(".", 1)[0])
    except (ValueError, AttributeError):
        return None


def _block_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif block.get("type") == "image":
                    parts.append("[image]")
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    if content is None:
        return ""
    return json.dumps(content)


class ClaudeCodeAdapter(Adapter):
    name = "claude-code"
    supported_versions = "Claude Code transcript JSONL, CLI major versions 1-2 (verified on 2.1.319)"

    def detect(self, path: Path) -> bool:
        first = first_json_line(path, max_lines=50)
        if not first:
            return False
        return "sessionId" in first or (first.get("type") in METADATA_TYPES | {"user", "assistant"}
                                        and "schema_version" not in first)

    def read(self, path: Path) -> tuple[list[Session], AdapterReport]:
        report = AdapterReport(adapter=self.name, files_read=1)
        sessions: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        good_records = 0
        for n, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                report.skip("corrupt_line")
                report.errors.append(f"{path}:{n}: corrupt JSON line skipped")
                continue
            if not isinstance(rec, dict):
                report.skip("non_object_line")
                continue
            good_records += 1
            version = rec.get("version")
            if isinstance(version, str):
                major = _major(version)
                if major not in SUPPORTED_MAJOR_VERSIONS:
                    raise UnsupportedFormatVersion(
                        f"{path}:{n}: Claude Code transcript version {version!r} is not supported "
                        f"({self.supported_versions})")
                if version not in report.format_versions:
                    report.format_versions.append(version)
            rtype = rec.get("type")
            if rtype not in ("user", "assistant"):
                report.skip(f"metadata_record:{rtype}" if rtype in METADATA_TYPES or rtype == "system"
                            else f"unknown_record:{rtype}")
                continue
            sid = str(rec.get("sessionId") or path.stem)
            if sid not in sessions:
                sessions[sid] = {"events": [], "cwd": rec.get("cwd"), "first_ts": rec.get("timestamp"),
                                 "last_ts": rec.get("timestamp"), "tool_names": {}}
                order.append(sid)
            state = sessions[sid]
            if rec.get("timestamp"):
                state["first_ts"] = state["first_ts"] or rec["timestamp"]
                state["last_ts"] = rec["timestamp"]
            self._convert_record(rec, state, report)
        if good_records == 0:
            raise ValueError(f"{path}: no readable records")
        if not report.format_versions:
            report.format_versions.append("unversioned")

        out: list[Session] = []
        for sid in order:
            state = sessions[sid]
            events: list[Event] = []
            for i, data in enumerate(state["events"]):
                events.append(Event(idx=i, **data))
            report.events_read += len(events)
            out.append(Session(
                session_id=sid, agent="claude-code", started_at=state["first_ts"] or "",
                ended_at=state["last_ts"], cwd=state["cwd"], events=events,
            ))
        report.sessions = len(out)
        return out, report

    @staticmethod
    def _convert_record(rec: dict[str, Any], state: dict[str, Any], report: AdapterReport) -> None:
        msg = rec.get("message")
        if not isinstance(msg, dict):
            report.skip("record_without_message")
            return
        ts = rec.get("timestamp")
        uuid = rec.get("uuid")
        content = msg.get("content")
        is_meta = bool(rec.get("isMeta"))
        role = rec.get("type")
        events: list[dict[str, Any]] = state["events"]
        if isinstance(content, str):
            etype = "system" if is_meta else ("user_msg" if role == "user" else "assistant_msg")
            events.append({"ts": ts, "type": etype, "text": content, "source_event_id": uuid})
            return
        if not isinstance(content, list):
            report.skip("unrecognized_content")
            return
        for i, block in enumerate(content):
            if not isinstance(block, dict):
                report.skip("non_object_block")
                continue
            btype = block.get("type")
            src = f"{uuid}:{i}" if uuid else None
            if btype == "text":
                etype = "system" if is_meta else ("user_msg" if role == "user" else "assistant_msg")
                events.append({"ts": ts, "type": etype, "text": block.get("text") or "",
                               "source_event_id": src})
            elif btype == "tool_use":
                name = block.get("name")
                state["tool_names"][block.get("id")] = name
                tool_input = block.get("input") if isinstance(block.get("input"), dict) else None
                events.append({"ts": ts, "type": "tool_call", "tool_name": name, "tool_input": tool_input,
                               "source_event_id": block.get("id") or src})
            elif btype == "tool_result":
                is_error = bool(block.get("is_error"))
                events.append({
                    "ts": ts, "type": "tool_result",
                    "tool_name": state["tool_names"].get(block.get("tool_use_id")),
                    "tool_output": _block_text(block.get("content")),
                    "status": "error" if is_error else "ok",
                    "source_event_id": f"result:{block.get('tool_use_id')}" if block.get("tool_use_id") else src,
                })
            elif btype in ("thinking", "redacted_thinking"):
                # Private reasoning is not evidence of what happened; counted, not kept.
                report.skip("thinking_block")
            elif btype == "image":
                report.skip("image_block")
            else:
                report.skip(f"unknown_block:{btype}")


def project_key(cwd: str) -> str:
    """Claude Code's project folder name for a working directory."""
    import re

    return re.sub(r"[^A-Za-z0-9-]", "-", cwd)
