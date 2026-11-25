"""Minimal synchronous MCP client for kubectl-ai's streamable-HTTP transport (sidecar or a local process).

Step 0 (docs/kubectl-ai.md) showed kubectl-ai answers streamable-HTTP requests with plain JSON and an
Mcp-Session-Id header; SSE responses are also handled for forward compatibility.
"""

from __future__ import annotations

import json
import time

import httpx

PROTOCOL_VERSION = "2025-03-26"
CLIENT_INFO = {"name": "rightsizing-agent", "version": "0.1.0"}


class McpError(RuntimeError):
    pass


def _result_text(result: dict) -> str:
    parts = [c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text"]
    text = "\n".join(parts)
    return f"[tool error] {text}" if result.get("isError") else text


class HttpMcpClient:
    def __init__(self, url: str, timeout: float = 60, transport: httpx.BaseTransport | None = None):
        self.url = url
        self.http = httpx.Client(timeout=timeout, transport=transport)
        self.session_id: str | None = None
        self._next_id = 0

    def _post(self, payload: dict, expect_response: bool = True) -> dict | None:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        try:
            resp = self.http.post(self.url, content=json.dumps(payload), headers=headers)
        except httpx.HTTPError as exc:
            raise McpError(f"MCP request failed: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            raise McpError(f"MCP HTTP {resp.status_code}")
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
        if not expect_response:
            return None
        message = self._parse(resp, payload.get("id"))
        if "error" in message:
            raise McpError(f"MCP error: {message['error'].get('message', message['error'])}")
        return message.get("result") or {}

    @staticmethod
    def _parse(resp: httpx.Response, request_id) -> dict:
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    msg = json.loads(line[5:].strip())
                    if msg.get("id") == request_id:
                        return msg
            raise McpError("no matching response in SSE stream")
        return resp.json()

    def _request(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            payload["params"] = params
        return self._post(payload) or {}

    def connect(self, deadline_seconds: float = 60, sleep=time.sleep) -> dict:
        start, delay, last = time.monotonic(), 1.0, None
        while True:
            try:
                info = self._request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                                    "clientInfo": CLIENT_INFO})
                self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_response=False)
                return info
            except McpError as exc:
                last = exc
            if time.monotonic() - start + delay > deadline_seconds:
                raise McpError(f"MCP server unreachable after {deadline_seconds:.0f}s: {last}")
            sleep(delay)
            delay = min(delay * 2, 8)

    def list_tools(self) -> list[dict]:
        return self._request("tools/list", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict) -> str:
        return _result_text(self._request("tools/call", {"name": name, "arguments": arguments}))

    def close(self) -> None:
        self.http.close()
