"""Structured, allowlisted kubectl tool for the LLM.

kubectl-ai's `kubectl` MCP tool takes a free-text command and runs it through a shell (docs/kubectl-ai.md),
so the LLM never supplies text that reaches the shell: it fills a structured schema, the agent validates every
field against an allowlist, builds argv itself and shell-quotes each element.
"""

from __future__ import annotations

import re
import shlex

OUTPUT_LIMIT = 32 * 1024

VERBS = {"get", "describe", "logs", "top", "events"}
RESOURCES = {
    "pods": "pods", "pod": "pods", "po": "pods",
    "deployments": "deployments", "deployment": "deployments", "deploy": "deployments",
    "statefulsets": "statefulsets", "statefulset": "statefulsets", "sts": "statefulsets",
    "replicasets": "replicasets", "replicaset": "replicasets", "rs": "replicasets",
    "hpa": "horizontalpodautoscalers", "horizontalpodautoscalers": "horizontalpodautoscalers",
    "events": "events", "limitranges": "limitranges", "limits": "limitranges",
}
OUTPUTS = {"json", "yaml", "wide", "name"}
ALLOWED_KEYS = {"verb", "resource", "name", "namespace", "output", "selector", "container", "tail", "since",
                "previous"}
SECURITY_KEYS = {"server", "s", "raw", "token", "kubeconfig", "context", "cluster", "user", "as", "as_group",
                 "as-group", "as_uid", "insecure_skip_tls_verify", "insecure-skip-tls-verify",
                 "certificate_authority", "certificate-authority", "client_key", "client_certificate", "username",
                 "password"}

NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
DNS_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
SELECTOR_RE = re.compile(r"^[a-z0-9A-Z._/-]+=[a-z0-9A-Z._-]+(,[a-z0-9A-Z._/-]+=[a-z0-9A-Z._-]+){0,4}$")
SINCE_RE = re.compile(r"^\d+[smh]$")
SHELL_META_RE = re.compile(r"[;&|`<>\n\r]|\$\(|\$\{")
SECURITY_FLAG_RE = re.compile(r"(^|\s)--?(server|s|token|kubeconfig|context|cluster|user|as|as-group|raw|"
                              r"insecure-skip-tls-verify|certificate-authority|client-key|client-certificate)(=|\s|$)")


class SecurityRefusal(Exception):
    """An attempt to escape the tool's boundary (exit 3 / held:security)."""


class ToolInvalid(Exception):
    """An ordinary invalid call; the error is returned to the LLM so it can correct itself."""


def _check_security(args: dict) -> None:
    for key, value in args.items():
        if str(key).lstrip("-").lower() in SECURITY_KEYS:
            raise SecurityRefusal(f"forbidden parameter {key!r}")
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, str) and (SHELL_META_RE.search(item) or SECURITY_FLAG_RE.search(item)):
                raise SecurityRefusal(f"forbidden content in {key!r}")


def build_command(args: dict, allowed_namespaces: set[str] | frozenset[str]) -> str:
    if not isinstance(args, dict):
        raise ToolInvalid("arguments must be an object")
    _check_security(args)
    unknown = set(args) - ALLOWED_KEYS
    if unknown:
        raise ToolInvalid(f"unsupported parameters: {sorted(unknown)}")
    verb = args.get("verb")
    if verb not in VERBS:
        raise ToolInvalid(f"verb must be one of {sorted(VERBS)}")
    ns = args.get("namespace")
    if ns not in allowed_namespaces:
        raise ToolInvalid(f"namespace is required and must be one of {sorted(allowed_namespaces)}")
    name = args.get("name")
    if name is not None and (not isinstance(name, str) or not NAME_RE.match(name)):
        raise ToolInvalid("name must be a DNS-1123 name")
    resource = args.get("resource")
    if resource is not None:
        if resource not in RESOURCES:
            raise ToolInvalid(f"resource must be one of {sorted(set(RESOURCES.values()))}")
        resource = RESOURCES[resource]

    argv = ["kubectl"]
    if verb in ("get", "describe"):
        if resource is None:
            raise ToolInvalid("resource is required")
        argv += [verb, resource] + ([name] if name else [])
        output = args.get("output")
        if output is not None:
            if verb != "get" or output not in OUTPUTS:
                raise ToolInvalid(f"output must be one of {sorted(OUTPUTS)} (get only)")
            argv += ["-o", output]
    elif verb == "logs":
        if resource not in (None, "pods") or not name:
            raise ToolInvalid("logs requires resource=pods and a pod name")
        argv += ["logs", name]
        container = args.get("container")
        if container is not None:
            if not isinstance(container, str) or not DNS_LABEL_RE.match(container):
                raise ToolInvalid("container must be a DNS-1123 label")
            argv += ["-c", container]
        tail = args.get("tail", 200)
        if not isinstance(tail, int) or isinstance(tail, bool) or not 1 <= tail <= 500:
            raise ToolInvalid("tail must be an integer 1..500")
        argv += ["--tail", str(tail)]
        since = args.get("since")
        if since is not None:
            if not isinstance(since, str) or not SINCE_RE.match(since):
                raise ToolInvalid("since must look like 30m / 2h / 3600s")
            argv += ["--since", since]
        if args.get("previous") is True:
            argv.append("--previous")
    elif verb == "top":
        if resource not in (None, "pods"):
            raise ToolInvalid("top supports pods only")
        argv += ["top", "pods"] + ([name] if name else []) + ["--containers"]
    elif verb == "events":
        argv += ["events"]
        if name:
            if resource is None:
                raise ToolInvalid("events for an object needs resource and name")
            argv += ["--for", f"{resource.rstrip('s')}/{name}"]
    selector = args.get("selector")
    if selector is not None:
        if verb not in ("get", "describe", "top") or not isinstance(selector, str) or not SELECTOR_RE.match(selector):
            raise ToolInvalid("selector must be equality-based: key=value[,key=value]")
        argv += ["-l", selector]
    argv += ["-n", ns]
    return " ".join(shlex.quote(a) for a in argv)


def truncate(text: str, limit: int = OUTPUT_LIMIT) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", "ignore") + f"\n[truncated {len(data) - limit} bytes]"
