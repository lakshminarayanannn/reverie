"""Command-line interface. Every command supports ``--json``; exit codes are in ``ExitCode``."""

from __future__ import annotations

import json
import logging
import sys
from enum import IntEnum
from pathlib import Path
from typing import Any, NoReturn

import typer

from reverie import __version__
from reverie.core.config import ReverieConfig, experience_db_path, load_config

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="reverie: dream over past agent sessions to build better memory.")


class ExitCode(IntEnum):
    OK = 0
    ERROR = 1
    USAGE = 2
    LLM_UNAVAILABLE = 3
    BUDGET_EXCEEDED = 4
    LOCKED = 5
    INPUT_ERROR = 6
    REFUSED = 7
    NOT_FOUND = 8


JsonOpt = typer.Option(False, "--json", help="Print machine-readable JSON.")
ConfigOpt = typer.Option(None, "--config", help="Path to reverie.toml.")
MemoryOpt = typer.Option(..., "--memory", "-m", help="Path to the memory repo.")


def _cfg(path: Path | None) -> ReverieConfig:
    try:
        return load_config(path)
    except Exception as exc:
        _fail(f"invalid config: {exc}", ExitCode.INPUT_ERROR, False)


def _out(data: Any, as_json: bool, human: str | None = None) -> None:
    if as_json:
        typer.echo(json.dumps(data, indent=2, sort_keys=True, default=str))
    else:
        typer.echo(human if human is not None else json.dumps(data, indent=2, default=str))


def _fail(message: str, code: ExitCode, as_json: bool) -> NoReturn:
    if as_json:
        typer.echo(json.dumps({"ok": False, "error": message, "exit_code": int(code)}))
    else:
        typer.echo(f"error: {message}", err=True)
    raise typer.Exit(int(code))


