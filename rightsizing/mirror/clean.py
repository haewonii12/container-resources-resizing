"""Turn a live Deployment into a clean, re-appliable manifest; decide which Deployments the mirror must not adopt."""

from __future__ import annotations

import copy
import hashlib
import io
from typing import Callable

from ruamel.yaml import YAML

INSTANCE_LABEL = "app.kubernetes.io/instance"
DROP_ANNOTATIONS = {"kubectl.kubernetes.io/last-applied-configuration", "deployment.kubernetes.io/revision"}
DROP_ANNOTATION_PREFIXES = ("argocd.argoproj.io/",)


def clean_deployment(obj: dict, app_name: str | None = None, hpa_targeted: bool = False) -> dict:
    """Keep apiVersion/kind/metadata.{name,namespace,labels,annotations}/spec; drop runtime-only data.

    `spec` is kept verbatim (server defaults equal live, so ArgoCD shows no diff), except:
    - spec.template.metadata.creationTimestamp (always null, not part of the pod hash)
    - spec.replicas when an HPA owns the replica count (the first sync must not scale it back)
    Only the top-level ArgoCD tracking label is removed; selector and template labels are never touched.
    """
    meta = obj.get("metadata") or {}
    out_meta: dict = {"name": meta["name"], "namespace": meta["namespace"]}
    labels = dict(meta.get("labels") or {})
    if app_name is not None and labels.get(INSTANCE_LABEL) == app_name:
        labels.pop(INSTANCE_LABEL)
    if labels:
        out_meta["labels"] = labels
    annotations = {k: v for k, v in (meta.get("annotations") or {}).items()
                   if k not in DROP_ANNOTATIONS and not k.startswith(DROP_ANNOTATION_PREFIXES)}
    if annotations:
        out_meta["annotations"] = annotations

    spec = copy.deepcopy(obj.get("spec") or {})
    template_meta = (spec.get("template") or {}).get("metadata")
    if isinstance(template_meta, dict):
        template_meta.pop("creationTimestamp", None)
    if hpa_targeted:
        spec.pop("replicas", None)
    return {"apiVersion": obj.get("apiVersion") or "apps/v1", "kind": "Deployment", "metadata": out_meta,
            "spec": spec}


def skip_reason(obj: dict, resolve_app: Callable[[dict], tuple[str, str] | None],
                get_application: Callable[[str, str], dict | None], skip_helm: bool = True) -> str | None:
    """Deployments with another source of truth are never adopted."""
    meta = obj.get("metadata") or {}
    if meta.get("ownerReferences"):
        return "owned"
    labels, annotations = meta.get("labels") or {}, meta.get("annotations") or {}
    if skip_helm and (labels.get("app.kubernetes.io/managed-by") == "Helm"
                      or "meta.helm.sh/release-name" in annotations):
        return "helm_managed"
    app_ref = resolve_app(obj)
    if app_ref:
        app = get_application(*app_ref)
        if app and any(r.get("kind") == "Deployment" and r.get("name") == meta.get("name")
                       and (r.get("namespace") or meta.get("namespace")) == meta.get("namespace")
                       for r in (app.get("status") or {}).get("resources") or []):
            return "already_tracked"
    return None


def to_yaml(doc: dict) -> str:
    y = YAML()
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    buf = io.StringIO()
    y.dump(doc, buf)
    return buf.getvalue()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
