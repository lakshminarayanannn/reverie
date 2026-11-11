"""Versioned prompt templates (``<name>.md`` with ``name``/``version`` front matter)."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path

PROMPT_DIR = Path(__file__).parent
PURPOSE_DIR = PROMPT_DIR.parent / "purposes"


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    body: str

    @property
    def tag(self) -> str:
        return f"{self.name}@{self.version}"

    def render(self, **values: str) -> str:
        text = self.body.replace("{{preamble}}", load_preamble())
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", value)
        # First line identifies the template; useful in logs, caches and the mock provider.
        return f"reverie-prompt: {self.tag}\n\n{text.strip()}\n"


@cache
def load_preamble() -> str:
    return (PROMPT_DIR / "_preamble.md").read_text(encoding="utf-8").strip()


@cache
def load_prompt(name: str) -> Prompt:
    raw = (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    body = raw
    if raw.startswith("---\n"):
        head, _, body = raw[4:].partition("\n---\n")
        for line in head.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                meta[key.strip()] = value.strip()
    return Prompt(name=meta.get("name", name), version=meta.get("version", "0"), body=body)


def prompt_versions() -> dict[str, str]:
    names = sorted(p.stem for p in PROMPT_DIR.glob("*.md") if not p.stem.startswith("_"))
    return {n: load_prompt(n).version for n in names}


def load_purpose(path: Path | None) -> str:
    if path is None:
        return (PURPOSE_DIR / "general.md").read_text(encoding="utf-8").strip()
    return Path(path).read_text(encoding="utf-8").strip()
