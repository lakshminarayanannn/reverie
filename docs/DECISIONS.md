# Decisions

One entry per non-obvious choice: context, choice, alternatives, why. Newest entries are appended at the end of each section.

## Sources of external formats

| Format | Source | How it was verified |
|---|---|---|
| Agent Memory Repo spec | https://github.com/AgentMemoryRepo/agentmemoryrepo `SPEC.md` and `skills/agent-memory-repo/SKILL.md`, commit `8798cb26` (2026-10-06) | Read the spec; store round-trip tests use its exact examples (`[source: ...; added: ...]`, `[[path]]`, `## Index`). |
| Claude Code hooks (input/output contracts) | https://code.claude.com/docs/en/hooks (fetched 2026-11-08) | SessionStart `hookSpecificOutput.additionalContext` (10,000-char limit), PreToolUse `permissionDecision`/`permissionDecisionReason`, PostToolUse `tool_input`/`tool_response`, SessionEnd `transcript_path`/`reason` and its 1.5 s default timeout. |
| Claude Code transcript JSONL | Real transcripts written by Claude Code 2.1.319 (`~/.claude/projects/<project-key>/<session>.jsonl`), inspected for keys and record/block types only | Record types `user`, `assistant`, `attachment`, `last-prompt`, `queue-operation`, `tools-used`, `atis-latch`; content blocks `text`, `thinking`, `tool_use`, `tool_result`; fields `sessionId`, `uuid`, `timestamp`, `cwd`, `version`. |
| FrontierMemory rules and harness interface | https://github.com/letta-ai/frontiermemory `docs/rules.md`, `docs/submitting-a-harness.md`, `src/frontier_memory/memory.py`, `agents/probe.py`, `dream_task/` at commit `9af28c2f` (2026-10-27) | Memory format checker semantics (MEMORY.md non-empty, UTF-8, no dot-names, no symlinks, ≤ 8 MiB) reimplemented and tested; Harbor `BaseAgent` shape copied from `probe.py`. |
| Anthropic Messages API / Python SDK | `anthropic` 2.6.0; model IDs and list prices from the official API reference (as of 2026-10) | `RateLimitError`, `APIStatusError`, `APIConnectionError` exist; `messages.create` has no `temperature` parameter; `output_config` exists. |

## Architecture

**D1. The dream makes no tool calls.** Context: the dream reads untrusted transcripts. Choice: v0.1 works only from transcript text; no shell, file, or web access during dreaming. Alternatives: read-only verification tools (check a claimed path exists). Why: safe and reproducible; prompt injection in a transcript cannot trigger actions. Read-only verification tools are a v0.2 option.

**D2. Deterministic defenses run before any model judgment.** Context: an injected instruction is, by construction, "supported" by the text it appears in, so an LLM verifier can be fooled. Choice: evidence flagged by an injection scanner, destructive advice without user-message evidence, and re-derivable observations are dropped by rules; verbatim values are checked by string match. The scripted test mock is deliberately gullible so the tests prove the engine's rules, not the mock's caution. Alternatives: rely on the verify prompt. Why: defense in depth; rules are auditable and testable.

**D3. Contradictions are resolved by evidence recency, not by the model.** Context: the design says newer verified evidence wins. Choice: the model only names the relation (duplicate / refines / contradicts / updates / unrelated); the winner is the side whose newest cited session is later. Equal or unknown recency marks both `contested`. The model's "stronger" hint is used only when timestamps are missing. Why: models are good at noticing conflicts and bad at knowing which is current.

**D4. Retired notes go to `archive.md`, not deleted immediately.** Context: "retiring a note never deletes evidence" and "removed notes stay in git history". Choice: superseded/stale/rejected notes move to `archive.md` (not linked from the index, so agents do not load it), and are deleted from it after `archive_retention_dreams` dreams. Alternatives: delete immediately; keep forever. Why: makes recent retirements inspectable without bloating the injected layer; git history keeps everything.

**D5. One note lives in exactly one file.** Promoted notes move into `MEMORY.md`; demoted ones go back to their recorded home topic. The Agent Memory Repo spec says keep information in one place and link to it.

**D6. Only bullets with an `id` metadata key are managed.** Every other line (user prose, spec-style entries without ids, unknown metadata) is preserved verbatim; unchanged managed notes re-render byte-identically. Why: never destroy content reverie did not write; stay compatible with spec-compliant readers (keys are open).

