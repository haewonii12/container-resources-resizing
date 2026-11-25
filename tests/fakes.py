"""Test doubles: fake kubectl-ai MCP (replays Step 0 fixtures) and a scripted OpenAI-compatible server."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import openai

FIXTURES = Path(__file__).parent / "fixtures" / "kubectl_ai"


class FakeKubectlAI:
    """httpx transport speaking kubectl-ai's streamable-HTTP MCP dialect (as recorded in Step 0)."""

    def __init__(self, outputs: dict[str, str] | None = None, fail_first: int = 0):
        self.tools = json.loads((FIXTURES / "tools_list.json").read_text())
        self.init = json.loads((FIXTURES / "initialize.json").read_text())
        self.outputs = outputs or {}
        self.commands: list[str] = []
        self.fail_first = fail_first
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.calls <= self.fail_first:
            raise httpx.ConnectError("sidecar not ready")
        msg = json.loads(request.content)
        if "id" not in msg:
            return httpx.Response(202)
        method = msg["method"]
        if method == "initialize":
            result = self.init
        elif method == "tools/list":
            result = self.tools
        elif method == "tools/call":
            cmd = msg["params"]["arguments"]["command"]
            self.commands.append(cmd)
            out = next((v for k, v in self.outputs.items() if k in cmd), "No resources found")
            result = {"content": [{"type": "text", "text": f'Command: "{cmd}"\nStdout: "{out}"\nExitCode: 0'}]}
        else:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"],
                                             "error": {"code": -32601, "message": "no"}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": result},
                              headers={"Mcp-Session-Id": "mcp-session-test"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def tool_call(name: str, args: dict | str, call_id: str = "c1") -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args)}}


class FakeLLM:
    """Scripted OpenAI-compatible /chat/completions endpoint; each script item is a list of tool calls or text."""

    def __init__(self, script: list, down: bool = False):
        self.script = list(script)
        self.down = down
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("llm down")
        body = json.loads(request.content)
        self.requests.append(body)
        step = self.script.pop(0) if self.script else "thinking..."
        message = {"role": "assistant", "content": step if isinstance(step, str) else None}
        if not isinstance(step, str):
            message["tool_calls"] = step
        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if not isinstance(step, str)
                         else "stop"}]})

    def client(self) -> openai.OpenAI:
        return openai.OpenAI(base_url="http://llm.test/v1", api_key="k", max_retries=0,
                             http_client=httpx.Client(transport=httpx.MockTransport(self)))
