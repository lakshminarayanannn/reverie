"""Adapter contract: turn an agent's native session files into canonical ``Session`` objects.

Every adapter must

* declare the source format versions it supports (``supported_versions``),
* detect the version at read time and raise ``UnsupportedFormatVersion`` on an unknown one,
* never drop events silently: every skipped record is counted with a reason, and
* report counts of sessions, events read, and events skipped.

Agent session stores change without notice; a silent parsing break would look like
"memory doesn't help", so loud failures are a feature.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, Field

from reverie.core.schema import Session


class UnsupportedFormatVersion(RuntimeError):
    """The input declares a format version this adapter does not support."""


class AdapterReport(BaseModel):
    adapter: str
    format_versions: list[str] = Field(default_factory=list)
    files_read: int = 0
    files_failed: int = 0
    sessions: int = 0
    events_read: int = 0
    events_skipped: dict[str, int] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)

    def skip(self, reason: str, n: int = 1) -> None:
        self.events_skipped[reason] = self.events_skipped.get(reason, 0) + n

    def merge(self, other: AdapterReport) -> None:
        self.files_read += other.files_read
        self.files_failed += other.files_failed
        self.sessions += other.sessions
        self.events_read += other.events_read
        for key, value in other.events_skipped.items():
            self.skip(key, value)
        self.errors.extend(other.errors)
        for version in other.format_versions:
            if version not in self.format_versions:
                self.format_versions.append(version)


class Adapter(ABC):
    name: str = "base"
    #: Human-readable description of supported source format versions.
    supported_versions: str = ""

    @abstractmethod
    def detect(self, path: Path) -> bool:
        """Cheap check whether ``path`` looks like this adapter's format."""

    @abstractmethod
    def read(self, path: Path) -> tuple[list[Session], AdapterReport]:
        """Read one file. Raise ``UnsupportedFormatVersion`` for unknown versions,
        ``ValueError`` for corrupt input."""


def first_json_line(path: Path, max_lines: int = 20) -> dict[str, object] | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for _, line in zip(range(max_lines), handle, strict=False):
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                return value if isinstance(value, dict) else None
    except OSError:
        return None
    return None


def session_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    files = [p for p in path.rglob("*") if p.is_file() and p.suffix in (".jsonl", ".json")]
    return sorted(f for f in files if not any(part.startswith(".") for part in f.relative_to(path).parts))


def count_types(values: list[str]) -> dict[str, int]:
    return dict(Counter(values))
