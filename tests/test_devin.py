"""Devin CLI / Devin desktop integration: hooks (payloads per the Devin CLI hooks docs), live capture,
plugin install, ATIF export adapter, and dreaming over captured Devin sessions."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from reverie.adapters import UnsupportedFormatVersion, detect_adapter, load_sessions
from reverie.adapters.atif import AtifAdapter
from reverie.cli import app
from reverie.core.config import ReverieConfig, sessions_dir
from reverie.online.inject import LABEL
from reverie.online.install import install_devin
from reverie.online.usage import read_usage
from tests.helpers import dream, new_store

runner = CliRunner()
SID = "3f8d1c2a-devin-session"


def hook(event: str, payload: dict, memory: Path, env: dict | None = None) -> dict | None:
    proc = subprocess.run([sys.executable, "-m", "reverie.online.hooks", event, "--agent", "devin",
                           "--memory", str(memory)], input=json.dumps(payload), capture_output=True, text=True,
                          timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout) if proc.stdout.strip() else None


@pytest.fixture
def mem(tmp_path: Path):
    store = new_store(tmp_path)
    dream(store, auto_accept=True)  # fixture memory with a `^\s*pytest\b` warn guard
    return store


def test_devin_session_through_hooks(mem, tmp_path: Path) -> None:
    import os

    env = {**os.environ, "DEVIN_PROJECT_DIR": "/work/widgetshop"}
    out = hook("session-start", {"hook_event_name": "SessionStart", "session_id": SID, "source": "startup"}, mem.root, env)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart" and ctx.startswith(LABEL)
    assert hook("user-prompt-submit", {"hook_event_name": "UserPromptSubmit", "session_id": SID,
                                       "prompt": "run the tests", "prompt_id": "p1"}, mem.root, env) is None
    # warn-mode guard: nothing before the call, context after it (where Devin accepts it)
    pre = {"hook_event_name": "PreToolUse", "session_id": SID, "tool_name": "exec", "tool_input": {"command": "pytest -q"}}
    assert hook("pre-tool-use", pre, mem.root, env) is None
    post = {**pre, "hook_event_name": "PostToolUse",
            "tool_response": {"success": False, "output": "ModuleNotFoundError: No module named 'app'", "error": None}}
    warn = hook("post-tool-use", post, mem.root, env)
    assert warn["hookSpecificOutput"]["additionalContext"].startswith("reverie guard (automated; may be outdated):")
    ok = {**post, "tool_input": {"command": "python -m pytest -q"},
          "tool_response": {"success": True, "output": "41 passed", "error": None}}
    assert hook("post-tool-use", ok, mem.root, env) is None
    # block mode uses Devin's {"decision": "block"} contract
    g = mem.root / "guards.toml"
    g.write_text(g.read_text().replace('mode = "warn"', 'mode = "block"'))
    blocked = hook("pre-tool-use", pre, mem.root, env)
    assert blocked["decision"] == "block" and blocked["reason"].startswith("reverie guard")
    # a read of a memory file is tracked
    hook("post-tool-use", {"hook_event_name": "PostToolUse", "session_id": SID, "tool_name": "read",
                           "tool_input": {"file_path": str(mem.root / "topics" / "databases.md")},
                           "tool_response": {"success": True, "output": "...", "error": None}}, mem.root, env)
    usage = read_usage(mem.root)
    assert {"injected", "guard_fired", "read"} <= {r["event"] for r in usage}
    assert {r.get("mode") for r in usage if r["event"] == "guard_fired"} == {"warn", "block"}
    # Stop finalizes the captured session (and again on SessionEnd: idempotent)
    hook("capture", {"hook_event_name": "Stop", "session_id": SID, "stop_hook_active": False}, mem.root, env)
    hook("capture", {"hook_event_name": "SessionEnd", "session_id": SID, "reason": "exit"}, mem.root, env)
    sessions, _ = load_sessions(sessions_dir(ReverieConfig()) / "devin")
    assert len(sessions) == 1
    s = sessions[0]
    assert s.agent == "devin" and s.session_id == SID and s.cwd == "/work/widgetshop"
    types = [e.type for e in s.events]
    assert types[0] == "user_msg" and types.count("tool_call") == 3 and types.count("tool_result") == 3
    assert s.events[2].status == "error" and s.events[2].tool_output.startswith("ModuleNotFoundError")


def test_dream_over_captured_devin_sessions(mem, tmp_path: Path) -> None:
    from reverie.online import devin

    cfg = ReverieConfig()
    for i in range(3):
        sid = f"devin-{i}"
        devin.user_prompt_submit({"session_id": sid, "prompt": "build the docs"}, mem, cfg)
        for cmd, okay, out in (("make docs", False, "sphinx-build: command not found"),
                               ("uv run --group docs make docs", True, "build succeeded")):
            devin.post_tool_use({"session_id": sid, "tool_name": "exec", "tool_input": {"command": cmd},
                                 "tool_response": {"success": okay, "output": out, "error": None}}, mem, cfg)
        devin.finalize({"session_id": sid}, mem, cfg)
    from reverie.providers.mock_provider import MockProvider
    from reverie.providers.scripted import ScriptedOracle

    text = "`make docs` fails with `sphinx-build: command not found`; run `uv run --group docs make docs`."
    script = {"extract": {f"devin-{i}": [{"text": text, "kind": "trap", "critical": True,
                                         "evidence": [f"devin-{i}#2", f"devin-{i}#3", f"devin-{i}#4"],
                                         "command_pattern": r"^\s*make docs", "replacement": "uv run --group docs make docs"}]
                          for i in range(3)}, "relations": [], "topics": {}}
    rep = dream(mem, sessions=sessions_dir(cfg) / "devin", provider=MockProvider(rule=ScriptedOracle(script)),
                auto_accept=True)
    assert rep.status == "ok"
    assert "uv run --group docs make docs" in (mem.root / "MEMORY.md").read_text()
    assert "^\\\\s*make docs" in (mem.root / "guards.toml").read_text()


def test_install_devin_plugin(mem, tmp_path: Path) -> None:
    res = install_devin(mem.root, out_dir=tmp_path / "devin")
    plugin = Path(res["plugin_dir"])
    manifest = json.loads((plugin / ".devin-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "reverie-memory"
    hooks = json.loads((plugin / "hooks.json").read_text())
    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd"}
    assert hooks["PreToolUse"][0]["matcher"] == "exec"
    cmd = hooks["SessionStart"][0]["hooks"][0]["command"]
    assert "--agent devin" in cmd and hooks["SessionStart"][0]["hooks"][0]["type"] == "command"
    proc = subprocess.run(cmd, shell=True, input="{}", capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0 and "additionalContext" in proc.stdout
    assert res["install_command"] == f"devin plugins install --local {plugin}"
    cli = runner.invoke(app, ["install", "devin", "-m", str(mem.root), "--out-dir", str(tmp_path / "d2")])
    assert cli.exit_code == 0 and "devin plugins install --local" in cli.stdout


def atif(version: str = "ATIF-v1.8") -> dict:
    return {"schema_version": version, "session_id": "devin-export-1",
            "agent": {"name": "devin", "version": "x", "model_name": "m"},
            "steps": [
                {"step_id": 1, "timestamp": "2026-05-01T10:00:00Z", "source": "user", "message": "run the tests"},
                {"step_id": 2, "timestamp": "2026-05-01T10:00:05Z", "source": "agent", "message": "Running them.",
                 "reasoning_content": "private",
                 "tool_calls": [{"tool_call_id": "c1", "function_name": "exec", "arguments": {"command": "pytest -q"}}],
                 "observation": {"results": [{"source_call_id": "c1", "content": "ModuleNotFoundError",
                                              "extra": {"is_error": True}}]}},
                {"step_id": 3, "timestamp": "2026-05-01T10:00:09Z", "source": "agent",
                 "message": [{"type": "text", "text": "Use python -m pytest."}]},
            ]}


def test_atif_adapter(tmp_path: Path) -> None:
    p = tmp_path / "export.json"
    p.write_text(json.dumps(atif(), indent=2))
    assert isinstance(detect_adapter(p), AtifAdapter)
    sessions, report = load_sessions(p)
    s = sessions[0]
    assert s.session_id == "devin-export-1" and s.agent == "devin" and s.started_at == "2026-05-01T10:00:00Z"
    assert [e.type for e in s.events] == ["user_msg", "assistant_msg", "tool_call", "tool_result", "assistant_msg"]
    assert s.events[2].command() == "pytest -q" and s.events[3].status == "error" and s.events[3].tool_name == "exec"
    assert report.events_skipped == {"reasoning_content": 1} and report.format_versions == ["ATIF-v1.8"]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(atif("ATIF-v2.0")))
    with pytest.raises(UnsupportedFormatVersion):
        AtifAdapter().read(bad)
    res = runner.invoke(app, ["convert", str(p), "--adapter", "atif", "--out", str(tmp_path / "c"), "--json"])
    assert res.exit_code == 0 and json.loads(res.stdout)["sessions"] == 1
