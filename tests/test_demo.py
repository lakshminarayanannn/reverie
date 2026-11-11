"""The examples/demo walkthrough must keep working (offline, scripted mock)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

DEMO = Path(__file__).parent.parent / "examples" / "demo" / "run_demo.py"


def test_demo_runs(tmp_path: Path) -> None:
    proc = subprocess.run([sys.executable, str(DEMO), "--workdir", str(tmp_path / "demo")], capture_output=True,
                          text=True, timeout=600)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    out = proc.stdout
    assert "trap note present: yes" in out and "reverie guard (automated; may be outdated)" in out
    assert "rejected n-" in out and "memory restored to" in out
