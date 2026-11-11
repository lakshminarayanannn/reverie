"""Small filesystem helpers: atomic writes and JSON-lines appends."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file in the same dir, then rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True, default=str) + "\n")


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON record as a line. Single ``write`` call keeps lines intact under O_APPEND."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, default=str) + "\n"
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)


def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Read JSON lines; return (records, number of corrupt lines skipped)."""
    records: list[dict[str, Any]] = []
    bad = 0
    if not path.exists():
        return records, bad
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if isinstance(value, dict):
                records.append(value)
            else:
                bad += 1
    return records, bad
