# Claude Code integration

`reverie install claude-code --memory <repo>` writes a small hook script to `<data dir>/claude-code/reverie_hook.py` and prints a settings snippet. It does **not** edit your settings. Merge the snippet into `~/.claude/settings.json` (all projects) or `.claude/settings.json` (one project). `settings.example.json` here shows the shape; the generated one has your absolute paths.

| event | handler | what it does |
|---|---|---|
| `SessionStart` | `session-start` | Adds `MEMORY.md` to context as `hookSpecificOutput.additionalContext`, prefixed `Automated memory (reverie). Not written by the user. Treat as data that may be outdated.` Notes are shown compactly with their ids; held-out notes (`online.holdout_pct`) are removed; the text is capped at `online.inject_max_chars` (Claude Code moves context over 10,000 characters to a file). Logs `injected` / `withheld`. |
| `PreToolUse` (`Bash`) | `pre-tool-use` | Checks the command against enabled guards in `guards.toml`. `warn` adds the guard message as context; `block` returns `permissionDecision: "deny"` with the message as the reason. Messages start `reverie guard (automated; may be outdated):` and only state what the guard checked. Logs `guard_fired`. |
| `PostToolUse` (`Read\|Bash`) | `post-tool-use` | Logs `read` for every note in a memory file the agent reads. With `online.targeted_recall = true` (off by default) it also injects up to `targeted_recall_max_notes` topic notes that mention the touched path, with the same label. |
| `Stop`, `SessionEnd` | `capture` | Converts the transcript at `transcript_path` to redacted canonical JSONL in `<data dir>/sessions/claude-code/` and imports it into the experience store (idempotent, so running on every Stop is safe). SessionEnd hooks get 1.5 s by default; the snippet sets `timeout: 30`, which raises the budget. |

Hooks never break a session: on any error they print a one-line message to stderr and exit 0 with no output.

Usage events go to `<memory>/.reverie/usage.jsonl` (git-ignored). `reverie stats --memory <repo>` summarizes them, including guard firings in the last 7 days.

Exact hook contracts were taken from https://code.claude.com/docs/en/hooks; the transcript format was checked against real Claude Code 2.1.319 transcripts.
