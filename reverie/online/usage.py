"""Usage tracking: ``.reverie/usage.jsonl`` in the memory repo (git-ignored).

Records: ``{"event", "note_id", "session_id", "ts", "dream", ...}`` where event is one of
``injected``, ``withheld`` (holdout), ``read`` (agent read a memory file), ``recalled``
(targeted recall), ``guard_fired``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from reverie.core.fsutil import append_jsonl, read_jsonl
from reverie.dream.prune import UsageStats
from reverie.online.holdout import holdout_stats, ineffective_notes

USAGE_FILE = ".reverie/usage.jsonl"


def usage_path(memory_root: Path) -> Path:
    return Path(memory_root) / USAGE_FILE


def now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def log_events(memory_root: Path, records: list[dict[str, Any]]) -> None:
    path = usage_path(memory_root)
    for rec in records:
        rec.setdefault("ts", now_iso())
        append_jsonl(path, rec)


def read_usage(memory_root: Path) -> list[dict[str, Any]]:
    records, _ = read_jsonl(usage_path(memory_root))
    return records


def usage_stats(records: list[dict[str, Any]], outcomes: dict[str, str], holdout_min: int) -> UsageStats:
    stats = UsageStats()
    sessions: set[str] = set()
    for rec in records:
        if rec.get("event") in ("injected", "read", "recalled"):
            if rec.get("session_id"):
                sessions.add(str(rec["session_id"]))
            nid = rec.get("note_id")
            if nid and nid != "*":
                dream = int(rec.get("dream", 0) or 0)
                stats.last_seen_dream[nid] = max(stats.last_seen_dream.get(nid, 0), dream)
    stats.sessions_seen = len(sessions)
    stats.ineffective = ineffective_notes(holdout_stats(records, outcomes), holdout_min)
    return stats


def guard_firings_per_week(records: list[dict[str, Any]], now: datetime | None = None) -> dict[str, float]:
    """Firings in the last 7 days per guard id (a deterministic 'failure prevented' count)."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=7)
    out: dict[str, float] = {}
    for rec in records:
        if rec.get("event") != "guard_fired":
            continue
        try:
            ts = datetime.fromisoformat(str(rec.get("ts", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts >= cutoff:
            gid = str(rec.get("guard_id", "?"))
            out[gid] = out.get(gid, 0) + 1
    return out


def success_levels(records: list[dict[str, Any]], live_note_ids: set[str]) -> dict[str, Any]:
    """The five success levels, reported separately and never merged."""
    injected = {r["note_id"] for r in records if r.get("event") == "injected" and r.get("note_id")}
    read = {r["note_id"] for r in records if r.get("event") in ("read", "recalled") and r.get("note_id")}
    return {
        "stored": len(live_note_ids),
        "injected": len(injected & live_note_ids),
        "read": len(read & live_note_ids),
        "used": "not measured (needs holdout_pct > 0 and labeled outcomes; see `reverie stats`)",
        "validated": "not measured (needs `reverie eval` on held-out tasks)",
    }
