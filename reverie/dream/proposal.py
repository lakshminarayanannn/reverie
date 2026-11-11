"""PROPOSAL.md rendering and the guard-proposal TOML format."""

from __future__ import annotations

from collections import Counter
from typing import Any

from reverie.dream.models import Change, GuardProposal, InstructionEdit, PlacementRecord


def toml_escape(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def guards_to_toml(guards: list[dict[str, Any]]) -> str:
    """Serialize guards as ``[[guard]]`` tables (flat string/bool/list values only)."""
    out = ["# reverie guards: PreToolUse checks derived from verified trap notes.",
           "# Edit `mode` (warn|block) or `enabled`, or use `reverie guards disable <id>`.", ""]
    for g in guards:
        out.append("[[guard]]")
        for key in ("id", "note_id", "pattern", "replacement", "mode", "message", "enabled", "evidence_sessions"):
            if key not in g:
                continue
            value = g[key]
            if isinstance(value, bool):
                out.append(f"{key} = {'true' if value else 'false'}")
            elif isinstance(value, list):
                out.append(f"{key} = [" + ", ".join(toml_escape(str(v)) for v in value) + "]")
            else:
                out.append(f"{key} = {toml_escape(str(value))}")
        out.append("")
    return "\n".join(out)


def render_proposal(*, run_id: str, provider: str, models: dict[str, str], changes: list[Change],
                    placements: list[PlacementRecord], guards: list[GuardProposal],
                    rejected_guards: list[dict[str, str]], edits: list[InstructionEdit],
                    contested: list[tuple[str, str]], budget: dict[str, Any], warnings: list[str],
                    errors: list[str], sessions: list[str], adapter_summary: dict[str, Any]) -> str:
    counts = Counter(c.action for c in changes)
    lines = [f"# Dream proposal {run_id}", ""]
    if provider == "mock":
        lines += ["> **Mock provider.** This proposal was produced with scripted model responses for testing.",
                  "> It is not the result of real learning.", ""]
    if provider == "none":
        lines += ["> **No model configured.** Only deterministic maintenance ran (dedupe, caps, links, pruning",
                  "> by rule). Extraction, verification and contradiction handling were skipped.", ""]
    lines += ["## Summary", "",
              "| added | merged | changed | contested | removed | promoted | demoted |",
              "|---|---|---|---|---|---|---|",
              f"| {counts['added']} | {counts['merged']} | {counts['changed']} | {counts['contested']} | "
              f"{counts['removed'] + counts['superseded'] + counts['stale']} | {counts['promoted']} | "
              f"{counts['demoted']} |", "",
              f"- sessions dreamed: {len(sessions)}",
              f"- provider: `{provider}`; models: " + ", ".join(f"{k}=`{v}`" for k, v in sorted(models.items())),
              f"- budget used: {budget.get('input_tokens', 0)} input + {budget.get('output_tokens', 0)} output "
              f"tokens, ${budget.get('usd', 0):.4f} ({budget.get('calls', 0)} calls, "
              f"{budget.get('cached_calls', 0)} from cache)",
              f"- adapter: {adapter_summary.get('sessions', 0)} sessions, {adapter_summary.get('events_read', 0)} "
              f"events read, skipped: {adapter_summary.get('events_skipped') or 'none'}",
              ""]
    lines += ["## Changes", ""]
    if not changes:
        lines.append("No memory changes.")
    for c in changes:
        ev = ", ".join(c.evidence[:6]) + (" …" if len(c.evidence) > 6 else "")
        nid = f" `{c.note_id}`" if c.note_id else ""
        lines.append(f"- **{c.action}**{nid}: {c.text}" + (f" — evidence: {ev}" if ev else "")
                     + (f" — {c.detail}" if c.detail else ""))
    lines += ["", "## Open contradictions", ""]
    lines += [f"- `{a}` vs `{b}`" for a, b in contested] or ["None."]
    lines += ["", "## Guard proposals", "",
              "Accepting this proposal adds these to `guards.toml` (mode `warn`; switch to `block` by hand).", ""]
    for g in guards:
        lines.append(f"- `{g.id}` from note `{g.note_id}`: pattern `{g.pattern}` -> replacement `{g.replacement}` "
                     f"(failed in sessions: {', '.join(g.evidence_sessions)})")
    if not guards:
        lines.append("None.")
    if rejected_guards:
        lines += ["", "Rejected guard candidates:", ""]
        lines += [f"- note `{r['note_id']}` pattern `{r['pattern']}`: {r['reason']}" for r in rejected_guards]
    lines += ["", "## Proposed edits to the agent's instruction file (not applied)", ""]
    lines += [f"- {e.target}: {e.text} — evidence: {', '.join(e.evidence)}" for e in edits] or ["None."]
    lines += ["", "## Placement of every candidate", "",
              "| placement | candidate | note / reason |", "|---|---|---|"]
    for p in placements:
        why = f"`{p.note_id}`" if p.note_id else ""
        if p.reason:
            why = (why + " " + p.reason).strip()
        lines.append(f"| {p.placement} | {p.text.replace('|', '/')} | {why.replace('|', '/')} |")
    if not placements:
        lines.append("| — | no candidates | |")
    if warnings or errors:
        lines += ["", "## Warnings and errors", ""]
        lines += [f"- warning: {w}" for w in warnings] + [f"- error: {e}" for e in errors]
    return "\n".join(lines) + "\n"
