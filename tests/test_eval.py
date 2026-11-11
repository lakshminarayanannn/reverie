"""Evaluation: stats, mock runner, arms, isolation checks, dev/test separation, CLI."""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from reverie.cli import app
from reverie.eval.runner import (
    ArmContext,
    ClaudeCodeRunner,
    EvalRefused,
    EvalTask,
    MockTaskRunner,
    load_tasks,
    naive_summary,
    run_eval,
    scan_isolation,
)
from reverie.eval.stats import noise_floor, paired_bootstrap, sample_sd
from tests.conftest import make_session
from tests.helpers import SESSIONS, dream, new_store

EXAMPLES = Path(__file__).parent.parent / "examples"
runner = CliRunner()


def test_stats() -> None:
    assert sample_sd([1, 1, 1]) == 0 and sample_sd([0, 1]) == pytest.approx(0.7071, abs=1e-3)
    assert noise_floor({"a": [0, 1], "b": [1, 1]}) == pytest.approx(0.3536, abs=1e-3)
    assert noise_floor({"a": [1]}) is None
    ci = paired_bootstrap({"a": 1.0, "b": 1.0, "c": 0.5}, {"a": 0.0, "b": 0.5, "c": 0.5}, n_boot=2000)
    assert ci["n_tasks"] == 3 and ci["mean_diff"] == 0.5 and 0 <= ci["ci_low"] <= 0.5 <= ci["ci_high"] <= 1
    assert paired_bootstrap({}, {})["mean_diff"] is None
    same = paired_bootstrap({"a": 0.4, "b": 0.6}, {"a": 0.4, "b": 0.6}, n_boot=500)
    assert same["ci_low"] == same["ci_high"] == 0


def test_scan_isolation() -> None:
    ctx = ArmContext(arm="no_memory", forbidden_paths=["/mem/repo"])
    use = lambda name, inp: json.dumps({"type": "assistant", "message": {"content": [  # noqa: E731
        {"type": "tool_use", "name": name, "input": inp}]}})
    assert scan_isolation([use("Bash", {"command": "ls"})], ctx) is None
    assert "forbidden path" in scan_isolation([use("Read", {"file_path": "/mem/repo/MEMORY.md"})], ctx)
    assert "WebFetch" in scan_isolation(["not json", use("WebFetch", {"url": "x"})], ctx)


def test_naive_summary_deterministic(tmp_path: Path) -> None:
    s = make_session("s", [{"type": "user_msg", "text": "fix the build"}, {"type": "assistant_msg", "text": "done"}])
    out = naive_summary([s], tmp_path / "n", None)
    assert "fix the build" in (out / "MEMORY.md").read_text()
    out2 = naive_summary([s], tmp_path / "m", lambda corpus: "# M\n- " + str("fix the build" in corpus))
    assert (out2 / "MEMORY.md").read_text() == "# M\n- True\n"


def _contexts(tmp_path: Path, store) -> dict:
    naive = naive_summary([], tmp_path / "naive", None)
    return {"no_memory": ArmContext(arm="no_memory"),
            "raw_search": ArmContext(arm="raw_search", sessions_dir=str(SESSIONS)),
            "naive_summary": ArmContext(arm="naive_summary", memory_dir=str(naive)),
            "agent_native": ArmContext(arm="agent_native"),
            "reverie": ArmContext(arm="reverie", memory_dir=str(store.root))}


def test_run_eval_mock_all_arms_and_split_rules(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    dream(store, auto_accept=True)
    tasks = load_tasks(EXAMPLES / "eval-tasks")
    assert {t.split for t in tasks} == {"dev", "test"}
    ledger = tmp_path / "ledger.json"
    ctx = _contexts(tmp_path, store)
    kwargs = dict(contexts=ctx, ledger=ledger, prompt_versions={"extract": "2"}, purpose="p", tasks_dir=tmp_path)
    with pytest.raises(EvalRefused, match="3 repeats"):
        run_eval(tasks, MockTaskRunner(), split="dev", repeats=2, **kwargs)
    rep = run_eval(tasks, MockTaskRunner(), split="dev", repeats=3, **kwargs)
    assert len(rep.runs) == 4 * 5 * 3 and rep.invalid_runs == 0
    assert set(rep.arm_means) == {"no_memory", "raw_search", "naive_summary", "agent_native", "reverie"}
    assert rep.noise_floor is not None and "reverie" in rep.vs_no_memory and "reverie" in rep.vs_raw_search
    assert "measure nothing" in " ".join(rep.notes)
    assert rep.arm_means["reverie"] > rep.arm_means["no_memory"]  # mock: reverie memory holds the facts
    again = run_eval(tasks, MockTaskRunner(), split="dev", repeats=3, **kwargs)
    assert again.arm_means == rep.arm_means  # deterministic
    # test split: tasks_dir must be a clean git repo, and the config must have been evaluated on dev
    with pytest.raises(EvalRefused, match="git repo"):
        run_eval(tasks, MockTaskRunner(), split="test", repeats=3, **kwargs)
    repo = tmp_path / "tasks"
    shutil.copytree(EXAMPLES / "eval-tasks", repo)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "tasks"], check=True)
    kwargs["tasks_dir"] = repo
    with pytest.raises(EvalRefused, match="never evaluated on the dev split"):
        run_eval(tasks, MockTaskRunner(), split="test", repeats=3, **{**kwargs, "purpose": "changed"})
    (repo / "dirty.txt").write_text("x")
    with pytest.raises(EvalRefused, match="dirty"):
        run_eval(tasks, MockTaskRunner(), split="test", repeats=3, **kwargs)
    (repo / "dirty.txt").unlink()
    test_rep = run_eval(tasks, MockTaskRunner(), split="test", repeats=3, **kwargs)
    assert test_rep.commit and len(test_rep.commit) == 40 and test_rep.tasks == ["t05-refund-tests", "t06-hotfix-lint"]


