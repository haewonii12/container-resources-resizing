"""Manifest format detection, editors, and render-diff validation."""

from __future__ import annotations

from dataclasses import dataclass, field


class SkipEdit(Exception):
    """The workload cannot be edited safely; the message is the skip reason."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


@dataclass
class AppSource:
    """The subset of an ArgoCD Application's single source that affects rendering."""

    app_name: str
    repo_url: str
    path: str
    target_revision: str
    dest_namespace: str
    helm: dict | None = None
    kustomize: dict | None = None
    directory: dict | None = None
    plugin: dict | None = None
    chart: str | None = None

    @classmethod
    def from_application(cls, app: dict) -> "AppSource":
        spec = app.get("spec", {})
        src = spec.get("source") or {}
        return cls(
            app_name=app.get("metadata", {}).get("name", ""),
            repo_url=src.get("repoURL", ""),
            path=src.get("path", ".") or ".",
            target_revision=src.get("targetRevision", "") or "",
            dest_namespace=(spec.get("destination") or {}).get("namespace", "") or "",
            helm=src.get("helm"),
            kustomize=src.get("kustomize"),
            directory=src.get("directory"),
            plugin=src.get("plugin"),
            chart=src.get("chart"),
        )


@dataclass
class EditResult:
    files: list[str] = field(default_factory=list)  # repo-relative paths touched
    description: str = ""
