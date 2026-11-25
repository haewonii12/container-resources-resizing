# kubectl-ai spike (plan Step 0)

Recorded 2026-11-25 on macOS. Binary: Homebrew `kubectl-ai` **0.0.31**, built 2026-03-25. Fixtures are in `tests/fixtures/kubectl_ai/`.

| Question | Finding | Evidence |
|---|---|---|
| Pinned version | `0.0.31`. The container image pins the same release: `KUBECTL_AI_VERSION=v0.0.31` in `Dockerfile.kubectl-ai`, with a checksum taken from the release's `checksums.txt`. | `kubectl-ai version` |
| MCP transports | `--mcp-server-mode stdio` (the default) and `streamable-http`. The port is set with `--http-port` (default 9080) and the endpoint path is `/mcp`. | `help.txt` |
| LLM key needed in MCP mode? | **No.** It started with `env -i` (no `GEMINI_API_KEY` or any other key) and served `initialize`, `tools/list` and `tools/call`. | `initialize.json`, `tools_list.json` |
| Tools exposed | `bash(command, modifies_resource)` and `kubectl(command, modifies_resource)`. The `command` argument is **free text**. | `tools_list.json` |
| Can bash be disabled? | **No flag for it.** Defence: the agent never proxies `bash` to the LLM (`review_loop.py`), and the sidecar holds no secrets. | `help.txt` |
| Does kubectl run through a shell? | **Yes.** `kubectl version --client; echo INJECTED` printed `INJECTED`. Every argument is therefore validated against an allowlist and `shlex.quote`d, and the agent builds the command string itself (`kubectl_tool.py`). | `calls.json` → `shell_meta_executed` |
| HTTP bind address | Binds **`*:<port>` on all interfaces, with no auth**. There is no bind-address flag. Defence: `deploy/networkpolicy.yaml` denies **all ingress** to the rightsizing pod. Traffic between containers over localhost is unaffected. | `lsof` showed `TCP *:19080 (LISTEN)` |
| Result format | `content[0].text` is a text blob containing `Command:`, `Error:`, `Stdout:`, `Stderr:`, `ExitCode:`. The agent forwards it to the LLM truncated to 32 KB. | `calls.json` |
| Cluster call transcript | **BLOCKED.** This machine has no cluster access: there is no `~/.kube`, and only an unreachable fake kubeconfig was used. Re-run `scripts/verify-cluster.sh` on a cluster to capture real `get`/`describe`/`logs` output. | `calls.json` → `get_unreachable` |

## Decision
- The sidecar runs `kubectl-ai --mcp-server --mcp-server-mode streamable-http --http-port 9080`.
- **No `bridge.py` is needed**, because the HTTP transport exists natively.
- The agent connects to `http://127.0.0.1:9080/mcp` using the MCP streamable-HTTP client.