@pytest.mark.skipif(sys.platform == "win32", reason="fake CLI is a POSIX script")
def test_claude_code_runner_with_fake_cli(tmp_path: Path) -> None:
    fake = tmp_path / "claude"
    fake.write_text(f"""#!{sys.executable}
import json, sys, os
prompt = sys.stdin.read()
args = sys.argv[1:]
open("RESULT.txt", "w").write("3")
touch = "/forbidden/mem" if "touch-forbidden" in prompt else "ok.txt"
print(json.dumps({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "name": "Read",
      "input": {{"file_path": touch}}}}]}}}}))
print(json.dumps({{"type": "result", "total_cost_usd": 0.01, "usage": {{"input_tokens": 5, "output_tokens": 6}},
                   "args": args}}))
""")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    task = EvalTask.load(EXAMPLES / "eval-tasks-real" / "t01-count-tests")
    r = ClaudeCodeRunner(binary=str(fake))
    ok = r.run(task, ArmContext(arm="no_memory", forbidden_paths=["/forbidden/mem"]), 0)
    assert ok.valid and ok.passed and ok.cost_usd == 0.01 and ok.tokens == 11
    assert r.permission_mode == "acceptEdits" and "Bash(python3 *)" in r.allowed_tools
    bad_task = task.model_copy(update={"instruction": "touch-forbidden"})
    bad = r.run(bad_task, ArmContext(arm="no_memory", forbidden_paths=["/forbidden/mem"]), 0)
    assert not bad.valid and not bad.passed and "forbidden path" in bad.invalid_reason
    native = r.run(task, ArmContext(arm="agent_native", native_notes="# Notes\n- x\n"), 0)
    assert native.native_notes_after == "# Notes\n- x\n"


def test_claude_code_runner_requires_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(EvalRefused, match="not on PATH"):
        ClaudeCodeRunner()


def test_cli_eval_mock(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    dream(store, auto_accept=True)
    out = tmp_path / "report.json"
    res = runner.invoke(app, ["eval", "--tasks", str(EXAMPLES / "eval-tasks"), "--sessions", str(SESSIONS),
                              "-m", str(store.root), "--out", str(out)])
    assert res.exit_code == 0, res.stdout
    assert "noise floor" in res.stdout and "reverie vs no_memory" in res.stdout
    data = json.loads(out.read_text())
    assert data["runner"] == "mock" and len(data["runs"]) == 60
    outside_git = tmp_path / "tasks-not-in-git"
    shutil.copytree(EXAMPLES / "eval-tasks", outside_git)
    res = runner.invoke(app, ["eval", "--tasks", str(outside_git), "--sessions", str(SESSIONS),
                              "-m", str(store.root), "--split", "test", "--json"])
    assert res.exit_code == 7 and "git repo" in res.stdout
    res = runner.invoke(app, ["eval", "--tasks", str(EXAMPLES / "eval-tasks"), "--sessions", str(SESSIONS),
                              "-m", str(store.root), "--arms", "bogus"])
    assert res.exit_code == 2


def test_recall_tasks_graders(tmp_path: Path) -> None:
    """Each recall task's check passes the right answer and fails the stale or missing one."""
    cases = {"r1-staging-db": ("db.env", "STAGING_HOST=staging-db\nSTAGING_PORT=6543\n", "STAGING_PORT=5432"),
             "r2-dev-server": ("DEV_URL.txt", "http://127.0.0.1:8080\n", "http://127.0.0.1:8000"),
             "r3-lint": ("LINT_CMD.txt", "ruff check .\n", "make lint"),
             "r4-test-env": ("TEST_ENV.txt", "APP_ENV=test\n", "UNKNOWN"),
             "r5-test-cmd": ("TEST_CMD.txt", "python -m pytest\n", "pytest")}
    tasks = {t.id: t for t in load_tasks(EXAMPLES / "eval-tasks-recall")}
    assert set(tasks) == set(cases)
    for tid, (name, good, bad) in cases.items():
        for content, want in ((good, 0), (bad, 1)):
            d = tmp_path / f"{tid}-{want}"
            d.mkdir()
            (d / name).write_text(content)
            assert subprocess.run(tasks[tid].check, cwd=d).returncode == want, (tid, content)
