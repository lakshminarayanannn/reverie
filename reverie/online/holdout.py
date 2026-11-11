"""Deterministic note holdout.

A note is withheld from a session when ``hash(session_id + note_id) mod 100 < holdout_pct``.
Comparing labeled outcomes of sessions that saw a note with sessions where it was held
out is the only way reverie measures whether a note is *used* (reads are not use).
"""

from __future__ import annotations

import hashlib
from typing import Any

from pydantic import BaseModel


def is_held_out(session_id: str, note_id: str, holdout_pct: int) -> bool:
    if holdout_pct <= 0:
        return False
    if holdout_pct >= 100:
        return True
    digest = hashlib.sha256((session_id + note_id).encode()).hexdigest()
    return int(digest[:8], 16) % 100 < holdout_pct


class HoldoutStat(BaseModel):
    note_id: str
    injected_n: int = 0
    injected_pass: int = 0
    withheld_n: int = 0
    withheld_pass: int = 0

    @property
    def injected_rate(self) -> float | None:
        return self.injected_pass / self.injected_n if self.injected_n else None

    @property
    def withheld_rate(self) -> float | None:
        return self.withheld_pass / self.withheld_n if self.withheld_n else None

    @property
    def lift(self) -> float | None:
        a, b = self.injected_rate, self.withheld_rate
        return None if a is None or b is None else a - b


def holdout_stats(records: list[dict[str, Any]], outcomes: dict[str, str]) -> dict[str, HoldoutStat]:
    """Aggregate pass rates per note over sessions with a pass/fail label."""
    seen: set[tuple[str, str, str]] = set()
    stats: dict[str, HoldoutStat] = {}
    for rec in records:
        event, nid, sid = rec.get("event"), rec.get("note_id"), rec.get("session_id")
        if event not in ("injected", "withheld") or not nid or not sid:
            continue
        label = outcomes.get(sid)
        if label not in ("pass", "fail") or (event, nid, sid) in seen:
            continue
        seen.add((event, nid, sid))
        st = stats.setdefault(nid, HoldoutStat(note_id=nid))
        if event == "injected":
            st.injected_n += 1
            st.injected_pass += label == "pass"
        else:
            st.withheld_n += 1
            st.withheld_pass += label == "pass"
    return stats


def ineffective_notes(stats: dict[str, HoldoutStat], min_sessions: int) -> dict[str, str]:
    """Notes with enough samples in both arms and no positive lift."""
    out: dict[str, str] = {}
    for nid, st in stats.items():
        if st.injected_n >= min_sessions and st.withheld_n >= min_sessions and (st.lift or 0.0) <= 0.0:
            out[nid] = (f"holdout: pass rate {st.injected_rate:.2f} with note vs {st.withheld_rate:.2f} without "
                        f"(n={st.injected_n}/{st.withheld_n}); no measured benefit")
    return out
