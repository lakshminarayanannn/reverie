"""Headless Claude Code (``claude -p``) as a plain completion provider.

For users who have Claude Code signed in but no API key. Each call runs
``claude -p`` with all built-in tools disabled (``--tools ""``), no MCP servers
(``--strict-mcp-config``), no settings/CLAUDE.md (``--setting-sources ""``, empty temp
cwd), no session persistence, and reverie's own system prompt replacing the default.
The CLI reports its own cost, which reverie records as-is.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from typing import Any

from reverie.providers.base import Completion, ProviderError, ProviderUnavailable, RetryableError


def flatten_messages(messages: list[dict[str, Any]]) -> str:
    """Render a multi-turn conversation as one prompt (the CLI takes a single prompt)."""
    if len(messages) == 1:
        return str(messages[0].get("content", ""))
    parts: list[str] = []
    for msg in messages:
        role = "PREVIOUS ASSISTANT REPLY" if msg.get("role") == "assistant" else "USER"
        parts.append(f"=== {role} ===\n{msg.get('content', '')}")
    return "\n\n".join(parts)


class ClaudeCliProvider:
    name = "claude_cli"
    is_cloud = True

    def __init__(self, *, binary: str | None = None, timeout_s: float = 600.0) -> None:
        found = binary or shutil.which("claude")
        if not found:
            raise ProviderUnavailable("provider 'claude_cli' needs the `claude` CLI on PATH")
        self.binary = found
        self.timeout_s = timeout_s

    def complete(self, *, model: str, system: str, messages: list[dict[str, Any]],
                 max_tokens: int, temperature: float = 0.0) -> Completion:
        prompt = flatten_messages(messages)
        cmd = [self.binary, "-p", "--model", model, "--output-format", "json", "--tools", "",
               "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config",
               "--system-prompt", system]
        with tempfile.TemporaryDirectory(prefix="reverie-cli-") as cwd:
            try:
                proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=cwd,
                                      timeout=self.timeout_s, check=False)
            except subprocess.TimeoutExpired as exc:
                raise RetryableError("claude -p timed out") from exc
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RetryableError(f"claude -p returned non-JSON output (exit {proc.returncode})") from exc
        if data.get("is_error"):
            status = data.get("api_error_status")
            if status in (429, 500, 502, 503, 529) or status is None:
                raise RetryableError(f"claude -p error (status {status})")
            raise ProviderError(f"claude -p error (status {status})")
        if data.get("stop_reason") == "refusal":
            raise ProviderError("model declined the request (stop_reason=refusal)")
        usage = data.get("usage") or {}
        in_tokens = int(usage.get("input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0) \
            + int(usage.get("cache_read_input_tokens") or 0)
        return Completion(text=str(data.get("result") or ""), input_tokens=in_tokens,
                          output_tokens=int(usage.get("output_tokens") or 0), model=model,
                          usd=float(data.get("total_cost_usd") or 0.0))
