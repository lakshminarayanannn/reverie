"""Claude Code hook handlers. Entry: ``python -m reverie.online.hooks <event> --memory <repo>``.

Contracts follow https://code.claude.com/docs/en/hooks: the payload arrives as JSON on
stdin; SessionStart/PostToolUse add context through ``hookSpecificOutput.additionalContext``;
PreToolUse denies with ``permissionDecision: "deny"`` and ``permissionDecisionReason``.

Hooks must never break a session: every error is reported on stderr and the hook exits 0
with no output (except a guard in ``block`` mode, which is the point).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

from reverie.core.config import ReverieConfig, experience_db_path, load_config, sessions_dir
from reverie.core.store import MemoryStore
from reverie.online.inject import RECALL_LABEL, build_injection, notes_for_target
from reverie.online.usage import log_events, read_usage

EVENTS = ("session-start", "pre-tool-use", "post-tool-use", "capture", "user-prompt-submit")
AGENTS = ("claude-code", "devin")


def _dream_no(store: MemoryStore) -> int:
    try:
        return int(store.read_state().get("dream_count", 0) or 0)
    except Exception:
        return 0


def session_start(payload: dict[str, Any], store: MemoryStore, cfg: ReverieConfig) -> dict[str, Any] | None:
    sid = str(payload.get("session_id") or "unknown")
    inj = build_injection(store, sid, cfg.online.holdout_pct, cfg.online.inject_max_chars,
                          cfg.online.inject_provenance)
    dream = _dream_no(store)
    records = [{"event": "injected", "note_id": n, "session_id": sid, "dream": dream} for n in inj.injected]
    records += [{"event": "withheld", "note_id": n, "session_id": sid, "dream": dream} for n in inj.withheld]
    log_events(store.root, records)
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": inj.text}}


def _guards(store: MemoryStore) -> list[dict[str, Any]]:
    path = store.root / "guards.toml"
    if not path.exists():
        return []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return [g for g in data.get("guard", []) if isinstance(g, dict) and g.get("enabled", True)]


def pre_tool_use(payload: dict[str, Any], store: MemoryStore, cfg: ReverieConfig) -> dict[str, Any] | None:
    if payload.get("tool_name") != "Bash":
        return None
    command = str((payload.get("tool_input") or {}).get("command") or "")
    if not command:
        return None
    for g in _guards(store):
        try:
            hit = re.search(str(g["pattern"]), command)
        except (re.error, KeyError):
            continue
        if not hit:
            continue
        mode = g.get("mode", "warn")
        log_events(store.root, [{"event": "guard_fired", "guard_id": g.get("id"), "note_id": g.get("note_id"),
                                 "session_id": payload.get("session_id"), "mode": mode,
                                 "dream": _dream_no(store)}])
        message = str(g.get("message"))
        if mode == "block":
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": message}}
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": message}}
    return None


def _memory_files_touched(payload: dict[str, Any], root: Path) -> list[Path]:
    tool = payload.get("tool_name")
    tin = payload.get("tool_input") or {}
    out: list[Path] = []
    if tool == "Read" and tin.get("file_path"):
        out.append(Path(str(tin["file_path"])))
    elif tool == "Bash" and str(root) in str(tin.get("command", "")):
        for m in re.finditer(re.escape(str(root)) + r"[/\\]?[^\s'\"|;&]*", str(tin.get("command", ""))):
            out.append(Path(m.group(0)))
    resolved: list[Path] = []
    for p in out:
        try:
            rp = p.resolve()
        except OSError:
            continue
        if rp == root or root in rp.parents:
            resolved.append(rp)
    return resolved


def post_tool_use(payload: dict[str, Any], store: MemoryStore, cfg: ReverieConfig) -> dict[str, Any] | None:
    sid = str(payload.get("session_id") or "unknown")
    dream = _dream_no(store)
    touched = _memory_files_touched(payload, store.root)
    if touched:
        repo = store.load()
        records = []
        for p in touched:
            rel = p.relative_to(store.root).as_posix() if p != store.root else ""
            f = repo.files.get(rel)
            ids = [n.id for n in f.notes()] if f else []
            records += [{"event": "read", "note_id": n, "session_id": sid, "path": rel, "dream": dream} for n in ids]
            if not ids:
                records.append({"event": "read", "note_id": "*", "session_id": sid, "path": rel, "dream": dream})
        log_events(store.root, records)
        return None
    if not cfg.online.targeted_recall:
        return None
    tin = payload.get("tool_input") or {}
    target = str(tin.get("file_path") or tin.get("path") or "")
    if not target:
        return None
    already = {r.get("note_id") for r in read_usage(store.root)
               if r.get("event") in ("recalled", "injected") and r.get("session_id") == sid}
    hits = [(nid, line) for nid, line in notes_for_target(store, target, cfg.online.targeted_recall_max_notes * 3)
            if nid not in already][: cfg.online.targeted_recall_max_notes]
    if not hits:
        return None
    log_events(store.root, [{"event": "recalled", "note_id": nid, "session_id": sid, "path": target, "dream": dream}
                            for nid, _ in hits])
    text = RECALL_LABEL.format(target=Path(target).name) + "\n" + "\n".join(line for _, line in hits)
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}


def capture(payload: dict[str, Any], store: MemoryStore | None, cfg: ReverieConfig) -> dict[str, Any] | None:
    """Stop/SessionEnd: convert the transcript to canonical (redacted) JSONL and import it."""
    from reverie.adapters.claude_code import ClaudeCodeAdapter
    from reverie.adapters.generic_jsonl import write_session
    from reverie.core.experience import ExperienceStore
    from reverie.dream.ingest import govern

    path = payload.get("transcript_path")
    if not path or not Path(str(path)).is_file():
        return None
    sessions, report = ClaudeCodeAdapter().read(Path(str(path)))
    governed = govern(sessions, report, cfg)
    out_dir = sessions_dir(cfg) / "claude-code"
    with ExperienceStore(experience_db_path(cfg)) as exp:
        for s in governed.sessions:
            write_session(s, out_dir / f"{s.session_id}.jsonl")
            exp.import_session(s, source_path=str(path))
    return None


HANDLERS = {"session-start": session_start, "pre-tool-use": pre_tool_use, "post-tool-use": post_tool_use}


def run(event: str, payload: dict[str, Any], memory: Path | None, config: Path | None = None,
        agent: str = "claude-code") -> dict[str, Any] | None:
    cfg = load_config(config)
    if agent == "devin":
        from reverie.online import devin

        if event in ("capture", "user-prompt-submit"):
            out: dict[str, Any] | None = devin.HANDLERS[event](payload, MemoryStore(memory) if memory else None, cfg)
            return out
        if memory is None:
            raise ValueError(f"{event} needs --memory")
        dstore = MemoryStore(memory)
        dstore.require()
        result: dict[str, Any] | None = devin.HANDLERS[event](payload, dstore, cfg)
        return result
    if event == "user-prompt-submit":
        return None
    if event == "capture":
        return capture(payload, MemoryStore(memory) if memory else None, cfg)
    if memory is None:
        raise ValueError(f"{event} needs --memory")
    store = MemoryStore(memory)
    store.require()
    return HANDLERS[event](payload, store, cfg)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reverie-hook", description="reverie hook handler (Claude Code, Devin)")
    parser.add_argument("event", choices=EVENTS)
    parser.add_argument("--agent", choices=AGENTS, default="claude-code")
    parser.add_argument("--memory", type=Path)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        out = run(args.event, payload, args.memory, args.config, args.agent)
    except Exception as exc:  # never break the agent session
        print(f"reverie hook {args.event}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 0
    if out is not None:
        sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