**D7. Proposal and run artifacts.** `PROPOSAL.md` is committed at `.reverie/proposals/<run id>.md` on the dream branch (and copied to the run dir), not at the repo root. Why: a root `PROPOSAL.md` would be merged into memory and read by agents as if it were memory. Run artifacts default to `<memory>/.reverie/runs/` (git-ignored); `[dream] runs_dir` overrides.

**D8. One pending proposal at a time.** A dream refuses to start while a `dream/*` branch awaits review (exit code 7), unless `--auto-accept`. Alternative: stack proposals. Why: two proposals both edit `.reverie/state.json` and conflict on merge; reviewing in order is simpler and safer for nightly runs.

**D9. Rejection is remembered.** Rejecting a proposal or a note records normalized-text fingerprints in `state.json` (`rejected_fingerprints`); later candidates with the same text are dropped. Rejected proposals are kept as `rejected/<id>` branches. Why: otherwise the next dream re-proposes what the user already said no to.

**D10. Sessions without a model stay un-dreamed.** With no model configured, the dream runs deterministic maintenance only and does not mark sessions as dreamed, so a later run with a model still learns from them. Exit code 3 tells scripts the LLM stages were skipped.

**D11. Canonical JSONL accepts two layouts.** A whole session object per line, or a header line followed by one event per line (supports resumed/incomplete sessions and out-of-order events; duplicates by `idx` are counted, not silently merged). The spec's example is a single object; "JSONL, one session per file" was ambiguous.

**D12. Candidate order inside a dream is chronological by session start, then by first cited event.** So a dream over a batch behaves like a sequence of dreams over single sessions.

**D13. Contrast is a property of the merged note.** A rule cited by a failing and a passing session gets `contrast: true` even when each session alone has one label; contrast notes rank higher for promotion.

**D14. Size limits.** `MEMORY.md` is capped by lines and bytes (defaults 200 / 25,000). Live memory is limited to `max_memory_to_corpus_ratio` (default 0.5) of the cumulative redacted corpus, with a 10 KB floor so tiny corpora are not starved; when exceeded, the lowest-ranked new notes are dropped and reported.

## Providers and safety

**D20. Default provider is `none`.** Context: "no data leaves the machine" unless explicitly configured. Choice: `[llm] provider` defaults to `none`; cloud providers additionally require `[llm] allow_cloud = true` or `--allow-cloud`, and the dream prints what will be sent (counts of redacted excerpts, notes, models, budget) before the first call. Local OpenAI-compatible endpoints (localhost) do not need the cloud flag.

**D21. Sampling parameters are not sent to Anthropic.** The current SDK/API has no `temperature` for current models; the `Provider` interface keeps the argument for other providers. Determinism comes from the response cache instead.

**D22. Own retries, SDK retries off.** `max_retries=0` in the SDK so every attempt is visible to reverie's backoff and budget. Retryable: 429 (honoring `retry-after`), 408/409, 5xx, connection errors.

**D23. A `claude_cli` provider (headless `claude -p`).** Context: many users have Claude Code signed in but no `ANTHROPIC_API_KEY`. Choice: a provider that runs `claude -p` with all tools disabled (`--tools ""`), no MCP, no settings, an empty temp cwd, and reverie's own system prompt. It reports its own cost. Why: lets users with Claude Code but no API key dream.

**D24. Server-side refusal fallbacks are not enabled.** The Anthropic reference recommends the `fallbacks` beta for current models. It could not be tested here (no API key), so v0.1 treats `stop_reason: refusal` as a per-chunk error that is logged and reported. Revisit in v0.2.

**D25. Thinking blocks from Claude Code transcripts are skipped and counted.** Private reasoning is not evidence of what happened in the environment, and it would multiply prompt size.

## Added while measuring (real runs, eval, benchmark)

**D26. Quoted values by string match, prose by the model.** Context: the design says verbatim-quote candidates are checked by string match, not by an LLM. In real run 1 the model marked almost every note `critical`, so string matching of backticked values became the only check on whole sentences. Choice: string match decides the quoted values (and a pure quote is fully checked by it); any prose claim around them also gets the clean-context model check. Alternative: string match only. Why: otherwise "it passed on the first try, so no workaround is needed" rides along unchecked.

