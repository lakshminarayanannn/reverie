"""Provider construction from config, including the cloud-consent gate."""

from __future__ import annotations

from pathlib import Path

from reverie.core.config import ReverieConfig, llm_cache_dir
from reverie.providers.base import (
    LLM,
    Completion,
    Provider,
    ProviderError,
    ProviderUnavailable,
    RetryableError,
)
from reverie.providers.cache import CachedProvider


class ConsentRequired(ProviderUnavailable):
    """A cloud provider is configured but sending data to it was not explicitly allowed."""


def build_provider(cfg: ReverieConfig, *, use_cache: bool = True, allow_cloud: bool = False) -> Provider:
    """Build the configured provider. Raises ``ProviderUnavailable`` with a clear reason."""
    kind = cfg.llm.provider
    provider: Provider
    if kind == "none":
        raise ProviderUnavailable(
            "no model configured: set [llm] provider in reverie.toml (anthropic, openai_compat, claude_cli). "
            "Extract/verify/reconcile need a model; deterministic maintenance still runs.")
    if kind == "anthropic":
        from reverie.providers.anthropic_provider import AnthropicProvider

        provider = AnthropicProvider(effort=cfg.llm.effort, timeout_s=cfg.llm.timeout_s)
    elif kind == "openai_compat":
        from reverie.providers.openai_compat import OpenAICompatProvider

        provider = OpenAICompatProvider(base_url=cfg.llm.base_url, api_key_env=cfg.llm.api_key_env,
                                        timeout_s=cfg.llm.timeout_s)
    elif kind == "claude_cli":
        from reverie.providers.claude_cli import ClaudeCliProvider

        provider = ClaudeCliProvider(timeout_s=cfg.llm.timeout_s)
    elif kind == "mock":
        from reverie.providers.mock_provider import MockProvider

        if not cfg.llm.mock_responses:
            raise ProviderUnavailable("provider 'mock' needs [llm] mock_responses (a JSON file)")
        provider = MockProvider.from_file(Path(cfg.llm.mock_responses))
    else:  # pragma: no cover - pydantic restricts the value
        raise ProviderUnavailable(f"unknown provider {kind!r}")
    if provider.is_cloud and not (cfg.llm.allow_cloud or allow_cloud):
        raise ConsentRequired(
            f"provider {kind!r} sends redacted transcript excerpts and memory notes off this machine. "
            "Set [llm] allow_cloud = true in reverie.toml or pass --allow-cloud to consent.")
    if use_cache and cfg.cache.enabled:
        provider = CachedProvider(provider, llm_cache_dir(cfg))
    return provider


__all__ = [
    "LLM", "CachedProvider", "Completion", "ConsentRequired", "Provider", "ProviderError",
    "ProviderUnavailable", "RetryableError", "build_provider",
]
