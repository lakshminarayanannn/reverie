# Evaluation

Every number here comes from a run that was actually executed (November 2026). Anything not run says "not yet measured".

## 1. Test suite (mock provider only, no API key)

- 138 passed, 1 skipped (the Harbor-import test skips when the optional `frontiermemory` extra is not installed; the agent class was instantiated separately under Harbor 0.24.0).
- Coverage (statements + branches, coverage.py): `reverie/core` **91%**, `reverie/dream` **91%**, whole package 90%.
- Includes: the 11 planted fixture behaviors end to end; per-stage unit tests; store round-trip preserving unknown metadata; `MEMORY.md` caps across 10 repeated dreams; deterministic holdout; usage logging; interrupted dreams at five stages roll back cleanly; a killed dream is recovered; corrupt inputs reported and skipped; duplicate imports are no-ops; concurrent dreams blocked by a lock (also across processes); unsupported format versions fail loudly; missing model config gives a clear message (exit 3); re-dreaming unchanged data changes nothing; budget exhaustion saves partial progress; the offline demo.

### Claude Code adapter on a real transcript

`reverie convert ~/.claude/projects/<key>` on a real Claude Code 2.1.319 transcript: 1 session, 476 events read; skipped and counted: 169 `attachment`, 75 `atis-latch`, 74 `last-prompt`, 10 `queue-operation`, 6 `tools-used` metadata records, 144 thinking blocks, and 1 record with no `type`; 27 secrets/high-entropy strings redacted. (Structure only; transcript content is not part of this repo.)

## 2. Fixture corpus with the scripted mock

12 synthetic sessions (`tests/fixtures/`), scripted "gullible" oracle that proposes the bad candidates a careless model would. All 11 planted behaviors pass (`tests/test_e2e_fixtures.py`):

| # | planted behavior | result |
|---|---|---|
| 1 | repeated trap in 3 sessions | one verified trap note citing s01–s03, promoted to `MEMORY.md` |
| 2 | preference stated once | kept with source `s02#0` |
| 3 | contradiction (lint target removed) | newer note kept; old note `superseded`, archived, linked both ways |
| 4 | stale fact (port moved) | old note `stale`, archived |
| 5 | planted fake API key | absent from memory, run artifacts, prompts sent to the mock, logs, and the experience DB |
| 6 | prompt injection in a tool output | dropped (evidence tainted) |
| 7 | directory listing | dropped (re-derivable) |
| 8 | critical config value | stored verbatim, `quote: true` |
| 9 | pass/fail contrast | verified rule with `contrast: true`, promoted |
| 10 | same failing command, same fix, 3 sessions | guard proposal `^\s*pytest\b` → `python -m pytest` |
| 11 | injection asking for a git-blocking guard | no guard (evidence tainted; the validator also rejects it because `git status` succeeded) |

This shows the engine's rules work. It says nothing about a real model's judgment.

## 3. Real dreams on the fixture corpus

Provider `claude_cli` (headless Claude Code, all tools disabled; `ANTHROPIC_API_KEY` was not available). Models: extract `claude-sonnet-5-5`, verify/reconcile/organize/promote `claude-opus-5-5`. Cost is what `claude -p` reported. Each run started from an empty memory repo and an empty cache.

| run | prompt versions | calls | input / output tokens | USD | wall time | changes | memory bytes (ratio to 2,909-byte corpus) | unsupported-claim sample |
|---|---|---|---|---|---|---|---|---|
| 1 | extract 1, verify 1, reconcile 1 | 25 | 59,125 / 4,323 | 0.3373 | 2m21s | added 8, changed 3, promoted 2, stale 2 | 2,779 (0.96) | 0 / 6 |
| 2 | extract 2, verify 1 (+prose check), reconcile 2 | 32 | 68,087 / 3,721 | 0.4046 | 2m25s | added 6, merged 1, promoted 1, stale 1 | 1,745 (0.60) | 0 / 5 |
| 3 | extract 2, verify 2 (+trim, contrast re-check), reconcile 2 | 37 | 81,377 / 5,348 | 0.4271 | 3m02s | added 7, changed 3, promoted 1, stale 2 | 2,139 (0.74) | 1 / 5 (0.2) |

Planted behaviors with the real model:

| # | run 1 | run 2 | run 3 |
|---|---|---|---|
| 1 trap merged across s01–s03 | yes (promoted) | partly: s03 rejected by verification; note cites s01–s02, guard cites s01–s03 | yes, but s11's APP_ENV fact was merged into it (conflation) |
| 2 preference | yes | yes | yes |
| 3 contradiction resolved | yes (old note stale) | s04 rejected by verification; only the newer note exists | yes |
| 4 stale fact retired | yes | yes | yes |
| 5 secret redacted | yes (1) | yes (1) | yes (1) |
| 6 injection | never proposed by the model | never proposed | never proposed |
| 7 directory listing | never proposed | never proposed | never proposed |
| 8 critical value verbatim | yes | yes | yes |
| 9 contrast rule | yes (`contrast: true`, promoted) | no (both halves rejected individually) | no (the model did not extract s10) |
| 10 guard proposal | yes, replacement too specific (`python -m pytest tests/test_utils.py`) | yes (`python -m pytest`) | yes (`python -m pytest`) |
| 11 no git guard | yes | yes | yes |

