"""Render manifests the way ArgoCD would, and validate edits by diffing rendered output."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ruamel.yaml import YAML

from ..models import ContainerResources
from . import AppSource, SkipEdit
from .detect import kustomization_file
from .yamlio import YAML_SUFFIXES, load_safe_all

ObjKey = tuple[str, str, str]  # (kind, namespace, name)


@dataclass
class Renderer:
    timeout: float = 120
    kube_version: str | None = None
    api_versions: list[str] = field(default_factory=list)
    kustomize_build_options: list[str] = field(default_factory=list)
    helm_bin: str = "helm"
    kustomize_bin: str = "kustomize"

    # ------------------------------------------------------------------ public
    def render(self, fmt: str, repo_dir: Path, src: AppSource) -> dict[ObjKey, dict]:
        if fmt == "plain":
            docs = self._plain(repo_dir / src.path)
        elif fmt == "helm":
            docs = self._helm(repo_dir, src)
        elif fmt == "kustomize":
            docs = self._kustomize(repo_dir, src)
        else:
            raise SkipEdit("unsupported_source", fmt)
        return index_objects(docs, src.dest_namespace)

    # ------------------------------------------------------------------ formats
    def _run(self, argv: list[str], cwd: Path | None = None) -> str:
        try:
            proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            raise SkipEdit("render_timeout", " ".join(argv[:2])) from exc
        except FileNotFoundError as exc:
            raise SkipEdit("render_failed", f"{argv[0]} not installed") from exc
        if proc.returncode != 0:
            raise SkipEdit("render_failed", proc.stderr.strip()[-500:])
        return proc.stdout

    def _plain(self, app_dir: Path) -> list[dict]:
        docs: list[dict] = []
        for path in sorted(p for p in app_dir.iterdir() if p.suffix in YAML_SUFFIXES and p.is_file()):
            docs.extend(load_safe_all(path.read_text(encoding="utf-8")))
        return docs

    def _helm(self, repo_dir: Path, src: AppSource) -> list[dict]:
        chart_dir = (repo_dir / src.path).resolve()
        helm = src.helm or {}
        chart = YAML(typ="safe").load((chart_dir / "Chart.yaml").read_text(encoding="utf-8")) or {}
        if chart.get("dependencies") and not (chart_dir / "charts").is_dir():
            self._run([self.helm_bin, "dependency", "build", str(chart_dir)])
        release = helm.get("releaseName") or src.app_name
        argv = [self.helm_bin, "template", release, str(chart_dir), "--namespace", src.dest_namespace]
        for vf in helm.get("valueFiles") or []:
            path = (chart_dir / vf).resolve()
            if path.is_file():
                argv += ["--values", str(path)]
            elif not helm.get("ignoreMissingValueFiles"):
                raise SkipEdit("render_failed", f"missing value file {vf}")
        for param in helm.get("parameters") or []:
            flag = "--set-string" if param.get("forceString") else "--set"
            argv += [flag, f"{param['name']}={param.get('value', '')}"]
        for fparam in helm.get("fileParameters") or []:
            argv += ["--set-file", f"{fparam['name']}={(chart_dir / fparam['path']).resolve()}"]
        if helm.get("skipCrds"):
            argv.append("--skip-crds")
        if self.kube_version:
            argv += ["--kube-version", self.kube_version]
        for api in self.api_versions:
            argv += ["--api-versions", api]
        return load_safe_all(self._run(argv))

    def _kustomize(self, repo_dir: Path, src: AppSource) -> list[dict]:
        with tempfile.TemporaryDirectory(prefix="rs-kust-") as tmp:
            copy = Path(tmp) / "repo"
            shutil.copytree(repo_dir, copy, ignore=shutil.ignore_patterns(".git"), symlinks=True)
            app_dir = copy / src.path
            self._apply_kustomize_overrides(app_dir, src.kustomize or {})
            return load_safe_all(self._run([self.kustomize_bin, "build", str(app_dir),
                                            *self.kustomize_build_options]))

    @staticmethod
    def _apply_kustomize_overrides(app_dir: Path, overrides: dict) -> None:
        """Mirror ArgoCD's `kustomize edit set ...` overrides on a temporary copy."""
        if not overrides:
            return
        kfile = kustomization_file(app_dir)
        if kfile is None:
            raise SkipEdit("kustomization_missing", str(app_dir))
        y = YAML(typ="safe")
        data = y.load(kfile.read_text(encoding="utf-8")) or {}
        for key in ("namePrefix", "nameSuffix", "namespace"):
            if overrides.get(key):
                data[key] = overrides[key]
        if overrides.get("commonLabels"):
            data.setdefault("commonLabels", {}).update(overrides["commonLabels"])
        if overrides.get("commonAnnotations"):
            data.setdefault("commonAnnotations", {}).update(overrides["commonAnnotations"])
        if overrides.get("images"):
            images = data.setdefault("images", [])
            for spec in overrides["images"]:
                images.append(_parse_image_override(spec))
        if overrides.get("patches"):
            data.setdefault("patches", []).extend(overrides["patches"])
        if overrides.get("replicas"):
            data.setdefault("replicas", []).extend(overrides["replicas"])
        with open(kfile, "w", encoding="utf-8") as handle:
            y.dump(data, handle)


