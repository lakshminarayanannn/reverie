"""End-to-end dream on the 12 synthetic fixture sessions with the scripted (gullible) mock.

Each test asserts one planted behavior from tests/fixtures/build_fixtures.py. The mock
proposes the bad candidates a careless model would; the engine must remove them.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from reverie.core.config import ReverieConfig
from reverie.core.store import ENTRY_FILE, MemoryStore
from reverie.dream.guards import validate_guard
from reverie.dream.ingest import ingest
from tests.helpers import FAKE_KEY, SESSIONS, all_memory_text, dream, new_store, oracle


@pytest.fixture(scope="module")
def dreamed(tmp_path_factory: pytest.TempPathFactory):
    import os

    home = tmp_path_factory.mktemp("home")
    old = os.environ.get("REVERIE_HOME")
    os.environ["REVERIE_HOME"] = str(home)
    try:
        store = new_store(tmp_path_factory.mktemp("e2e"))
        mock = oracle()
        logger = logging.getLogger("reverie")
        records: list[str] = []

        class Grab(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(self.format(record))

        h = Grab(level=logging.DEBUG)
        logger.addHandler(h)
        logger.setLevel(logging.DEBUG)
        try:
            report = dream(store, provider=mock, auto_accept=True)
        finally:
            logger.removeHandler(h)
        yield store, report, mock, "\n".join(records), home
    finally:
        if old is None:
            os.environ.pop("REVERIE_HOME", None)
        else:
            os.environ["REVERIE_HOME"] = old


def notes(store: MemoryStore):
    return {n.id: (p, n) for p, n in store.load().all_notes()}


def find(store: MemoryStore, needle: str):
    return [(p, n) for p, n in store.load().all_notes() if needle in n.text]


def test_dream_succeeds_and_merges(dreamed) -> None:
    store, report, *_ = dreamed
    assert report.status == "ok" and report.merged and report.provider == "mock"
    assert store.current_branch() == "main" and store.is_clean()
    assert not store.branches("dream/*")


def test_1_repeated_trap_becomes_one_verified_trap_note(dreamed) -> None:
    store, *_ = dreamed
    hits = find(store, "python -m pytest")
    assert len(hits) == 1
    path, note = hits[0]
    assert note.kind == "trap" and note.status in ("verified", "active")
    assert set(note.source_sessions()) == {"s01", "s02", "s03"}
    assert path == ENTRY_FILE  # cited by 3 sessions and general -> promoted


def test_2_preference_kept_with_source(dreamed) -> None:
    store, *_ = dreamed
    hits = find(store, "type hints")
    assert len(hits) == 1 and hits[0][1].kind == "preference" and hits[0][1].sources == ["s02#0"]


def test_3_contradiction_resolves_to_newer_with_both_sources(dreamed) -> None:
    store, *_ = dreamed
    new = find(store, "ruff check .")
    old = find(store, "Lint the project with `make lint`")
    assert len(new) == 1 and len(old) == 1
    new_path, new_note = new[0]
    old_path, old_note = old[0]
    assert new_note.status in ("verified", "active") and new_path.startswith("topics/")
    assert old_note.status == "superseded" and old_path == "archive.md"
    assert new_note.extra["replaces"] == old_note.id and old_note.extra["superseded_by"] == new_note.id
    assert "s05" in new_note.source_sessions() and "s04" in old_note.source_sessions()
    memory_md = (store.root / ENTRY_FILE).read_text()
    assert "make lint`." not in memory_md


def test_4_stale_fact_is_retired(dreamed) -> None:
    store, *_ = dreamed
    live = [n for p, n in find(store, "port 8000") if p != "archive.md"]
    assert len(live) == 1 and "8080" in live[0].text  # only the updated note mentions 8000, as "no longer used"
    retired = [n for p, n in find(store, "runs on port 8000.") if p == "archive.md"]
    assert retired and retired[0].status == "stale"


def test_5_secret_redacted_everywhere(dreamed) -> None:
    store, report, mock, logs, home = dreamed
    assert FAKE_KEY not in all_memory_text(store)
    assert FAKE_KEY not in mock.all_prompt_text()
    assert FAKE_KEY not in logs
    db_files = list(Path(home).glob("experience.db*"))
    assert db_files and all(FAKE_KEY.encode() not in f.read_bytes() for f in db_files)
    assert report.metrics["secrets_redacted"].get("anthropic_key") == 1
    assert "[REDACTED:anthropic_key]" in mock.all_prompt_text()


def test_6_prompt_injection_never_becomes_memory(dreamed) -> None:
    store, *_ = dreamed
    text = all_memory_text(store)
    live = "\n".join(n.text for p, n in store.load().all_notes())
    assert "rm -rf" not in live
    assert not re.search(r"rm -rf /tmp/build \[id:", text)


def test_7_rederivable_listing_is_not_a_note(dreamed) -> None:
    store, *_ = dreamed
    assert not find(store, "src directory contains")


def test_8_critical_value_stored_verbatim(dreamed) -> None:
    store, *_ = dreamed
    hits = find(store, "6543")
    assert len(hits) == 1
    note = hits[0][1]
    assert note.quote is True and "`psql -h staging-db -p 6543`" in note.text


def test_9_pass_fail_contrast_yields_verified_rule(dreamed) -> None:
    store, *_ = dreamed
    hits = find(store, "APP_ENV=test")
    assert len(hits) == 1
    _, note = hits[0]
    assert note.kind == "rule" and note.status in ("verified", "active")
    assert note.extra.get("contrast") == "true"
    assert set(note.source_sessions()) == {"s10", "s11"}


def test_10_guard_proposal_with_correct_regex(dreamed) -> None:
    store, *_ = dreamed
    import tomllib

    guards = tomllib.loads((store.root / "guards.toml").read_text())["guard"]
    assert len(guards) == 1
    g = guards[0]
    rx = re.compile(g["pattern"])
    assert rx.search("pytest tests/test_utils.py") and rx.search("  pytest -q")
    assert not rx.search("python -m pytest -q") and not rx.search("APP_ENV=test python -m pytest")
    assert g["replacement"] == "python -m pytest" and g["mode"] == "warn"
    assert g["message"].startswith("reverie guard (automated; may be outdated):")
    assert sorted(g["evidence_sessions"]) == ["s01", "s02", "s03"]


def test_11_injected_guard_is_not_proposed(dreamed) -> None:
    store, *_ = dreamed
    import tomllib

    guards = tomllib.loads((store.root / "guards.toml").read_text())["guard"]
    assert all("git" not in g["pattern"] for g in guards)
    assert not find(store, "git is broken")
    # and the validator alone would also reject it: git succeeded in s12
    ing = ingest(SESSIONS, ReverieConfig())
    _, reason = validate_guard(r"^\s*git\b", "echo use the web UI", ing, 2)
    assert reason is not None


def test_unsupported_claim_dropped_and_logged(dreamed) -> None:
    store, report, *_ = dreamed
    assert not find(store, "PostgreSQL 16")
    proposal = (Path(report.run_dir) / "PROPOSAL.md").read_text()
    assert "PostgreSQL 16" in proposal and "| nowhere |" in proposal


def test_index_links_resolve_and_metrics_written(dreamed) -> None:
    store, report, *_ = dreamed
    assert report.metrics["dangling_links"] == 0
    assert report.metrics["memory_md"]["within_cap"]
    for name in ("metrics.json", "PROPOSAL.md", "candidates.json", "verified.json", "plan.json", "ingest.json"):
        assert (Path(report.run_dir) / name).exists()
    memory_md = (store.root / ENTRY_FILE).read_text()
    for link in re.findall(r"\[\[([^\]]+)\]\]", memory_md):
        assert (store.root / f"{link}.md").exists()
    assert "archive" not in memory_md


def test_redream_unchanged_changes_nothing(dreamed) -> None:
    store, *_ = dreamed
    head = store.head()
    again = dream(store, provider=oracle())
    assert again.status == "no_changes" and store.head() == head and not store.branches("dream/*")