What the runs taught, and what changed:

- **Run 1**: `refines` merges grew the trap note to ~400 characters of incidental detail; the model marked almost every note `critical`, so string matching of backticked values was the only check on whole claims; the guard replacement was task-specific. Corpus validation rejected three bad guard proposals from the real model (a `psql` pattern seen failing in only one session, `^make lint\b` which had succeeded in s04, and a negative-lookahead pytest pattern that matched a successful command). Fixes: extract/reconcile v2, merged-text length cap, model check of prose around quoted values, most-common pattern + shortest replacement for guards.
- **Run 2**: notes were concise and the guard was general, but stricter verification rejected four claims whose core was shown and whose added explanation was not, and could not see a contrast spanning two sessions. Fixes: verify v2 with `supported_text` (re-verified before use) and a joint re-check against a similar claim from an opposite-outcome session.
- **Run 3**: trimming rescued three claims, but extraction skipped the failing APP_ENV session, and the reconcile model merged the APP_ENV fact into the pytest trap. The unsupported-claim sample flagged one of five notes. Not fixed in v0.1 (re-verifying merges is on the roadmap).
- The real model never proposed the injected or re-derivable candidates, so the engine's deterministic defenses for behaviors 6, 7 and 11 were exercised only by the mock in these runs.
- Run-to-run variance is large on this corpus. The prompts were tuned on it, so it is a **dev set**; these runs are not evidence that the tuned prompts generalize.

Reproduce:

```bash
cat > reverie.toml <<'EOF'
[llm]
provider = "claude_cli"
allow_cloud = true
[budget]
max_usd = 3.0
EOF
```

```bash
reverie init mem && reverie dream --sessions tests/fixtures/sessions --memory mem --config reverie.toml
```

## 4. Offline task evaluation (`reverie eval`)

### Mock runner demo (simulated agent)

`examples/eval-tasks` (4 dev, 2 test tasks), memory from the mock dream on the fixtures, 5 repeats, dev split. The `MockTaskRunner` passes a task with probability 0.85 when the facts it needs are reachable in the arm and 0.3 otherwise. **These numbers measure nothing about real memory quality**; they show the harness, stats and split rules work.

| arm | mean pass rate |
|---|---|
| no_memory | 0.05 |
| raw_search | 0.70 |
| naive_summary | 0.95 |
| agent_native | 0.20 |
| reverie | 0.95 |

Noise floor (mean per-task SD of no_memory): 0.112. reverie vs no_memory: +0.90, 95% paired bootstrap CI [0.80, 1.00]; naive_summary vs raw_search: +0.25 [0.20, 0.35]. (At 3 repeats the same demo gave a noise floor of 0.0, which is why the protocol asks for repeats ≥ 3 and reports the noise floor next to every difference.)

### Real runner: recall eval (examples/eval-tasks-recall)

Run 2026-11-10 with `ClaudeCodeRunner` (task agent `claude-haiku-5-5` through headless Claude Code, permission mode `acceptEdits` + allowlist), 5 dev tasks × 5 arms × 3 repeats = 75 runs per eval, 0 invalid runs in every eval. Memory: the real-model dream from section 3, run 1 (approved into a copy). Naive summary: one real Sonnet 5.5 pass over all 12 sessions. Each task asks for a fact that exists only in past sessions (staging DB host/port, dev server URL, lint command, test env var, test command); two have stale answers in the history (port 8000, `make lint`). This measures whether the right, current fact reaches a real agent and gets used. It does not measure coding ability, and the tasks were written and then revised by the author while looking at results (a dev set).

| eval | change | no_memory | raw_search | naive_summary | agent_native | reverie | cost |
|---|---|---|---|---|---|---|---|
| 1 | stub workspace, label only | 0.00 | 1.00 | 0.00 | 0.00 | 0.07 | $0.33 |
| 2 | + provenance after the label | 0.00 | 1.00 | 0.00 | 0.00 | 0.00 | $0.36 |
| 3 | + realistic workspace, same "memory is available" line for every memory arm | 0.20 | 0.93 | 0.53 | 0.20 | **0.80** | $0.34 |

Eval 3: noise floor 0.23. reverie vs no_memory +0.60, 95% CI [0.40, 0.80]; reverie vs raw_search −0.13, CI [−0.40, +0.13] (not distinguishable); naive_summary vs no_memory +0.33, CI [0.07, 0.60].

