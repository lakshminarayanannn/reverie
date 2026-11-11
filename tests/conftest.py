from __future__ import annotations

import os
from pathlib import Path

import pytest

from reverie.core.schema import Event, Outcome, Session

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test's data/cache/config dirs inside a temp dir."""
    home = tmp_path_factory.mktemp("reverie-home")
    monkeypatch.setenv("REVERIE_HOME", str(home))
    monkeypatch.delenv("REVERIE_CONFIG", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    monkeypatch.chdir(home)
    os.environ.pop("REVERIE_CONFIG", None)


def make_session(sid: str, events: list[dict], started: str = "2026-01-01T00:00:00Z",
                 outcome: str = "unknown", agent: str = "generic") -> Session:
    evs = [Event(idx=i, **e) for i, e in enumerate(events)]
    out = Outcome(label=outcome, source="verifier" if outcome != "unknown" else "none")
    return Session(session_id=sid, agent=agent, started_at=started, events=evs, outcome=out)
