"""Claude Code integration: transcript adapter, hooks, install, usage tracking, holdout."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from reverie.adapters import UnsupportedFormatVersion, detect_adapter, load_sessions
from reverie.adapters.claude_code import ClaudeCodeAdapter, project_key
from reverie.cli import app
from reverie.core.config import ReverieConfig, experience_db_path, sessions_dir
from reverie.core.experience import ExperienceStore
from reverie.online import hooks
from reverie.online.holdout import holdout_stats, ineffective_notes, is_held_out
from reverie.online.inject import LABEL, build_injection
from reverie.online.install import install_claude_code
from reverie.online.usage import guard_firings_per_week, read_usage, success_levels, usage_stats
from tests.helpers import dream, new_store

TRANSCRIPT = Path(__file__).parent / "fixtures" / "claude_code" / "transcript.jsonl"
runner = CliRunner()


def test_adapter_reads_verified_shape() -> None:
    adapter = detect_adapter(TRANSCRIPT)
    assert isinstance(adapter, ClaudeCodeAdapter)
    sessions, report = adapter.read(TRANSCRIPT)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.agent == "claude-code" and s.cwd == "/work/widgetshop"
    assert s.started_at == "2026-03-20T10:00:01.000Z" and s.ended_at == "2026-03-20T10:00:10.000Z"
    types = [e.type for e in s.events]
    assert types == ["user_msg", "assistant_msg", "tool_call", "tool_result", "tool_call", "tool_result",
                     "assistant_msg"]
    assert s.events[2].command() == "pytest -q" and s.events[3].status == "error"
    assert s.events[3].tool_name == "Bash" and s.events[5].tool_output == "12 passed"
    assert report.format_versions == ["2.1.319"]
    assert report.events_skipped == {"metadata_record:queue-operation": 1, "thinking_block": 1,
                                     "metadata_record:attachment": 1, "metadata_record:last-prompt": 1,
                                     "unknown_record:mystery-new-record": 1}


def test_adapter_unknown_version_fails_loudly(tmp_path: Path) -> None:
    bad = tmp_path / "t.jsonl"
    rec = json.loads(TRANSCRIPT.read_text().splitlines()[1])
    rec["version"] = "3.0.0"
    bad.write_text(json.dumps(rec) + "\n")
    with pytest.raises(UnsupportedFormatVersion, match=r"3\.0\.0"):
        ClaudeCodeAdapter().read(bad)
    with pytest.raises(UnsupportedFormatVersion):
        load_sessions(bad, adapter="claude-code")


def test_adapter_corrupt_lines_counted(tmp_path: Path) -> None:
    p = tmp_path / "t.jsonl"
    p.write_text(TRANSCRIPT.read_text() + "{not json\n")
    _, report = ClaudeCodeAdapter().read(p)
    assert report.events_skipped["corrupt_line"] == 1 and report.errors
    empty = tmp_path / "e.jsonl"
    empty.write_text("garbage\n")
    with pytest.raises(ValueError):
        ClaudeCodeAdapter().read(empty)


def test_resumed_transcript_keeps_event_indices(tmp_path: Path) -> None:
    lines = TRANSCRIPT.read_text().splitlines()
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(lines[:4]) + "\n")
    first = ClaudeCodeAdapter().read(p)[0][0]
    p.write_text("\n".join(lines) + "\n")
    full = ClaudeCodeAdapter().read(p)[0][0]
    assert [e.model_dump() for e in first.events] == [e.model_dump() for e in full.events[: len(first.events)]]
    with ExperienceStore(tmp_path / "x.db") as store:
        store.import_session(first)
        res = store.import_session(full)
        assert res.events_new == len(full.events) - len(first.events) and res.events_existing == len(first.events)


def test_project_key() -> None:
    assert project_key("/app") == "-app" and project_key("/srv/my proj") == "-srv-my-proj"


REAL = sorted(Path.home().glob(".claude/projects/*/*.jsonl"))


@pytest.mark.skipif(not REAL, reason="no real Claude Code transcripts on this machine")
def test_real_local_transcripts_parse_without_silent_drops() -> None:
    """Runs against whatever real transcripts exist locally; asserts only structure, never content."""
    for path in REAL[:5]:
        sessions, report = ClaudeCodeAdapter().read(path)
        assert sessions and report.events_read > 0
        known = {k.split(":")[0] for k in report.events_skipped}
        assert known <= {"metadata_record", "thinking_block", "image_block", "unknown_record", "corrupt_line",
                         "unknown_block", "record_without_message", "unrecognized_content", "non_object_block"}


# -- hooks ---------------------------------------------------------------------------


@pytest.fixture
def dreamed_store(tmp_path: Path):
    store = new_store(tmp_path)
    dream(store, auto_accept=True)
    return store


def test_session_start_injection_and_usage(dreamed_store) -> None:
    out = hooks.session_start({"session_id": "sess-1"}, dreamed_store, ReverieConfig())
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert ctx.startswith(LABEL) and "python -m pytest" in ctx and len(ctx) <= 9000
    assert "source:" not in ctx and "(topics/" in ctx  # compact notes; readable index paths
    recs = read_usage(dreamed_store.root)
    assert {r["event"] for r in recs} == {"injected"} and all(r["session_id"] == "sess-1" for r in recs)
    assert all(r["dream"] == 1 for r in recs)


def test_holdout_is_deterministic_and_logged(dreamed_store) -> None:
    cfg = ReverieConfig()
    cfg.online.holdout_pct = 50
    ids_seen = []
    for sid in ("a", "b", "c", "d", "e", "f"):
        inj = build_injection(dreamed_store, sid, 50)
        again = build_injection(dreamed_store, sid, 50)
        assert inj.withheld == again.withheld and inj.injected == again.injected
        ids_seen.append((sid, tuple(inj.withheld)))
        for nid in inj.withheld:
            assert f"({nid})" not in inj.text
    assert any(w for _, w in ids_seen)  # with 50% some note is held out somewhere
    assert all(is_held_out("x", "n", 100) for _ in range(3)) and not is_held_out("x", "n", 0)
    hooks.session_start({"session_id": "a"}, dreamed_store, cfg)
    assert any(r["event"] == "withheld" for r in read_usage(dreamed_store.root)) or not ids_seen[0][1]


def test_injection_truncates_to_budget(dreamed_store) -> None:
    inj = build_injection(dreamed_store, "s", 0, max_chars=300)
    assert inj.truncated and len(inj.text) <= 300 and "truncated" in inj.text


def test_guard_warn_and_block(dreamed_store) -> None:
    cfg = ReverieConfig()
    payload = {"session_id": "s", "tool_name": "Bash", "tool_input": {"command": "pytest -q"}}
    out = hooks.pre_tool_use(payload, dreamed_store, cfg)
    msg = out["hookSpecificOutput"]["additionalContext"]
    assert msg.startswith("reverie guard (automated; may be outdated):") and "permissionDecision" not in str(out)
    assert hooks.pre_tool_use({**payload, "tool_input": {"command": "python -m pytest"}}, dreamed_store, cfg) is None
    assert hooks.pre_tool_use({**payload, "tool_name": "Read"}, dreamed_store, cfg) is None
    gpath = dreamed_store.root / "guards.toml"
    gpath.write_text(gpath.read_text().replace('mode = "warn"', 'mode = "block"'))
    out = hooks.pre_tool_use(payload, dreamed_store, cfg)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"].startswith("reverie guard (automated")
    fired = [r for r in read_usage(dreamed_store.root) if r["event"] == "guard_fired"]
    assert len(fired) == 2 and {r["mode"] for r in fired} == {"warn", "block"}
    assert sum(guard_firings_per_week(read_usage(dreamed_store.root)).values()) == 2
    gpath.write_text(gpath.read_text().replace("enabled = true", "enabled = false"))
    assert hooks.pre_tool_use(payload, dreamed_store, cfg) is None


def test_read_tracking_and_targeted_recall(dreamed_store) -> None:
    cfg = ReverieConfig()
    topic = dreamed_store.root / "topics" / "databases.md"
    hooks.post_tool_use({"session_id": "s", "tool_name": "Read", "tool_input": {"file_path": str(topic)}},
                        dreamed_store, cfg)
    hooks.post_tool_use({"session_id": "s", "tool_name": "Bash",
                         "tool_input": {"command": f"cat {dreamed_store.root}/MEMORY.md"}}, dreamed_store, cfg)
    reads = [r for r in read_usage(dreamed_store.root) if r["event"] == "read"]
    assert {r["path"] for r in reads} == {"topics/databases.md", "MEMORY.md"}
    live = {n.id for _, n in dreamed_store.load().all_notes() if n.status in ("verified", "active")}
    levels = success_levels(read_usage(dreamed_store.root), live)
    assert levels["read"] >= 3 and levels["used"].startswith("not measured")
    # targeted recall is off by default
    payload = {"session_id": "s2", "tool_name": "Edit", "tool_input": {"file_path": "/work/x/staging-db"}}
    assert hooks.post_tool_use(payload, dreamed_store, cfg) is None
    cfg.online.targeted_recall = True
    out = hooks.post_tool_use(payload, dreamed_store, cfg)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("Automated memory (reverie) related to staging-db. Not written by the user.")
    assert "6543" in ctx
    assert hooks.post_tool_use(payload, dreamed_store, cfg) is None  # not repeated in the same session


def test_capture_writes_redacted_canonical(tmp_path: Path) -> None:
    cfg = ReverieConfig()
    hooks.capture({"transcript_path": str(TRANSCRIPT), "session_id": "x"}, None, cfg)
    out = sessions_dir(cfg) / "claude-code" / "0f8e2a10-1111-4c2b-9d3e-5a6b7c8d9e0f.jsonl"
    text = out.read_text()
    assert "ghp_FAKE" not in text and "[REDACTED:github_token]" in text
    with ExperienceStore(experience_db_path(cfg)) as store:
        assert store.counts()["events"] == 7
    hooks.capture({"transcript_path": str(TRANSCRIPT)}, None, cfg)  # Stop then SessionEnd: idempotent
    with ExperienceStore(experience_db_path(cfg)) as store:
        assert store.counts()["events"] == 7
    assert hooks.capture({"transcript_path": str(tmp_path / "missing.jsonl")}, None, cfg) is None


def test_hook_process_contract(dreamed_store) -> None:
    """The real entry point: JSON on stdin, JSON on stdout, exit 0 even on bad input."""
    env = {**os.environ}
    cmd = [sys.executable, "-m", "reverie.online.hooks"]
    ok = subprocess.run(cmd + ["session-start", "--memory", str(dreamed_store.root)],
                        input=json.dumps({"session_id": "p", "hook_event_name": "SessionStart", "source": "startup"}),
                        capture_output=True, text=True, env=env, timeout=60)
    assert ok.returncode == 0 and json.loads(ok.stdout)["hookSpecificOutput"]["additionalContext"].startswith(LABEL)
    bad = subprocess.run(cmd + ["session-start", "--memory", "/nonexistent/x"], input="{}", capture_output=True,
                         text=True, env=env, timeout=60)
    assert bad.returncode == 0 and bad.stdout == "" and "reverie hook" in bad.stderr
    garbage = subprocess.run(cmd + ["pre-tool-use", "--memory", str(dreamed_store.root)], input="not json",
                             capture_output=True, text=True, env=env, timeout=60)
    assert garbage.returncode == 0 and garbage.stdout == ""


def test_install_writes_script_and_snippet(dreamed_store, tmp_path: Path) -> None:
    res = install_claude_code(dreamed_store.root, out_dir=tmp_path / "cc")
    snippet = res["snippet"]["hooks"]
    assert set(snippet) == {"SessionStart", "PreToolUse", "PostToolUse", "Stop", "SessionEnd"}
    h = snippet["SessionStart"][0]["hooks"][0]
    assert h["type"] == "command" and h["command"] == sys.executable and h["args"][1] == "session-start"
    assert snippet["PreToolUse"][0]["matcher"] == "Bash" and snippet["SessionEnd"][0]["hooks"][0]["timeout"] == 30
    assert Path(res["script"]).read_text().startswith('"""reverie hook entry point')
    proc = subprocess.run([sys.executable, res["script"], "session-start", "--memory", str(dreamed_store.root)],
                          input="{}", capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0 and "additionalContext" in proc.stdout
    cli = runner.invoke(app, ["install", "claude-code", "-m", str(dreamed_store.root), "--out-dir", str(tmp_path / "c2")])
    assert cli.exit_code == 0 and "did not edit any settings file" in cli.stdout


def test_cli_inject_and_convert(dreamed_store, tmp_path: Path) -> None:
    res = runner.invoke(app, ["inject", "-m", str(dreamed_store.root), "--json"])
    assert res.exit_code == 0 and json.loads(res.stdout)["chars"] <= 9000
    assert not read_usage(dreamed_store.root)  # preview does not log usage
    res = runner.invoke(app, ["convert", str(TRANSCRIPT), "--out", str(tmp_path / "canon"), "--json"])
    assert res.exit_code == 0 and json.loads(res.stdout)["sessions"] == 1
    sessions, _ = load_sessions(tmp_path / "canon")
    assert sessions[0].agent == "claude-code"
    res = runner.invoke(app, ["hook", "session-start", "-m", str(dreamed_store.root)], input="{}")
    assert res.exit_code == 0 and "additionalContext" in res.stdout


def test_holdout_stats_and_ineffective() -> None:
    recs = []
    outcomes = {}
    for i in range(12):
        recs.append({"event": "injected", "note_id": "n-a", "session_id": f"i{i}"})
        recs.append({"event": "withheld", "note_id": "n-a", "session_id": f"w{i}"})
        outcomes[f"i{i}"] = "pass" if i % 2 else "fail"
        outcomes[f"w{i}"] = "pass" if i % 2 else "fail"
    stats = holdout_stats(recs + recs, outcomes)  # duplicates are counted once
    assert stats["n-a"].injected_n == 12 and stats["n-a"].lift == 0
    assert "n-a" in ineffective_notes(stats, 10) and not ineffective_notes(stats, 13)
    us = usage_stats(recs, outcomes, 10)
    assert us.sessions_seen == 12 and "n-a" in us.ineffective
