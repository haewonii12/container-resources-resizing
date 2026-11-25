"""Read-only discovery of candidate workloads and their ArgoCD Applications."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

from . import ALLOWED_NAMESPACES, quantity
from .config import Config
from .manifest import AppSource
from .models import ContainerResources, LimitRangeBounds, WorkloadRef

TRACKING_ANNOTATION = "argocd.argoproj.io/tracking-id"
KINDS = ("Deployment", "StatefulSet")


class KubeAPI(Protocol):
    def list_workloads(self, namespace: str) -> list[dict]: ...
    def list_hpas(self, namespace: str) -> list[dict]: ...
    def list_limitranges(self, namespace: str) -> list[dict]: ...
    def get_application(self, namespace: str, name: str) -> dict | None: ...
    def version(self) -> str | None: ...
    def api_versions(self) -> list[str]: ...


@dataclass
class Candidate:
    ref: WorkloadRef
    live: dict
    containers: dict[str, ContainerResources]
    app: dict
    source: AppSource
    blocked_resources: set[str] = field(default_factory=set)
    limitrange: LimitRangeBounds | None = None
    argo_status: str | None = None

    @property
    def replicas(self) -> int | None:
        return (self.live.get("spec") or {}).get("replicas")


@dataclass
class Skipped:
    ref: WorkloadRef
    reason: str
    detail: str = ""


def normalize_repo(url: str) -> str:
    url = url.strip().lower()
    url = re.sub(r"^[a-z+]+://", "", url)
    url = re.sub(r"^[^@/]+@", "", url)
    url = url.replace(":", "/", 1) if re.match(r"^[^/]+:[^0-9]", url) else url
    return url.removesuffix("/").removesuffix(".git")


def resolve_app(workload: dict, cfg: Config) -> tuple[str, str] | None:
    """Return (app namespace, app name) from the owner's tracking id or the instance label."""
    meta = workload.get("metadata") or {}
    method = cfg.argocd.tracking_method
    if "annotation" in method:
        tid = (meta.get("annotations") or {}).get(TRACKING_ANNOTATION)
        if tid:
            app_part, _, rest = tid.partition(":")
            group_kind, _, ns_name = rest.partition(":")
            expected = f"{meta.get('namespace')}/{meta.get('name')}"
            if ns_name == expected and group_kind.split("/")[-1] == workload.get("kind"):
                if "_" in app_part:
                    app_ns, app_name = app_part.split("_", 1)
                    return app_ns, app_name
                return cfg.argocd.namespace, app_part
    if "label" in method:
        name = (meta.get("labels") or {}).get(cfg.argocd.instance_label)
        if name:
            return cfg.argocd.namespace, name
    return None


def _ignore_differences_touch_resources(app: dict, ref: WorkloadRef) -> bool:
    for entry in (app.get("spec") or {}).get("ignoreDifferences") or []:
        if entry.get("kind") not in (ref.kind, None) or entry.get("group") not in ("apps", None, ""):
            continue
        if entry.get("name") not in (None, ref.name) or entry.get("namespace") not in (None, ref.namespace):
            continue
        if entry.get("managedFieldsManagers"):
            return True
        exprs = list(entry.get("jsonPointers") or []) + list(entry.get("jqPathExpressions") or [])
        if any("resources" in e or e in ("/spec", "/spec/template", ".spec", ".spec.template") for e in exprs):
            return True
    return False


def check_application(app: dict, ref: WorkloadRef, cfg: Config) -> tuple[str, str] | None:
    """Return (skip reason, detail) or None if the Application is supported."""
    spec = app.get("spec") or {}
    if spec.get("sources"):
        return "multi_source", f"{len(spec['sources'])} sources"
    if ((spec.get("syncPolicy") or {}).get("automated")) is not None:
        return "auto_sync_enabled", "approval gate would be bypassed"
    src = spec.get("source") or {}
    if normalize_repo(src.get("repoURL", "")) != normalize_repo(cfg.git.url):
        return "foreign_repo", src.get("repoURL", "")
    rev = src.get("targetRevision") or ""
    branch = cfg.git.branch
    if not (rev in (branch, f"refs/heads/{branch}") or (rev in ("", "HEAD") and cfg.git.head_is_branch)):
        return "target_revision_mismatch", rev
    if _ignore_differences_touch_resources(app, ref):
        return "unsupported_ignore_differences", "ignoreDifferences may hide resource drift"
    return None


def hpa_blocked(hpas: list[dict], ref: WorkloadRef) -> set[str]:
    blocked: set[str] = set()
    for hpa in hpas:
        target = (hpa.get("spec") or {}).get("scaleTargetRef") or {}
        if target.get("kind") != ref.kind or target.get("name") != ref.name:
            continue
        spec = hpa.get("spec") or {}
        if spec.get("targetCPUUtilizationPercentage") is not None:  # autoscaling/v1 shape
            blocked.add("cpu")
        for metric in spec.get("metrics") or []:
            body = metric.get("resource") or metric.get("containerResource") or {}
            if metric.get("type") in ("Resource", "ContainerResource") and \
                    (body.get("target") or {}).get("type") == "Utilization" and body.get("name") in ("cpu", "memory"):
                blocked.add(body["name"])
    return blocked


