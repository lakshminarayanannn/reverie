"""Provider adapters tested without network: fake SDK client, local HTTP server, fake CLI."""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2 as httpx
import pytest

from reverie.core.budget import Budget
from reverie.providers.anthropic_provider import AnthropicProvider
from reverie.providers.base import LLM, ProviderError, RetryableError
from reverie.providers.claude_cli import ClaudeCliProvider, flatten_messages
from reverie.providers.openai_compat import OpenAICompatProvider

REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _resp(text: str = '{"ok": true}', stop: str = "end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="thinking", text=""), SimpleNamespace(type="text", text=text)],
                           stop_reason=stop, usage=SimpleNamespace(input_tokens=100, output_tokens=20),
                           model="claude-sonnet-5-5")


class FakeMessages:
    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def create(self, **kw):
        self.calls.append(kw)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def fake_anthropic(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-not-real-00000000")
    holder: dict = {}

    class FakeClient:
        def __init__(self, **kw):
            holder["init"] = kw
            self.messages = holder["messages"]

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    return holder


def test_anthropic_success_and_params(fake_anthropic) -> None:
    fake_anthropic["messages"] = FakeMessages([_resp("hello")])
    p = AnthropicProvider(effort="low")
    comp = p.complete(model="claude-sonnet-5-5", system="sys", messages=[{"role": "user", "content": "q"}],
                      max_tokens=50, temperature=0.7)
    assert comp.text == "hello" and comp.input_tokens == 100 and comp.output_tokens == 20
    call = fake_anthropic["messages"].calls[0]
    assert "temperature" not in call and call["output_config"] == {"effort": "low"} and call["system"] == "sys"
    assert fake_anthropic["init"]["max_retries"] == 0


def test_anthropic_retries_rate_limit_then_succeeds(fake_anthropic) -> None:
    rate = anthropic.RateLimitError("slow down", response=httpx.Response(429, request=REQ,
                                    headers={"retry-after": "0"}), body=None)
    server = anthropic.InternalServerError("oops", response=httpx.Response(529, request=REQ), body=None)
    conn = anthropic.APIConnectionError(request=REQ)
    fake_anthropic["messages"] = FakeMessages([rate, server, conn, _resp('{"x": 1}')])
    sleeps: list[float] = []
    llm = LLM(AnthropicProvider(), Budget(), sleep=sleeps.append)
    comp = llm.complete(stage="t", model="claude-sonnet-5-5", system="s", messages=[{"role": "user", "content": "q"}])
    assert comp.text == '{"x": 1}' and len(sleeps) == 3
    assert llm.budget.usd == pytest.approx((100 * 2 + 20 * 10) / 1e6)


def test_anthropic_permanent_errors(fake_anthropic) -> None:
    bad = anthropic.BadRequestError("nope", response=httpx.Response(400, request=REQ), body=None)
    fake_anthropic["messages"] = FakeMessages([bad, _resp("", stop="refusal")])
    p = AnthropicProvider()
    with pytest.raises(ProviderError, match="400"):
        p.complete(model="m", system="s", messages=[], max_tokens=5)
    with pytest.raises(ProviderError, match="refusal"):
        p.complete(model="m", system="s", messages=[], max_tokens=5)


class _Handler(BaseHTTPRequestHandler):
    replies: list[tuple[int, dict]] = []
    seen: list[dict] = []

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Handler.seen.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization")})
        code, payload = _Handler.replies.pop(0)
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a) -> None:
        pass


def test_openai_compat_against_local_server(monkeypatch: pytest.MonkeyPatch) -> None:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ok = {"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 7, "completion_tokens": 2},
              "model": "llama"}
        _Handler.replies = [(503, {}), (200, ok), (400, {})]
        _Handler.seen = []
        monkeypatch.setenv("LOCAL_KEY", "abc")
        p = OpenAICompatProvider(base_url=f"http://127.0.0.1:{server.server_port}/v1", api_key_env="LOCAL_KEY")
        assert p.is_cloud is False
        with pytest.raises(RetryableError):
            p.complete(model="llama", system="s", messages=[{"role": "user", "content": "q"}], max_tokens=5)
        comp = p.complete(model="llama", system="s", messages=[{"role": "user", "content": "q"}], max_tokens=5)
        assert comp.text == "hi" and comp.input_tokens == 7 and comp.model == "llama"
        assert _Handler.seen[1]["path"] == "/v1/chat/completions" and _Handler.seen[1]["auth"] == "Bearer abc"
        assert _Handler.seen[1]["body"]["messages"][0] == {"role": "system", "content": "s"}
        with pytest.raises(ProviderError):
            p.complete(model="llama", system="s", messages=[], max_tokens=5)
    finally:
        server.shutdown()
    assert OpenAICompatProvider(base_url="https://api.example.com/v1").is_cloud is True


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shebang script as the fake CLI")
def test_claude_cli_provider_with_fake_binary(tmp_path: Path) -> None:
    log = tmp_path / "args.json"
    fake = tmp_path / "claude"
    fake.write_text(f"""#!{sys.executable}
import json, sys, os
prompt = sys.stdin.read()
json.dump({{"argv": sys.argv[1:], "prompt": prompt, "cwd": os.getcwd()}}, open({str(log)!r}, "w"))
print(json.dumps({{"type": "result", "is_error": False, "result": "pong", "stop_reason": "end_turn",
                   "total_cost_usd": 0.0123, "usage": {{"input_tokens": 3, "cache_read_input_tokens": 10,
                   "cache_creation_input_tokens": 5, "output_tokens": 4}}}}))
""")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    p = ClaudeCliProvider(binary=str(fake))
    comp = p.complete(model="claude-haiku-5-5", system="SYS", messages=[{"role": "user", "content": "ping"}],
                      max_tokens=5)
    assert comp.text == "pong" and comp.usd == pytest.approx(0.0123) and comp.input_tokens == 18
    seen = json.loads(log.read_text())
    argv = seen["argv"]
    assert argv[argv.index("--tools") + 1] == "" and "--strict-mcp-config" in argv
    assert argv[argv.index("--system-prompt") + 1] == "SYS" and seen["prompt"] == "ping"
    assert not os.path.exists(seen["cwd"])  # ran in a temp dir that is gone now
    b = Budget()
    LLM(p, b).complete(stage="x", model="claude-haiku-5-5", system="s", messages=[{"role": "user", "content": "q"}])
    assert b.usd == pytest.approx(0.0123)


def test_flatten_messages() -> None:
    assert flatten_messages([{"role": "user", "content": "a"}]) == "a"
    out = flatten_messages([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
                            {"role": "user", "content": "c"}])
    assert "=== PREVIOUS ASSISTANT REPLY ===\nb" in out and out.endswith("=== USER ===\nc")
