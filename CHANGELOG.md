# Changelog

## 0.1.0 (2026-11-08)

First release.

- Dream pipeline: ingest (adapters, redaction, injection flagging), select, extract, verify (rules, string match, clean-context model check, claim trimming, pass/fail contrast re-check), reconcile (dedupe, evidence-recency resolution, contested pairs), organize (placements, topics, one writer per file, link repair), promote (caps), prune (stale, unread, holdout, archive retention), emit (dream branch, PROPOSAL.md, metrics.json).
- Agent Memory Repo storage with lossless round-trips and a git-backed review flow: proposals list/show/approve/reject, notes reject, memory rollback.
- Experience store: SQLite + FTS5, idempotent imports, `reverie search`, retention.
- Providers: Anthropic (default), OpenAI-compatible (incl. Ollama), headless Claude Code (`claude_cli`), mock; response cache, retries with backoff, hard token/USD budget, cloud-consent gate.
- Claude Code integration: transcript adapter (verified on CLI 2.1.319 transcripts), SessionStart injection with label and budget, PreToolUse guards (warn/block), read tracking, deterministic holdout, optional targeted recall (off by default), transcript capture; `reverie install claude-code` prints settings and edits nothing.
- Evaluation: five-arm `reverie eval` with mock and Claude Code runners, isolation scan, noise floor, paired bootstrap CIs, enforced dev/test separation.
- FrontierMemory: corpus conversion with verifier labels, benchmark-mode dream, format checker, Harbor memory-generation agent (verified under Harbor 0.24.0 + Docker), smoke script.
- Devin CLI / Devin desktop integration: local plugin with hooks (memory injection, live session capture, guards), ATIF export adapter.
- Offline demo, three purpose files, docs.
