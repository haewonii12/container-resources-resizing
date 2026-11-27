"""Semantic comparison of two Deployment manifests (git file vs clean(live)), with readable change strings.

Resource quantities are compared normalized (1 == 1000m, 512Mi == 536870912), dict key order is ignored, a key
holding null equals an absent key, and annotations under the configured prefixes are ignored. Change strings never
contain env values (only names), annotation or label values, or any other free-form field content.
"""

from __future__ import annotations

from .. import quantity

_CONTAINER_LISTS = ("containers", "initContainers")


def _qty(resource: str, value) -> object:
    try:
        if resource in ("cpu", "memory"):
            return quantity.parse(resource, value)
        return float(quantity.parse_number(value))
    except (quantity.QuantityError, TypeError):
        return value


def _short(value) -> str:
    return "-" if value is None else str(value)


def _by_name(items) -> dict | None:
    """name -> item for a list of named dicts; None when the list is not uniquely named."""
    if not isinstance(items, list) or not all(isinstance(i, dict) and "name" in i for i in items):
        return None
    out = {i["name"]: i for i in items}
    return out if len(out) == len(items) else None


def _generic(a, b, path: str, out: list[str]) -> None:
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b), key=str):
            if a.get(key) is None and b.get(key) is None:
                continue
            _generic(a.get(key), b.get(key), f"{path}.{key}" if path else str(key), out)
    elif a != b:
        out.append(f"{path} {'added' if a is None else 'removed' if b is None else 'changed'}")


def _resources(a: dict, b: dict, cname: str, out: list[str]) -> None:
    for kind, label in (("requests", "request"), ("limits", "limit")):
        ra, rb = (a or {}).get(kind) or {}, (b or {}).get(kind) or {}
        for res in sorted(set(ra) | set(rb)):
            va, vb = ra.get(res), rb.get(res)
            if va is None and vb is None or _qty(res, va) == _qty(res, vb):
                continue
            out.append(f"{res}.{label}[{cname}] {_short(va)}→{_short(vb)}")
    rest_a = {k: v for k, v in (a or {}).items() if k not in ("requests", "limits")}
    rest_b = {k: v for k, v in (b or {}).items() if k not in ("requests", "limits")}
    _generic(rest_a, rest_b, f"resources[{cname}]", out)


def _env(a, b, cname: str, out: list[str]) -> None:
    ea, eb = _by_name(a or []), _by_name(b or [])
    if ea is None or eb is None:
        if (a or []) != (b or []):
            out.append(f"env[{cname}] changed")
        return
    before = len(out)
    for name in sorted(set(ea) | set(eb)):
        if name not in ea:
            out.append(f"env[{cname}] {name} added")
        elif name not in eb:
            out.append(f"env[{cname}] {name} removed")
        elif ea[name] != eb[name]:
            out.append(f"env[{cname}] {name} changed")
    if len(out) == before and list(ea) != list(eb):
        out.append(f"env[{cname}] order changed")


def _container(a: dict, b: dict, cname: str, out: list[str]) -> None:
    for key in sorted(set(a) | set(b)):
        va, vb = a.get(key), b.get(key)
        if va is None and vb is None:
            continue
        if key == "image":
            if va != vb:
                out.append(f"image[{cname}] {_short(va)}→{_short(vb)}")
        elif key == "env":
            _env(va, vb, cname, out)
        elif key == "resources":
            _resources(va or {}, vb or {}, cname, out)
        elif key != "name":
            _generic(va, vb, f"{key}[{cname}]", out)


def _containers(a, b, field: str, out: list[str]) -> None:
    ca, cb = _by_name(a or []), _by_name(b or [])
    if ca is None or cb is None:
        if (a or []) != (b or []):
            out.append(f"{field} changed")
        return
    before = len(out)
    for name in sorted(set(ca) | set(cb)):
        if name not in ca:
            out.append(f"{field}[{name}] added")
        elif name not in cb:
            out.append(f"{field}[{name}] removed")
        else:
            _container(ca[name], cb[name], name, out)
    if len(out) == before and list(ca) != list(cb):
        out.append(f"{field} order changed")


def _strip_annotations(doc: dict | None, prefixes: tuple[str, ...]) -> dict:
    doc = dict(doc or {})
    meta = dict(doc.get("metadata") or {})
    ann = {k: v for k, v in (meta.get("annotations") or {}).items() if not (prefixes and k.startswith(prefixes))}
    meta["annotations"] = ann or None
    doc["metadata"] = meta
    return doc


def semantic_diff(old: dict | None, new: dict | None, ignore_annotation_prefixes=()) -> list[str]:
    """Readable differences from `old` (git) to `new` (live); [] when both describe the same Deployment."""
    prefixes = tuple(ignore_annotation_prefixes)
    a, b = _strip_annotations(old, prefixes), _strip_annotations(new, prefixes)
    out: list[str] = []
    for key in sorted(set(a) | set(b)):
        if key == "spec":
            continue
        if key == "metadata":
            ma, mb = a["metadata"], b["metadata"]
            for mk in sorted(set(ma) | set(mb)):
                if mk == "annotations":
                    _generic(ma.get(mk) or {}, mb.get(mk) or {}, "annotation", out)
                elif mk == "labels":
                    _generic(ma.get(mk) or {}, mb.get(mk) or {}, "label", out)
                else:
                    _generic(ma.get(mk), mb.get(mk), f"metadata.{mk}", out)
        else:
            _generic(a.get(key), b.get(key), key, out)
    sa, sb = a.get("spec") or {}, b.get("spec") or {}
    for key in sorted(set(sa) | set(sb)):
        va, vb = sa.get(key), sb.get(key)
        if va is None and vb is None:
            continue
        if key == "replicas":
            if va != vb:
                out.append(f"replicas {_short(va)}→{_short(vb)}")
        elif key == "template":
            ta, tb = va or {}, vb or {}
            _generic(ta.get("metadata"), tb.get("metadata"), "template.metadata", out)
            pa, pb = ta.get("spec") or {}, tb.get("spec") or {}
            for pk in sorted(set(pa) | set(pb)):
                if pk in _CONTAINER_LISTS:
                    _containers(pa.get(pk), pb.get(pk), pk, out)
                elif not (pa.get(pk) is None and pb.get(pk) is None):
                    _generic(pa.get(pk), pb.get(pk), f"template.spec.{pk}", out)
            _generic({k: v for k, v in ta.items() if k not in ("metadata", "spec")},
                     {k: v for k, v in tb.items() if k not in ("metadata", "spec")}, "template", out)
        else:
            _generic(va, vb, f"spec.{key}", out)
    return out