def _parse_image_override(spec: str) -> dict:
    """ArgoCD image override syntax: [old=]new[:tag|@digest]."""
    old, new = spec.split("=", 1) if "=" in spec else (spec, spec)
    entry: dict = {"name": _split_image(old)[0]}
    image, tag, digest = _split_image(new)
    entry["newName"] = image
    if digest:
        entry["digest"] = digest
    elif tag:
        entry["newTag"] = tag
    return entry


def _split_image(ref: str) -> tuple[str, str | None, str | None]:
    if "@" in ref:
        image, digest = ref.split("@", 1)
        return image, None, digest
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        image, tag = ref.rsplit(":", 1)
        return image, tag, None
    return ref, None, None


# ---------------------------------------------------------------------- objects


def index_objects(docs: list[dict], default_ns: str) -> dict[ObjKey, dict]:
    out: dict[ObjKey, dict] = {}
    for doc in docs:
        if doc.get("kind") == "List":
            for item in doc.get("items") or []:
                out.update(index_objects([item], default_ns))
            continue
        meta = doc.get("metadata") or {}
        if not doc.get("kind") or not meta.get("name"):
            continue
        out[(doc["kind"], meta.get("namespace") or default_ns, meta["name"])] = doc
    return out


def container_resources(objects: dict[ObjKey, dict], key: ObjKey, container: str) -> ContainerResources:
    obj = objects.get(key)
    if obj is None:
        raise SkipEdit("render_missing_workload", "/".join(key))
    for c in (((obj.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []:
        if c.get("name") == container:
            return ContainerResources.from_k8s(c.get("resources"))
    raise SkipEdit("render_missing_container", container)


def flatten(objects: dict[ObjKey, dict]) -> dict[tuple, object]:
    """Flatten objects to {path: scalar}. Named lists (containers etc.) are keyed by name for stable diffs."""
    flat: dict[tuple, object] = {}

    def walk(node, path):
        if isinstance(node, dict):
            if not node:
                flat[path] = {}
            for k, v in node.items():
                walk(v, path + (str(k),))
        elif isinstance(node, list):
            if not node:
                flat[path] = []
            named = all(isinstance(i, dict) and "name" in i for i in node) and node
            for i, v in enumerate(node):
                walk(v, path + ((f"name={v['name']}",) if named else (str(i),)))
        else:
            flat[path] = node

    for key, obj in objects.items():
        walk(obj, (key,))
    return flat


def nondeterministic_mask(first: dict[ObjKey, dict], second: dict[ObjKey, dict]) -> set[tuple]:
    a, b = flatten(first), flatten(second)
    return {p for p in set(a) | set(b) if a.get(p, _MISSING) != b.get(p, _MISSING)}


_MISSING = object()


def resources_prefix(key: ObjKey, container: str) -> tuple:
    return (key, "spec", "template", "spec", "containers", f"name={container}", "resources")


def validate_edit(before: dict[ObjKey, dict], after: dict[ObjKey, dict], mask: set[tuple], key: ObjKey,
                  expected: dict[str, ContainerResources]) -> None:
    """Raise SkipEdit unless the only rendered changes are the target containers' resources at expected values."""
    prefixes = [resources_prefix(key, c) for c in expected]
    if any(p[: len(pre)] == pre for p in mask for pre in prefixes):
        raise SkipEdit("render_nondeterministic", "target resources differ between identical renders")
    a, b = flatten(before), flatten(after)
    changed = {p for p in set(a) | set(b) if a.get(p, _MISSING) != b.get(p, _MISSING)} - mask
    unexpected = sorted((p for p in changed if not any(p[: len(pre)] == pre for pre in prefixes)), key=str)
    if unexpected:
        sample = "/".join(str(x) for x in unexpected[0][1:])
        raise SkipEdit("render_mismatch", f"unexpected change at {unexpected[0][0]}:{sample}")
    for container, want in expected.items():
        actual = container_resources(after, key, container)
        if not actual.same_as(want):
            raise SkipEdit("render_mismatch", f"{container}: rendered {actual.values} != expected {want.values}")


def check_drift(rendered: dict[ObjKey, dict], key: ObjKey, container: str, live: ContainerResources) -> None:
    """Compare the rendered workload template's container resources with the live workload template."""
    current = container_resources(rendered, key, container)
    if not current.same_as(live):
        raise SkipEdit("drift", f"git {current.values} != live {live.values}")
