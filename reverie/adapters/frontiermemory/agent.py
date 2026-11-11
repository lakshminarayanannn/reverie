"""Harbor memory-generation agent that runs reverie inside the FrontierMemory dream task.

    frontier-memory generate-memory --corpus <corpus> \\
        --harness reverie.adapters.frontiermemory.agent:ReverieMemoryAgent \\
        -m anthropic/claude-opus-5-5 -e docker

Rules honored (github.com/letta-ai/frontiermemory docs/rules.md):

* reads only ``/evidence/inputs/`` and writes only ``/memory/``;
* the only network use while the agent runs is the model API host (reverie's dream makes
  no tool calls and has no web tools; the Anthropic provider talks to the API only);
* setup installs reverie (setup may use the network).

Requires the optional extra: ``uv pip install -e .[frontiermemory]``.
Interface verified against harbor 0.24.0 (``BaseAgent`` abstract methods ``name``,
``version``, ``setup``, ``run``; ``BaseEnvironment.exec(command, cwd, env, timeout_sec)``,
``upload_dir``).
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

import reverie

REMOTE_SRC = "/opt/reverie-src"
VENV = "/opt/reverie-venv"
REPO_ROOT = Path(reverie.__file__).resolve().parent.parent


class ReverieMemoryAgent(BaseAgent):
    def __init__(self, logs_dir: Path, *args: Any, extract_model: str | None = None,
                 budget_usd: float = 20.0, **kwargs: Any) -> None:
        super().__init__(logs_dir, *args, **kwargs)
        self._extract_model = extract_model
        self._budget = budget_usd

    @staticmethod
    def name() -> str:
        return "reverie-memory"

    def version(self) -> str:
        return reverie.__version__

    async def setup(self, environment: BaseEnvironment) -> None:
        """Upload reverie and install it in a venv inside the task container.

        On a Linux host, wheels for reverie and its dependencies are built on the host and
        installed offline (``--no-index``), so setup downloads nothing in the container.
        Otherwise (or if building wheels fails) pip installs from PyPI during setup, which
        the benchmark rules allow.
        """
        import shutil
        import subprocess
        import sys
        import tempfile

        with tempfile.TemporaryDirectory(prefix="reverie-src-") as tmp:
            stage = Path(tmp) / "src"
            stage.mkdir()
            for name in ("pyproject.toml", "README.md", "LICENSE"):
                shutil.copy(REPO_ROOT / name, stage / name)
            shutil.copytree(REPO_ROOT / "reverie", stage / "reverie",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            offline = False
            if sys.platform.startswith("linux") and sys.version_info[:2] == (3, 12):
                build = subprocess.run([sys.executable, "-m", "pip", "wheel", "-q", "-w", str(stage / "wheels"),
                                        str(stage)], capture_output=True, text=True, check=False)
                offline = build.returncode == 0
            await environment.upload_dir(stage, REMOTE_SRC)
        if offline:
            install = (f"{VENV}/bin/pip install -q --no-index --find-links {REMOTE_SRC}/wheels reverie")
        else:
            install = f"{VENV}/bin/pip install -q {shlex.quote(REMOTE_SRC)}"
        res = await environment.exec(f"python3 -m venv {VENV} && {install}", timeout_sec=1200)
        if res.return_code != 0:
            raise RuntimeError(f"installing reverie failed: {(res.stderr or res.stdout or '')[-2000:]}")

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        model = (self.model_name or "anthropic/claude-opus-5-5").split("/", 1)[-1]
        extract = self._extract_model or model
        config = (f'[llm]\nprovider = "anthropic"\nallow_cloud = true\nextract_model = "{extract}"\n'
                  f'verify_model = "{model}"\nreconcile_model = "{model}"\n'
                  f"[budget]\nmax_usd = {self._budget}\n")
        env = {k: v for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL") if (v := self._get_env(k))}
        env["REVERIE_HOME"] = "/tmp/reverie-home"
        script = (f"mkdir -p /tmp/reverie-home && printf %s {shlex.quote(config)} > /tmp/reverie.toml && "
                  f"{VENV}/bin/reverie frontiermemory dream --evidence /evidence/inputs --out /memory "
                  f"--config /tmp/reverie.toml --json > /tmp/reverie-report.json")
        res = await environment.exec(script, env=env, timeout_sec=6 * 3600)
        (Path(self.logs_dir) / "reverie-stdout.txt").write_text((res.stdout or "") + (res.stderr or ""))
        report = await environment.exec("cat /tmp/reverie-report.json", timeout_sec=60)
        (Path(self.logs_dir) / "reverie-report.json").write_text(report.stdout or "")
        if res.return_code not in (0, 3):
            raise RuntimeError(f"reverie dream failed with exit code {res.return_code}")
