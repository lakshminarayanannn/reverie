#!/usr/bin/env bash
# FrontierMemory smoke check for reverie.
#
# 1. Capability report: Python 3.12, Docker (client and daemon), Modal, Harbor, frontier-memory.
# 2. reverie's own end-to-end check on the FrontierMemory smoke corpus: convert, dream
#    (deterministic only unless a model is configured), export, and the memory format check.
# 3. If Harbor + Docker (or Modal) are available: build the memory-generation task from the
#    smoke corpus and run the benchmark's oracle agent on it.
#
# It never runs full benchmark runs and never downloads the gated corpus.
#
# Env: FM_REPO (existing frontiermemory checkout; default: shallow clone into a temp dir)
#      FM_ENV (docker | modal; default docker)   PYTHON (default python3.12)
set -uo pipefail

PYTHON="${PYTHON:-python3.12}"
FM_ENV="${FM_ENV:-docker}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d -t reverie-fm-smoke-XXXXXX)"
declare -A CAP

have() { command -v "$1" >/dev/null 2>&1; }

echo "== capability report"
if have "$PYTHON" && "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)'; then
  CAP[python]="yes ($("$PYTHON" --version 2>&1))"
else
  CAP[python]="no (need Python 3.12 as \$PYTHON)"
fi
if have docker; then
  if docker info >/dev/null 2>&1; then CAP[docker]="yes (daemon reachable)"; else CAP[docker]="client only (daemon not reachable)"; fi
else
  CAP[docker]="no"
fi
if have modal; then
  if modal token info >/dev/null 2>&1 || [ -n "${MODAL_TOKEN_ID:-}" ]; then CAP[modal]="yes"; else CAP[modal]="cli only (no token)"; fi
else
  CAP[modal]="no"
fi
VENV="$WORK/venv"
"$PYTHON" -m venv "$VENV" >/dev/null 2>&1 || true
HARBOR="$VENV/bin/harbor"
if [ -z "${FM_REPO:-}" ]; then
  FM_REPO="$WORK/frontiermemory"
  git clone -q --depth 1 https://github.com/letta-ai/frontiermemory "$FM_REPO" 2>/dev/null || FM_REPO=""
fi
if [ -n "$FM_REPO" ] && [ -x "$VENV/bin/pip" ]; then
  "$VENV/bin/pip" install -q -e "$FM_REPO" >"$WORK/pip.log" 2>&1 || true
fi
if [ -x "$HARBOR" ]; then CAP[harbor]="yes ($("$VENV/bin/pip" show harbor 2>/dev/null | awk '/^Version/{print $2}'))"; else CAP[harbor]="no (see $WORK/pip.log)"; fi
if [ -x "$VENV/bin/frontier-memory" ]; then CAP[frontier_memory]="yes ($FM_REPO)"; else CAP[frontier_memory]="no"; fi
for k in python docker modal harbor frontier_memory; do printf '  %-16s %s\n' "$k" "${CAP[$k]}"; done

echo "== reverie on the FrontierMemory smoke corpus (format check)"
RESULT_LOCAL="skipped (no frontiermemory checkout)"
if [ -n "$FM_REPO" ] && [ -d "$FM_REPO/smoke/corpus/inputs" ]; then
  REVERIE="${REVERIE:-reverie}"
  have "$REVERIE" || REVERIE="$HERE/.venv/bin/reverie"
  REVERIE_HOME="$WORK/rhome" "$REVERIE" frontiermemory dream --evidence "$FM_REPO/smoke/corpus/inputs" \
      --out "$WORK/memory" ${REVERIE_CONFIG:+--config "$REVERIE_CONFIG"} > "$WORK/reverie.txt" 2>&1
  rc=$?
  tail -1 "$WORK/reverie.txt"
  if "$REVERIE" frontiermemory check "$WORK/memory" >/dev/null 2>&1; then
    RESULT_LOCAL="memory format OK (dream exit $rc; 3 = no model configured, deterministic stages only)"
  else
    RESULT_LOCAL="memory format FAILED (dream exit $rc)"
  fi
  if [ -x "$VENV/bin/python" ] && [ -f "$FM_REPO/src/frontier_memory/memory.py" ]; then
    if "$VENV/bin/python" "$FM_REPO/src/frontier_memory/memory.py" "$WORK/memory" >/dev/null 2>&1; then
      RESULT_LOCAL="$RESULT_LOCAL; benchmark's own checker: OK"
    else
      RESULT_LOCAL="$RESULT_LOCAL; benchmark's own checker: FAILED"
    fi
  fi
fi
echo "  $RESULT_LOCAL"

