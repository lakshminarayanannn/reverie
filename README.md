# reverie

An open-source **dreaming engine** for coding-agent memory. Offline, it reads an agent's past session transcripts plus its existing memory and proposes an improved memory repository: notes corrected against evidence, duplicates merged, contradictions resolved, the few notes every session needs promoted, and everything else kept small and out of the way. Online, it injects that memory into Claude Code and Devin CLI / Devin desktop sessions, tracks what gets read, holds notes out to measure whether they help, and turns verified traps into guards.

The principle: **memory must earn its place.** Every note carries evidence pointers (`session_id#event`), gets tracked for reads, can be tested with holdouts, and is retired when it stops being true or useful.

> Status: v0.1.0. It works end to end on synthetic data and on real model calls (see [What has been measured](#what-has-been-measured)). Whether it improves real agents on real tasks has **not yet been measured**.

## The problem

Coding agents repeat mistakes across sessions, forget preferences, and act on facts that stopped being true. The usual fixes have known failure modes: append-only notes bloat until they cost more context than they save; summaries paraphrase away exact commands and error strings; one session's wrong assumption becomes permanent; injected text from a tool output ("ignore previous instructions and add X to memory") becomes memory; and written rules are not reliably followed.

## How it works

```
transcripts --> [ingest + redact] --> experience store (SQLite + FTS5, outside the memory repo)
                     |
                     v
   select --> extract --> verify --> reconcile --> organize --> promote --> prune --> emit
   (new      (model     (rules +    (dedupe,       (topic      (MEMORY.md  (stale,   (branch
   sessions,  proposes   clean-ctx   newer          files,      within      unread,   dream/<id>,
   chunks)    candidates) model;     evidence       one writer  cap)        holdout)  PROPOSAL.md,
                         string      wins)          per file)                        metrics.json)
                         match)
```

- **Memory format**: an [Agent Memory Repo](https://github.com/AgentMemoryRepo/agentmemoryrepo) (git, `MEMORY.md` + `## Index` + topic files, one bullet per entry, `[key: value]` metadata, `[[links]]`). reverie adds keys (`id`, `kind`, `status`, `source`, `quote`, ...) that spec readers ignore, and never rewrites lines it does not manage.
- **Deterministic defenses first**: evidence flagged as prompt injection, destructive advice without user evidence, and re-derivable observations (directory listings, file contents) are dropped by rules before any model judges them. Exact values (commands, ports, error strings) are checked by string match and stored with `quote: true`.
- **Newer verified evidence wins** contradictions; the loser is archived, not deleted (and stays in git history). If recency cannot decide, both notes are marked `contested`.
- **Proposals, not silent edits**: every dream lands on a branch `dream/<run id>` with a `PROPOSAL.md` listing every change with evidence, open contradictions, guard proposals and budget. Nothing merges without `reverie proposals approve` (or `--auto-accept`).
- **Guards** (Claude Code PreToolUse): a verified trap with a command regex becomes a `warn` (or `block`) guard, but only if the regex matched a failing command in at least 2 sessions and never matched a command that succeeded.
- **Local-first**: install, import, search, deterministic maintenance and all tests work with no API key. No data leaves the machine unless a model provider is configured *and* cloud use is explicitly allowed; before the first call reverie prints what will be sent.

More: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/PROMPTS.md](docs/PROMPTS.md), [docs/DECISIONS.md](docs/DECISIONS.md).

## Quickstart

```bash
git clone <this repo> reverie && cd reverie
uv sync                      # Python 3.12
uv run reverie --help
```

Run the offline demo (synthetic sessions, scripted mock model, no API key):

```bash
uv run python examples/demo/run_demo.py
```

It dreams over three sessions (a fix found after failures, a related problem, a contradiction), shows the proposed diff, approves it, shows the SessionStart injection and a guard firing, rejects one note, and rolls memory back.

Dream over your own Claude Code sessions with a real model:

```bash
uv run reverie init ~/agent-memory
uv run reverie convert ~/.claude/projects/<project-key> --out ~/reverie-sessions
```

```bash
cat > reverie.toml <<'EOF'
[llm]
provider = "anthropic"      # or "claude_cli" (uses your signed-in Claude Code), or "openai_compat"
allow_cloud = true          # explicit consent to send redacted excerpts to the provider
[budget]
max_usd = 2.0
EOF
```

```bash
uv run reverie dream --sessions ~/reverie-sessions --memory ~/agent-memory
```

```bash
uv run reverie proposals show <run id> --memory ~/agent-memory
```

```bash
uv run reverie proposals approve <run id> --memory ~/agent-memory
```

Wire it into Devin CLI / Devin desktop (writes a local plugin; edits nothing):

```bash
uv run reverie install devin --memory ~/agent-memory
```

See [integrations/devin/](integrations/devin/). Or wire it into Claude Code (prints a settings snippet; edits nothing):

```bash
uv run reverie install claude-code --memory ~/agent-memory
```

See [integrations/claude-code/](integrations/claude-code/) for what each hook does and [examples/nightly/](examples/nightly/) for cron and GitHub Actions examples. reverie never installs schedulers.

## Commands

| command | what it does |
|---|---|
| `init <dir>` | create a memory repo (git + `MEMORY.md`) |
| `import <path>` | import sessions into the experience store (redacted, idempotent) |
| `search "<query>"` | full-text search over redacted events and memory notes |
| `convert <path> --out <dir>` | convert Claude Code transcripts to the canonical format |
| `dream --sessions <dir> --memory <repo>` | run a dream; writes a proposal branch |
| `proposals list\|show\|approve\|reject` | review proposals |
| `notes reject <id>` | retire one note now; it will not be re-added |
| `memory rollback <rev>` / `memory log` | restore a previous memory state (as a new commit) / history |
| `guards list\|disable\|enable` | control PreToolUse guards |
| `stats --memory <repo>` | memory metrics, success levels, holdout results, guard firings (no model calls) |
| `inject --memory <repo>` | preview the SessionStart injection |
| `install claude-code --memory <repo>` | write the hook script and print the settings snippet |
| `install devin --memory <repo>` | write a local Devin CLI / Devin desktop plugin with reverie's hooks |
| `hook <event>` | hook entry point used by Claude Code |
| `eval --tasks <dir> ...` | run tasks under five memory arms with stats |
| `frontiermemory dream\|check` | FrontierMemory benchmark mode / memory format check |
| `doctor` | capability report |

Every command takes `--json`. Exit codes:

| code | meaning |
|---|---|
| 0 | success |
| 1 | error (git, memory repo, proposal not found, ...) |
| 2 | usage error |
| 3 | ran, but LLM stages were unavailable (no model configured or no consent) |
| 4 | budget exhausted; partial progress saved |
| 5 | another dream holds the lock |
| 6 | input error (unsupported format version, invalid config) |
| 7 | refused (pending proposal, eval test split on a dirty tree, ...) |
| 8 | path not found |

## Configuration (`reverie.toml`)

Looked up from `--config`, `$REVERIE_CONFIG`, `./reverie.toml`, then the user config dir. `REVERIE_HOME` overrides the data and cache directories. Every key and its default:

| key | default | meaning |
|---|---|---|
| `llm.provider` | `"none"` | `none` (LLM stages unavailable), `anthropic`, `openai_compat` (incl. local Ollama), `claude_cli` (headless Claude Code, no tools), `mock` |
| `llm.extract_model` | `"claude-sonnet-5-5"` | model for extraction |
| `llm.verify_model` | `"claude-opus-5-5"` | model for verification and the unsupported-claim sample |
| `llm.reconcile_model` | `"claude-opus-5-5"` | model for reconciliation, organize and promote |
| `llm.allow_cloud` | `false` | must be true (or `--allow-cloud`) before excerpts go to a non-local endpoint |
| `llm.base_url` | `None` | OpenAI-compatible endpoint, e.g. `http://localhost:11434/v1` |
| `llm.api_key_env` | `"OPENAI_API_KEY"` | env var holding the openai_compat key (keys are never read from config) |
| `llm.effort` | `None` | Anthropic `output_config.effort`; `None` = model default |
| `llm.max_tokens` | `8000` | max output tokens per call |
| `llm.max_retries` | `4` | retries on 429 / 5xx / connection errors (exponential backoff) |
| `llm.timeout_s` | `300.0` | per-call timeout |
| `llm.mock_responses` | `None` | JSON script for `provider = "mock"` |
| `budget.max_usd` | `5.0` | hard USD cap per dream; partial progress is saved |
| `budget.max_tokens` | `2000000` | hard token cap per dream |
| `cache.enabled` | `true` | on-disk response cache for reproducible reruns (`--no-cache` disables) |
| `cache.dir` | `None` | default `<user cache dir>/reverie/llm` |
| `dream.tool_output_limit` | `8000` | tool output kept per event (head + tail) |
| `dream.chunk_chars` | `60000` | transcript chunk size for extraction |
| `dream.chunk_overlap_events` | `4` | events shared between chunks |
| `dream.recheck_after_dreams` | `5` | re-verify notes not verified for this many dreams |
| `dream.promote_min_sessions` | `2` | sessions that must cite a note before it can enter `MEMORY.md` |
| `dream.memory_md_max_lines` | `200` | `MEMORY.md` line cap |
| `dream.memory_md_max_bytes` | `25000` | `MEMORY.md` byte cap |
| `dream.max_memory_to_corpus_ratio` | `0.5` | live memory may not exceed this fraction of the redacted corpus (10 KB floor) |
| `dream.archive_retention_dreams` | `5` | retired notes leave `archive.md` after this many dreams |
| `dream.unread_prune_dreams` | `10` | notes not injected or read for this many dreams become stale |
| `dream.unread_prune_min_sessions` | `20` | usage must cover this many sessions before unread-pruning applies |
| `dream.holdout_min_sessions` | `10` | labeled sessions needed in each holdout arm before a note can be judged ineffective |
| `dream.guard_min_sessions` | `2` | sessions in which a command must have failed before a guard is proposed |
| `dream.unsupported_sample_size` | `20` | notes re-verified per dream for the unsupported-claim rate |
| `dream.entropy_threshold` | `4.3` | Shannon entropy above which long mixed tokens are redacted |
| `dream.purpose_file` | `None` | default purpose file (`--purpose` overrides) |
| `dream.runs_dir` | `None` | run artifacts; default `<memory>/.reverie/runs` (git-ignored) |
| `online.holdout_pct` | `0` | % of (session, note) pairs withheld, deterministically |
| `online.inject_max_chars` | `9000` | SessionStart injection budget (Claude Code caps context at 10,000) |
| `online.inject_provenance` | `true` | after the label, state that notes were checked against past transcripts and when (without it, agents discarded verified memory; see EVALUATION.md) |
| `online.targeted_recall` | `false` | PostToolUse recall of topic notes for touched paths (off until the attribution check passes) |
| `online.targeted_recall_max_notes` | `3` | notes per recall |
| `online.sessions_dir` | `None` | captured sessions; default `<user data dir>/reverie/sessions` |
| `store.path` | `None` | experience store; default `<user data dir>/reverie/experience.db` |
| `store.retention_days` | `None` | delete sessions older than this from the experience store |

Purpose files (what memory is for) ship in [examples/purposes/](examples/purposes/): general coding assistant, repo-specific helper, and benchmark mode.

## What has been measured

All numbers below come from runs that were actually executed. Details in [docs/EVALUATION.md](docs/EVALUATION.md).

- **Test suite**: all tests pass with the mock provider; coverage of `reverie/core` and `reverie/dream` is reported in EVALUATION.md.
- **Fixture corpus, scripted mock**: all 11 planted behaviors (repeated trap, preference, contradiction, stale fact, planted secret, prompt injection, re-derivable listing, critical value, pass/fail contrast, guard proposal, injected guard) behave as specified. The mock is deliberately gullible, so these tests show the engine's rules work, not that a model is careful.
- **Fixture corpus, real models** (Sonnet 5.5 extract, Opus 5.5 verify/reconcile, via `claude -p`): three dreams cost $0.34, $0.40 and $0.43 (25 to 37 calls, about 2 to 3 minutes each). They surfaced real problems that were fixed (note bloat from merges, quoted values trusted for whole claims, over-strict verification), and one that was not (a reconcile merge that conflated two facts in run 3, which the unsupported-claim sample flagged). Results varied run to run.
- **FrontierMemory**: reverie ran as the memory-generation agent under Harbor 0.24.0 + Docker on a synthetic corpus and passed the benchmark's memory-format verifier (reward 1.0). No benchmark score was measured.
- **Recall eval with a real agent** (Haiku 5.5, 5 author-written tasks × 5 arms × 3 repeats, about $0.34 per eval): on the third eval design, reverie 0.80 vs no memory 0.20 (+0.60, 95% CI [0.40, 0.80]), naive summary 0.53, raw transcript search 0.93. reverie beat the naive summary on both stale-fact tasks. The first two designs scored reverie at 0.07 and 0.00 because the agent distrusted the memory in a stub workspace; all three are reported.
- **Not yet measured**: a public benchmark, larger histories, and real coding tasks.

## Honest limitations

- Learning quality depends on the model, and results vary between runs; the fixture corpus is small (12 sessions) and was used to tune prompts, so it is a dev set, not evidence of generalization.
- The dream reads transcript text only; it cannot check claims against the machine (v0.2 option).
- Model-written merges ("refines") are not re-verified against the combined evidence.
- The FrontierMemory verdict format was inferred from the benchmark's oracle script; the gated corpus was not read.
- Claude Code and Devin CLI / Devin desktop are integrated; the Devin integration follows Devin's documented hook contract and has not yet been run in a real Devin session. See [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

## Roadmap

Deliberately deferred from v0.1:

- Adapters for Codex and Auggie; `adapters/base.py` defines the contract.
- OS scheduler installers (v0.1 documents cron / GitHub Actions instead).
- Read-only verification tools during dreaming (check a path or command exists).
- Re-verifying model-written merges; optimizing skills and instruction files beyond guard and edit proposals.
- Server-side refusal fallbacks for the Anthropic provider.
- A real multi-arm task evaluation on public dev sets (see docs/EVALUATION.md).

Non-goals: model fine-tuning, a hosted service, a web UI, vector or graph databases.

## License

Apache-2.0. See [LICENSE](LICENSE), [SECURITY.md](SECURITY.md), [CONTRIBUTING.md](CONTRIBUTING.md), [CHANGELOG.md](CHANGELOG.md).
