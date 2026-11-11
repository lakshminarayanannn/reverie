"""reverie end-to-end demo on synthetic data (runs offline, no API key).

    python examples/demo/run_demo.py            # scripted mock model (default)
    python examples/demo/run_demo.py --config my-reverie.toml   # your configured model

Story:
  session 1  finds a fix after failures (`make docs` fails; `uv run --group docs make docs` works)
  session 2  hits a related problem (`make docs-linkcheck`) and applies the same fix
  session 3  contradicts an earlier assumption (docs no longer deploy on every push to main)
  dream      reconciles them and proposes a diff on a branch
  review     approve; SessionStart injects the right note within budget; targeted recall finds a topic note
  undo       the user rejects one note, then rolls memory back to before the dream

With the default scripted mock, the "model" answers from demo_script.json. That shows the
engine's mechanics, not real learning.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def sh(*args: str, env: dict[str, str], check: bool = True, stdin: str | None = None) -> str:
    cmd = [sys.executable, "-m", "reverie.cli", *args]
    print(f"\n$ reverie {' '.join(args)}")
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, input=stdin)
    out = (proc.stdout + proc.stderr).rstrip()
    if out:
        print(out)
    if check and proc.returncode not in (0, 3):
        raise SystemExit(f"command failed with exit code {proc.returncode}")
    return proc.stdout


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, help="reverie.toml to use instead of the scripted mock")
    ap.add_argument("--workdir", type=Path, help="keep the demo files here (default: a temp dir)")
    args = ap.parse_args()
    work = args.workdir or Path(tempfile.mkdtemp(prefix="reverie-demo-"))
    work.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "REVERIE_HOME": str(work / "home"), "PYTHONIOENCODING": "utf-8"}
    for key in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        env.setdefault(key, "reverie-demo")
    for key in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        env.setdefault(key, "demo@localhost")
    config = args.config
    if config is None:
        config = work / "reverie.toml"
        config.write_text('[llm]\nprovider = "mock"\nmock_responses = "'
                          + (HERE / "demo_script.json").as_posix() + '"\n')
    mem = work / "memory"

    print("== 1. create an empty memory repo")
    sh("init", str(mem), env=env)
    before = git(mem, "rev-parse", "HEAD").strip()

    print("\n== 2. dream over three sessions (fix found, related problem, contradiction)")
    report = json.loads(sh("dream", "--sessions", str(HERE / "sessions"), "--memory", str(mem),
                           "--config", str(config), "--json", env=env))
    rid = report["run_id"]
    print(f"\nproposal {rid}: {report['counts']}")

    print("\n== 3. review the proposed diff")
    print(git(mem, "diff", "main", f"dream/{rid}", "--", ".", ":!.reverie"))
    sh("proposals", "show", rid, "--memory", str(mem), env=env)

    print("\n== 4. approve it")
    sh("proposals", "approve", rid, "--memory", str(mem), env=env)
    sh("guards", "list", "--memory", str(mem), env=env)

    print("\n== 5. a new session starts: SessionStart injects memory (label + budget)")
    out = sh("hook", "session-start", "--memory", str(mem), env=env,
             stdin=json.dumps({"session_id": "demo-next", "hook_event_name": "SessionStart"}))
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "uv run --group docs" in ctx, "the trap note should be injected"
    print(f"\ninjected {len(ctx)} characters (budget 9000); trap note present: yes")
    print("\nthe agent is about to run `make docs` again; the PreToolUse guard warns:")
    sh("hook", "pre-tool-use", "--memory", str(mem), env=env,
       stdin=json.dumps({"session_id": "demo-next", "tool_name": "Bash", "tool_input": {"command": "make docs"}}))

    print("\n== 6. the user disagrees with one note and rejects it")
    notes = [line for line in (mem / "topics").rglob("*.md") for line in line.read_text().splitlines()
             if "tagged releases" in line]
    note_id = notes[0].split("[id: ")[1].split(";")[0] if notes else None
    if note_id:
        sh("notes", "reject", note_id, "--memory", str(mem), "--reason", "demo: user disagrees", env=env)

    print("\n== 7. ...then rolls the whole memory back to before the dream")
    sh("memory", "rollback", before, "--memory", str(mem), env=env)
    print((mem / "MEMORY.md").read_text())
    sh("memory", "log", "--memory", str(mem), env=env)
    print(f"\ndemo files: {work}")


if __name__ == "__main__":
    main()
