"""Format-specific editors. Each returns the repo-relative files it changed or raises SkipEdit."""

from __future__ import annotations

from pathlib import Path

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from ..config import ManualMapping
from ..models import ContainerResources
from . import AppSource, SkipEdit
from .detect import kustomization_file
from .yamlio import YAML_SUFFIXES, dump_all_rt, load_all_rt, rt_yaml, set_resource_fields


def _rel(repo_dir: Path, path: Path) -> str:
    return str(path.resolve().relative_to(repo_dir.resolve()))


def _containers(doc: dict) -> list:
    try:
        return doc["spec"]["template"]["spec"]["containers"] or []
    except (KeyError, TypeError):
        return []


# --------------------------------------------------------------------------- plain


def edit_plain(repo_dir: Path, src: AppSource, kind: str, name: str, namespace: str, container: str,
               changes: dict[str, float]) -> list[str]:
    app_dir = repo_dir / src.path
    matches = []
    for path in sorted(p for p in app_dir.iterdir() if p.suffix in YAML_SUFFIXES and p.is_file()):
        docs, y, explicit = load_all_rt(path)
        for idx, doc in enumerate(docs):
            if not isinstance(doc, dict) or doc.get("kind") != kind:
                continue
            meta = doc.get("metadata") or {}
            if meta.get("name") != name or (meta.get("namespace") or src.dest_namespace) != namespace:
                continue
            matches.append((path, docs, y, explicit, idx))
    if not matches:
        raise SkipEdit("workload_not_in_repo", f"{kind}/{name} not found under {src.path}")
    if len(matches) > 1:
        raise SkipEdit("ambiguous_manifest", f"{kind}/{name} defined {len(matches)} times")
    path, docs, y, explicit, idx = matches[0]
    target = [c for c in _containers(docs[idx]) if isinstance(c, dict) and c.get("name") == container]
    if len(target) != 1:
        raise SkipEdit("container_not_found", container)
    set_resource_fields(target[0], "resources", changes)
    dump_all_rt(path, docs, y, explicit)
    return [_rel(repo_dir, path)]


# --------------------------------------------------------------------------- helm


def helm_value_files(repo_dir: Path, src: AppSource) -> list[Path]:
    app_dir = repo_dir / src.path
    names = list((src.helm or {}).get("valueFiles") or [])
    files = [(app_dir / n).resolve() for n in names]
    default = app_dir / "values.yaml"
    if default.is_file() and default.resolve() not in files:
        files.insert(0, default.resolve())  # helm always applies the chart's values.yaml first
    for f in files:
        if app_dir.resolve() not in f.parents:
            # the commit path guard would reject it anyway; refuse before writing anything
            raise SkipEdit("path_guard", f"value file outside the application path: {f.relative_to(repo_dir.resolve())}"
                           if repo_dir.resolve() in f.parents else f"value file outside repo: {f}")
    return [f for f in files if f.is_file()]


def _looks_like_resources(node) -> bool:
    if not isinstance(node, dict) or not node:
        return False
    if not set(node.keys()) <= {"requests", "limits", "claims"}:
        return False
    for section in ("requests", "limits"):
        sect = node.get(section)
        if sect is None:
            continue
        if not isinstance(sect, dict) or not (set(sect.keys()) & {"cpu", "memory"}):
            return False
    return "requests" in node or "limits" in node


def _walk(node, path=()):
    if isinstance(node, dict):
        if _looks_like_resources(node):
            yield path, node
            return
        for key, value in node.items():
            yield from _walk(value, path + (str(key),))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _walk(value, path + (str(i),))


