from __future__ import annotations

import json
from pathlib import Path

from reverie.core.config import ReverieConfig
from reverie.core.store import MemoryStore
from reverie.dream.pipeline import DreamOptions, DreamReport, run_dream
from reverie.providers.mock_provider import MockProvider
from reverie.providers.scripted import ScriptedOracle

FIXTURES = Path(__file__).parent / "fixtures"
SESSIONS = FIXTURES / "sessions"
FAKE_KEY = "sk-ant-api03-FAKEfixtureKEY0123456789abcdefGHIJKLmnop"


def oracle() -> MockProvider:
    script = json.loads((FIXTURES / "oracle_script.json").read_text())["script"]
    return MockProvider(rule=ScriptedOracle(script))


def new_store(tmp: Path, name: str = "mem") -> MemoryStore:
    store = MemoryStore(tmp / name)
    store.init()
    return store


def dream(store: MemoryStore, sessions: Path = SESSIONS, provider: MockProvider | None = None,
          cfg: ReverieConfig | None = None, fail_hook=None, **kw: object) -> DreamReport:
    opts = DreamOptions(sessions=sessions, memory=store.root, **kw)  # type: ignore[arg-type]
    return run_dream(opts, cfg or ReverieConfig(), provider=provider if provider is not None else oracle(),
                     notify=lambda m: None, fail_hook=fail_hook)


def all_memory_text(store: MemoryStore) -> str:
    """Every file in the memory repo working tree, including .reverie/ (runs, proposals, state)."""
    parts = []
    for p in sorted(store.root.rglob("*")):
        if p.is_file() and ".git" not in p.relative_to(store.root).parts:
            parts.append(p.read_text(errors="replace"))
    return "\n".join(parts)
