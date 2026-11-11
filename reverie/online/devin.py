"""Devin CLI / Devin desktop hook handlers (local sessions).

Contract from the Devin CLI hooks docs (docs.devin.ai/cli/extensibility/hooks):

* every payload has ``hook_event_name``, ``session_id``, ``prompt_id``; ``DEVIN_PROJECT_DIR``
  holds the project root;
* ``SessionStart`` (``source``), ``UserPromptSubmit`` (``prompt``), ``PreToolUse`` /
  ``PostToolUse`` (``tool_name``, ``tool_input``; PostToolUse adds ``tool_response`` with
  ``success``, ``output``, ``error``), ``Stop``, ``SessionEnd`` (``reason``);
* context is added with ``hookSpecificOutput.additionalContext`` (SessionStart,
  UserPromptSubmit, PostToolUse); a tool call is blocked with ``{"decision": "block",
  "reason": ...}`` (or exit code 2).

Devin hooks carry no transcript path, so reverie builds the session from the hook stream
itself: prompts, every tool call and its result. Assistant prose between tool calls is not
in the hook stream; export with ``devin --export`` and ``reverie convert --adapter atif``
for the full conversation.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from reverie.core.config import ReverieConfig, data_dir, experience_db_path, sessions_dir
from reverie.core.fsutil import append_jsonl, read_jsonl
from reverie.core.store import MemoryStore
from reverie.online import hooks as cc
from reverie.online.usage import log_events, now_iso

SHELL_TOOLS = {"exec", "bash", "shell", "run_command", "terminal"}


def live_path(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id or "unknown")
    return data_dir() / "devin-live" / f"{safe}.jsonl"


def _command(payload: dict[str, Any]) -> str:
    tin = payload.get("tool_input") or {}
    cmd = tin.get("command") or tin.get("cmd") if isinstance(tin, dict) else None
    return str(cmd) if isinstance(cmd, str) else ""


def _is_shell(payload: dict[str, Any]) -> bool:
    return str(payload.get("tool_name", "")).lower() in SHELL_TOOLS or bool(_command(payload))


def _record(payload: dict[str, Any], rec: dict[str, Any]) -> None:
    rec.setdefault("ts", now_iso())
    rec["cwd"] = os.environ.get("DEVIN_PROJECT_DIR")
    append_jsonl(live_path(str(payload.get("session_id") or "unknown")), rec)


def session_start(payload: dict[str, Any], store: MemoryStore, cfg: ReverieConfig) -> dict[str, Any] | None:
    _record(payload, {"kind": "start", "source": payload.get("source")})
    return cc.session_start(payload, store, cfg)  # same additionalContext contract


def user_prompt_submit(payload: dict[str, Any], store: MemoryStore | None, cfg: ReverieConfig) -> None:
    prompt = payload.get("prompt")
    if isinstance(prompt, str) and prompt:
        _record(payload, {"kind": "user", "text": prompt})
    return None


def _matching_guard(store: MemoryStore, command: str) -> dict[str, Any] | None:
    for g in cc._guards(store):
        try:
            if re.search(str(g["pattern"]), command):
                return g
        except (re.error, KeyError):
            continue
    return None


def pre_tool_use(payload: dict[str, Any], store: MemoryStore, cfg: ReverieConfig) -> dict[str, Any] | None:
    if not _is_shell(payload):
        return None
    command = _command(payload)
    g = _matching_guard(store, command) if command else None
    if g is None or g.get("mode", "warn") != "block":
        return None  # warn-mode guards speak in PostToolUse, where Devin accepts added context
    log_events(store.root, [{"event": "guard_fired", "guard_id": g.get("id"), "note_id": g.get("note_id"),
                             "session_id": payload.get("session_id"), "mode": "block", "agent": "devin",
                             "dream": cc._dream_no(store)}])
    return {"decision": "block", "reason": str(g.get("message"))}


def post_tool_use(payload: dict[str, Any], store: MemoryStore, cfg: ReverieConfig) -> dict[str, Any] | None:
    resp: dict[str, Any] = payload["tool_response"] if isinstance(payload.get("tool_response"), dict) else {}
    output = str(resp["output"]) if isinstance(resp.get("output"), str) else ""
    error = str(resp["error"]) if isinstance(resp.get("error"), str) else ""
    _record(payload, {"kind": "tool", "tool_name": payload.get("tool_name"), "tool_input": payload.get("tool_input"),
                      "success": resp.get("success"), "output": output + (("\n" + error) if error else "")})
    sid = str(payload.get("session_id") or "unknown")
    # memory reads: any tool argument naming a file inside the memory repo
    blob = " ".join(str(v) for v in (payload.get("tool_input") or {}).values()) \
        if isinstance(payload.get("tool_input"), dict) else ""
    if str(store.root) in blob:
        cc.post_tool_use({"session_id": sid, "tool_name": "Bash", "tool_input": {"command": blob}}, store, cfg)
    contexts: list[str] = []
    command = _command(payload)
    if _is_shell(payload) and command:
        g = _matching_guard(store, command)
        if g is not None and g.get("mode", "warn") == "warn":
            log_events(store.root, [{"event": "guard_fired", "guard_id": g.get("id"), "note_id": g.get("note_id"),
                                     "session_id": sid, "mode": "warn", "agent": "devin",
                                     "dream": cc._dream_no(store)}])
            contexts.append(str(g.get("message")))
    if cfg.online.targeted_recall:
        tin = payload.get("tool_input") or {}
        target = str(tin.get("file_path") or tin.get("path") or "") if isinstance(tin, dict) else ""
        if target:
            out = cc.post_tool_use({"session_id": sid, "tool_name": "Edit", "tool_input": {"file_path": target}},
                                   store, cfg)
            if out:
                contexts.append(out["hookSpecificOutput"]["additionalContext"])
    if not contexts:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "\n\n".join(contexts)}}


def finalize(payload: dict[str, Any], store: MemoryStore | None, cfg: ReverieConfig) -> None:
    """Stop / SessionEnd: turn the live hook log into a redacted canonical session and import it."""
    from reverie.adapters.base import AdapterReport
    from reverie.adapters.generic_jsonl import write_session
    from reverie.core.experience import ExperienceStore
    from reverie.core.schema import Session
    from reverie.dream.ingest import govern

    sid = str(payload.get("session_id") or "unknown")
    records, bad = read_jsonl(live_path(sid))
    if not records:
        return None
    events: list[dict[str, Any]] = []
    cwd = None
    for n, rec in enumerate(records):
        cwd = cwd or rec.get("cwd")
        if rec.get("kind") == "user":
            events.append({"type": "user_msg", "text": rec.get("text"), "ts": rec.get("ts"),
                           "source_event_id": f"devin:{n}"})
        elif rec.get("kind") == "tool":
            tin = rec.get("tool_input") if isinstance(rec.get("tool_input"), dict) else None
            events.append({"type": "tool_call", "tool_name": rec.get("tool_name"), "tool_input": tin,
                           "ts": rec.get("ts"), "source_event_id": f"devin:{n}:call"})
            ok = rec.get("success")
            events.append({"type": "tool_result", "tool_name": rec.get("tool_name"), "tool_output": rec.get("output"),
                           "status": "error" if ok is False else "ok", "ts": rec.get("ts"),
                           "source_event_id": f"devin:{n}:result"})
    if not events:
        return None
    session = Session.model_validate({
        "session_id": sid, "agent": "devin", "started_at": records[0].get("ts") or "",
        "ended_at": records[-1].get("ts"), "cwd": cwd, "events": [{"idx": i, **e} for i, e in enumerate(events)]})
    report = AdapterReport(adapter="devin-hooks", sessions=1, events_read=len(events))
    if bad:
        report.skip("corrupt_line", bad)
    governed = govern([session], report, cfg)
    out = sessions_dir(cfg) / "devin" / f"{sid}.jsonl"
    with ExperienceStore(experience_db_path(cfg)) as exp:
        for s in governed.sessions:
            write_session(s, out)
            exp.import_session(s, source_path=str(live_path(sid)))
    return None


HANDLERS: dict[str, Any] = {"session-start": session_start, "user-prompt-submit": user_prompt_submit,
            "pre-tool-use": pre_tool_use, "post-tool-use": post_tool_use, "capture": finalize}