@app.callback()
def _root(verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging to stderr.")) -> None:
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")


@app.command()
def version(as_json: bool = JsonOpt) -> None:
    """Print the reverie version."""
    _out({"version": __version__}, as_json, __version__)


@app.command()
def init(memory: Path = typer.Argument(..., help="Directory for the new memory repo."),
         as_json: bool = JsonOpt) -> None:
    """Create a memory repo (git + MEMORY.md). Idempotent."""
    from reverie.core.store import MemoryStore, StoreError

    store = MemoryStore(memory)
    try:
        store.init()
    except StoreError as exc:
        _fail(str(exc), ExitCode.INPUT_ERROR, as_json)
    _out({"ok": True, "memory": str(store.root)}, as_json, f"memory repo ready at {store.root}")


@app.command("import")
def import_cmd(path: Path = typer.Argument(..., help="Session file or directory."),
               adapter: str = typer.Option("auto", help="auto | generic | claude-code"),
               config: Path | None = ConfigOpt, as_json: bool = JsonOpt) -> None:
    """Import sessions into the experience store (redacted, idempotent)."""
    from reverie.adapters import UnsupportedFormatVersion
    from reverie.core.experience import ExperienceStore
    from reverie.dream.ingest import import_into_store

    cfg = _cfg(config)
    try:
        with ExperienceStore(experience_db_path(cfg)) as store:
            summary = import_into_store(store, path, cfg, adapter=adapter)
    except UnsupportedFormatVersion as exc:
        _fail(str(exc), ExitCode.INPUT_ERROR, as_json)
    except FileNotFoundError as exc:
        _fail(str(exc), ExitCode.NOT_FOUND, as_json)
    human = (f"sessions: {summary['sessions']} ({summary['sessions_new']} new), events new: "
             f"{summary['events_new']}, existing: {summary['events_existing']}, "
             f"files skipped (unchanged): {summary['files_unchanged']}, secrets redacted: "
             f"{summary['secrets_redacted']}")
    if summary["errors"]:
        human += "\n" + "\n".join(f"warning: {e}" for e in summary["errors"])
    _out(summary, as_json, human)


@app.command()
def search(query: str = typer.Argument(...), limit: int = typer.Option(10, help="Max hits."),
           memory: Path | None = typer.Option(None, "--memory", "-m", help="Also index this memory repo."),
           config: Path | None = ConfigOpt, as_json: bool = JsonOpt) -> None:
    """Full-text search over redacted session events and memory notes."""
    from reverie.core.experience import ExperienceStore
    from reverie.core.store import MemoryStore

    cfg = _cfg(config)
    with ExperienceStore(experience_db_path(cfg)) as store:
        if memory is not None:
            mstore = MemoryStore(memory)
            mstore.require()
            store.index_notes(mstore.load().all_notes())
        hits = store.search(query, limit=limit)
    data = [{"kind": h.kind, "ref": h.ref, "snippet": h.snippet} for h in hits]
    human = "\n".join(f"[{h.kind}] {h.ref}: {h.snippet}" for h in hits) or "no matches"
    _out(data, as_json, human)


@app.command()
def doctor(config: Path | None = ConfigOpt, as_json: bool = JsonOpt) -> None:
    """Report which capabilities are available in this environment."""
    import os
    import shutil

    from reverie.core.experience import has_fts5

    cfg = _cfg(config)
    report = {
        "version": __version__,
        "python": sys.version.split()[0],
        "git": shutil.which("git") is not None,
        "sqlite_fts5": has_fts5(),
        "config_file": cfg.source,
        "llm_provider": cfg.llm.provider,
        "allow_cloud": cfg.llm.allow_cloud,
        "anthropic_api_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "claude_cli": shutil.which("claude") is not None,
        "experience_db": str(experience_db_path(cfg)),
    }
    human = "\n".join(f"{k}: {v}" for k, v in report.items())
    _out(report, as_json, human)



# ---------------------------------------------------------------------------
# dream
# ---------------------------------------------------------------------------


@app.command()
def dream(sessions: Path = typer.Option(..., "--sessions", "-s", help="Directory (or file) of session transcripts."),
          memory: Path = MemoryOpt,
          purpose: Path | None = typer.Option(None, "--purpose", help="Markdown file stating what memory is for."),
          budget_usd: float | None = typer.Option(None, "--budget-usd", help="Hard USD cap for this run."),
          auto_accept: bool = typer.Option(False, "--auto-accept", help="Merge the proposal immediately."),
          no_cache: bool = typer.Option(False, "--no-cache", help="Disable the model response cache."),
          allow_cloud: bool = typer.Option(False, "--allow-cloud",
                                           help="Consent to sending redacted excerpts to a cloud model."),
          adapter: str = typer.Option("auto", help="auto | generic | claude-code"),
          config: Path | None = ConfigOpt, as_json: bool = JsonOpt) -> None:
    """Dream: turn new sessions into a reviewed memory proposal (branch dream/<run id>)."""
    from reverie.adapters import UnsupportedFormatVersion
    from reverie.core.store import LockHeld, StoreError
    from reverie.dream.pipeline import DreamOptions, PendingProposal, run_dream

    cfg = _cfg(config)
    opts = DreamOptions(sessions=sessions, memory=memory, purpose=purpose, budget_usd=budget_usd,
                        auto_accept=auto_accept, use_cache=not no_cache, allow_cloud=allow_cloud, adapter=adapter)
    try:
        rep = run_dream(opts, cfg, notify=lambda m: typer.echo(m, err=True))
    except LockHeld as exc:
        _fail(str(exc), ExitCode.LOCKED, as_json)
    except PendingProposal as exc:
        _fail(str(exc), ExitCode.REFUSED, as_json)
    except UnsupportedFormatVersion as exc:
        _fail(str(exc), ExitCode.INPUT_ERROR, as_json)
    except FileNotFoundError as exc:
        _fail(str(exc), ExitCode.NOT_FOUND, as_json)
    except StoreError as exc:
        _fail(str(exc), ExitCode.ERROR, as_json)
    code = ExitCode.OK
    if rep.status == "partial_budget":
        code = ExitCode.BUDGET_EXCEEDED
    elif not rep.llm_available:
        code = ExitCode.LLM_UNAVAILABLE
    lines = [f"dream {rep.run_id}: {rep.status}", f"provider: {rep.provider}",
             "changes: " + (", ".join(f"{k}={v}" for k, v in sorted(rep.counts.items())) or "none")]
    if rep.branch:
        lines.append(f"proposal branch: {rep.branch}" + (" (merged)" if rep.merged else
                                                         f"  ->  reverie proposals show {rep.run_id}"))
    lines.append(f"artifacts: {rep.run_dir}")
    lines += [f"warning: {w}" for w in rep.warnings] + [f"error: {e}" for e in rep.errors]
    _out(rep.model_dump() | {"exit_code": int(code)}, as_json, "\n".join(lines))
    raise typer.Exit(int(code))


# ---------------------------------------------------------------------------
# proposals / memory / notes / guards
# ---------------------------------------------------------------------------

proposals_app = typer.Typer(no_args_is_help=True, help="Review dream proposals.")
app.add_typer(proposals_app, name="proposals")
memory_app = typer.Typer(no_args_is_help=True, help="Memory repo history.")
app.add_typer(memory_app, name="memory")
notes_app = typer.Typer(no_args_is_help=True, help="Act on single notes.")
app.add_typer(notes_app, name="notes")
guards_app = typer.Typer(no_args_is_help=True, help="Manage PreToolUse guards.")
app.add_typer(guards_app, name="guards")


def _store(memory: Path) -> Any:
    from reverie.core.store import MemoryStore

    store = MemoryStore(memory)
    if not store.exists():
        _fail(f"{store.root} is not a memory repo; run `reverie init {memory}`", ExitCode.NOT_FOUND, False)
    return store


def _review(fn: Any, as_json: bool) -> Any:
    from reverie.core.store import StoreError

    try:
        return fn()
    except StoreError as exc:
        _fail(str(exc), ExitCode.ERROR, as_json)


@proposals_app.command("list")
def proposals_list(memory: Path = MemoryOpt, as_json: bool = JsonOpt) -> None:
    """List pending and rejected proposals."""
    from reverie.online.review import list_proposals

    items = list_proposals(_store(memory))
    human = "\n".join(f"{p['id']}  [{p['status']}]  {p['summary']}" for p in items) or "no proposals"
    _out(items, as_json, human)


@proposals_app.command("show")
def proposals_show(run_id: str, memory: Path = MemoryOpt, as_json: bool = JsonOpt) -> None:
    """Print a proposal's PROPOSAL.md."""
    from reverie.online.review import show_proposal

    text = _review(lambda: show_proposal(_store(memory), run_id), as_json)
    _out({"id": run_id, "proposal": text}, as_json, text)


@proposals_app.command("approve")
def proposals_approve(run_id: str, memory: Path = MemoryOpt,
                      no_guards: bool = typer.Option(False, "--no-guards", help="Do not add proposed guards."),
                      as_json: bool = JsonOpt) -> None:
    """Merge a proposal into the default branch (and add its guards)."""
    from reverie.online.review import approve

    res = _review(lambda: approve(_store(memory), run_id, include_guards=not no_guards), as_json)
    _out(res, as_json, f"approved {res['approved']} into {res['merged_into']}; guards added: {res['guards_added']}")


@proposals_app.command("reject")
def proposals_reject(run_id: str, memory: Path = MemoryOpt, as_json: bool = JsonOpt) -> None:
    """Reject a proposal (kept as rejected/<id>; its notes will not be proposed again)."""
    from reverie.online.review import reject

    res = _review(lambda: reject(_store(memory), run_id), as_json)
    _out(res, as_json, f"rejected {res['rejected']} (kept as {res['kept_as']})")


@memory_app.command("rollback")
def memory_rollback(rev: str, memory: Path = MemoryOpt, as_json: bool = JsonOpt) -> None:
    """Restore the memory tree of a previous git revision as a new commit."""
    store = _store(memory)
    sha = _review(lambda: store.rollback(rev), as_json)
    _out({"rolled_back_to": rev, "commit": sha}, as_json,
         f"memory restored to {rev}" + (f" (commit {sha[:10]})" if sha else " (no change)"))


@memory_app.command("log")
def memory_log(memory: Path = MemoryOpt, limit: int = typer.Option(20), as_json: bool = JsonOpt) -> None:
    """Show recent memory commits."""
    from reverie.core.store import git

    out = git(_store(memory).root, "log", f"-{limit}", "--format=%h%x09%ad%x09%s", "--date=short").stdout
    rows = [dict(zip(("rev", "date", "subject"), line.split("\t", 2), strict=False)) for line in out.splitlines()]
    _out(rows, as_json, out.rstrip())


@notes_app.command("reject")
def notes_reject(note_id: str, memory: Path = MemoryOpt,
                 reason: str = typer.Option("rejected by user", help="Why."), as_json: bool = JsonOpt) -> None:
    """Retire one note now; future dreams will not re-add it."""
    from reverie.online.review import reject_note

    res = _review(lambda: reject_note(_store(memory), note_id, reason), as_json)
    _out(res, as_json, f"rejected {note_id} (was in {res['from']})")


@guards_app.command("list")
def guards_list(memory: Path = MemoryOpt, as_json: bool = JsonOpt) -> None:
    """List guards with their mode and state."""
    from reverie.online.review import read_guards

    guards = read_guards(_store(memory))
    human = "\n".join(f"{g.get('id')}  {'on ' if g.get('enabled', True) else 'off'}  {g.get('mode', 'warn'):5}  "
                      f"{g.get('pattern')}  ->  {g.get('replacement')}" for g in guards) or "no guards"
    _out(guards, as_json, human)


@guards_app.command("disable")
def guards_disable(guard_id: str, memory: Path = MemoryOpt, as_json: bool = JsonOpt) -> None:
    """Disable a guard."""
    from reverie.online.review import set_guard_enabled

    res = _review(lambda: set_guard_enabled(_store(memory), guard_id, False), as_json)
    _out(res, as_json, f"disabled {guard_id}")


@guards_app.command("enable")
def guards_enable(guard_id: str, memory: Path = MemoryOpt, as_json: bool = JsonOpt) -> None:
    """Enable a guard."""
    from reverie.online.review import set_guard_enabled

    res = _review(lambda: set_guard_enabled(_store(memory), guard_id, True), as_json)
    _out(res, as_json, f"enabled {guard_id}")


@app.command()
def stats(memory: Path = MemoryOpt, config: Path | None = ConfigOpt, as_json: bool = JsonOpt) -> None:
    """Memory metrics, usage, holdout results and guard firings (deterministic; no model calls)."""
    from reverie.core.schema import LIVE_STATUSES
    from reverie.eval.metrics import memory_metrics
    from reverie.online.holdout import holdout_stats
    from reverie.online.usage import guard_firings_per_week, read_usage, success_levels

    cfg = _cfg(config)
    store = _store(memory)
    repo = store.load()
    state = store.read_state()
    m = memory_metrics(repo.files, set(repo.other_files), int(state.get("corpus_bytes", 0) or 0),
                       cfg.dream.memory_md_max_lines, cfg.dream.memory_md_max_bytes)
    usage = read_usage(store.root)
    live = {n.id for _, n in repo.all_notes() if n.status in LIVE_STATUSES}
    outcomes = _outcomes(cfg)
    hold = {k: v.model_dump() | {"lift": v.lift} for k, v in holdout_stats(usage, outcomes).items()}
    data = {"memory": m, "success_levels": success_levels(usage, live), "dream_count": state.get("dream_count", 0),
            "guard_firings_last_7_days": guard_firings_per_week(usage), "holdout": hold,
            "usage_events": len(usage)}
    human = "\n".join([
        f"memory: {m['memory_bytes']} bytes, MEMORY.md {m['memory_md']['lines']} lines / {m['memory_md']['bytes']} "
        f"bytes (cap {m['memory_md']['max_lines']} / {m['memory_md']['max_bytes']})",
        f"notes by status: {m['notes_by_status']}; dangling links: {m['dangling_links']}",
        "success levels: " + "; ".join(f"{k}={v}" for k, v in data["success_levels"].items()),
        f"guard firings (7 days): {data['guard_firings_last_7_days'] or 'none'}",
        f"holdout notes with labeled sessions: {len(hold)}",
    ])
    _out(data, as_json, human)


# ---------------------------------------------------------------------------
# Claude Code integration
# ---------------------------------------------------------------------------

install_app = typer.Typer(no_args_is_help=True, help="Install integrations (prints settings; edits nothing).")
app.add_typer(install_app, name="install")


@install_app.command("claude-code")
def install_claude_code_cmd(memory: Path = MemoryOpt, config: Path | None = ConfigOpt,
                            out_dir: Path | None = typer.Option(None, "--out-dir", help="Hook script dir."),
                            targeted_recall: bool = typer.Option(False, "--targeted-recall",
                                                                 help="Also match Edit/Write for targeted recall."),
                            as_json: bool = JsonOpt) -> None:
    """Write the hook script and print the Claude Code settings snippet (your settings are not edited)."""
    from reverie.online.install import install_claude_code

    _store(memory)
    res = install_claude_code(memory, config, out_dir, targeted_recall)
    human = (f"hook script: {res['script']}\nsnippet saved to: {res['snippet_path']}\n\n"
             "Merge this into ~/.claude/settings.json (all projects) or .claude/settings.json (one project). "
             "reverie did not edit any settings file.\n\n" + json.dumps(res["snippet"], indent=2))
    _out(res, as_json, human)


@install_app.command("devin")
def install_devin_cmd(memory: Path = MemoryOpt, config: Path | None = ConfigOpt,
                      out_dir: Path | None = typer.Option(None, "--out-dir", help="Where to write the plugin."),
                      as_json: bool = JsonOpt) -> None:
    """Write a local Devin CLI / Devin desktop plugin with reverie's hooks (your settings are not edited)."""
    from reverie.online.install import install_devin

    _store(memory)
    res = install_devin(memory, config, out_dir)
    human = (f"plugin written to: {res['plugin_dir']}\n\nInstall it in Devin CLI (also used by Devin desktop):\n"
             f"  {res['install_command']}\n\nThen start a new Devin session. Captured sessions go to "
             "<data dir>/sessions/devin; dream over them with `reverie dream --sessions ...`. "
             "reverie did not edit any settings file.")
    _out(res, as_json, human)


@app.command()
def hook(event: str = typer.Argument(..., help="session-start | pre-tool-use | post-tool-use | capture | "
                                                 "user-prompt-submit"),
         memory: Path | None = typer.Option(None, "--memory", "-m"), config: Path | None = ConfigOpt,
         agent: str = typer.Option("claude-code", "--agent", help="claude-code | devin")) -> None:
    """Run a hook handler (reads the hook payload JSON on stdin)."""
    from reverie.online.hooks import main as hook_main

    args = [event, "--agent", agent] + (["--memory", str(memory)] if memory else []) + \
        (["--config", str(config)] if config else [])
    raise typer.Exit(hook_main(args))


@app.command()
def inject(memory: Path = MemoryOpt, session_id: str = typer.Option("preview", "--session-id"),
           config: Path | None = ConfigOpt, as_json: bool = JsonOpt) -> None:
    """Print what SessionStart would inject for a session (no usage is logged)."""
    from reverie.online.inject import build_injection

    cfg = _cfg(config)
    inj = build_injection(_store(memory), session_id, cfg.online.holdout_pct, cfg.online.inject_max_chars)
    _out(inj.model_dump() | {"chars": len(inj.text)}, as_json, inj.text)


@app.command()
def convert(path: Path = typer.Argument(..., help="Transcript file or directory."),
            out: Path = typer.Option(..., "--out", help="Directory for canonical JSONL."),
            adapter: str = typer.Option("claude-code", help="claude-code | atif (devin --export) | generic | auto"),
            config: Path | None = ConfigOpt, as_json: bool = JsonOpt) -> None:
    """Convert transcripts to reverie's canonical (redacted) session format."""
    from reverie.adapters import UnsupportedFormatVersion, load_sessions
    from reverie.adapters.generic_jsonl import write_session
    from reverie.dream.ingest import govern

    cfg = _cfg(config)
    try:
        sessions, report = load_sessions(path, adapter=adapter)
    except UnsupportedFormatVersion as exc:
        _fail(str(exc), ExitCode.INPUT_ERROR, as_json)
    except FileNotFoundError as exc:
        _fail(str(exc), ExitCode.NOT_FOUND, as_json)
    gov = govern(sessions, report, cfg)
    for s in gov.sessions:
        write_session(s, out / f"{s.session_id}.jsonl")
    data = {"sessions": len(gov.sessions), "report": report.model_dump(), "redactions": gov.redactions,
            "out": str(out)}
    human = (f"wrote {len(gov.sessions)} session(s) to {out}; events read {report.events_read}; skipped "
             f"{report.events_skipped or 'none'}; secrets redacted {sum(gov.redactions.values())}")
    _out(data, as_json, human)


@app.command("eval")
def eval_cmd(tasks: Path = typer.Option(..., "--tasks", help="Directory of task folders (each with task.toml)."),
             sessions: Path = typer.Option(..., "--sessions", "-s", help="Sessions the memory was built from."),
             memory: Path = MemoryOpt,
             runner: str = typer.Option("mock", help="mock | claude-code"),
             split: str = typer.Option("dev", help="dev | test"),
             repeats: int = typer.Option(3, min=1),
             arms: str = typer.Option("no_memory,raw_search,naive_summary,agent_native,reverie",
                                      help="Comma-separated arms."),
             model: str = typer.Option("claude-haiku-5-5", help="Task-agent model for claude-code."),
             max_budget_usd: float = typer.Option(1.0, help="Per-run budget cap for claude-code."),
             permission_mode: str = typer.Option("acceptEdits", help="claude-code task-agent permission mode "
                                                 "(bypassPermissions only in a disposable sandbox)."),
             purpose: Path | None = typer.Option(None, "--purpose"),
             out: Path | None = typer.Option(None, "--out", help="Write the JSON report here."),
             config: Path | None = ConfigOpt, as_json: bool = JsonOpt) -> None:
    """Run tasks under all memory arms and report per-task results, noise floor and bootstrap CIs."""
    import tempfile

    from reverie.adapters import load_sessions
    from reverie.core.config import data_dir
    from reverie.dream.ingest import govern
    from reverie.dream.prompts import load_purpose, prompt_versions
    from reverie.eval.runner import (
        ARMS,
        ArmContext,
        ClaudeCodeRunner,
        EvalRefused,
        MockTaskRunner,
        load_tasks,
        naive_summary,
        run_eval,
    )

    cfg = _cfg(config)
    store = _store(memory)
    chosen = [a.strip() for a in arms.split(",") if a.strip()]
    bad = [a for a in chosen if a not in ARMS]
    if bad:
        _fail(f"unknown arm(s) {bad}; choose from {list(ARMS)}", ExitCode.USAGE, as_json)
    try:
        task_list = load_tasks(tasks)
        task_runner = MockTaskRunner() if runner == "mock" else ClaudeCodeRunner(
            model=model, max_budget_usd=max_budget_usd, permission_mode=permission_mode)
    except EvalRefused as exc:
        _fail(str(exc), ExitCode.REFUSED, as_json)
    except Exception as exc:
        _fail(f"could not load tasks: {exc}", ExitCode.INPUT_ERROR, as_json)
    work = Path(tempfile.mkdtemp(prefix="reverie-eval-"))
    raw, report_ = load_sessions(sessions)
    gov = govern(raw, report_, cfg)
    red_dir = work / "sessions"
    from reverie.adapters.generic_jsonl import write_session

    for sess in gov.sessions:
        write_session(sess, red_dir / f"{sess.session_id}.jsonl")
    summarize = None
    notes: list[str] = []
    if "naive_summary" in chosen and runner != "mock":
        try:
            from reverie.core.budget import Budget
            from reverie.dream.prompts import load_prompt
            from reverie.providers import LLM, build_provider

            llm = LLM(build_provider(cfg), Budget(max_usd=cfg.budget.max_usd))

            def summarize(corpus: str) -> str:
                system = load_prompt("naive_summary").render()
                return llm.complete(stage="naive_summary", model=cfg.llm.extract_model, system=system,
                                    messages=[{"role": "user", "content": corpus}]).text
        except Exception as exc:
            notes.append(f"naive summary used the deterministic fallback: {exc}")
    from reverie.core.store import MemoryStore, git

    naive = MemoryStore(work / "naive")
    naive.init()
    naive_summary(gov.sessions, naive.root, summarize)
    naive.commit_all("naive summary")
    reverie_copy = work / "reverie-memory"
    git(store.root, "clone", "-q", str(store.root), str(reverie_copy))
    forbid_all = [str(store.root), str(red_dir), str(sessions.resolve()), str(reverie_copy), str(naive.root)]
    allowed = {"no_memory": [], "raw_search": [str(red_dir)], "naive_summary": [str(naive.root)],
               "agent_native": [], "reverie": [str(reverie_copy)]}
    contexts = {}
    for arm in chosen:
        contexts[arm] = ArmContext(
            arm=arm, memory_dir=str(naive.root) if arm == "naive_summary" else
            (str(reverie_copy) if arm == "reverie" else None),
            sessions_dir=str(red_dir) if arm == "raw_search" else None,
            forbidden_paths=[p for p in forbid_all if p not in allowed[arm]])
    try:
        rep = run_eval(task_list, task_runner, split=split, repeats=repeats, contexts=contexts,  # type: ignore[arg-type]
                       ledger=data_dir() / "eval-ledger.json", prompt_versions=prompt_versions(),
                       purpose=load_purpose(purpose), tasks_dir=tasks)
    except EvalRefused as exc:
        _fail(str(exc), ExitCode.REFUSED, as_json)
    rep.notes.extend(notes)
    data = rep.model_dump()
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, indent=2, default=str))
    lines = [f"runner={rep.runner} split={rep.split} repeats={rep.repeats} tasks={len(rep.tasks)} "
             f"invalid runs={rep.invalid_runs} cost=${rep.total_cost_usd:.4f}",
             "arm means: " + ", ".join(f"{a}={v}" for a, v in rep.arm_means.items()),
             f"noise floor (mean per-task SD, no_memory): {rep.noise_floor}"]
    for arm, ci in rep.vs_no_memory.items():
        lines.append(f"{arm} vs no_memory: diff {ci['mean_diff']} 95% CI [{ci['ci_low']}, {ci['ci_high']}]")
    for arm, ci in rep.vs_raw_search.items():
        lines.append(f"{arm} vs raw_search: diff {ci['mean_diff']} 95% CI [{ci['ci_low']}, {ci['ci_high']}]")
    lines += [f"note: {n}" for n in rep.notes]
    _out(data, as_json, "\n".join(lines))


