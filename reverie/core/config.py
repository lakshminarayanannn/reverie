"""Configuration (``reverie.toml``). Every default here is documented in the README.

Lookup order: ``--config PATH``, ``$REVERIE_CONFIG``, ``./reverie.toml``,
``<user config dir>/reverie/reverie.toml``. Missing file means all defaults.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

import platformdirs
from pydantic import BaseModel, ConfigDict, Field


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LLMConfig(_Section):
    #: "none" (default: LLM stages unavailable), "anthropic", "openai_compat", "claude_cli", "mock".
    provider: Literal["none", "anthropic", "openai_compat", "claude_cli", "mock"] = "none"
    extract_model: str = "claude-sonnet-5-5"
    verify_model: str = "claude-opus-5-5"
    reconcile_model: str = "claude-opus-5-5"
    #: Must be true before any transcript excerpt is sent to a non-local model endpoint.
    allow_cloud: bool = False
    #: OpenAI-compatible endpoint (e.g. http://localhost:11434/v1 for Ollama).
    base_url: str | None = None
    #: Env var holding the key for openai_compat (never the key itself).
    api_key_env: str = "OPENAI_API_KEY"
    #: Anthropic effort level sent as output_config.effort; None = model default.
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    max_tokens: int = 8000
    max_retries: int = 4
    timeout_s: float = 300.0
    #: Path to a JSON file of canned responses for provider = "mock".
    mock_responses: str | None = None


class BudgetConfig(_Section):
    max_usd: float | None = 5.0
    max_tokens: int | None = 2_000_000


class CacheConfig(_Section):
    enabled: bool = True
    #: Default: <user cache dir>/reverie/llm.
    dir: str | None = None


class DreamConfig(_Section):
    tool_output_limit: int = 8000
    chunk_chars: int = 60_000
    chunk_overlap_events: int = 4
    #: Re-check notes not verified for this many dreams.
    recheck_after_dreams: int = 5
    #: Promote to MEMORY.md only notes cited by at least this many sessions.
    promote_min_sessions: int = 2
    memory_md_max_lines: int = 200
    memory_md_max_bytes: int = 25_000
    #: Live memory may not exceed this fraction of the redacted corpus (10 KB floor); the
    #: lowest-ranked new notes are dropped and reported when it would.
    max_memory_to_corpus_ratio: float = 0.5
    #: Drop notes from the archive after this many dreams.
    archive_retention_dreams: int = 5
    #: Notes not injected/read for this many dreams become stale (needs usage data).
    unread_prune_dreams: int = 10
    #: Usage data must cover at least this many sessions before unread-pruning applies.
    unread_prune_min_sessions: int = 20
    #: Holdout: flag a note ineffective when both arms have at least this many labeled sessions.
    holdout_min_sessions: int = 10
    #: Guards: failing sessions needed before a guard is proposed.
    guard_min_sessions: int = 2
    unsupported_sample_size: int = 20
    entropy_threshold: float = 4.3
    purpose_file: str | None = None
    #: Where run artifacts go; default <memory>/.reverie/runs.
    runs_dir: str | None = None


class OnlineConfig(_Section):
    holdout_pct: int = Field(default=0, ge=0, le=100)
    inject_max_chars: int = 9000
    #: State where injected notes came from and when they were checked (see EVALUATION.md, recall eval).
    inject_provenance: bool = True
    targeted_recall: bool = False
    targeted_recall_max_notes: int = 3
    #: Default: <user data dir>/reverie/sessions.
    sessions_dir: str | None = None


class StoreConfig(_Section):
    #: Default: <user data dir>/reverie/experience.db.
    path: str | None = None
    #: Delete sessions older than this many days from the experience store (None = keep all).
    retention_days: int | None = None


class ReverieConfig(_Section):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    dream: DreamConfig = Field(default_factory=DreamConfig)
    online: OnlineConfig = Field(default_factory=OnlineConfig)
    store: StoreConfig = Field(default_factory=StoreConfig)
    #: Path the config was loaded from (not a TOML key).
    source: str | None = Field(default=None, exclude=True)


def data_dir() -> Path:
    override = os.environ.get("REVERIE_HOME")
    if override:
        return Path(override).expanduser()
    return Path(platformdirs.user_data_dir("reverie", appauthor=False))


def cache_dir() -> Path:
    override = os.environ.get("REVERIE_HOME")
    if override:
        return Path(override).expanduser() / "cache"
    return Path(platformdirs.user_cache_dir("reverie", appauthor=False))


def find_config(explicit: Path | None = None) -> Path | None:
    if explicit is not None:
        return explicit
    env = os.environ.get("REVERIE_CONFIG")
    if env:
        return Path(env).expanduser()
    local = Path.cwd() / "reverie.toml"
    if local.is_file():
        return local
    user = Path(platformdirs.user_config_dir("reverie", appauthor=False)) / "reverie.toml"
    return user if user.is_file() else None


def load_config(explicit: Path | None = None) -> ReverieConfig:
    path = find_config(explicit)
    if path is None:
        return ReverieConfig()
    if not path.is_file():
        raise FileNotFoundError(f"config file not found: {path}")
    with open(path, "rb") as handle:
        data = tomllib.load(handle)
    cfg = ReverieConfig.model_validate(data)
    cfg.source = str(path)
    return cfg


def experience_db_path(cfg: ReverieConfig) -> Path:
    return Path(cfg.store.path).expanduser() if cfg.store.path else data_dir() / "experience.db"


def sessions_dir(cfg: ReverieConfig) -> Path:
    return Path(cfg.online.sessions_dir).expanduser() if cfg.online.sessions_dir else data_dir() / "sessions"


def llm_cache_dir(cfg: ReverieConfig) -> Path:
    return Path(cfg.cache.dir).expanduser() if cfg.cache.dir else cache_dir() / "llm"
