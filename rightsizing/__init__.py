"""Approval-gated Kubernetes CPU/memory rightsizing agent."""

import re

# Target namespaces come from the config (`namespaces`, `mirror.namespaces`); these are only the defaults.
DEFAULT_NAMESPACES = ("inference", "development")
# Never a target, whatever the config says: control-plane namespaces, ArgoCD, and the agent's own namespace.
FORBIDDEN_NAMESPACES = frozenset({"kube-system", "kube-public", "kube-node-lease", "argocd", "rightsizing"})
_NAMESPACE_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def namespace_error(namespace: object) -> str | None:
    """Why `namespace` cannot be a target (None = it can). Also the PromQL injection guard for label values."""
    if not isinstance(namespace, str) or len(namespace) > 63 or not _NAMESPACE_RE.match(namespace):
        return f"{namespace!r} is not a DNS-1123 label"
    if namespace in FORBIDDEN_NAMESPACES:
        return f"{namespace!r} is a forbidden namespace {sorted(FORBIDDEN_NAMESPACES)}"
    return None