def limitrange_bounds(limitranges: list[dict]) -> LimitRangeBounds | None:
    bounds = LimitRangeBounds()
    found = False
    for lr in limitranges:
        for item in (lr.get("spec") or {}).get("limits") or []:
            if item.get("type") != "Container":
                continue
            for attr, target in (("min", bounds.min), ("max", bounds.max), ("maxLimitRequestRatio", bounds.max_ratio)):
                for res in ("cpu", "memory"):
                    raw = (item.get(attr) or {}).get(res)
                    if raw is None:
                        continue
                    found = True
                    if attr == "maxLimitRequestRatio":
                        target[res] = float(quantity.parse_number(raw))
                    else:
                        value = quantity.parse(res, raw)
                        target[res] = min(target.get(res, value), value) if attr == "max" else \
                            max(target.get(res, value), value)
    return bounds if found else None


def argo_resource_status(app: dict, ref: WorkloadRef) -> str | None:
    for res in (app.get("status") or {}).get("resources") or []:
        if res.get("kind") == ref.kind and res.get("name") == ref.name and \
                (res.get("namespace") or ref.namespace) == ref.namespace:
            return res.get("status")
    return None


def discover(api: KubeAPI, cfg: Config) -> tuple[list[Candidate], list[Skipped]]:
    candidates: list[Candidate] = []
    skipped: list[Skipped] = []
    for ns in [n for n in cfg.namespaces if n in ALLOWED_NAMESPACES]:
        hpas = api.list_hpas(ns)
        lr = limitrange_bounds(api.list_limitranges(ns))
        for w in api.list_workloads(ns):
            meta = w.get("metadata") or {}
            if w.get("kind") not in KINDS or meta.get("namespace") != ns:
                continue
            ref = WorkloadRef(ns, w["kind"], meta["name"])
            containers = {c["name"]: ContainerResources.from_k8s(c.get("resources"))
                          for c in (((w.get("spec") or {}).get("template") or {}).get("spec") or {}).get(
                              "containers") or []}
            app_ref = resolve_app(w, cfg)
            if app_ref is None:
                skipped.append(Skipped(ref, "no_argocd_app"))
                continue
            if app_ref[0] != cfg.argocd.namespace:
                skipped.append(Skipped(ref, "app_namespace_unsupported", f"{app_ref[0]}/{app_ref[1]}"))
                continue
            try:
                app = api.get_application(*app_ref)
            except Exception as exc:  # 403/5xx on one Application must not fail the whole run
                skipped.append(Skipped(ref, "app_unreadable", f"{'/'.join(app_ref)}: {type(exc).__name__}"))
                continue
            if app is None:
                skipped.append(Skipped(ref, "app_not_found", "/".join(app_ref)))
                continue
            problem = check_application(app, ref, cfg)
            if problem:
                skipped.append(Skipped(ref, *problem))
                continue
            candidates.append(Candidate(ref=ref, live=w, containers=containers, app=app,
                                        source=AppSource.from_application(app),
                                        blocked_resources=hpa_blocked(hpas, ref), limitrange=lr,
                                        argo_status=argo_resource_status(app, ref)))
    return candidates, skipped


class KubeClient:
    """KubeAPI backed by the official python client (in-cluster SA token, read-only RBAC)."""

    def __init__(self, cfg: Config):
        from kubernetes import client, config as kconfig

        try:
            kconfig.load_incluster_config()
        except kconfig.ConfigException:
            kconfig.load_kube_config()
        self._client = client
        self.api = client.ApiClient()
        self.apps = client.AppsV1Api(self.api)
        self.core = client.CoreV1Api(self.api)
        self.autoscaling = client.AutoscalingV2Api(self.api)
        self.custom = client.CustomObjectsApi(self.api)

    def _dicts(self, items, kind: str) -> list[dict]:
        out = []
        for item in items:
            d = self.api.sanitize_for_serialization(item)
            d["kind"] = kind
            out.append(d)
        return out

    def list_workloads(self, namespace: str) -> list[dict]:
        return (self._dicts(self.apps.list_namespaced_deployment(namespace).items, "Deployment")
                + self._dicts(self.apps.list_namespaced_stateful_set(namespace).items, "StatefulSet"))

    def list_hpas(self, namespace: str) -> list[dict]:
        return self._dicts(self.autoscaling.list_namespaced_horizontal_pod_autoscaler(namespace).items,
                           "HorizontalPodAutoscaler")

    def list_limitranges(self, namespace: str) -> list[dict]:
        return self._dicts(self.core.list_namespaced_limit_range(namespace).items, "LimitRange")

    def get_application(self, namespace: str, name: str) -> dict | None:
        from kubernetes.client.exceptions import ApiException

        try:
            return self.custom.get_namespaced_custom_object("argoproj.io", "v1alpha1", namespace, "applications",
                                                            name)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise

    def version(self) -> str | None:
        return self._client.VersionApi(self.api).get_code().git_version

    def api_versions(self) -> list[str]:
        groups = self._client.ApisApi(self.api).get_api_versions().groups
        return ["v1"] + [v.group_version for g in groups for v in g.versions]
