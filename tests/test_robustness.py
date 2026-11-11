"""Robustness: interruption, recovery, locks, missing model, idempotence, caps, budgets, review flows."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
from typer.testing import CliRunner

from reverie.adapters.generic_jsonl import write_session
from reverie.cli import app
from reverie.core.config import ReverieConfig
from reverie.core.store import ENTRY_FILE, LockHeld, StoreError, git
from reverie.dream.pipeline import MARKER, DreamOptions, PendingProposal, run_dream
from reverie.online.review import approve, list_proposals, reject, reject_note, show_proposal
from reverie.providers.mock_provider import MockProvider
from tests.conftest import make_session
from tests.helpers import SESSIONS, dream, new_store

runner = CliRunner()


class Boom(RuntimeError):
    pass


def snapshot(store) -> dict[str, str]:
    return {p.relative_to(store.root).as_posix(): p.read_text() for p in store.root.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(store.root).parts}


@pytest.mark.parametrize("stage", ["extract", "verify", "reconcile", "promote", "write"])
def test_interrupted_dream_rolls_back_cleanly(tmp_path: Path, stage: str) -> None:
    store = new_store(tmp_path)
    head, before = store.head(), snapshot(store)

    def fail(s: str) -> None:
        if s == stage:
            raise Boom(s)

    with pytest.raises(Boom):
        dream(store, fail_hook=fail)
    assert store.head() == head and store.current_branch() == "main"
    assert not store.branches("dream/*") and not (store.root / ".git" / MARKER).exists()
    after = {k: v for k, v in snapshot(store).items() if not k.startswith(".reverie/runs/")}
    assert after == before
    assert not store.lock_path.exists()
    # and the next run works
    rep = dream(store)
    assert rep.status == "ok" and rep.branch


def test_killed_dream_is_recovered_on_next_run(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    # simulate a process killed mid-write: branch created, files half-written, marker left behind
    git(store.root, "checkout", "-q", "-b", "dream/dead")
    (store.root / "topics").mkdir()
    (store.root / "topics" / "half.md").write_text("# half\n- partial [id: n-zzzzzz]\n")
    (store.root / ".git" / MARKER).write_text(json.dumps({"branch": "dream/dead", "base_branch": "main"}))
    rep = dream(store)
    assert any("recovered" in w for w in rep.warnings)
    assert "dream/dead" not in store.branches("dream/*")
    assert not (store.root / "topics" / "half.md").exists() or "n-zzzzzz" not in (store.root / "topics/half.md").read_text()


def test_concurrent_dreams_prevented(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    with store.lock(), pytest.raises(LockHeld):
        dream(store)


def test_dirty_tree_refused(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    (store.root / "notes.md").write_text("- uncommitted\n")
    with pytest.raises(StoreError, match="uncommitted"):
        dream(store)


def test_missing_model_runs_deterministic_only(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    rep = run_dream(DreamOptions(sessions=SESSIONS, memory=store.root), ReverieConfig(), notify=lambda m: None)
    assert not rep.llm_available and rep.provider == "none"
    assert any("no model configured" in w and "reverie.toml" in w for w in rep.warnings)
    # nothing extracted, sessions stay un-dreamed for a later run with a model
    assert rep.status == "no_changes"
    res = runner.invoke(app, ["dream", "--sessions", str(SESSIONS), "--memory", str(store.root), "--json"])
    assert res.exit_code == 3
    assert json.loads(res.stdout)["llm_available"] is False


def test_cloud_provider_without_consent_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = new_store(tmp_path)
    cfg_file = tmp_path / "reverie.toml"
    cfg_file.write_text('[llm]\nprovider = "anthropic"\n')
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-0000000000000000000000")
    res = runner.invoke(app, ["dream", "-s", str(SESSIONS), "-m", str(store.root), "--config", str(cfg_file)])
    assert res.exit_code == 3 and "allow_cloud" in res.stdout


def test_pending_proposal_blocks_new_dream(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    rep = dream(store)
    assert rep.branch and not rep.merged
    with pytest.raises(PendingProposal):
        dream(store)
    res = runner.invoke(app, ["dream", "-s", str(SESSIONS), "-m", str(store.root)])
    assert res.exit_code == 7


def test_review_approve_reject_rollback(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    first = store.head()
    rep = dream(store)
    rid = rep.run_id
    items = list_proposals(store)
    assert items[0]["id"] == rid and items[0]["status"] == "pending"
    assert "# Dream proposal" in show_proposal(store, rid)
    res = approve(store, rid)
    assert res["guards_added"] == 1 and (store.root / "guards.toml").exists()
    assert "python -m pytest" in (store.root / ENTRY_FILE).read_text()
    # reject one note: it leaves the live memory and is blocked from coming back
    note_id = next(n.id for p, n in store.load().all_notes() if "type hints" in n.text)
    reject_note(store, note_id)
    path, note = store.load().find(note_id)
    assert path == "archive.md" and note.status == "rejected"
    # roll memory back to before the dream
    store.rollback(first)
    assert "python -m pytest" not in (store.root / ENTRY_FILE).read_text()
    assert not (store.root / "guards.toml").exists()


def test_rejected_proposal_is_not_reproposed(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    rep = dream(store)
    out = reject(store, rep.run_id)
    assert out["notes_blocked"] > 0 and store.branches("rejected/*")
    again = dream(store)
    assert again.status == "no_changes"
    assert any(p["status"] == "rejected" for p in list_proposals(store))


def test_rejected_note_not_readded_by_new_sessions(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    dream(store, auto_accept=True)
    note_id = next(n.id for p, n in store.load().all_notes() if "type hints" in n.text)
    reject_note(store, note_id)
    # a new session repeats the preference; the candidate text matches the rejected fingerprint
    d = tmp_path / "more"
    d.mkdir()
    s = json.loads((SESSIONS / "s02.jsonl").read_text())
    s["session_id"] = "s02b"
    (d / "s02b.jsonl").write_text(json.dumps(s))
    script = json.loads((SESSIONS.parent / "oracle_script.json").read_text())["script"]
    script["extract"]["s02b"] = [{**c, "evidence": [e.replace("s02", "s02b") for e in c["evidence"]]}
                                 for c in script["extract"]["s02"]]
    from reverie.providers.scripted import ScriptedOracle

    rep = dream(store, sessions=d, provider=MockProvider(rule=ScriptedOracle(script)), auto_accept=True)
    live = [n for p, n in store.load().all_notes() if "type hints" in n.text and n.status != "rejected"]
    assert not live and rep.counts.get("dropped", 0) >= 1


def test_budget_exceeded_saves_partial_progress(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    cfg = ReverieConfig()
    cfg.budget.max_tokens = 3000  # the scripted mock reports ~1k tokens per extract call
    rep = dream(store, cfg=cfg, auto_accept=True)
    assert rep.status == "partial_budget"
    state = store.read_state()
    done = len(state.get("dreamed", {}))
    assert 0 < done < 12
    # next run with budget finishes the rest
    rep2 = dream(store, auto_accept=True)
    assert rep2.status == "ok" and len(store.read_state()["dreamed"]) == 12


def test_memory_stays_within_caps_across_10_dreams(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    cfg = ReverieConfig()
    cfg.dream.memory_md_max_lines = 12
    cfg.dream.memory_md_max_bytes = 1500
    rng = random.Random(7)
    script = {"extract": {}, "relations": [], "topics": {}}
    from reverie.providers.scripted import ScriptedOracle

    for i in range(10):
        d = tmp_path / f"batch{i}"
        d.mkdir()
        for j in range(4):
            sid = f"b{i}s{j}"
            tool = rng.choice(["npm", "yarn", "cargo", "go", "mvn", "gradle", "make", "tox"])
            events = [{"type": "user_msg", "text": f"always run {tool} check {i} before committing step {j}"}]
            write_session(make_session(sid, events, started=f"2026-04-{i + 1:02d}T0{j}:00:00Z"), d / f"{sid}.jsonl")
            script["extract"][sid] = [{"text": f"Always run {tool} check {i} before committing step {j}",
                                       "kind": "rule", "evidence": [f"{sid}#0"]}]
        mock = MockProvider(rule=ScriptedOracle(script))
        rep = dream(store, sessions=d, provider=mock, cfg=cfg, auto_accept=True)
        md = (store.root / ENTRY_FILE).read_text()
        assert md.count("\n") <= 12 and len(md.encode()) <= 1500, f"dream {i}: cap exceeded"
        assert rep.metrics["memory_md"]["within_cap"]
        assert rep.metrics["dangling_links"] == 0


def test_cli_end_to_end_with_mock_config(tmp_path: Path) -> None:
    script = SESSIONS.parent / "oracle_script.json"
    cfg_file = tmp_path / "reverie.toml"
    cfg_file.write_text(f'[llm]\nprovider = "mock"\nmock_responses = "{script.as_posix()}"\n')
    mem = tmp_path / "mem"
    assert runner.invoke(app, ["init", str(mem)]).exit_code == 0
    res = runner.invoke(app, ["dream", "-s", str(SESSIONS), "-m", str(mem), "--config", str(cfg_file), "--json"])
    assert res.exit_code == 0, res.stdout
    rid = json.loads(res.stdout)["run_id"]
    assert rid in runner.invoke(app, ["proposals", "list", "-m", str(mem)]).stdout
    assert "Guard proposals" in runner.invoke(app, ["proposals", "show", rid, "-m", str(mem)]).stdout
    assert runner.invoke(app, ["proposals", "approve", rid, "-m", str(mem)]).exit_code == 0
    glist = json.loads(runner.invoke(app, ["guards", "list", "-m", str(mem), "--json"]).stdout)
    gid = glist[0]["id"]
    assert runner.invoke(app, ["guards", "disable", gid, "-m", str(mem)]).exit_code == 0
    assert json.loads(runner.invoke(app, ["guards", "list", "-m", str(mem), "--json"]).stdout)[0]["enabled"] is False
    stats = json.loads(runner.invoke(app, ["stats", "-m", str(mem), "--json"]).stdout)
    assert stats["memory"]["live_notes"] >= 5 and stats["success_levels"]["used"].startswith("not measured")
    log = json.loads(runner.invoke(app, ["memory", "log", "-m", str(mem), "--json"]).stdout)
    assert len(log) >= 3
    assert runner.invoke(app, ["memory", "rollback", log[-1]["rev"], "-m", str(mem)]).exit_code == 0
    assert runner.invoke(app, ["proposals", "show", "nope", "-m", str(mem)]).exit_code == 1
    assert runner.invoke(app, ["search", "pgbouncer", "-m", str(mem)]).exit_code == 0
    assert runner.invoke(app, ["doctor", "--json"]).exit_code == 0
    assert runner.invoke(app, ["version"]).stdout.strip() == "0.1.0"


def test_cli_import_and_unsupported_version(tmp_path: Path) -> None:
    res = runner.invoke(app, ["import", str(SESSIONS), "--json"])
    assert res.exit_code == 0 and json.loads(res.stdout)["sessions_new"] == 12
    res2 = runner.invoke(app, ["import", str(SESSIONS), "--json"])
    assert json.loads(res2.stdout)["files_unchanged"] == 12
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"schema_version": "7", "session_id": "x", "started_at": "t", "events": []}))
    assert runner.invoke(app, ["import", str(bad)]).exit_code == 6
    assert runner.invoke(app, ["import", str(tmp_path / "missing")]).exit_code == 8
    hits = runner.invoke(app, ["search", "pgbouncer", "--json"])
    assert any(h["ref"].startswith("s09#") for h in json.loads(hits.stdout))


def test_retention_applied_from_config(tmp_path: Path) -> None:
    from reverie.core.config import experience_db_path
    from reverie.core.experience import ExperienceStore

    store = new_store(tmp_path)
    cfg = ReverieConfig()
    cfg.store.retention_days = 30  # fixture sessions are from March 2026, months before "now"
    rep = dream(store, cfg=cfg)
    assert any("retention: removed 12" in w for w in rep.warnings)
    with ExperienceStore(experience_db_path(cfg)) as exp:
        assert exp.counts()["sessions"] == 0


def test_readme_documents_every_config_key() -> None:
    from reverie.core.config import ReverieConfig

    readme = (Path(__file__).parent.parent / "README.md").read_text()
    cfg = ReverieConfig()
    for section in ("llm", "budget", "cache", "dream", "online", "store"):
        for name in type(getattr(cfg, section)).model_fields:
            assert f"`{section}.{name}`" in readme, f"README is missing {section}.{name}"


def test_foreign_memory_repo_keeps_artifacts_out_of_git(tmp_path: Path) -> None:
    """A memory repo created with plain git (no reverie .gitignore) must not commit runs or usage logs."""
    from reverie.core.store import MemoryStore

    root = tmp_path / "plain"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "MEMORY.md").write_text("# Memory\n\n- hand-written note\n\n## Index\n")
    git(root, "add", "MEMORY.md")
    git(root, "commit", "-q", "-m", "init")
    store = MemoryStore(root)
    rep = dream(store, auto_accept=True)
    tracked = git(root, "ls-files").stdout.split()
    assert not any(p.startswith(".reverie/runs/") for p in tracked) and ".reverie/usage.jsonl" not in tracked
    assert store.is_clean() and "- hand-written note" in (root / "MEMORY.md").read_text()
    assert Path(rep.run_dir).exists()
    assert dream(store).status == "no_changes"
