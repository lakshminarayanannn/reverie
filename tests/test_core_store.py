from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from reverie.core.schema import Note
from reverie.core.store import (
    LockHeld,
    MemoryStore,
    StoreError,
    link_target,
    links_in,
    parse_file,
    parse_note_line,
    path_to_link,
)

SAMPLE = """# Memory: test

Free text the user wrote.
- A user bullet without metadata
- A spec-style entry [source: https://example.com/s/1; added: 2026-09-03]
- Run `make test` before pushing [id: n-abc123; kind: rule; status: active; source: s1#4, s2#7; added: 2026-01-01; custom_key: keep me; quote: true]
  - Nested managed [id: n-def456; status: verified; source: s3#1; added: 2026-01-02]

## Index
- [[topics/build]]
- [[metrics/x.sql]]
"""


def test_roundtrip_is_lossless() -> None:
    f = parse_file("MEMORY.md", SAMPLE)
    assert f.render() == SAMPLE
    notes = f.notes()
    assert [n.id for n in notes] == ["n-abc123", "n-def456"]
    first = notes[0]
    assert first.extra == {"custom_key": "keep me"}
    assert first.quote is True
    assert first.sources == ["s1#4", "s2#7"]
    assert first.source_sessions() == ["s1", "s2"]
    assert notes[1].indent == "  "


def test_unknown_metadata_survives_edit() -> None:
    f = parse_file("MEMORY.md", SAMPLE)
    note = f.notes()[0]
    note.status = "contested"
    out = f.render()
    assert "custom_key: keep me" in out
    assert "status: contested" in out
    assert "- A spec-style entry [source: https://example.com/s/1; added: 2026-09-03]" in out


def test_note_render_parse_with_brackets_in_text() -> None:
    note = Note(id="n-111111", text="Use arr[0] not arr[1]", sources=["s#1"])
    parsed = parse_note_line(note.render())
    assert parsed is not None and parsed.text == "Use arr[0] not arr[1]"


def test_invalid_status_kept_as_extra() -> None:
    parsed = parse_note_line("- x [id: n-1; status: weird; kind: odd]")
    assert parsed is not None
    assert parsed.status == "verified"
    assert parsed.extra == {"kind": "odd", "status": "weird"}


def test_links() -> None:
    assert links_in("see [[topics/build]] and [[a.sql]]") == ["topics/build", "a.sql"]
    assert link_target("topics/build") == "topics/build.md"
    assert link_target("metrics/x.sql") == "metrics/x.sql"
    assert path_to_link("topics/build.md") == "topics/build"


def test_init_load_write_commit_rollback(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "mem")
    store.init()
    store.init()  # idempotent
    assert store.exists() and store.is_clean()
    first = store.head()
    store.write_files({"topics/a.md": "# A\n- hello [id: n-aaaaaa; status: verified; added: 2026-01-01]\n"})
    sha = store.commit_all("add a")
    assert sha and not store.commit_all("noop")
    repo = store.load()
    assert repo.find("n-aaaaaa") is not None
    store.rollback(first)
    assert not (store.root / "topics/a.md").exists()
    with pytest.raises(StoreError):
        store.write_files({"../escape.md": "x"})


def test_init_refuses_non_empty_dir(tmp_path: Path) -> None:
    (tmp_path / "x.txt").write_text("hi")
    with pytest.raises(StoreError):
        MemoryStore(tmp_path).init()


def test_lock_prevents_concurrent(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "mem")
    store.init()
    with store.lock(), pytest.raises(LockHeld), store.lock():
        pass
    with store.lock():  # released
        pass


def _hold_lock(path: str, ready, release) -> None:  # pragma: no cover - runs in child
    store = MemoryStore(Path(path))
    with store.lock():
        ready.set()
        release.wait(10)


def test_lock_across_processes(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "mem")
    store.init()
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=_hold_lock, args=(str(store.root), ready, release))
    proc.start()
    try:
        assert ready.wait(30)
        with pytest.raises(LockHeld), store.lock():
            pass
    finally:
        release.set()
        proc.join(30)


def test_stale_lock_from_dead_process_is_broken(tmp_path: Path) -> None:
    import json
    import socket
    import time

    store = MemoryStore(tmp_path / "mem")
    store.init()
    store.lock_path.write_text(json.dumps({"pid": 999999, "host": socket.gethostname(), "ts": time.time()}))
    with store.lock():
        pass
