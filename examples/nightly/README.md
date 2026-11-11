# Nightly dreams

reverie does not install schedulers. Two examples you can adapt:

- `crontab.txt`: a nightly cron entry for a machine where Claude Code runs (sessions are captured by the hooks into `<data dir>/sessions/claude-code`).
- `dream.yml`: a GitHub Actions workflow for a memory repo hosted on GitHub that also holds exported sessions.

Both leave a proposal for review. A dream refuses to start (exit 7) while a previous proposal is pending; approve or reject it with `reverie proposals ...`, or add `--auto-accept` if you accept unreviewed changes.
