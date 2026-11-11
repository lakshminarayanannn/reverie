"""Session adapters and the loader that picks one per file."""

from __future__ import annotations

from pathlib import Path

from reverie.adapters.atif import AtifAdapter
from reverie.adapters.base import Adapter, AdapterReport, UnsupportedFormatVersion, session_files
from reverie.adapters.claude_code import ClaudeCodeAdapter
from reverie.adapters.generic_jsonl import GenericJsonlAdapter
from reverie.core.schema import Session

ADAPTERS: dict[str, type[Adapter]] = {
    "generic": GenericJsonlAdapter,
    "claude-code": ClaudeCodeAdapter,
    "atif": AtifAdapter,
}


def get_adapter(name: str) -> Adapter:
    if name not in ADAPTERS:
        raise ValueError(f"unknown adapter {name!r}; choose from {sorted(ADAPTERS)}")
    return ADAPTERS[name]()


def detect_adapter(path: Path) -> Adapter | None:
    for cls in (GenericJsonlAdapter, AtifAdapter, ClaudeCodeAdapter):
        adapter = cls()
        if adapter.detect(path):
            return adapter
    return None


def load_sessions(path: Path, adapter: str = "auto") -> tuple[list[Session], AdapterReport]:
    """Load every session file under ``path``.

    Corrupt or unrecognizable files are reported and skipped. An unsupported format
    version raises ``UnsupportedFormatVersion`` (fail loudly, never guess).
    """
    report = AdapterReport(adapter=adapter)
    sessions: list[Session] = []
    if not path.exists():
        raise FileNotFoundError(f"sessions path does not exist: {path}")
    for file in session_files(path):
        chosen = get_adapter(adapter) if adapter != "auto" else detect_adapter(file)
        if chosen is None:
            report.files_failed += 1
            report.errors.append(f"{file}: unrecognized session format (skipped)")
            continue
        try:
            got, sub = chosen.read(file)
        except UnsupportedFormatVersion:
            raise
        except (ValueError, OSError, UnicodeDecodeError) as exc:
            report.files_failed += 1
            report.errors.append(f"{file}: {exc} (skipped)")
            continue
        report.merge(sub)
        sessions.extend(got)
    return sessions, report


__all__ = [
    "ADAPTERS", "Adapter", "AdapterReport", "UnsupportedFormatVersion", "detect_adapter",
    "get_adapter", "load_sessions",
]
