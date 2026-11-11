# Contributing

```bash
uv sync
uv run ruff check reverie tests
uv run mypy reverie            # strict on reverie/core
uv run pytest --cov=reverie
```

- Tests use the mock provider only; CI has no API key. Keep coverage of `reverie/core` and `reverie/dream` at 85% or more.
- Stages communicate through typed models (`reverie/dream/models.py`), not dicts. Prefer pure functions.
- Prompts live in `reverie/dream/prompts/*.md` with `name`/`version` front matter. Bump the version when you change a prompt, and evaluate prompt or purpose changes on the `dev` split only (`reverie eval --split dev`).
- Do not fabricate results. Numbers in docs must come from runs you executed; otherwise write "not yet measured".
- Record non-obvious choices in `docs/DECISIONS.md`.
- New adapters must declare supported source versions, fail loudly on unknown ones, and count every skipped record with a reason (`reverie/adapters/base.py`).
- Use only public documentation and synthetic data in fixtures.
