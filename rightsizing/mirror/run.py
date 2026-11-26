"""Mirror run: read cluster + repo + ArgoCD + state -> plan -> git commits -> push -> ArgoCD create/delete.

Exit codes: 0 ok | 2 infrastructure (listing, git, ArgoCD API errors) | 4 safety halt
(mass-delete guard, Application with resources finalizer, Application name conflict).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from ruamel.yaml import YAML

from ..config import Config
from ..discovery import normalize_repo, resolve_app
from ..gitops import GitError, GitRepo
from .clean import clean_deployment, skip_reason, to_yaml
from .plan import STATE_PATH, AppInfo, Inputs, MirrorState, Plan, build_plan
from .scan import scan_repo

log = logging.getLogger("rightsizing.mirror")

RESOURCES_FINALIZER = "resources-finalizer.argocd.argoproj.io"
EXIT_OK, EXIT_INFRA, EXIT_HALT = 0, 2, 4


class MirrorKube(Protocol):
    def list_deployments(self, namespace: str) -> list[dict]: ...
    def list_hpa_targets(self, namespace: str) -> set[str]: ...
    def list_applications(self) -> list[dict]: ...
    def create_application(self, body: dict) -> None: ...
    def get_application(self, name: str) -> dict | None: ...
    def delete_application(self, name: str, uid: str, resource_version: str) -> None: ...


@dataclass
class MirrorReport:
    exit_code: int
    plan: Plan
    commits: list[str] = field(default_factory=list)
    app_results: dict[str, str] = field(default_factory=dict)  # app name -> created|deleted|<problem>

    def summary(self) -> dict:
        p = self.plan
        return {"added": len(p.add_dir), "refreshed": len(p.refresh_dir), "deleted_dirs": len(p.delete_dir),
                "apps": self.app_results, "missing": {k: v["consecutive_ok_runs"] for k, v in p.state.missing.items()},
                "halts": p.halts, "manual": p.manual, "list_errors": p.list_errors, "skipped": p.skipped, "commits": len(self.commits),
                "exit_code": self.exit_code}


def app_name(cfg: Config, ns: str, name: str) -> str:
    return cfg.mirror.app_name_template.format(ns=ns, name=name)


def application_body(cfg: Config, ns: str, name: str) -> dict:
    m = cfg.mirror
    return {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
            "metadata": {"name": app_name(cfg, ns, name), "namespace": cfg.argocd.namespace,
                         "labels": {m.managed_label_key: m.managed_label_value}},
            "spec": {"project": m.argocd_project,
                     "source": {"repoURL": cfg.git.url, "targetRevision": cfg.git.branch, "path": f"{ns}/{name}"},
                     "destination": {"server": m.destination_server, "namespace": ns},
                     "syncPolicy": {}}}  # manual sync only: humans approve every sync, including the first


def _is_managed(cfg: Config, app: dict) -> bool:
    labels = (app.get("metadata") or {}).get("labels") or {}
    return labels.get(cfg.mirror.managed_label_key) == cfg.mirror.managed_label_value


def index_applications(cfg: Config, apps: list[dict]) -> dict[str, tuple[dict, AppInfo]]:
    """path -> (Application, AppInfo) for Applications pointing into the manifest repo.

    Multi-source Applications are indexed too (their paths count as occupied) but flagged so they are never
    created, refreshed or deleted by the mirror."""
    out: dict[str, tuple[dict, AppInfo]] = {}
    for app in apps:
        spec, status, meta = app.get("spec") or {}, app.get("status") or {}, app.get("metadata") or {}
        multi = bool(spec.get("sources"))
        for src in (spec.get("sources") or []) if multi else [spec.get("source") or {}]:
            if normalize_repo(src.get("repoURL", "")) != normalize_repo(cfg.git.url) or not src.get("path"):
                continue
            out[src["path"].strip("/")] = (app, AppInfo(
                name=meta["name"], managed=_is_managed(cfg, app) and not multi,
                synced_ever=bool(status.get("history")), operation_pending=bool(status.get("operationState")),
                multi_source=multi, has_resources_finalizer=RESOURCES_FINALIZER in (meta.get("finalizers") or [])))
    return out


def _load_state(repo_dir: Path) -> MirrorState:
    path = repo_dir / STATE_PATH
    if not path.exists():
        return MirrorState()
    return MirrorState.from_dict(YAML(typ="safe").load(path.read_text(encoding="utf-8")))


def _dump_state(state: MirrorState) -> str:
    return "# Managed by rightsizing.mirror - see README 'GitOps mirror'.\n" + to_yaml(state.to_dict())


def _last_commit_is_mirror(repo: GitRepo, path: str) -> bool:
    out = repo.run("log", "-1", "--format=%B", "--", path, check=False).stdout
    return any(line.strip() in ("Mirror-Action: add", "Mirror-Action: refresh") for line in out.splitlines())


def _message(action: str, ns: str, name: str, extra: str = "") -> str:
    explain = {
        "add": "클러스터에 새로 생긴 Deployment를 등록합니다. ArgoCD Application은 수동 sync로 생성되며, "
               "첫 Sync도 사람이 해야 rightsizing 대상이 됩니다.",
        "refresh": "아직 한 번도 Sync되지 않은 미러 앱을 현재 클러스터 내용으로 갱신합니다 (첫 Sync 때 롤백 방지).",
        "delete": "클러스터에서 사라진 Deployment를 정리합니다 (연속 확인 완료). ArgoCD Application은 "
                  "non-cascade로 삭제되며 클러스터 리소스는 건드리지 않습니다. 되돌리려면 이 commit을 revert하고 "
                  "Deployment를 다시 만드세요.",
    }[action]
    return (f"mirror({ns}/{name}): {action}\n\n{explain}{extra}\n\n"
            f"Mirror-Action: {action}\nMirror-Workload: {ns}/Deployment/{name}\n")


def run_mirror(cfg: Config, kube: MirrorKube, repo: GitRepo, now: datetime, dry_run: bool = False) -> MirrorReport:
    m = cfg.mirror
    apps_failed = False
    try:
        all_apps = kube.list_applications()
    except Exception as exc:
        log.error("listing ArgoCD Applications failed", extra={"error": type(exc).__name__})
        all_apps, apps_failed = [], True
    apps = index_applications(cfg, all_apps)
    apps_by_name = {a["metadata"]["name"]: a for a in all_apps}

    cluster_names: dict[str, set[str] | None] = {}
    adoptable: dict[str, dict[str, str]] = {}
    skipped: dict[str, str] = {}
    for ns in m.namespaces:  # validated by MirrorConfig (subset of Config.namespaces)
        if apps_failed:  # without the Application list nothing can be judged: reset counters, change nothing
            cluster_names[ns] = None
            continue
        try:
            deployments = kube.list_deployments(ns)
            hpa_targets = kube.list_hpa_targets(ns)
        except Exception as exc:
            log.error("listing failed", extra={"namespace": ns, "error": type(exc).__name__})
            cluster_names[ns] = None
            continue
        cluster_names[ns] = {d["metadata"]["name"] for d in deployments}
        adoptable[ns] = {}
        for d in deployments:
            name = d["metadata"]["name"]
            reason = skip_reason(d, lambda o: resolve_app(o, cfg),
                                 lambda app_ns, app: apps_by_name.get(app), m.skip_helm)
            if reason:
                skipped[f"{ns}/{name}"] = reason
                continue
            adoptable[ns][name] = to_yaml(clean_deployment(d, app_name(cfg, ns, name), name in hpa_targets))

    scan = scan_repo(repo.workdir, m.namespaces)
    paths_on_disk = {f"{ns}/{p.name}" for ns in m.namespaces if (repo.workdir / ns).is_dir()
                     for p in (repo.workdir / ns).iterdir() if p.is_dir() and not p.name.startswith(".")}
    repo_files = {path: (repo.workdir / path / "deployment.yaml").read_text(encoding="utf-8")
                  for path in scan.apps.values() if (repo.workdir / path / "deployment.yaml").is_file()}
    try:
        state = _load_state(repo.workdir)
    except Exception as exc:  # corrupt state file: refuse to guess (deletes depend on it)
        log.error("cannot read mirror state", extra={"error": type(exc).__name__})
        return MirrorReport(EXIT_INFRA, Plan(list_errors=list(m.namespaces)))
    plan = build_plan(Inputs(
        namespaces=list(m.namespaces), cluster_names=cluster_names, adoptable=adoptable, skipped=skipped, repo=scan,
        repo_files=repo_files,
        last_commit_is_mirror={p: _last_commit_is_mirror(repo, p) for p in repo_files},
        apps_by_path={p: info for p, (_, info) in apps.items()}, paths_on_disk=paths_on_disk, state=state, now=now),
        m)
    report = MirrorReport(plan.exit_code, plan)
    if dry_run:
        return report

    try:
        _apply_git(cfg, repo, plan, state, report)
    except GitError as exc:
        log.error("git apply failed", extra={"error": str(exc)})
        repo.reset_worktree()
        report.exit_code = EXIT_INFRA
        return report  # nothing pushed -> do not touch ArgoCD; the next run replans from scratch
    _apply_argocd(cfg, kube, plan, apps, apps_by_name, report)
    return report


def _apply_git(cfg: Config, repo: GitRepo, plan: Plan, old_state: MirrorState, report: MirrorReport) -> None:
    author = (cfg.git.author_name, cfg.git.author_email)
    for action, items in (("add", plan.add_dir), ("refresh", plan.refresh_dir)):
        for ns, name, text in items:
            target = repo.workdir / ns / name / "deployment.yaml"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            sha = repo.commit([f"{ns}/{name}/deployment.yaml"], _message(action, ns, name), *author,
                              allowed_prefix=f"{ns}/{name}")
            if sha:
                report.commits.append(sha)
    for ns, name in plan.delete_dir:
        path = f"{ns}/{name}"
        repo.run("rm", "-r", "-q", "--", path)
        sha = repo.commit([path], _message("delete", ns, name), *author, allowed_prefix=path)
        if sha:
            report.commits.append(sha)
    new_state = _dump_state(plan.state)
    state_file = repo.workdir / STATE_PATH
    if plan.state.to_dict() != old_state.to_dict() or not state_file.exists():
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(new_state, encoding="utf-8")
        sha = repo.commit([STATE_PATH], "mirror: update state\n\nMirror-Action: state\n", *author,
                          allowed_prefix=".mirror")
        if sha:
            report.commits.append(sha)
    if report.commits:
        repo.push(cfg.git.branch)


def _apply_argocd(cfg: Config, kube: MirrorKube, plan: Plan, apps: dict, apps_by_name: dict,
                  report: MirrorReport) -> None:
    for ns, name in plan.create_app:
        body = application_body(cfg, ns, name)
        aname = body["metadata"]["name"]
        existing = apps_by_name.get(aname)
        if existing is not None:
            src = (existing.get("spec") or {}).get("source") or {}
            if src.get("path", "").strip("/") == f"{ns}/{name}" and \
                    normalize_repo(src.get("repoURL", "")) == normalize_repo(cfg.git.url):
                continue
            report.app_results[aname] = "app_name_conflict"
            if report.exit_code != EXIT_INFRA:
                report.exit_code = EXIT_HALT
            continue
        try:
            kube.create_application(body)
            report.app_results[aname] = "created"
        except Exception as exc:
            log.error("Application create failed", extra={"app": aname, "error": type(exc).__name__})
            report.app_results[aname] = "create_failed"
            report.exit_code = EXIT_INFRA
    for path in plan.delete_app:
        entry = apps.get(path)
        if entry is None or entry[1].multi_source:
            continue
        aname = entry[0]["metadata"]["name"]
        try:
            app = kube.get_application(aname)
            if app is None:
                continue
            meta = app.get("metadata") or {}
            if RESOURCES_FINALIZER in (meta.get("finalizers") or []):
                report.app_results[aname] = "needs_manual_delete"  # DV-A: a delete would cascade
                if report.exit_code != EXIT_INFRA:
                    report.exit_code = EXIT_HALT
                continue
            kube.delete_application(aname, meta["uid"], meta["resourceVersion"])
            report.app_results[aname] = "deleted"
        except Exception as exc:
            log.error("Application delete failed", extra={"app": aname, "error": type(exc).__name__})
            report.app_results[aname] = "delete_failed"
            report.exit_code = EXIT_INFRA


class KubeMirrorClient:
    """MirrorKube backed by the python client (SA gitops-mirror: read Deployments/HPAs, create/delete Applications)."""

    GROUP, VERSION, PLURAL = "argoproj.io", "v1alpha1", "applications"

    def __init__(self, cfg: Config):
        from kubernetes import client, config as kconfig

        try:
            kconfig.load_incluster_config()
        except kconfig.ConfigException:
            kconfig.load_kube_config()
        self.cfg, self.client = cfg, client
        self.api = client.ApiClient()
        self.apps = client.AppsV1Api(self.api)
        self.autoscaling = client.AutoscalingV2Api(self.api)
        self.custom = client.CustomObjectsApi(self.api)
        self.timeout = cfg.mirror.k8s_timeout
        self.argo_ns = cfg.argocd.namespace

    def list_deployments(self, namespace: str) -> list[dict]:
        items = self.apps.list_namespaced_deployment(namespace, _request_timeout=self.timeout).items
        out = []
        for item in items:
            d = self.api.sanitize_for_serialization(item)
            d.setdefault("apiVersion", "apps/v1")
            d["kind"] = "Deployment"
            out.append(d)
        return out

    def list_hpa_targets(self, namespace: str) -> set[str]:
        items = self.autoscaling.list_namespaced_horizontal_pod_autoscaler(namespace,
                                                                          _request_timeout=self.timeout).items
        return {i.spec.scale_target_ref.name for i in items if i.spec.scale_target_ref.kind == "Deployment"}

    def list_applications(self) -> list[dict]:
        return self.custom.list_namespaced_custom_object(self.GROUP, self.VERSION, self.argo_ns, self.PLURAL,
                                                         _request_timeout=self.timeout).get("items", [])

    def create_application(self, body: dict) -> None:
        self.custom.create_namespaced_custom_object(self.GROUP, self.VERSION, self.argo_ns, self.PLURAL, body,
                                                    _request_timeout=self.timeout)

    def get_application(self, name: str) -> dict | None:
        from kubernetes.client.exceptions import ApiException

        try:
            return self.custom.get_namespaced_custom_object(self.GROUP, self.VERSION, self.argo_ns, self.PLURAL, name,
                                                            _request_timeout=self.timeout)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise

    def delete_application(self, name: str, uid: str, resource_version: str) -> None:
        body = self.client.V1DeleteOptions(
            preconditions=self.client.V1Preconditions(uid=uid, resource_version=resource_version),
            propagation_policy="Orphan")
        self.custom.delete_namespaced_custom_object(self.GROUP, self.VERSION, self.argo_ns, self.PLURAL, name,
                                                    body=body, _request_timeout=self.timeout)

