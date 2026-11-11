# Limitations

What v0.1 does not do, what has not been verified yet, and known weaknesses. Measured numbers are in [EVALUATION.md](EVALUATION.md).

## Verification status

| item | status |
|---|---|
| Anthropic provider | Tested against a fake SDK client (success, retries on 429/5xx/connection errors, permanent errors, refusals, budget accounting). Not yet run against the live API; real-model dreams so far used the `claude_cli` provider. |
| Task evaluation | One small recall eval with a real agent (3 designs × 75 runs on 5 author-written tasks; EVALUATION.md). No public benchmark score and no real coding tasks yet. |
| PostToolUse attribution check (≥ 10 runs) | Not run, so `online.targeted_recall` stays **off by default**. Protocol in EVALUATION.md. |
| FrontierMemory | Format verifier passed under Harbor 0.24.0 + Docker on a synthetic corpus. The gated corpus was not used; verdict framing (`Verifier result: PASS/FAIL`) was taken from the benchmark's oracle script. Modal untested. Where container HTTPS cannot reach PyPI, the Harbor agent installs from a host-built wheelhouse. |
| Devin CLI / Devin desktop | Hooks, plugin and ATIF export reader follow the Devin CLI documentation and are tested against the documented payloads; not yet run inside a real Devin session. Assistant prose between tool calls is not in Devin's hook stream (use `devin --export` + `--adapter atif` for full conversations). Hook commands are POSIX-quoted. |
| Claude Code | Transcript adapter checked against real Claude Code 2.1.319 transcripts; hook contracts from the official hooks docs. |
| Windows / macOS | Not run. Code uses `pathlib`, no `shell=True`, atomic writes, exec-form hooks for Claude Code, and avoids `os.kill(pid, 0)` on Windows. CI is configured for all three OSes. |
| Codex, Auggie | Not implemented; `adapters/base.py` defines the contract. |

## Known weaknesses

- **Model variance.** Three real dreams over the same 12 sessions produced different memories (EVALUATION.md). Extraction sometimes misses a session (run 3 skipped the failing APP_ENV session), so a pass/fail contrast rule can be lost.
- **Merges are not re-verified.** A model-written `refines` merge is length-capped but not re-checked against the combined evidence. Run 3 merged an unrelated fact into the pytest trap note; the unsupported-claim sample flagged it (rate 0.2), but the dream still proposed it.
- **Verification sees only cited events** (plus the tool call that produced a cited result). A claim that needs wider context can be rejected; a claim built from carefully chosen excerpts can pass.
- **The injection scanner is pattern-based.** It catches common phrasings ("ignore previous instructions", "add ... to MEMORY.md", "note to the AI", "create a guard that blocks"). Novel phrasings rely on the model and on the remaining rules.
- **Re-derivable detection is narrow**: a `fact` whose evidence is only read-only tool output (`ls`, `cat`, `git status`, Read/Glob/Grep). Re-derivable claims stated by the assistant in prose can pass.
- **Guards** match the raw command string with a regex; shell aliases, wrappers or `bash -c "..."` can evade or trigger them. `warn` mode adds context after the PreToolUse decision; it does not stop the command.
- **Holdout** needs labeled outcomes; Claude Code sessions have none by default (`outcome` is `unknown`), so "used" stays "not measured" until outcomes are supplied (tests, verifier, or user labels).
- **Recency** is the session start time. Sessions without timestamps (some benchmark trajectories) are ordered by file name, which may not reflect real order; equal times make contradictions `contested`.
- **One pending proposal at a time.** Nightly runs stop (exit 7) until the previous proposal is approved or rejected, unless `--auto-accept`.
- **Size accounting** compares memory to the cumulative redacted corpus of dreamed sessions; on very small corpora the 10 KB floor dominates.
- **Cost estimates** use list prices for known model ids; unknown models are recorded as unpriced (cost 0) and listed in `budget.unpriced_models`.
- **Claude Code transcript format** is versioned by CLI version; majors 1 and 2 are accepted and anything else fails loudly. Unknown record and block types inside a supported version are counted and reported, not parsed.
- **The dream is single-threaded**; large corpora are slow. Response caching makes reruns cheap.
