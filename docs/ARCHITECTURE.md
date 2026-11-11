# Architecture

## Layers of memory

A memory hierarchy: **transcripts** (raw, append-only) → **notes** (one claim each, with evidence) → **representations** (topic files and the `MEMORY.md` index) → **injected context** (what a session actually sees). reverie keeps each layer separate:

| layer | where | written by |
|---|---|---|
| transcripts | agent's own store; canonical copies in `<data dir>/sessions`; redacted events in the experience store (SQLite + FTS5) | adapters, `capture` hook, `import` |
| notes | bullets in the memory repo, `[id; kind; status; source; added; ...]` | the dream |
| representations | `MEMORY.md` (promoted notes + `## Index`), `topics/*.md`, `archive.md` | the dream (organize / promote / prune) |
| injected context | SessionStart `additionalContext` (≤ 9,000 chars), optional PostToolUse recall | hooks |

The experience store lives outside the memory repo so raw history never bloats memory, and so `reverie search` can give explicit recall instead of injecting more.

## Package layout

```
reverie/core/        schema (Session/Event/Note/MemoryRepo), store (git, lossless parse, lock),
                     experience (SQLite + FTS5), redact, budget, config, fsutil
reverie/adapters/    base contract, generic canonical JSONL, Claude Code transcripts,
                     frontiermemory/ (corpus conversion, benchmark dream, Harbor agent)
reverie/providers/   Provider protocol + LLM (budget, retries, JSON validation), anthropic,
                     openai_compat, claude_cli, mock + scripted oracle, cache
reverie/dream/       ingest, select, extract, verify, reconcile, organize, promote, prune,
                     guards, proposal, pipeline, prompts/*.md, purposes/*.md
reverie/online/      hooks, inject, usage, holdout, install, review
reverie/eval/        metrics, runner (arms, task runners, isolation), stats
```

## Dream stages

Each stage takes and returns typed objects (`reverie/dream/models.py`) and is unit-tested alone. Intermediate artifacts go to `<runs dir>/<run id>/` (`ingest.json`, `selection.json`, `candidates.json`, `verified.json`, `reconcile.json`, `plan.json`, `PROPOSAL.md`, `metrics.json`, `report.json`).

1. **Ingest and govern** (`ingest.py`): adapters → canonical sessions; duplicate session ids merged (most complete copy wins); tool output truncated head+tail; secrets redacted to `[REDACTED:<kind>]` (counts only are logged); events that look like prompt injection are flagged as tainted. Everything is imported into the experience store.
2. **Select** (`select.py`): sessions whose content hash is not in `.reverie/state.json` (or in a pending proposal); notes to re-check (cited by selected sessions, contested, or not verified for N dreams); chunking with overlap. Transcript text is wrapped in `<<<TRANSCRIPT ...>>>` blocks; delimiters inside data are escaped; tainted events carry `[possible-injection]`.
3. **Extract** (`extract.py`): one call per chunk with the purpose file. Evidence pointers are validated against real events of that session and chunk; candidates without valid evidence are dropped and logged.
4. **Verify** (`verify.py`): deterministic rules (taint, destructive advice without user evidence, re-derivable observations), then quoted values by string match, then a clean-context model call that sees only the cited events (plus the tool call that produced a cited result). A partially supported claim can be trimmed to its supported part, which is re-verified before use. An unsupported claim from a labeled session is re-verified together with a similar claim from a session with the opposite outcome (pass/fail contrast).
5. **Reconcile** (`reconcile.py`): chronological; exact / near duplicates merge deterministically; overlapping notes go to the model for a relation; resolution by evidence recency; `refines` merges are length-capped; rejected fingerprints are dropped.
6. **Organize** (`organize.py`): placement record for every candidate (memory note, instruction edit, guard proposal, evidence only, nowhere); topic assignment (model, or by kind); a write plan of full file contents; one writer per file; dangling links fixed; index regenerated while user index lines are kept.
7. **Promote** (`promote.py`): eligible = verified/active, ≥ `promote_min_sessions` sessions, kind rule/trap/preference/decision, judged general; ranked by sessions, contrast, kind; greedily fit into the `MEMORY.md` caps. Others are demoted to their home topic.
8. **Prune** (`prune.py`): holdout-ineffective and unread notes become stale; retired notes move to `archive.md` and are deleted from it after `archive_retention_dreams`.
9. **Emit** (`pipeline.py`): a marker file in `.git/`, a branch `dream/<run id>`, atomic file writes, `state.json`, the proposal (and proposed guards) under `.reverie/proposals/`, one commit, back to the default branch. Any exception rolls the repo back; a killed dream is recovered from the marker on the next run. `--auto-accept` merges.

A file lock in `.git/reverie-dream.lock` prevents concurrent dreams (stale locks from dead local processes are broken). A dream refuses to start while a proposal is pending.

## Online loop (Claude Code; Devin uses the same handlers through `--agent devin`, see integrations/devin)

| hook | handler | effect |
|---|---|---|
| SessionStart | `session-start` | inject `MEMORY.md` (labeled, compact, held-out notes removed); log `injected` / `withheld` |
| PreToolUse (Bash) | `pre-tool-use` | guards: `warn` adds context, `block` denies; log `guard_fired` |
| PostToolUse (Read\|Bash) | `post-tool-use` | log `read` when a memory file is read; optional targeted recall |
| Stop, SessionEnd | `capture` | transcript → redacted canonical JSONL + experience store |

Hooks never fail a session: errors go to stderr and the hook exits 0 with no output.

## Success levels

Reported separately, never merged into one number: **stored** (live notes), **injected** (notes injected at least once), **read** (notes in files the agent read), **used** (only from holdout differences on labeled sessions), **validated** (only from `reverie eval` on held-out tasks).

## Data flow and privacy

Nothing leaves the machine unless `llm.provider` is a remote endpoint **and** `llm.allow_cloud = true` (or `--allow-cloud`). Before the first call of a dream, reverie prints how many redacted excerpts, from how many sessions, plus how many notes will be sent, to which provider and models, with the budget cap. API keys come from environment variables only.