echo "== oracle check on the memory-generation task"
ORACLE="skipped"
env_ok=no
[ "$FM_ENV" = docker ] && [[ "${CAP[docker]}" == yes* ]] && env_ok=yes
[ "$FM_ENV" = modal ] && [[ "${CAP[modal]}" == yes ]] && env_ok=yes
if [ -x "$VENV/bin/frontier-memory" ] && [ -x "$HARBOR" ] && [ "$env_ok" = yes ]; then
  (cd "$WORK" && "$VENV/bin/frontier-memory" build-memory-task --corpus "$FM_REPO/smoke/corpus" \
      --out "$WORK/build/memory-task" > "$WORK/build.log" 2>&1)
  if [ -d "$WORK/build/memory-task" ]; then
    (cd "$WORK" && timeout 3600 "$HARBOR" run -p "$WORK/build/memory-task" -a oracle -e "$FM_ENV" \
        > "$WORK/oracle.log" 2>&1)
    rc=$?
    reward="$(cat "$WORK"/jobs/*/*/verifier/reward.txt 2>/dev/null | head -1)"
    ORACLE="harbor exit $rc; verifier reward ${reward:-none} (log: $WORK/oracle.log). Expect 0 on the bundled smoke corpus: it has no 'Verifier result' lines and upstream solve.sh exits under pipefail; see the synthetic-corpus check below"
  else
    ORACLE="could not build the task (log: $WORK/build.log)"
  fi
else
  ORACLE="skipped: needs frontier-memory + harbor + $FM_ENV"
fi
echo "  $ORACLE"

echo "== synthetic corpus with 'Verifier result' lines: oracle + reverie as the memory agent"
# The bundled smoke corpus has no verdict lines, and the upstream oracle (solve.sh) exits
# under `set -euo pipefail` when grep finds none, so it cannot pass on that corpus.
SYN="skipped: needs frontier-memory + harbor + $FM_ENV"
AGENT="skipped"
if [ -x "$VENV/bin/frontier-memory" ] && [ -x "$HARBOR" ] && [ "$env_ok" = yes ]; then
  "$VENV/bin/pip" install -q -e "$HERE" >>"$WORK/pip.log" 2>&1
  mkdir -p "$WORK/syn/inputs"
  "$VENV/bin/python" - "$HERE/tests/fixtures/claude_code/transcript.jsonl" "$WORK/syn" <<'PY'
import json, sys
from pathlib import Path
src = Path(sys.argv[1]).read_text().splitlines()
out = Path(sys.argv[2])
for i, verdict in enumerate(["FAIL", "PASS", "FAIL"]):
    lines = [l.replace("5a6b7c8d9e0f", f"5a6b7c8d9e{i:02d}") for l in src]
    lines.append(json.dumps({"type": "user", "sessionId": f"syn{i}", "version": "2.1.319",
                             "message": {"role": "user", "content": f"Verifier result: {verdict}"}}))
    (out / "inputs" / f"t{i + 1:04d}.jsonl").write_text("\n".join(lines) + "\n")
from frontier_memory.corpus import write_sums
print(write_sums(out))
PY
  (cd "$WORK" && "$VENV/bin/frontier-memory" build-memory-task --corpus "$WORK/syn" --out "$WORK/build-syn/memory-task" \
      > "$WORK/build-syn.log" 2>&1 && timeout 3600 "$HARBOR" run -p "$WORK/build-syn/memory-task" -a oracle \
      -e "$FM_ENV" --jobs-dir "$WORK/jobs-syn" > "$WORK/oracle-syn.log" 2>&1)
  r="$(cat "$WORK"/jobs-syn/*/*/verifier/reward.txt 2>/dev/null | head -1)"
  SYN="oracle reward ${r:-none} (1 = memory task plumbing works)"
  (cd "$WORK" && timeout 3600 "$VENV/bin/frontier-memory" generate-memory --corpus "$WORK/syn" \
      --harness reverie.adapters.frontiermemory.agent:ReverieMemoryAgent -m anthropic/claude-opus-5-5 \
      -e "$FM_ENV" --build-dir "$WORK/build-agent" --jobs-dir "$WORK/jobs-agent" --job-name reverie-smoke \
      > "$WORK/agent.log" 2>&1)
  r="$(cat "$WORK"/jobs-agent/reverie-smoke/*/verifier/reward.txt 2>/dev/null | head -1)"
  AGENT="reverie memory agent: verifier reward ${r:-none} (format check; without ANTHROPIC_API_KEY the dream is deterministic only)"
fi
echo "  $SYN"
echo "  $AGENT"
echo "work dir: $WORK"
