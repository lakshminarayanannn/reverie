"""Append-only experience store (SQLite + FTS5).

Holds redacted sessions and events outside the memory repo, so raw history can be
searched (``reverie search``) and re-checked without bloating memory. Imports are
idempotent: an event's id is a hash of (agent, session id, source event id) or, when
the source has no ids, of (agent, session id, idx, content).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reverie.core.schema import Event, Note, Outcome, Session

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_key TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    session_id TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    cwd TEXT,
    outcome_label TEXT,
    outcome_source TEXT,
    outcome_detail TEXT,
    source_path TEXT,
    imported_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    idx INTEGER NOT NULL,
    ts TEXT,
    type TEXT NOT NULL,
    text TEXT,
    tool_name TEXT,
    tool_input TEXT,
    tool_output TEXT,
    exit_code INTEGER,
    status TEXT,
    source_event_id TEXT,
    imported_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_session ON events(session_key, idx);
CREATE TABLE IF NOT EXISTS checkpoints (
    source TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    imported_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id TEXT,
    session_id TEXT,
    event TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(event_id UNINDEXED, session_id UNINDEXED, body);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(note_id UNINDEXED, path UNINDEXED, body);
"""


def session_key(agent: str, session_id: str) -> str:
    return hashlib.sha256(f"{agent}\0{session_id}".encode()).hexdigest()[:32]


def event_id(agent: str, session_id: str, event: Event) -> str:
    if event.source_event_id:
        basis = f"{agent}\0{session_id}\0src\0{event.source_event_id}"
    else:
        content = json.dumps(event.model_dump(exclude={"ts"}), sort_keys=True)
        basis = f"{agent}\0{session_id}\0idx\0{event.idx}\0{content}"
    return hashlib.sha256(basis.encode()).hexdigest()[:32]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class ImportResult:
    sessions_seen: int = 0
    sessions_new: int = 0
    events_new: int = 0
    events_existing: int = 0
    skipped_unchanged_file: bool = False


@dataclass
class SearchHit:
    kind: str  # "event" | "note"
    ref: str  # session_id#idx or note id
    snippet: str
    score: float


def fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 query: quoted terms joined with OR."""
    terms = [t for t in "".join(c if c.isalnum() or c in "_-." else " " for c in text).split() if t]
    if not terms:
        return '""'
    return " OR ".join('"' + t.replace('"', "") + '"' for t in terms[:32])


class ExperienceStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        # executescript manages its own transaction; the DDL is idempotent.
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> ExperienceStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # -- import -------------------------------------------------------------

    def file_unchanged(self, source: Path) -> bool:
        row = self._conn.execute(
            "SELECT size, mtime_ns, sha256 FROM checkpoints WHERE source = ?", (str(source.resolve()),)
        ).fetchone()
        if row is None:
            return False
        st = source.stat()
        if row["size"] == st.st_size and row["mtime_ns"] == st.st_mtime_ns:
            return True
        return bool(row["sha256"] == file_sha256(source))

    def checkpoint(self, source: Path) -> None:
        st = source.stat()
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO checkpoints(source, size, mtime_ns, sha256, imported_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(source.resolve()), st.st_size, st.st_mtime_ns, file_sha256(source), time.time()),
            )

    def import_session(self, session: Session, source_path: str | None = None) -> ImportResult:
        """Insert a (redacted) session. Existing events are left untouched (append-only)."""
        result = ImportResult(sessions_seen=1)
        key = session_key(session.agent, session.session_id)
        now = time.time()
        with self.transaction() as conn:
            existing = conn.execute("SELECT 1 FROM sessions WHERE session_key = ?", (key,)).fetchone()
            if existing is None:
                result.sessions_new = 1
                conn.execute(
                    "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (key, session.agent, session.session_id, session.started_at, session.ended_at,
                     session.cwd, session.outcome.label, session.outcome.source, session.outcome.detail,
                     source_path, now),
                )
            else:
                # Resumed / completed sessions may gain an end time or an outcome label.
                conn.execute(
                    "UPDATE sessions SET ended_at = COALESCE(?, ended_at), "
                    "outcome_label = CASE WHEN ? != 'unknown' THEN ? ELSE outcome_label END, "
                    "outcome_source = CASE WHEN ? != 'unknown' THEN ? ELSE outcome_source END "
                    "WHERE session_key = ?",
                    (session.ended_at, session.outcome.label, session.outcome.label,
                     session.outcome.label, session.outcome.source, key),
                )
            for ev in session.events:
                eid = event_id(session.agent, session.session_id, ev)
                cur = conn.execute(
                    "INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (eid, key, ev.idx, ev.ts, ev.type, ev.text, ev.tool_name,
                     json.dumps(ev.tool_input, sort_keys=True) if ev.tool_input is not None else None,
                     ev.tool_output, ev.exit_code, ev.status, ev.source_event_id, now),
                )
                if cur.rowcount:
                    result.events_new += 1
                    body = "\n".join(p for p in (ev.text, ev.command(), ev.tool_output) if p)
                    if body:
                        conn.execute("INSERT INTO events_fts(event_id, session_id, body) VALUES (?, ?, ?)",
                                     (eid, session.session_id, body))
                else:
                    result.events_existing += 1
        return result

    # -- read ---------------------------------------------------------------

    def session_ids(self) -> list[tuple[str, str]]:
        rows = self._conn.execute("SELECT agent, session_id FROM sessions ORDER BY started_at").fetchall()
        return [(r["agent"], r["session_id"]) for r in rows]

    def load_session(self, agent: str, session_id: str) -> Session | None:
        key = session_key(agent, session_id)
        row = self._conn.execute("SELECT * FROM sessions WHERE session_key = ?", (key,)).fetchone()
        if row is None:
            return None
        events: list[Event] = []
        for ev in self._conn.execute("SELECT * FROM events WHERE session_key = ? ORDER BY idx, ts", (key,)):
            events.append(Event(
                idx=ev["idx"], ts=ev["ts"], type=ev["type"], text=ev["text"], tool_name=ev["tool_name"],
                tool_input=json.loads(ev["tool_input"]) if ev["tool_input"] else None,
                tool_output=ev["tool_output"], exit_code=ev["exit_code"], status=ev["status"],
                source_event_id=ev["source_event_id"],
            ))
        return Session(
            session_id=row["session_id"], agent=row["agent"], started_at=row["started_at"] or "",
            ended_at=row["ended_at"], cwd=row["cwd"], events=events,
            outcome=Outcome(label=row["outcome_label"] or "unknown", source=row["outcome_source"] or "none",
                            detail=row["outcome_detail"]),
        )

    def find_session(self, session_id: str) -> Session | None:
        """Load a session by id regardless of agent (first match)."""
        row = self._conn.execute("SELECT agent FROM sessions WHERE session_id = ? ORDER BY agent LIMIT 1",
                                 (session_id,)).fetchone()
        return self.load_session(row["agent"], session_id) if row else None

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for table in ("sessions", "events", "checkpoints", "usage"):
            out[table] = int(self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        return out

    # -- notes index ----------------------------------------------------------

    def index_notes(self, notes: list[tuple[str, Note]]) -> int:
        with self.transaction() as conn:
            conn.execute("DELETE FROM notes_fts")
            for path, note in notes:
                conn.execute("INSERT INTO notes_fts(note_id, path, body) VALUES (?, ?, ?)",
                             (note.id, path, note.text))
        return len(notes)

    def search(self, query: str, limit: int = 10, kinds: tuple[str, ...] = ("note", "event")) -> list[SearchHit]:
        q = fts_query(query)
        hits: list[SearchHit] = []
        if "note" in kinds:
            for row in self._conn.execute(
                "SELECT note_id, path, snippet(notes_fts, 2, '[', ']', '…', 16) AS snip, bm25(notes_fts) AS s "
                "FROM notes_fts WHERE notes_fts MATCH ? ORDER BY s LIMIT ?", (q, limit)
            ):
                hits.append(SearchHit("note", f"{row['note_id']} ({row['path']})", row["snip"], float(row["s"])))
        if "event" in kinds:
            for row in self._conn.execute(
                "SELECT f.session_id AS sid, e.idx AS idx, snippet(events_fts, 2, '[', ']', '…', 16) AS snip, "
                "bm25(events_fts) AS s FROM events_fts f JOIN events e ON e.event_id = f.event_id "
                "WHERE events_fts MATCH ? ORDER BY s LIMIT ?", (q, limit)
            ):
                hits.append(SearchHit("event", f"{row['sid']}#{row['idx']}", row["snip"], float(row["s"])))
        hits.sort(key=lambda h: h.score)
        return hits[:limit]

    # -- usage & retention --------------------------------------------------------

    def log_usage(self, records: list[dict[str, Any]]) -> None:
        with self.transaction() as conn:
            for rec in records:
                conn.execute("INSERT INTO usage(note_id, session_id, event, ts) VALUES (?, ?, ?, ?)",
                             (rec.get("note_id"), rec.get("session_id"), rec["event"], rec["ts"]))

    def apply_retention(self, older_than_iso: str) -> int:
        """Delete sessions (and their events) that started before ``older_than_iso``."""
        with self.transaction() as conn:
            keys = [r[0] for r in conn.execute(
                "SELECT session_key FROM sessions WHERE started_at < ?", (older_than_iso,))]
            for key in keys:
                ids = [r[0] for r in conn.execute("SELECT event_id FROM events WHERE session_key = ?", (key,))]
                for eid in ids:
                    conn.execute("DELETE FROM events_fts WHERE event_id = ?", (eid,))
                conn.execute("DELETE FROM events WHERE session_key = ?", (key,))
                conn.execute("DELETE FROM sessions WHERE session_key = ?", (key,))
        return len(keys)


def has_fts5() -> bool:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()
