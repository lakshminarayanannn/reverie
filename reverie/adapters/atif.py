"""ATIF (Agent Trajectory Interchange Format) adapter.

ATIF is a JSON trajectory format used by agent harnesses; Devin CLI writes it with
``devin --export [PATH]``. Verified against the pydantic models in ``harbor``
(``harbor.models.trajectories``, versions ATIF-v1.0 to ATIF-v1.8):

* top level: ``schema_version``, ``session_id``, ``agent`` (``name``, ``model_name``), ``steps``;
* step: ``step_id``, ``timestamp``, ``source`` (``system`` | ``user`` | ``agent``), ``message``
  (string or list of content parts with ``type``/``text``), optional ``reasoning_content``,
  ``tool_calls`` (``tool_call_id``, ``function_name``, ``arguments``) and ``observation``
  (``results``: ``source_call_id``, ``content``, ``extra``).

Unknown major versions fail loudly. Reasoning content is skipped and counted.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from reverie.adapters.base import Adapter, AdapterReport, UnsupportedFormatVersion
from reverie.core.schema import Session

_VERSION_RE = re.compile(r"^ATIF-v1\.(\d+)$")


def _text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        parts = []
        for part in message:
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, dict):
                parts.append(f"[{part.get('type', 'part')}]")
        return "\n".join(parts)
    return "" if message is None else json.dumps(message)


def _load(path: Path) -> list[dict[str, Any]]:
    raw = path.read_text(encoding="utf-8", errors="replace").strip()
    if not raw:
        raise ValueError(f"{path}: empty file")
    try:
        data = json.loads(raw)
        return [data] if isinstance(data, dict) else [d for d in data if isinstance(d, dict)]
    except json.JSONDecodeError:
        out = []
        for line in raw.splitlines():  # JSON-lines of trajectories
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
        if not out:
            raise ValueError(f"{path}: not JSON") from None
        return out


class AtifAdapter(Adapter):
    name = "atif"
    supported_versions = "ATIF-v1.0 to ATIF-v1.x (verified against harbor's v1.0-v1.8 models)"

    def detect(self, path: Path) -> bool:
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            return False
        return '"ATIF-v' in head and '"steps"' in head

    def read(self, path: Path) -> tuple[list[Session], AdapterReport]:
        report = AdapterReport(adapter=self.name, files_read=1)
        sessions: list[Session] = []
        for traj in _load(path):
            version = str(traj.get("schema_version", ""))
            if not _VERSION_RE.match(version):
                raise UnsupportedFormatVersion(f"{path}: ATIF schema_version {version!r} is not supported "
                                               f"({self.supported_versions})")
            if version not in report.format_versions:
                report.format_versions.append(version)
            sessions.append(self._convert(path, traj, report))
        report.sessions = len(sessions)
        return sessions, report

    @staticmethod
    def _convert(path: Path, traj: dict[str, Any], report: AdapterReport) -> Session:
        agent = traj.get("agent") or {}
        events: list[dict[str, Any]] = []
        names: dict[str, str] = {}
        first_ts = last_ts = None
        for step in traj.get("steps") or []:
            if not isinstance(step, dict):
                report.skip("non_object_step")
                continue
            ts = step.get("timestamp")
            first_ts = first_ts or ts
            last_ts = ts or last_ts
            source = step.get("source")
            text = _text(step.get("message"))
            if step.get("reasoning_content"):
                report.skip("reasoning_content")
            if text:
                etype = {"user": "user_msg", "agent": "assistant_msg"}.get(str(source), "system")
                events.append({"ts": ts, "type": etype, "text": text, "source_event_id": f"step:{step.get('step_id')}"})
            for call in step.get("tool_calls") or []:
                if not isinstance(call, dict):
                    report.skip("non_object_tool_call")
                    continue
                cid = str(call.get("tool_call_id") or "")
                names[cid] = str(call.get("function_name") or "")
                args = call.get("arguments") if isinstance(call.get("arguments"), dict) else None
                events.append({"ts": ts, "type": "tool_call", "tool_name": names[cid], "tool_input": args,
                               "source_event_id": f"call:{cid}" if cid else None})
            obs = step.get("observation") or {}
            for res in obs.get("results") or [] if isinstance(obs, dict) else []:
                if not isinstance(res, dict):
                    report.skip("non_object_result")
                    continue
                cid = str(res.get("source_call_id") or "")
                extra: dict[str, Any] = res["extra"] if isinstance(res.get("extra"), dict) else {}
                failed = bool(extra.get("is_error")) or extra.get("success") is False
                code = extra.get("exit_code")
                events.append({"ts": ts, "type": "tool_result", "tool_name": names.get(cid),
                               "tool_output": _text(res.get("content")),
                               "exit_code": code if isinstance(code, int) else None,
                               "status": "error" if failed else "ok",
                               "source_event_id": f"result:{cid}" if cid else None})
        report.events_read += len(events)
        return Session.model_validate({
            "session_id": str(traj.get("session_id") or traj.get("trajectory_id") or path.stem),
            "agent": str(agent.get("name") or "atif"), "started_at": first_ts or "", "ended_at": last_ts,
            "events": [{"idx": i, **e} for i, e in enumerate(events)],
        })
