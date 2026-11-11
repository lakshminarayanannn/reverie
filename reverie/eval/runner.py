"""Offline task evaluation: run the same tasks under five memory arms and compare.

Arms (always run together on the same tasks):

1. ``no_memory``      - nothing.
2. ``raw_search``     - the agent may grep the redacted sessions folder.
3. ``naive_summary``  - one-pass summary of all sessions into a single MEMORY.md.
4. ``agent_native``   - the agent may write its own notes to its native rules file
                        (CLAUDE.md), carried across tasks within a repeat; no reverie.
5. ``reverie``        - the reverie memory repo, injected like the SessionStart hook does.

Isolation is checked, not configured: after each run the transcript is scanned for
tools, files or memory sources the arm must not use, and the run is invalidated if any
is found.
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from reverie.core.schema import Session
from reverie.eval.stats import noise_floor, paired_bootstrap

Arm = Literal["no_memory", "raw_search", "naive_summary", "agent_native", "reverie"]
ARMS: tuple[Arm, ...] = ("no_memory", "raw_search", "naive_summary", "agent_native", "reverie")
FORBIDDEN_TOOLS = {"WebFetch", "WebSearch"}


class EvalRefused(RuntimeError):
    """The eval cannot run under the requested conditions (e.g. test split on a dirty tree)."""


class MockSpec(BaseModel):
    requires: list[str] = Field(default_factory=list)
    teaches: list[str] = Field(default_factory=list)
    base_pass: float = 0.3
    with_fact_pass: float = 0.85


class EvalTask(BaseModel):
    id: str
    split: Literal["dev", "test"]
    instruction: str
    check: list[str] = Field(default_factory=list)
    workspace: str | None = None
    mock: MockSpec = Field(default_factory=MockSpec)
    root: str = ""

    @classmethod
    def load(cls, task_dir: Path) -> EvalTask:
        data = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
        return cls.model_validate({**data, "root": str(task_dir)})


def load_tasks(tasks_dir: Path) -> list[EvalTask]:
    return sorted((EvalTask.load(p.parent) for p in Path(tasks_dir).glob("*/task.toml")), key=lambda t: t.id)


class ArmContext(BaseModel):
    arm: Arm
    memory_dir: str | None = None  # a folder with MEMORY.md (naive or reverie)
    sessions_dir: str | None = None  # raw_search only
    native_notes: str = ""  # agent_native: carried CLAUDE.md content
    forbidden_paths: list[str] = Field(default_factory=list)


class RunResult(BaseModel):
    task_id: str
    arm: Arm
    repeat: int
    passed: bool
    cost_usd: float = 0.0
    tokens: int = 0
    valid: bool = True
    invalid_reason: str | None = None
    native_notes_after: str | None = None
    transcript: str | None = None


class TaskRunner(Protocol):
    name: str

    def run(self, task: EvalTask, ctx: ArmContext, repeat: int) -> RunResult: ...


def _memory_text(memory_dir: str | None) -> str:
    if not memory_dir:
        return ""
    return "\n".join(p.read_text(errors="replace") for p in sorted(Path(memory_dir).rglob("*.md"))
                     if ".git" not in p.parts and ".reverie" not in p.parts and p.name != "archive.md")


class MockTaskRunner:
    """Deterministic simulated agent. Demonstrates the machinery; it measures nothing real.

    A task passes with ``with_fact_pass`` probability when every fact in ``mock.requires``
    is reachable in the arm (substring of its memory / sessions / native notes), otherwise
    ``base_pass``. Raw search finds a present fact with probability 0.6 per run.
    """

    name = "mock"

    def run(self, task: EvalTask, ctx: ArmContext, repeat: int) -> RunResult:
        seed = int(hashlib.sha256(f"{task.id}|{ctx.arm}|{repeat}".encode()).hexdigest()[:12], 16)
        rng = random.Random(seed)
        if ctx.arm == "raw_search":
            corpus = "\n".join(p.read_text(errors="replace") for p in Path(ctx.sessions_dir or ".").glob("*.jsonl"))
            known = all(f in corpus and rng.random() < 0.6 for f in task.mock.requires)
        elif ctx.arm == "agent_native":
            known = all(f in ctx.native_notes for f in task.mock.requires)
        elif ctx.arm in ("naive_summary", "reverie"):
            text = _memory_text(ctx.memory_dir)
            known = all(f in text for f in task.mock.requires)
        else:
            known = False
        p = task.mock.with_fact_pass if (known and task.mock.requires) else task.mock.base_pass
        passed = rng.random() < p
        notes = ctx.native_notes
        if ctx.arm == "agent_native":  # the agent sometimes saves what it learned
            for fact in task.mock.teaches:
                if fact not in notes and rng.random() < 0.5:
                    notes += f"\n- {fact}"
        return RunResult(task_id=task.id, arm=ctx.arm, repeat=repeat, passed=passed, cost_usd=0.0,
                         tokens=0, native_notes_after=notes if ctx.arm == "agent_native" else None)


def scan_isolation(transcript_lines: list[str], ctx: ArmContext) -> str | None:
    """Return why a run is invalid, or None. Looks at every tool_use in a stream-json transcript."""
    for line in transcript_lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = rec.get("message") if isinstance(rec, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if block.get("name") in FORBIDDEN_TOOLS:
                return f"used forbidden tool {block.get('name')}"
            blob = json.dumps(block.get("input", {}))
            for path in ctx.forbidden_paths:
                if path and path in blob:
                    return f"touched forbidden path {path}"
    return None


class ClaudeCodeRunner:
    """Runs ``claude -p`` headless in a temp copy of the task workspace.

    Permissions: ``acceptEdits`` plus an explicit allowlist of tools by default; anything
    else is denied in headless mode. ``bypassPermissions`` is never the default; pass it
    explicitly (``--permission-mode``) only in a disposable sandbox.

    Memory for the naive and reverie arms is injected through a SessionStart hook passed
    with ``--settings`` (same channel and label for both). User settings are not loaded
    (``--setting-sources ""``, or ``project`` for the agent-native arm so it reads its own
    CLAUDE.md). Web tools are disallowed and, independently, checked for in the transcript.
    """

    name = "claude-code"
    #: Tools the task agent may use without a prompt. Headless runs deny anything else.
    DEFAULT_ALLOWED_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep", "Bash(python3 *)", "Bash(python *)",
                             "Bash(pytest *)", "Bash(ls *)", "Bash(cat *)", "Bash(uv run *)")

    def __init__(self, model: str = "claude-haiku-5-5", max_budget_usd: float = 1.0, timeout_s: float = 900,
                 binary: str | None = None, permission_mode: str = "acceptEdits",
                 allowed_tools: tuple[str, ...] | None = None) -> None:
        found = binary or shutil.which("claude")
        if not found:
            raise EvalRefused("the `claude` CLI is not on PATH; ClaudeCodeRunner is unavailable (use --runner mock)")
        self.binary = found
        self.permission_mode = permission_mode
        self.allowed_tools = tuple(allowed_tools) if allowed_tools is not None else self.DEFAULT_ALLOWED_TOOLS
        self.model = model
        self.max_budget_usd = max_budget_usd
        self.timeout_s = timeout_s

    def run(self, task: EvalTask, ctx: ArmContext, repeat: int) -> RunResult:
        import sys

        with tempfile.TemporaryDirectory(prefix=f"reverie-eval-{task.id}-") as tmp:
            ws = Path(tmp) / "ws"
            src = Path(task.root) / (task.workspace or "workspace")
            if src.is_dir():
                shutil.copytree(src, ws)
            else:
                ws.mkdir()
            instruction = task.instruction
            settings: dict[str, Any] = {"permissions": {"deny": ["WebFetch", "WebSearch"]}}
            sources = ""
            if ctx.arm == "raw_search" and ctx.sessions_dir:
                shutil.copytree(ctx.sessions_dir, ws / "past-sessions")
                instruction += ("\n\nTranscripts of past sessions in this environment are in ./past-sessions/ "
                                "(redacted JSONL). You may search them.")
            elif ctx.arm in ("naive_summary", "reverie") and ctx.memory_dir:
                hook = {"type": "command", "command": sys.executable,
                        "args": ["-m", "reverie.online.hooks", "session-start", "--memory", ctx.memory_dir]}
                settings["hooks"] = {"SessionStart": [{"hooks": [hook]}]}
                # Same disclosure as the other memory arms get for their source.
                instruction += ("\n\nMemory from your earlier sessions in this project was loaded at the start "
                                "of this session.")
            elif ctx.arm == "agent_native":
                (ws / "CLAUDE.md").write_text(ctx.native_notes or "# Notes\n")
                instruction += ("\n\nYou may record durable notes for future sessions in this environment in "
                                "./CLAUDE.md.")
                sources = "project"
            settings_path = Path(tmp) / "settings.json"
            settings_path.write_text(json.dumps(settings))
            cmd = [self.binary, "-p", "--model", self.model, "--output-format", "stream-json", "--verbose",
                   "--setting-sources", sources, "--settings", str(settings_path), "--strict-mcp-config",
                   "--disallowedTools", "WebFetch", "WebSearch", "--permission-mode", self.permission_mode,
                   "--allowedTools", *self.allowed_tools,
                   "--max-budget-usd", str(self.max_budget_usd), "--no-session-persistence"]
            try:
                proc = subprocess.run(cmd, input=instruction, capture_output=True, text=True, cwd=ws,
                                      timeout=self.timeout_s, check=False)
                lines = proc.stdout.splitlines()
            except subprocess.TimeoutExpired:
                lines = []
            cost = 0.0
            tokens = 0
            for line in lines:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("type") == "result":
                    cost = float(rec.get("total_cost_usd") or 0.0)
                    usage = rec.get("usage") or {}
                    tokens = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
            invalid = scan_isolation(lines, ctx) if lines else "no transcript (timeout or CLI failure)"
            passed = False
            if task.check and not invalid:
                chk = subprocess.run(task.check, cwd=ws, capture_output=True, text=True, timeout=600, check=False)
                passed = chk.returncode == 0
            notes_after = (ws / "CLAUDE.md").read_text(errors="replace") if ctx.arm == "agent_native" and \
                (ws / "CLAUDE.md").exists() else None
            return RunResult(task_id=task.id, arm=ctx.arm, repeat=repeat, passed=passed, cost_usd=cost,
                             tokens=tokens, valid=invalid is None, invalid_reason=invalid,
                             native_notes_after=notes_after, transcript="\n".join(lines[-2000:]))


# ---------------------------------------------------------------------------
# naive baseline
# ---------------------------------------------------------------------------


def naive_summary(sessions: list[Session], out_dir: Path, summarize: Callable[[str], str] | None) -> Path:
    """One-pass summary of all sessions into ``out_dir/MEMORY.md`` (deterministic without a model)."""
    from reverie.dream.select import render_transcript

    out_dir.mkdir(parents=True, exist_ok=True)
    if summarize is not None:
        corpus = "\n\n".join(render_transcript(s, s.events, set()) for s in sessions)[:400_000]
        text = summarize(corpus)
    else:
        bullets = []
        for s in sessions:
            first = next((e.text for e in s.events if e.type == "user_msg" and e.text), "")
            last = next((e.text for e in reversed(s.events) if e.type == "assistant_msg" and e.text), "")
            bullets.append(f"- {s.session_id}: asked: {first[:200]} | outcome: {last[:200]}")
        text = "# Memory (naive summary)\n\n" + "\n".join(bullets)
    (out_dir / "MEMORY.md").write_text(text.strip() + "\n", encoding="utf-8")
    return out_dir


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


class EvalReport(BaseModel):
    runner: str
    split: str
    repeats: int
    commit: str | None = None
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    purpose_sha256: str | None = None
    tasks: list[str] = Field(default_factory=list)
    runs: list[RunResult] = Field(default_factory=list)
    per_task: dict[str, dict[str, float | None]] = Field(default_factory=dict)
    arm_means: dict[str, float | None] = Field(default_factory=dict)
    noise_floor: float | None = None
    vs_no_memory: dict[str, dict[str, Any]] = Field(default_factory=dict)
    vs_raw_search: dict[str, dict[str, Any]] = Field(default_factory=dict)
    invalid_runs: int = 0
    total_cost_usd: float = 0.0
    notes: list[str] = Field(default_factory=list)


def git_state(path: Path) -> tuple[bool, str | None]:
    """(is clean, HEAD sha) for the git repo containing ``path``; (False, None) if not a repo."""
    top = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if top.returncode != 0:
        return False, None
    status = subprocess.run(["git", "-C", str(path), "status", "--porcelain"], capture_output=True, text=True)
    sha = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True)
    return status.stdout.strip() == "", sha.stdout.strip() or None


def config_fingerprint(prompt_versions: dict[str, str], purpose: str) -> str:
    return hashlib.sha256(json.dumps({"p": prompt_versions, "u": purpose}, sort_keys=True).encode()).hexdigest()


def run_eval(tasks: list[EvalTask], runner: TaskRunner, *, split: str, repeats: int, contexts: dict[Arm, ArmContext],
             ledger: Path, prompt_versions: dict[str, str], purpose: str, tasks_dir: Path) -> EvalReport:
    if repeats < 3:
        raise EvalRefused("at least 3 repeats per arm are required")
    chosen = [t for t in tasks if t.split == split]
    if not chosen:
        raise EvalRefused(f"no tasks tagged split={split!r}")
    fingerprint = config_fingerprint(prompt_versions, purpose)
    ledger_data: dict[str, Any] = json.loads(ledger.read_text()) if ledger.exists() else {"dev": []}
    commit = None
    if split == "test":
        clean, commit = git_state(tasks_dir)
        if commit is None:
            raise EvalRefused(f"--split test needs the tasks to live in a git repo ({tasks_dir})")
        if not clean:
            raise EvalRefused("--split test refuses to run on a dirty git tree; commit or stash first")
        if fingerprint not in ledger_data.get("dev", []):
            raise EvalRefused("these prompts/purpose were never evaluated on the dev split; run --split dev first "
                              "(prompt and purpose changes may only be evaluated on dev)")
    else:
        _, commit = git_state(tasks_dir)
    report = EvalReport(runner=runner.name, split=split, repeats=repeats, commit=commit,
                        prompt_versions=prompt_versions, purpose_sha256=hashlib.sha256(purpose.encode()).hexdigest(),
                        tasks=[t.id for t in chosen])
    for rep in range(repeats):
        native = contexts["agent_native"].native_notes if "agent_native" in contexts else ""
        for task in chosen:
            for arm in ARMS:
                if arm not in contexts:
                    continue
                ctx = contexts[arm]
                if arm == "agent_native":
                    ctx = ctx.model_copy(update={"native_notes": native})
                res = runner.run(task, ctx, rep)
                if arm == "agent_native" and res.native_notes_after is not None:
                    native = res.native_notes_after
                res.transcript = None  # keep reports small; transcripts are scanned, not stored
                report.runs.append(res)
    _summarize(report)
    if split == "dev":
        dev = set(ledger_data.get("dev", []))
        dev.add(fingerprint)
        ledger_data["dev"] = sorted(dev)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps(ledger_data, indent=2))
    if runner.name == "mock":
        report.notes.append("MockTaskRunner: simulated agent; these numbers demonstrate the pipeline and measure "
                            "nothing about real memory quality.")
    return report


def _summarize(report: EvalReport) -> None:
    valid = [r for r in report.runs if r.valid]
    report.invalid_runs = len(report.runs) - len(valid)
    report.total_cost_usd = round(sum(r.cost_usd for r in report.runs), 6)
    by: dict[str, dict[str, list[float]]] = {}
    for r in valid:
        by.setdefault(r.arm, {}).setdefault(r.task_id, []).append(1.0 if r.passed else 0.0)
    for task in report.tasks:
        report.per_task[task] = {arm: (sum(v) / len(v) if (v := by.get(arm, {}).get(task)) else None) for arm in ARMS
                                 if arm in by}
    for arm, tasks in by.items():
        vals = [sum(v) / len(v) for v in tasks.values()]
        report.arm_means[arm] = round(sum(vals) / len(vals), 4) if vals else None
    report.noise_floor = noise_floor(by.get("no_memory", {}))
    means = {arm: {t: sum(v) / len(v) for t, v in tasks.items()} for arm, tasks in by.items()}
    for arm in by:
        if arm != "no_memory" and "no_memory" in means:
            report.vs_no_memory[arm] = paired_bootstrap(means[arm], means["no_memory"])
        if arm not in ("raw_search", "no_memory") and "raw_search" in means:
            report.vs_raw_search[arm] = paired_bootstrap(means[arm], means["raw_search"])