def _get_path(doc, dotted: str):
    node = doc
    for part in dotted.split("."):
        if isinstance(node, list):
            node = node[int(part)]
        elif isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def edit_helm(repo_dir: Path, src: AppSource, container: str, rendered_current: ContainerResources,
              changes: dict[str, float], mappings: list[ManualMapping] | None = None) -> list[str]:
    if (src.helm or {}).get("values") or (src.helm or {}).get("valuesObject"):
        raise SkipEdit("helm_inline_values", "inline values in the Application are not supported")
    files = helm_value_files(repo_dir, src)
    if not files:
        raise SkipEdit("helm_no_values_file")

    mapping = next((m for m in (mappings or []) if m.values_key and (m.container in (None, container))), None)
    if mapping:
        vf = (repo_dir / src.path / mapping.values_file).resolve() if mapping.values_file else files[-1]
        if (repo_dir / src.path).resolve() not in vf.parents:
            raise SkipEdit("path_guard", f"manual mapping values_file outside the application path: {mapping.values_file}")
        docs, y, explicit = load_all_rt(vf)
        node = _get_path(docs[0], mapping.values_key)
        if node is None or not isinstance(node, dict):
            raise SkipEdit("manual_mapping_invalid", mapping.values_key)
        set_resource_fields_inplace(node, changes)
        dump_all_rt(vf, docs, y, explicit)
        return [_rel(repo_dir, vf)]

    candidates = []
    for vf in files:
        docs, y, explicit = load_all_rt(vf)
        if not docs or not isinstance(docs[0], dict):
            continue
        for path, node in _walk(docs[0]):
            if ContainerResources.from_k8s(node).same_as(rendered_current):
                candidates.append((vf, docs, y, explicit, path, node))
    if not candidates:
        raise SkipEdit("helm_no_match", "no resources block in values equals the rendered values")
    if len(candidates) > 1:
        where = ", ".join(f"{c[0].name}:{'.'.join(c[4])}" for c in candidates)
        raise SkipEdit("helm_ambiguous", where)
    vf, docs, y, explicit, _path, node = candidates[0]
    set_resource_fields_inplace(node, changes)
    dump_all_rt(vf, docs, y, explicit)
    return [_rel(repo_dir, vf)]


def set_resource_fields_inplace(block: dict, changes: dict[str, float]) -> None:
    holder = CommentedMap()
    holder["resources"] = block
    set_resource_fields(holder, "resources", changes)


# --------------------------------------------------------------------------- kustomize


def effective_name_affixes(app_dir: Path, src: AppSource) -> tuple[str, str]:
    kfile = kustomization_file(app_dir)
    prefix = suffix = ""
    if kfile:
        data = rt_yaml().load(kfile.read_text(encoding="utf-8")) or {}
        prefix = data.get("namePrefix", "") or ""
        suffix = data.get("nameSuffix", "") or ""
    overrides = src.kustomize or {}
    prefix = overrides.get("namePrefix", prefix) or prefix
    suffix = overrides.get("nameSuffix", suffix) or suffix
    return prefix, suffix


def edit_kustomize(repo_dir: Path, src: AppSource, kind: str, live_name: str, container: str,
                   changes: dict[str, float]) -> list[str]:
    app_dir = repo_dir / src.path
    kfile = kustomization_file(app_dir)
    if kfile is None:
        raise SkipEdit("kustomization_missing", src.path)
    prefix, suffix = effective_name_affixes(app_dir, src)
    base_name = live_name
    if prefix and base_name.startswith(prefix):
        base_name = base_name[len(prefix):]
    if suffix and base_name.endswith(suffix):
        base_name = base_name[: -len(suffix)]

    patch_name = f"rightsizing-patch-{kind.lower()}-{base_name}.yaml"
    patch_path = app_dir / patch_name
    if patch_path.exists():
        docs, y, explicit = load_all_rt(patch_path)
        patch = docs[0]
    else:
        y, explicit = rt_yaml(), False
        patch = CommentedMap()
        patch["apiVersion"] = "apps/v1"
        patch["kind"] = kind
        patch["metadata"] = CommentedMap({"name": base_name})
        patch["spec"] = CommentedMap({"template": CommentedMap({"spec": CommentedMap({
            "containers": CommentedSeq()})})})
    containers = patch["spec"]["template"]["spec"]["containers"]
    entry = next((c for c in containers if c.get("name") == container), None)
    if entry is None:
        entry = CommentedMap({"name": container})
        containers.append(entry)
    set_resource_fields(entry, "resources", changes)
    dump_all_rt(patch_path, [patch], y, explicit)

    kdocs, ky, kexplicit = load_all_rt(kfile)
    kdata = kdocs[0]
    patches = kdata.get("patches")
    if patches is None:
        patches = CommentedSeq()
        kdata["patches"] = patches
    already = any((p.get("path") if isinstance(p, dict) else p) == patch_name for p in patches)
    smp = kdata.get("patchesStrategicMerge") or []
    if not already and patch_name not in smp:
        patches.append(CommentedMap({"path": patch_name}))
        dump_all_rt(kfile, kdocs, ky, kexplicit)
        return [_rel(repo_dir, patch_path), _rel(repo_dir, kfile)]
    return [_rel(repo_dir, patch_path)]
