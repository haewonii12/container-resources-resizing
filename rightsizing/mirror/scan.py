"""Classify the manifest repo: which `<ns>/<name>/` directories are mirror-style apps and which are foreign."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..manifest.yamlio import YAML_SUFFIXES, load_safe_all

FOREIGN_MARKERS = ("kustomization.yaml", "kustomization.yml", "Kustomization", "Chart.yaml")


@dataclass
class RepoScan:
    apps: dict[tuple[str, str], str] = field(default_factory=dict)  # (ns, name) -> "ns/name"
    foreign: dict[str, str] = field(default_factory=dict)  # "ns/dir" -> reason

    def present(self, ns: str, name: str, application_paths: set[str] = frozenset()) -> bool:
        """Anything already occupying the path (app, foreign dir, or an Application path) counts as present."""
        path = f"{ns}/{name}"
        return (ns, name) in self.apps or path in self.foreign or path in application_paths


def scan_repo(repo_dir: Path, namespaces: list[str]) -> RepoScan:
    out = RepoScan()
    for ns in namespaces:
        ns_dir = repo_dir / ns
        if not ns_dir.is_dir():
            continue
        for sub in sorted(p for p in ns_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
            rel = f"{ns}/{sub.name}"
            if any((sub / m).exists() for m in FOREIGN_MARKERS):
                out.foreign[rel] = "foreign_layout:kustomize_or_helm"
                continue
            docs: list[dict] = []
            try:
                for f in sorted(p for p in sub.iterdir() if p.is_file() and p.suffix in YAML_SUFFIXES):
                    docs.extend(load_safe_all(f.read_text(encoding="utf-8")))
            except Exception as exc:  # unparseable -> never deleted, never re-added
                out.foreign[rel] = f"foreign_layout:parse_error:{type(exc).__name__}"
                continue
            deps = [d for d in docs if d.get("kind") == "Deployment"]
            if len(deps) == 1 and (deps[0].get("metadata") or {}).get("name") == sub.name:
                out.apps[(ns, sub.name)] = rel
            else:
                out.foreign[rel] = f"foreign_layout:deployments={len(deps)}"
    return out