fm_app = typer.Typer(no_args_is_help=True, help="FrontierMemory benchmark mode.")
app.add_typer(fm_app, name="frontiermemory")


@fm_app.command("dream")
def fm_dream(evidence: Path = typer.Option(Path("/evidence/inputs"), "--evidence", help="Trajectory corpus."),
             out: Path = typer.Option(Path("/memory"), "--out", help="Memory output folder."),
             purpose: Path | None = typer.Option(None, "--purpose", help="Defaults to the benchmark purpose."),
             config: Path | None = ConfigOpt, as_json: bool = JsonOpt) -> None:
    """Convert the corpus, dream in benchmark mode, export a format-valid memory folder."""
    from reverie.adapters.frontiermemory.run import benchmark_dream

    cfg = _cfg(config)
    if not evidence.exists():
        _fail(f"evidence folder not found: {evidence}", ExitCode.NOT_FOUND, as_json)
    res = benchmark_dream(evidence, out, cfg, purpose=purpose)
    code = ExitCode.OK if res["check"]["ok"] else ExitCode.ERROR
    if not res["dream"]["llm_available"] and code == ExitCode.OK:
        code = ExitCode.LLM_UNAVAILABLE
    human = (f"converted {res['convert']['sessions']} trajectories (labels {res['convert']['labels']}); "
             f"dream: {res['dream']['status']} {res['dream']['counts']}; exported {len(res['exported'])} file(s) to "
             f"{out}; format check: {'ok' if res['check']['ok'] else res['check']['errors']}")
    _out(res | {"exit_code": int(code)}, as_json, human)
    raise typer.Exit(int(code))


@fm_app.command("check")
def fm_check(memory_dir: Path = typer.Argument(...), as_json: bool = JsonOpt) -> None:
    """Check a folder against FrontierMemory's memory format rules."""
    from reverie.adapters.frontiermemory.run import check_memory

    rep = check_memory(memory_dir)
    _out(rep, as_json, "ok" if rep["ok"] else "\n".join(rep["errors"]))
    raise typer.Exit(0 if rep["ok"] else 1)


def _outcomes(cfg: ReverieConfig) -> dict[str, str]:
    from reverie.core.experience import ExperienceStore

    path = experience_db_path(cfg)
    if not path.exists():
        return {}
    with ExperienceStore(path) as store:
        rows = store._conn.execute("SELECT session_id, outcome_label FROM sessions").fetchall()
    return {r[0]: r[1] for r in rows if r[1]}


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:  # pragma: no cover
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
