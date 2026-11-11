"""Secret redaction. Replaces secrets with ``[REDACTED:<kind>]`` and counts them by kind.

Values are never logged or returned; only counts leave this module.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable
from typing import Any

from reverie.core.schema import Event, Session

# Ordered: specific patterns first so the generic ones do not steal their matches.
_PATTERNS: list[tuple[str, re.Pattern[str], int]] = [
    # (kind, regex, group to replace; 0 = whole match)
    ("private_key", re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S), 0),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}"), 0),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}"), 0),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), 0),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"), 0),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), 0),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), 0),
    ("stripe_key", re.compile(r"\b[sr]k_(?:live|test)_[0-9A-Za-z]{16,}"), 0),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), 0),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/\-]{16,}=*)"), 1),
    # scheme://user:password@host  -> password only
    ("url_password", re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+:([^\s@/]+)@"), 1),
    # key=value style secrets (connection strings, env files, CLI flags)
    ("assigned_secret", re.compile(
        r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|token|"
        r"client[_-]?secret)\b[\"']?\s*[:=]\s*[\"']?([^\s\"';,&]{6,})"), 1),
]

_CANDIDATE_RE = re.compile(r"[A-Za-z0-9_\-+=]{32,}")
_ALREADY = re.compile(r"\[REDACTED:[a-z_]+\]")


def shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = Counter(value)
    n = len(value)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def _looks_like_secret(token: str, threshold: float) -> bool:
    if not (any(c.isupper() for c in token) and any(c.islower() for c in token)
            and any(c.isdigit() for c in token)):
        return False
    return shannon_entropy(token) >= threshold


class Redactor:
    """Stateless-per-call redactor that accumulates counts by kind across calls."""

    def __init__(self, entropy_threshold: float = 4.3, extra_patterns: list[str] | None = None) -> None:
        self.entropy_threshold = entropy_threshold
        self.counts: Counter[str] = Counter()
        self._extra = [re.compile(p) for p in (extra_patterns or [])]

    def redact(self, text: str | None) -> str | None:
        if text is None:
            return None
        if not text:
            return text
        out = text
        for kind, pattern, group in _PATTERNS:
            out = pattern.sub(self._replacer(kind, group), out)
        for pattern in self._extra:
            out = pattern.sub(self._replacer("custom", 0), out)
        out = _CANDIDATE_RE.sub(self._entropy_replacer, out)
        return out

    def _replacer(self, kind: str, group: int) -> Callable[[re.Match[str]], str]:
        def repl(match: re.Match[str]) -> str:
            whole = match.group(0)
            if _ALREADY.fullmatch(whole):
                return whole
            self.counts[kind] += 1
            if group == 0:
                return f"[REDACTED:{kind}]"
            start, end = match.span(group)
            offset = match.start(0)
            return whole[: start - offset] + f"[REDACTED:{kind}]" + whole[end - offset:]
        return repl

    def _entropy_replacer(self, match: re.Match[str]) -> str:
        token = match.group(0)
        if _looks_like_secret(token, self.entropy_threshold):
            self.counts["high_entropy"] += 1
            return "[REDACTED:high_entropy]"
        return token

    def redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, list):
            return [self.redact_value(v) for v in value]
        if isinstance(value, dict):
            return {k: self.redact_value(v) for k, v in value.items()}
        return value

    def redact_event(self, event: Event) -> Event:
        return event.model_copy(update={
            "text": self.redact(event.text),
            "tool_output": self.redact(event.tool_output),
            "tool_input": self.redact_value(event.tool_input) if event.tool_input is not None else None,
        })

    def redact_session(self, session: Session) -> Session:
        outcome = session.outcome.model_copy(update={"detail": self.redact(session.outcome.detail)})
        return session.model_copy(update={
            "events": [self.redact_event(e) for e in session.events],
            "cwd": self.redact(session.cwd),
            "outcome": outcome,
        })

    def total(self) -> int:
        return sum(self.counts.values())
