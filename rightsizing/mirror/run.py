"""Mirror run: read cluster + repo + ArgoCD + state -> plan -> git commits -> push -> ArgoCD create/delete.

Exit codes: 0 ok | 2 infrastructure (listing, git, ArgoCD API errors) | 4 safety halt
(mass-delete guard, Application with resources finalizer, Application name conflict).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from ruamel.yaml import YAML

from .. import commit_msg
from ..config import Config
from ..discovery import normalize_repo, resolve_app
from ..gitops import GitError, GitRepo
from ..manifest.yamlio import load_safe_all
from ..runrecord import RECORD_VERSION, local_date
from .clean import clean_deployment, skip_reason, to_yaml
from .diff import semantic_diff
from .plan import STATE_PATH, AppInfo, Inputs, MirrorState, Plan, build_plan
from .scan import scan_repo

log = logging.getLogger("rightsizing.mirror")

RESOURCES_FINALIZER = "resources-finalizer.argocd.argoproj.io"
EXIT_OK, EXIT_INFRA, EXIT_HALT = 0, 2, 4
MIRROR_USERNAME = "gitops-mirror"  # operation.initiatedBy of the first sync
CONTENT_ACTIONS = ("add", "refresh", "adopt")  # commits whose file content the mirror wrote
_SHA = re.compile(r"^[0-9a-f]{40}$")


class MirrorKube(Protocol):
    def list_deployments(self, namespace: str) -> list[dict]: ...
    def get_deployment(self, namespace: str, name: str) -> dict | None: ...
    def list_hpa_targets(self, namespace: str) -> set[str]: ...
    def list_applications(self) -> list[dict]: ...
    def create_application(self, body: dict, dry_run: bool = False) -> None: ...
    def get_application(self, name: str) -> dict | None: ...
    def delete_application(self, name: str, uid: str, resource_version: str) -> None: ...


@dataclass
class MirrorReport:
    exit_code: int
    plan: Plan
    commits: list[str] = field(default_factory=list)
    app_results: dict[str, str] = field(default_factory=dict)  # app name -> created|deleted|<problem>
    first_synced: list[str] = field(default_factory=list)  # "ns/name" created with a SHA-pinned sync operation
    sync_skipped: dict[str, str] = field(default_factory=dict)  # "ns/name" -> reason (plan + apply phase)
    recreate_failed: list[str] = field(default_factory=list)  # deleted for the first sync, create failed
    git_applied: bool = True  # False when the git phase failed: the planned git-side actions did not happen

    def summary(self) -> dict:
        p = self.plan
        out = {"added": len(p.add_dir), "refreshed": len(p.refresh_dir), "deleted_dirs": len(p.delete_dir),
               "apps": self.app_results, "missing": {k: v["consecutive_ok_runs"] for k, v in p.state.missing.items()},
               "halts": p.halts, "manual": p.manual, "list_errors": p.list_errors, "skipped": p.skipped, "commits": len(self.commits),
               "exit_code": self.exit_code}
        if p.adopt_dir or self.first_synced or self.sync_skipped or self.recreate_failed:
            out.update(adopted=len(p.adopt_dir), first_synced=self.first_synced, sync_skipped=self.sync_skipped,
                       recreate_failed=self.recreate_failed)
        return out


def mirror_record(cfg: Config, report: MirrorReport, now: datetime) -> dict:
    """Pure: the mirror/YYYY/MM/<date>.json document the daily report reads (dated in the report timezone)."""
    p = report.plan
    tz = cfg.report.timezone if cfg.report else "Asia/Seoul"
    skipped = {**report.sync_skipped, **{path: "recreate_failed" for path in report.recreate_failed}}
    if not report.git_applied:  # nothing was committed or pushed: report the planned items as not done
        skipped.update({f"{ns}/{name}": "git_failed"
                        for items in (p.add_dir, p.adopt_dir, p.refresh_dir) for ns, name, *_ in items})
        skipped.update({f"{ns}/{name}": "git_failed" for ns, name in p.delete_dir})
        p = Plan(state=p.state)
    return {
        "record_version": RECORD_VERSION,
        "date": local_date(now, tz).isoformat(),
        "generated_at": now.isoformat(timespec="seconds"),
        "exit_code": report.exit_code,
        "created": [f"{ns}/{name}" for ns, name, _ in p.add_dir],
        "first_synced": list(report.first_synced),
        "adopted": [{"path": f"{ns}/{name}", "changes": list(changes),
                     "overwrote": [{"sha": sha, "kind": kind} for sha, kind in overwrote]}
                    for ns, name, _, changes, overwrote in p.adopt_dir],
        "refreshed": [{"path": f"{ns}/{name}",
                       "overwrote": [{"sha": sha, "kind": kind}
                                     for sha, kind in p.refresh_overwrote.get(f"{ns}/{name}", [])]}
                      for ns, name, _ in p.refresh_dir],
        "skipped": [{"path": path, "reason": reason} for path, reason in sorted(skipped.items())],
        "deleted": [f"{ns}/{name}" for ns, name in p.delete_dir],
        "recreate_failed": list(report.recreate_failed),
    }


def merge_mirror_records(old: dict, new: dict) -> dict:
    """Combine two mirror records of the same day (several runs, e.g. a manual Job after the nightly one):
    list entries accumulate (later entries for the same path win), scalars come from the newer run."""
    if old.get("record_version") != new.get("record_version") or old.get("date") != new.get("date"):
        return new
    out = dict(new)
    for key, value in new.items():
        if not isinstance(value, list):
            continue
        merged: dict[str, object] = {}
        for item in list(old.get(key) or []) + value:
            merged[item["path"] if isinstance(item, dict) else item] = item
        out[key] = list(merged.values())
    done = set(out.get("first_synced") or []) | {a["path"] for a in out.get("adopted") or []}
    out["skipped"] = [s for s in out.get("skipped") or [] if s["path"] not in done]
    out["recreate_failed"] = [p for p in out.get("recreate_failed") or [] if p not in done]
    out["runs"] = int(old.get("runs", 1)) + 1
    out["exit_code"] = max(int(old.get("exit_code", 0)), int(new.get("exit_code", 0)))
    return out


def app_name(cfg: Config, ns: str, name: str) -> str:
    return cfg.mirror.app_name_template.format(ns=ns, name=name)


def application_body(cfg: Config, ns: str, name: str, sync_revision: str | None = None) -> dict:
    """Manual-sync Application. With `sync_revision` (auto-first-sync namespaces only) it also requests the one
    first sync, pinned to that commit SHA (the ValidatingAdmissionPolicy admits nothing else)."""
    m = cfg.mirror
    body = {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
            "metadata": {"name": app_name(cfg, ns, name), "namespace": cfg.argocd.namespace,
                         "labels": {m.managed_label_key: m.managed_label_value}},
            "spec": {"project": m.argocd_project,
                     "source": {"repoURL": cfg.git.url, "targetRevision": cfg.git.branch, "path": f"{ns}/{name}"},
                     "destination": {"server": m.destination_server, "namespace": ns},
                     "syncPolicy": {}}}  # manual sync only: humans approve every later sync
    if sync_revision is not None:
        if not _SHA.match(sync_revision):
            raise ValueError(f"first sync needs a 40-hex commit sha, got {sync_revision!r}")
        body["operation"] = {"initiatedBy": {"username": MIRROR_USERNAME},
                             "sync": {"revision": sync_revision, "syncStrategy": {"apply": {}}}}
    return body


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
                synced_ever=bool(status.get("history")), operation_pending=_operation_pending(app),
                multi_source=multi, has_resources_finalizer=RESOURCES_FINALIZER in (meta.get("finalizers") or []),
                has_finalizers=bool(meta.get("finalizers"))))
    return out


def _operation_pending(app: dict) -> bool:
    """A sync is requested or running. A finished operationState (every synced app has one) is not pending."""
    phase = ((app.get("status") or {}).get("operationState") or {}).get("phase")
    return bool(app.get("operation")) or phase in ("Running", "Terminating")


def _load_state(repo_dir: Path) -> MirrorState:
    path = repo_dir / STATE_PATH
    if not path.exists():
        return MirrorState()
    return MirrorState.from_dict(YAML(typ="safe").load(path.read_text(encoding="utf-8")))


def _dump_state(state: MirrorState) -> str:
    return "# Managed by rightsizing.mirror - see README 'GitOps mirror'.\n" + to_yaml(state.to_dict())


def _is_mirror_message(body: str) -> bool:
    return any(line.strip() in {f"Mirror-Action: {a}" for a in CONTENT_ACTIONS} for line in body.splitlines())


def _last_commit_is_mirror(repo: GitRepo, path: str) -> bool:
    return _is_mirror_message(repo.run("log", "-1", "--format=%B", "--", path, check=False).stdout)


def _unsynced_commits(repo: GitRepo, path: str) -> list[tuple[str, str]]:
    """Commits on the path since its last mirror content commit, newest first: (sha, rightsizing|human)."""
    out = repo.run("log", "--format=%H%x1f%B%x1e", "--", path, check=False).stdout
    commits: list[tuple[str, str]] = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, body = record.split("\x1f", 1)
        if _is_mirror_message(body):
            break
        commits.append((sha, "rightsizing" if commit_msg.parse_trailers(body).workload else "human"))
    return commits


def _deployment_doc(text: str) -> dict | None:
    """The single Deployment of a mirror-style file; None for multi-document files (never adopted)."""
    docs = load_safe_all(text)
    return docs[0] if len(docs) == 1 and docs[0].get("kind") == "Deployment" else None


def _overwrite_lines(commits: list[tuple[str, str]]) -> str:
    return "".join(f"\noverwrote {'pending proposal' if kind == 'rightsizing' else 'human edit'} {sha}"
                   for sha, kind in commits)


def _message(action: str, ns: str, name: str, extra: str = "", trailers: str = "") -> str:
    explain = {
        "add": "클러스터에 새로 생긴 Deployment를 등록합니다. ArgoCD Application은 수동 sync로 생성되며, "
               "첫 Sync도 사람이 해야 rightsizing 대상이 됩니다.",
        "refresh": "아직 한 번도 Sync되지 않은 미러 앱을 현재 클러스터 내용으로 갱신합니다 (첫 Sync 때 롤백 방지).",
        "adopt": "kubectl 등으로 바뀐 클러스터 내용을 git에 반영합니다 (클러스터가 기준). Sync하지 않으며 "
                 "pod는 재시작되지 않습니다.",
        "delete": "클러스터에서 사라진 Deployment를 정리합니다 (연속 확인 완료). ArgoCD Application은 "
                  "non-cascade로 삭제되며 클러스터 리소스는 건드리지 않습니다. 되돌리려면 이 commit을 revert하고 "
                  "Deployment를 다시 만드세요.",
    }[action]
    return (f"mirror({ns}/{name}): {action}\n\n{explain}{extra}\n\n"
            f"Mirror-Action: {action}\nMirror-Workload: {ns}/Deployment/{name}\n{trailers}")


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
    live_docs: dict[str, dict] = {}
    hpa_by_ns: dict[str, set[str]] = {}
    opted = set(m.auto_first_sync_namespaces) | set(m.adopt_drift_namespaces)
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
        hpa_by_ns[ns] = hpa_targets
        adoptable[ns] = {}
        for d in deployments:
            name, path = d["metadata"]["name"], f"{ns}/{d['metadata']['name']}"
            reason = skip_reason(d, lambda o: resolve_app(o, cfg),
                                 lambda app_ns, app: apps_by_name.get(app), m.skip_helm)
            if reason == "already_tracked" and ns in opted:
                if _own_mirror_app(cfg, d, apps.get(path)):
                    live_docs[path] = clean_deployment(d, app_name(cfg, ns, name), name in hpa_targets)
                else:
                    reason = "foreign_tracked"  # another Application owns it: never adopted
            if reason:
                skipped[path] = reason
                continue
            doc = clean_deployment(d, app_name(cfg, ns, name), name in hpa_targets)
            adoptable[ns][name] = to_yaml(doc)
            if ns in opted:
                live_docs[path] = doc

    scan = scan_repo(repo.workdir, m.namespaces)
    paths_on_disk = {f"{ns}/{p.name}" for ns in m.namespaces if (repo.workdir / ns).is_dir()
                     for p in (repo.workdir / ns).iterdir() if p.is_dir() and not p.name.startswith(".")}
    repo_files = {path: (repo.workdir / path / "deployment.yaml").read_text(encoding="utf-8")
                  for path in scan.apps.values() if (repo.workdir / path / "deployment.yaml").is_file()}
    repo_docs = {p: doc for p, text in repo_files.items()
                 if p.split("/", 1)[0] in opted and (doc := _deployment_doc(text)) is not None}
    unsynced = {p: _unsynced_commits(repo, p) for p in repo_files if p.split("/", 1)[0] in m.adopt_drift_namespaces}
    try:
        state = _load_state(repo.workdir)
    except Exception as exc:  # corrupt state file: refuse to guess (deletes depend on it)
        log.error("cannot read mirror state", extra={"error": type(exc).__name__})
        return MirrorReport(EXIT_INFRA, Plan(list_errors=list(m.namespaces)))
    plan = build_plan(Inputs(
        namespaces=list(m.namespaces), cluster_names=cluster_names, adoptable=adoptable, skipped=skipped, repo=scan,
        repo_files=repo_files,
        last_commit_is_mirror={p: _last_commit_is_mirror(repo, p) for p in repo_files},
        apps_by_path={p: info for p, (_, info) in apps.items()}, paths_on_disk=paths_on_disk, state=state, now=now,
        live_docs=live_docs, repo_docs=repo_docs, unsynced_commits=unsynced),
        m)
    report = MirrorReport(plan.exit_code, plan, sync_skipped=dict(plan.sync_skipped))
    if dry_run:
        return report

    try:
        _apply_git(cfg, repo, plan, state, report)
    except GitError as exc:
        log.error("git apply failed", extra={"error": str(exc)})
        repo.reset_worktree()
        report.exit_code = EXIT_INFRA
        report.git_applied = False
        return report  # nothing pushed -> do not touch ArgoCD; the next run replans from scratch
    recheck = _Recheck(cfg, kube, repo, repo.head(), hpa_by_ns) if plan.first_sync else None
    _apply_argocd(cfg, kube, plan, apps, apps_by_name, report, recheck)
    if recheck is not None:
        _apply_recreate(cfg, kube, plan, report, recheck)
    return report


def _own_mirror_app(cfg: Config, deployment: dict, entry: tuple[dict, AppInfo] | None) -> bool:
    """The Deployment is tracked by the mirror-managed Application of its own path."""
    ref = resolve_app(deployment, cfg)
    return bool(entry and ref and entry[1].managed and ref[1] == entry[1].name
                and ref[0] == ((entry[0].get("metadata") or {}).get("namespace") or cfg.argocd.namespace))


def _apply_git(cfg: Config, repo: GitRepo, plan: Plan, old_state: MirrorState, report: MirrorReport) -> None:
    author = (cfg.git.author_name, cfg.git.author_email)
    content = [("add", ns, name, text, [], []) for ns, name, text in plan.add_dir]
    content += [("refresh", ns, name, text, [], plan.refresh_overwrote.get(f"{ns}/{name}", []))
                for ns, name, text in plan.refresh_dir]
    content += [("adopt", ns, name, text, changes, overwrote) for ns, name, text, changes, overwrote in plan.adopt_dir]
    for action, ns, name, text, changes, overwrote in content:
        target = repo.workdir / ns / name / "deployment.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        extra = ("\n\n변경:" + "".join(f"\n- {c}" for c in changes) if changes else "") + \
            ("\n" + _overwrite_lines(overwrote) if overwrote else "")
        trailers = f"Mirror-Overwrote: {','.join(sha for sha, _ in overwrote)}\n" if overwrote else ""
        sha = repo.commit([f"{ns}/{name}/deployment.yaml"], _message(action, ns, name, extra, trailers), *author,
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


@dataclass
class _Recheck:
    """Right before a first sync: clean(live), re-read now, must equal the file at the pushed SHA."""
    cfg: Config
    kube: MirrorKube
    repo: GitRepo
    pushed: str
    hpa_by_ns: dict[str, set[str]]

    def live_matches(self, ns: str, name: str) -> bool:
        try:
            live = self.kube.get_deployment(ns, name)
            text = self.repo.run("show", f"{self.pushed}:{ns}/{name}/deployment.yaml").stdout
            pushed = _deployment_doc(text)
        except Exception as exc:
            log.warning("first-sync re-check failed", extra={"path": f"{ns}/{name}", "error": type(exc).__name__})
            return False
        if live is None or pushed is None:
            return False
        doc = clean_deployment(live, app_name(self.cfg, ns, name), name in self.hpa_by_ns.get(ns, set()))
        return not semantic_diff(pushed, doc, self.cfg.mirror.adopt_ignore_annotation_prefixes)


def _apply_argocd(cfg: Config, kube: MirrorKube, plan: Plan, apps: dict, apps_by_name: dict,
                  report: MirrorReport, recheck: _Recheck | None = None) -> None:
    first = {(ns, name) for ns, name, mode in plan.first_sync if mode == "create"}
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
        revision = None
        if recheck is not None and (ns, name) in first:
            if recheck.live_matches(ns, name):
                revision = recheck.pushed
            else:
                report.sync_skipped[f"{ns}/{name}"] = "live_changed"  # created plain; retried next night
        try:
            try:
                kube.create_application(application_body(cfg, ns, name, revision))
            except Exception as exc:
                if not revision or getattr(exc, "status", None) not in _ADMISSION_DENIED:
                    raise
                # the admission policy refused the first sync (e.g. out/20 not re-applied): register it plain
                log.error("first sync refused by admission, creating without it",
                          extra={"app": aname, "error": _api_error(exc)})
                report.sync_skipped[f"{ns}/{name}"] = "admission_denied"
                revision = None
                kube.create_application(application_body(cfg, ns, name))
            report.app_results[aname] = "created"
            if revision:
                report.first_synced.append(f"{ns}/{name}")
        except Exception as exc:
            log.error("Application create failed", extra={"app": aname, "error": _api_error(exc)})
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


_ADMISSION_DENIED = (400, 403, 422)  # Invalid / Forbidden: the ValidatingAdmissionPolicy (or RBAC) said no


def _api_error(exc: Exception) -> str:
    """Short, loggable cause of a Kubernetes API error (status + server message), never the request body."""
    status = getattr(exc, "status", None)
    message = ""
    body = getattr(exc, "body", None)
    if body:
        try:
            message = json.loads(body).get("message", "")
        except (ValueError, AttributeError, TypeError):
            message = str(body)
    text = f"{type(exc).__name__}" + (f" {status}" if status else "") + (f": {message}" if message else "")
    return text[:400]


def _recreate_guard(cfg: Config, app: dict | None, path: str) -> str | None:
    """Re-asserted on the freshly read Application right before the orphan delete."""
    if app is None:
        return "app_missing"
    meta, status, spec = app.get("metadata") or {}, app.get("status") or {}, app.get("spec") or {}
    src_path = (spec.get("source") or {}).get("path", "").strip("/")
    if not _is_managed(cfg, app) or spec.get("sources") or src_path != path:
        return "not_managed"
    if status.get("history"):
        return "already_synced"
    if _operation_pending(app):
        return "operation_pending"
    if meta.get("finalizers"):
        return "finalizer"
    return None


def _apply_recreate(cfg: Config, kube: MirrorKube, plan: Plan, report: MirrorReport, recheck: _Recheck) -> None:
    """Existing never-synced mirror apps: orphan delete (uid/rv precondition), then create with the operation."""
    if not _SHA.match(recheck.pushed):  # checked before any delete: a bad revision must not cost an app
        raise ValueError(f"pushed revision is not a commit SHA: {recheck.pushed!r}")
    for ns, name, mode in plan.first_sync:
        if mode != "recreate":
            continue
        path, aname = f"{ns}/{name}", app_name(cfg, ns, name)
        if not recheck.live_matches(ns, name):
            report.sync_skipped[path] = "live_changed"
            continue
        try:
            app = kube.get_application(aname)
        except Exception as exc:
            log.error("Application read failed", extra={"app": aname, "error": _api_error(exc)})
            report.sync_skipped[path] = "app_read_failed"
            report.exit_code = EXIT_INFRA
            continue
        guard = _recreate_guard(cfg, app, path)
        if guard:
            report.sync_skipped[path] = guard
            continue
        meta = app["metadata"]
        body = application_body(cfg, ns, name, recheck.pushed)
        try:  # admission is evaluated before the name check: AlreadyExists (409) means the policy admits it
            kube.create_application(body, dry_run=True)
        except Exception as exc:
            if getattr(exc, "status", None) != 409:
                log.error("first sync would be refused, keeping the Application",
                          extra={"app": aname, "error": _api_error(exc)})
                report.sync_skipped[path] = ("admission_denied" if getattr(exc, "status", None) in _ADMISSION_DENIED
                                             else "precheck_failed")
                if getattr(exc, "status", None) not in _ADMISSION_DENIED:
                    report.exit_code = EXIT_INFRA
                continue
        try:
            kube.delete_application(aname, meta["uid"], meta["resourceVersion"])
        except Exception as exc:  # e.g. a human synced in between (resourceVersion precondition)
            log.warning("first-sync delete failed", extra={"app": aname, "error": _api_error(exc)})
            report.sync_skipped[path] = "delete_failed"
            continue
        try:
            kube.create_application(body)
        except Exception as exc:  # the next run's self-heal recreates it without an operation
            log.error("first-sync create failed after delete", extra={"app": aname, "error": _api_error(exc)})
            report.app_results[aname] = "recreate_failed"
            report.recreate_failed.append(path)
            report.exit_code = EXIT_INFRA
            continue
        report.app_results[aname] = "recreated"
        report.first_synced.append(path)


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

    def get_deployment(self, namespace: str, name: str) -> dict | None:
        from kubernetes.client.exceptions import ApiException

        try:
            item = self.apps.read_namespaced_deployment(name, namespace, _request_timeout=self.timeout)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise
        d = self.api.sanitize_for_serialization(item)
        d.setdefault("apiVersion", "apps/v1")
        d["kind"] = "Deployment"
        return d

    def list_hpa_targets(self, namespace: str) -> set[str]:
        items = self.autoscaling.list_namespaced_horizontal_pod_autoscaler(namespace,
                                                                          _request_timeout=self.timeout).items
        return {i.spec.scale_target_ref.name for i in items if i.spec.scale_target_ref.kind == "Deployment"}

    def list_applications(self) -> list[dict]:
        return self.custom.list_namespaced_custom_object(self.GROUP, self.VERSION, self.argo_ns, self.PLURAL,
                                                         _request_timeout=self.timeout).get("items", [])

    def create_application(self, body: dict, dry_run: bool = False) -> None:
        extra = {"dry_run": "All"} if dry_run else {}
        self.custom.create_namespaced_custom_object(self.GROUP, self.VERSION, self.argo_ns, self.PLURAL, body,
                                                    _request_timeout=self.timeout, **extra)

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

