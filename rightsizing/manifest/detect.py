"""Decide how an Application's manifests are produced."""

from __future__ import annotations

from pathlib import Path

from ..config import ManualMapping
from . import AppSource, SkipEdit

KUSTOMIZATION_FILES = ("kustomization.yaml", "kustomization.yml", "Kustomization")


def kustomization_file(app_dir: Path) -> Path | None:
    for name in KUSTOMIZATION_FILES:
        if (app_dir / name).is_file():
            return app_dir / name
    return None


def detect_format(src: AppSource, repo_dir: Path, mappings: list[ManualMapping] | None = None) -> str:
    """Return 'helm' | 'kustomize' | 'plain' or raise SkipEdit('unsupported_source')."""
    if src.plugin:
        raise SkipEdit("unsupported_source", "config management plugin")
    if src.chart:
        raise SkipEdit("unsupported_source", "helm chart from a chart repository")
    directory = src.directory or {}
    if directory.get("jsonnet"):
        raise SkipEdit("unsupported_source", "jsonnet")
    if directory.get("recurse") or directory.get("include") or directory.get("exclude"):
        raise SkipEdit("unsupported_source", "directory recurse/include/exclude")

    for mapping in mappings or []:
        if mapping.format:
            return mapping.format

    app_dir = (repo_dir / src.path).resolve()
    if not app_dir.is_dir() or repo_dir.resolve() not in (app_dir, *app_dir.parents):
        raise SkipEdit("app_path_missing", src.path)
    if src.helm is not None or (app_dir / "Chart.yaml").is_file():
        return "helm"
    if src.kustomize is not None or kustomization_file(app_dir):
        return "kustomize"
    return "plain"
