"""Approval-gated Kubernetes CPU/memory rightsizing agent."""

ALLOWED_NAMESPACES = frozenset({"inference", "development"})
