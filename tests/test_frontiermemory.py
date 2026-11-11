"""FrontierMemory adapter: corpus conversion, verdict detection, benchmark dream, format check."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from reverie.adapters.frontiermemory.convert import convert_corpus, detect_verdict
from reverie.adapters.frontiermemory.run import benchmark_dream, check_memory, export_memory
from reverie.cli import app
from reverie.core.config import ReverieConfig
from reverie.providers.mock_provider import MockProvider
from reverie.providers.scripted import ScriptedOracle

runner = CliRunner()
CC = Path(__file__).parent / "fixtures" / "claude_code" / "transcript.jsonl"


def make_corpus(root: Path) -> Path:
    inputs = root / "inputs"
    inputs.mkdir(parents=True)
    lines = CC.read_text().splitlines()
    (inputs / "t0001.jsonl").write_text("\n".join(lines) + "\n" + json.dumps({"verifier_result": "FAIL"}) + "\n")
    (inputs / "t0002.jsonl").write_text("\n".join(lines) + "\nVerifier result: PASS\n")
    other = [{"role": "user", "content": "build the parser"}, {"role": "tool", "content": "ok"},
             {"event": "verifier", "text": "reward=0 -> FAIL"}]
    (inputs / "t0003.jsonl").write_text("\n".join(json.dumps(r) for r in other) + "\n")
    return inputs


def test_detect_verdict() -> None:
    assert detect_verdict([{"x": 1}, {"verifier_result": "PASS"}]) == "pass"
    assert detect_verdict(["...", "Verifier: FAIL"]) == "fail"
    assert detect_verdict([{"reward": 1.0}]) == "pass" and detect_verdict([{"reward": 0}]) == "fail"
    assert detect_verdict([{"text": "PASSWORD reset"}]) == "unknown"
    assert detect_verdict([]) == "unknown"


def test_convert_corpus(tmp_path: Path) -> None:
    sessions, rep = convert_corpus(make_corpus(tmp_path))
    by = {s.session_id: s for s in sessions}
    assert set(by) == {"t0001", "t0002", "t0003"}
    assert by["t0001"].outcome.label == "fail" and by["t0002"].outcome.label == "pass"
    assert by["t0003"].outcome.label == "fail" and by["t0003"].events[0].type == "system"
    assert rep.claude_code == 2 and rep.generic == 1 and rep.labels == {"fail": 2, "pass": 1}
    assert all(s.agent == "frontiermemory" for s in sessions)
    assert by["t0003"].started_at.startswith("0000-00-00T")  # no timestamps: ordered by file name


def test_check_memory_rules(tmp_path: Path) -> None:
    good = tmp_path / "good"
    good.mkdir()
    (good / "MEMORY.md").write_text("# Memory\n- x\n")
    assert check_memory(good)["ok"]
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "MEMORY.md").write_text("  \n")
    (bad / ".hidden").write_text("x")
    (bad / "bin.dat").write_bytes(b"\xff\xfe\x00")
    if sys.platform != "win32":
        os.symlink(bad / "MEMORY.md", bad / "link.md")
    errors = " ".join(check_memory(bad)["errors"])
    assert "dot" in errors and "UTF-8" in errors and "empty" in errors
    assert not check_memory(tmp_path / "missing")["ok"]
    assert "over the" in " ".join(check_memory(good, max_bytes=3)["errors"])


def test_benchmark_dream_with_mock(tmp_path: Path) -> None:
    inputs = make_corpus(tmp_path / "corpus")
    script = {"extract": {
        "t0001": [{"text": "Bare `pytest -q` fails with ModuleNotFoundError: No module named 'app'; use "
                           "`python -m pytest -q`.", "kind": "trap", "evidence": ["t0001#2", "t0001#3", "t0001#4"],
                   "critical": True}],
        "t0002": [{"text": "Bare `pytest -q` fails with ModuleNotFoundError: No module named 'app'; use "
                           "`python -m pytest -q`.", "kind": "trap", "evidence": ["t0002#2", "t0002#3", "t0002#4"],
                   "critical": True}]}, "relations": [], "topics": {}}
    out = tmp_path / "memory"
    res = benchmark_dream(inputs, out, ReverieConfig(), provider=MockProvider(rule=ScriptedOracle(script)),
                          work=tmp_path / "work")
    assert res["check"]["ok"], res["check"]
    assert res["convert"]["labels"] == {"fail": 2, "pass": 1}
    text = (out / "MEMORY.md").read_text()
    assert "python -m pytest -q" in text  # cited by two trajectories -> promoted
    assert not any(p.name.startswith(".") for p in out.rglob("*"))
    assert "ghp_FAKE" not in "".join(p.read_text() for p in out.rglob("*.md"))


def test_benchmark_dream_without_model_still_valid(tmp_path: Path) -> None:
    inputs = make_corpus(tmp_path / "corpus")
    res = benchmark_dream(inputs, tmp_path / "memory", ReverieConfig(), work=tmp_path / "work")
    assert res["check"]["ok"] and (tmp_path / "memory" / "MEMORY.md").read_text().strip()
    assert res["dream"]["llm_available"] is False


def test_export_skips_bookkeeping(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".reverie").mkdir(parents=True)
    (repo / ".reverie" / "state.json").write_text("{}")
    (repo / "MEMORY.md").write_text("# M\n")
    (repo / "archive.md").write_text("# A\n")
    (repo / "topics").mkdir()
    (repo / "topics" / "t.md").write_text("# T\n")
    assert export_memory(repo, tmp_path / "out") == ["MEMORY.md", "topics/t.md"]


def test_cli_frontiermemory(tmp_path: Path) -> None:
    inputs = make_corpus(tmp_path / "corpus")
    res = runner.invoke(app, ["frontiermemory", "dream", "--evidence", str(inputs), "--out", str(tmp_path / "m"),
                              "--json"])
    assert res.exit_code == 3  # no model configured: deterministic only, memory still valid
    assert runner.invoke(app, ["frontiermemory", "check", str(tmp_path / "m")]).exit_code == 0
    assert runner.invoke(app, ["frontiermemory", "check", str(tmp_path / "nope")]).exit_code == 1
    assert runner.invoke(app, ["frontiermemory", "dream", "--evidence", str(tmp_path / "x")]).exit_code == 8


def test_harbor_agent_module_imports_when_harbor_installed() -> None:
    pytest.importorskip("harbor")
    from reverie.adapters.frontiermemory.agent import ReverieMemoryAgent

    assert ReverieMemoryAgent.name() == "reverie-memory"


def test_verifier_line_wins_anywhere() -> None:
    recs = [{"message": {"content": "Verifier result: FAIL"}}] + [{"x": i} for i in range(10)] + ["noise PASS"]
    assert detect_verdict(recs) == "fail"