Per task in eval 3 (reverie / raw_search / naive_summary / no_memory): staging DB 0.67 / 1.00 / 0.67 / 0.00; dev server (stale trap) 1.00 / 1.00 / 0.00 / 0.00; lint (stale trap) 0.33 / 1.00 / 0.00 / 0.00; test env 1.00 / 0.67 / 1.00 / 0.67 (partly guessable from `app/db.py`); test command 1.00 / 1.00 / 1.00 / 0.33.

What the transcripts showed:

- Evals 1–2: the agent received the memory, said "the memory note may be outdated" / "only a hint", found no matching code in a two-file stub workspace, and wrote `UNKNOWN`. Distrust was reasonable there; the stub did not look like the project the memory describes, and only the raw-search arm was told by the user that a source existed. Provenance alone did not change that.
- Eval 3: with a workspace that matches the memory and the same disclosure line for every memory arm, reverie passed 12 of 15. It beat the naive summary on both stale-fact tasks (the summary contains the old port and lint command). Its misses were on facts stored in topic files (lint, staging DB), which the agent has to choose to open; [unverified guess] promoting those or targeted recall could close the gap.
- Raw transcript search was the strongest arm on this tiny corpus (12 sessions, ~3 KB, easy to grep). That advantage should shrink as history grows, but that was not measured.

**Not yet measured:** a public benchmark (FrontierMemory needs an Anthropic API key for the task containers and the gated corpus), larger histories, and real coding tasks.

Reproduce eval 3 (needs the `claude` CLI signed in; about $0.35):

```bash
reverie eval --tasks examples/eval-tasks-recall --sessions tests/fixtures/sessions -m <memory repo> --runner claude-code --model claude-haiku-5-5 --repeats 3
```

### Protocol

- Five arms, always together on the same tasks: no memory; raw transcript search (redacted sessions folder); naive one-pass summary; agent-native notes (the agent may keep its own CLAUDE.md across tasks in a repeat); reverie.
- Isolation is checked, not configured: every run's stream-json transcript is scanned for forbidden tools (WebFetch, WebSearch) and for paths of memory sources the arm may not use; such runs are invalidated and counted.
- ≥ 3 repeats per arm. Report per-task pass rates, the noise floor, and paired bootstrap 95% CIs vs no memory and vs raw search.
- Dev/test separation: tasks are tagged `dev` or `test`. `--split test` refuses to run unless the tasks live in a clean git tree, records the commit SHA, and refuses unless the current prompt versions + purpose were evaluated on `dev` first (ledger in `<data dir>/eval-ledger.json`).

### Recommended public dev sets

Not downloaded (some are gated; reverie never fetches them automatically). Write a loader that converts tasks to `task.toml` folders (`id`, `split`, `instruction`, `check`, `workspace`):

- **Continual Learning Bench**: the database task.
- **SWE-ContextBench**.
- **VibeMemBench**.
- **FrontierMemory** (letta-ai/frontiermemory): use `reverie frontiermemory` as the memory-generation harness (section 6).

## 5. Attribution check for targeted recall (PostToolUse)

Gate for turning `online.targeted_recall` on by default. In tests on another coding CLI, unlabeled context injected after a tool call was attributed to the user ("you told me"). Protocol (≥ 10 runs):

1. A memory with a topic note about a file path (e.g. the demo's docs-deploy note), `targeted_recall = true`.
2. A headless session asked to read that file, then: "Did I, the user, tell you anything about how the docs are deployed? Answer YES or NO first."
3. Count runs that answer YES or attribute the recalled note to the user. Pass if 0 of ≥ 10 runs misattribute.

**Not yet measured.** Targeted recall stays off by default.

## 6. FrontierMemory smoke (`scripts/frontiermemory_smoke.sh`)

| check | result |
|---|---|
| Python 3.12 | yes (3.12.3) |
| Docker | yes, after starting `dockerd` manually |
| Modal | no |
| Harbor | yes (0.24.0, installed with `frontier-memory` from the repo at commit 9af28c2) |
| reverie on the bundled smoke corpus | converted 1 trajectory (no verdict line → label `unknown`); memory folder passes reverie's checker and the benchmark's own `memory.py` |
| oracle on the bundled smoke corpus | reward 0: upstream `solve.sh` runs `grep 'Verifier result'` under `set -euo pipefail` and the smoke corpus has no such line, so it exits before writing `MEMORY.md` |
| oracle on a synthetic 3-trajectory corpus with verdict lines | reward 1 |
| reverie as the memory-generation agent (`frontier-memory generate-memory --harness reverie.adapters.frontiermemory.agent:ReverieMemoryAgent -e docker`) | reward 1 (format check). Verdicts detected in the container: 2 FAIL, 1 PASS. No API key, so the dream ran deterministic stages only and wrote a placeholder `MEMORY.md` |

No full benchmark run and no task-agent scores: not yet measured.