**D27. Trim to the supported part, then re-verify.** Context: real run 2 rejected four claims whose core facts were shown and whose added explanation was not. Choice: verify v2 may return `supported_text`; reverie uses it only if it is not longer than the claim, keeps quoted values intact, and passes a second clean-context check. Why: keeps the useful core without letting the model invent a replacement.

**D28. Pass/fail contrasts are verified jointly.** Context: a rule like "set APP_ENV=test or the tests fail" is not shown by the failing session alone or by the passing session alone. Choice: an unsupported candidate from a labeled session is re-verified with the evidence of a similar candidate (token overlap ≥ 0.3) from a session with the opposite label; success marks `contrast: true`. Why: the design prefers contrast-backed rules, and per-candidate verification structurally cannot see them.

**D29. Refines merges are length-capped, not yet re-verified.** A merged text longer than max(1.3 × the longer note, 240 chars) is ignored (sources still merge). Re-verifying merges against combined evidence is deferred to v0.2; run 3 shows why it matters.

**D30. Guard hints: most common pattern, shortest replacement.** Several sessions propose guards for one merged note; the shortest replacement is the most general (run 1's first proposal was `python -m pytest tests/test_utils.py`).

**D31. The eval task agent does not bypass permissions by default.** Context: an eval harness launches autonomous agents; unrestricted shell access is a bad default. Choice: `acceptEdits` plus an explicit allowlist (`Read`, `Edit`, `Write`, `Glob`, `Grep`, a few `Bash(...)` prefixes); `--permission-mode bypassPermissions` only when the user passes it, for disposable sandboxes. Why: an eval harness should not hand an autonomous agent unrestricted shell access by default.

**D32. FrontierMemory agent installs from a host-built wheelhouse.** Context: container networks may block or intercept HTTPS to PyPI, and network during setup is a source of variance. Choice: on Linux + Python 3.12 hosts, build wheels for reverie and its dependencies on the host and install them with `--no-index`; otherwise fall back to installing from PyPI during setup (allowed by the benchmark rules). Verified: Harbor 0.24.0 + Docker, reward 1.0 on the format verifier.

**D33. Benchmark verdict framing from the oracle script.** The gated corpus was not read. The benchmark's `dream_task/solution/solve.sh` counts verdicts with `grep 'Verifier result.*PASS'`, so that line is matched first anywhere in a trajectory; key-based and bare `PASS`/`FAIL` detection in the last records are fallbacks; otherwise the label is `unknown` (counted).

**D34. Trajectories become sessions named after their file** (`t0001.jsonl` → `t0001`), so evidence pointers stay stable and readable; trajectories without timestamps are ordered by file name.

**D35. SessionStart injection budget 9,000 characters.** Claude Code moves `additionalContext` over 10,000 characters to a file with a 2,000-character preview, which would silently drop most of the memory. `MEMORY.md`'s own cap (25 KB) is larger, so the injection truncates at a line boundary with a pointer to the file, and only notes actually included are logged as injected.

**D36. Usage logs live in the memory repo but outside git.** `.reverie/usage.jsonl`, run artifacts and locks are ignored via `.gitignore` (repos created by `reverie init`) and `.git/info/exclude` (any repo), so they never enter memory history or make the tree dirty.

**D37. Warn-mode guards add context, block-mode guards deny.** PreToolUse `additionalContext` reaches the model next to the tool result; it does not stop the command. Blocking is opt-in per guard because a regex can be wrong and agents repeat deny reasons to users as fact.

**D38. Devin capture is built from the hook stream.** Devin CLI hooks carry no transcript path, so the PostToolUse/UserPromptSubmit stream is logged per session and turned into a canonical session on Stop/SessionEnd (idempotent). Full conversations come from `devin --export` (ATIF), read by the ATIF adapter.

**D39. Devin warn-mode guards speak after the command.** Devin documents added context for SessionStart, UserPromptSubmit and PostToolUse, not PreToolUse, so `warn` guards attach their message on PostToolUse; `block` guards use PreToolUse `{"decision": "block", "reason": ...}`.

**D40. Devin integration ships as a local plugin.** `reverie install devin` writes `.devin-plugin/plugin.json` + `hooks.json`, installed with `devin plugins install --local`; it never edits Devin config files.
