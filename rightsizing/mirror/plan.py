"""Pure reconcile planner + the persisted state (`.mirror/state.yaml`). No I/O here: everything is testable.

Deletion is deliberately slow (plan Step 4 + amendments 5-8):
- an app must be missing on >= min_confirmations consecutive SUCCESSFUL lists AND for >= min_missing_interval;
- a listing error for a namespace resets its missing counters and blocks all deletes there;
- an app is only ever deleted if its Deployment was seen live AND its Application has a successful sync;
- the mass-delete guard (count / ratio / empty namespace) halts all deletes of a namespace.
The state holds counters and hashes only (no per-run timestamps), so it is committed only when it changes.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..config import MirrorConfig
from .clean import content_hash, to_yaml
from .diff import semantic_diff
from .scan import RepoScan

STATE_PATH = ".mirror/state.yaml"


@dataclass
class MirrorState:
    seen: set[str] = field(default_factory=set)  # "ns/name" of Deployments observed live
    missing: dict[str, dict] = field(default_factory=dict)  # "ns/name" -> {first_missing_at, consecutive_ok_runs}
    exports: dict[str, str] = field(default_factory=dict)  # "ns/name" -> hash of the last mirror-written manifest

    def to_dict(self) -> dict:
        return {"version": 1, "seen": sorted(self.seen),
                "missing": {k: dict(v) for k, v in sorted(self.missing.items())},
                "exports": dict(sorted(self.exports.items()))}

    @classmethod
    def from_dict(cls, data: dict | None) -> "MirrorState":
        data = data or {}
        return cls(seen=set(data.get("seen") or []), missing={k: dict(v) for k, v in (data.get("missing") or {}).items()},
                   exports=dict(data.get("exports") or {}))


@dataclass
class AppInfo:
    name: str
    managed: bool  # created by the mirror (managed label)
    synced_ever: bool  # status.history non-empty
    operation_pending: bool  # app.operation set, or status.operationState.phase Running/Terminating
    multi_source: bool = False
    has_resources_finalizer: bool = False  # deleting it would cascade (DV-A)
    has_finalizers: bool = False  # any finalizer (a recreate for the first sync needs none)


@dataclass
class Inputs:
    namespaces: list[str]
    cluster_names: dict[str, set[str] | None]  # ns -> every live Deployment name; None = listing failed
    adoptable: dict[str, dict[str, str]]  # ns -> {name: cleaned manifest YAML} (skip_reason was None)
    skipped: dict[str, str]  # "ns/name" -> reason (owned/helm_managed/already_tracked)
    repo: RepoScan
    repo_files: dict[str, str]  # "ns/name" -> current deployment.yaml text (mirror-style apps)
    last_commit_is_mirror: dict[str, bool]  # "ns/name" -> last commit touching the path is Mirror add/refresh
    apps_by_path: dict[str, AppInfo]  # "ns/name" -> Application pointing there
    paths_on_disk: set[str]  # "ns/name" directories that exist in the repo
    state: MirrorState
    now: datetime
    # only filled for auto-first-sync / adopt-drift namespaces
    live_docs: dict[str, dict] = field(default_factory=dict)  # "ns/name" -> clean(live) of an own Deployment
    repo_docs: dict[str, dict] = field(default_factory=dict)  # "ns/name" -> parsed HEAD deployment.yaml
    unsynced_commits: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # -> [(sha, rightsizing|human)]


@dataclass
class Plan:
    add_dir: list[tuple[str, str, str]] = field(default_factory=list)  # (ns, name, yaml)
    refresh_dir: list[tuple[str, str, str]] = field(default_factory=list)
    delete_dir: list[tuple[str, str]] = field(default_factory=list)
    create_app: list[tuple[str, str]] = field(default_factory=list)
    delete_app: list[str] = field(default_factory=list)  # "ns/name" path of the Application to remove
    skipped: dict[str, str] = field(default_factory=dict)
    halts: dict[str, str] = field(default_factory=dict)  # ns -> reason
    manual: dict[str, str] = field(default_factory=dict)  # "ns/name" -> needs a human (DV-A)
    list_errors: list[str] = field(default_factory=list)
    state: MirrorState = field(default_factory=MirrorState)
    # (ns, name, yaml, changes, overwritten unsynced commits [(sha, kind)])
    adopt_dir: list[tuple[str, str, str, list[str], list[tuple[str, str]]]] = field(default_factory=list)
    refresh_overwrote: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # adopt ns only
    first_sync: list[tuple[str, str, str]] = field(default_factory=list)  # (ns, name, create|recreate)
    sync_skipped: dict[str, str] = field(default_factory=dict)  # "ns/name" -> why no first sync / adoption

    @property
    def exit_code(self) -> int:
        if self.list_errors:
            return 2
        return 4 if (self.halts or self.manual) else 0

    def is_empty(self) -> bool:
        return not (self.add_dir or self.refresh_dir or self.delete_dir or self.create_app or self.delete_app
                    or self.adopt_dir or self.first_sync)


def build_plan(inp: Inputs, cfg: MirrorConfig) -> Plan:
    plan = Plan(state=copy.deepcopy(inp.state))
    st = plan.state
    interval = timedelta(hours=cfg.min_missing_interval_hours)
    app_paths = set(inp.apps_by_path)
    plan.skipped.update(inp.skipped)

    for ns in inp.namespaces:
        names = inp.cluster_names.get(ns)
        if names is None:
            plan.list_errors.append(ns)
            for key in [k for k in st.missing if k.startswith(f"{ns}/")]:
                del st.missing[key]  # counters restart after a failed listing
            continue
        adoptable = inp.adoptable.get(ns, {})
        repo_apps = sorted(n for (n_ns, n) in inp.repo.apps if n_ns == ns)
        st.seen |= {f"{ns}/{n}" for n in names}

        # --- add new apps
        for name, text in sorted(adoptable.items()):
            path = f"{ns}/{name}"
            if inp.repo.present(ns, name, app_paths) or path in inp.paths_on_disk:
                continue
            plan.add_dir.append((ns, name, text))
            st.exports[path] = content_hash(text)
            if path not in app_paths:
                plan.create_app.append((ns, name))

        # --- refresh never-synced mirror apps (DV-B, amendment 4)
        for name in repo_apps:
            path = f"{ns}/{name}"
            info, text = inp.apps_by_path.get(path), adoptable.get(name)
            current = inp.repo_files.get(path)
            if not (info and info.managed and text is not None and current is not None) or current == text:
                continue
            if info.synced_ever or info.operation_pending:
                continue  # synced at least once: git owns the content now
            if not inp.last_commit_is_mirror.get(path) or st.exports.get(path) != content_hash(current):
                if ns not in cfg.adopt_drift_namespaces:
                    plan.skipped[path] = "human_edited"
                    continue
                plan.refresh_overwrote[path] = list(inp.unsynced_commits.get(path, []))  # the cluster wins
            plan.refresh_dir.append((ns, name, text))
            st.exports[path] = content_hash(text)

        if ns in cfg.adopt_drift_namespaces:
            _plan_adopt(inp, cfg, plan, ns, repo_apps)
        if ns in cfg.auto_first_sync_namespaces:
            _plan_first_sync(inp, cfg, plan, ns, repo_apps)

        # --- self-heal: every app dir needs an Application; Applications without a dir are removed
        for name in repo_apps:
            if f"{ns}/{name}" not in app_paths:
                plan.create_app.append((ns, name))
        selfheal_deletes = [p for p, info in inp.apps_by_path.items()
                            if p.startswith(f"{ns}/") and info.managed and not info.multi_source
                            and not info.has_resources_finalizer and p not in inp.paths_on_disk
                            and p not in {f"{a[0]}/{a[1]}" for a in plan.add_dir}]

        # --- missing detection
        deletes: list[str] = []
        for name in repo_apps:
            path = f"{ns}/{name}"
            if name in names:
                st.missing.pop(path, None)
                continue
            info = inp.apps_by_path.get(path)
            if path not in st.seen or not (info and info.synced_ever):
                plan.skipped[path] = "never_deployed"
                st.missing.pop(path, None)
                continue
            entry = st.missing.get(path)
            if entry is None:
                st.missing[path] = {"first_missing_at": inp.now.isoformat(timespec="seconds"),
                                    "consecutive_ok_runs": 1}
                continue
            entry["consecutive_ok_runs"] = int(entry.get("consecutive_ok_runs", 0)) + 1
            first = datetime.fromisoformat(entry["first_missing_at"])
            if entry["consecutive_ok_runs"] >= cfg.min_confirmations and inp.now - first >= interval:
                deletes.append(path)

        # --- mass-delete guard (count / ratio / empty namespace), self-heal deletes included
        total = deletes + selfheal_deletes
        if total:
            halt = None
            if len(total) > cfg.max_deletes_per_run:
                halt = f"mass_delete:count {len(total)} > {cfg.max_deletes_per_run}"
            elif (len(repo_apps) >= cfg.min_apps_for_ratio and len(total) >= 2
                  and len(total) / len(repo_apps) > cfg.max_delete_ratio):
                halt = f"mass_delete:ratio {len(total)}/{len(repo_apps)} > {cfg.max_delete_ratio}"
            elif not names and repo_apps:
                halt = "mass_delete:empty_namespace"
            if halt:
                plan.halts[ns] = halt
                continue
        for path in list(deletes):
            info = inp.apps_by_path.get(path)
            if info and (info.has_resources_finalizer or info.multi_source):
                # keep the directory and the missing counter: exit 4 repeats until a human deletes the app
                plan.manual[path] = "needs_manual_delete" if info.has_resources_finalizer else "multi_source"
                deletes.remove(path)
        for path in deletes:
            name = path.split("/", 1)[1]
            plan.delete_dir.append((ns, name))
            if path in app_paths:
                plan.delete_app.append(path)
            st.missing.pop(path, None)
            st.seen.discard(path)
            st.exports.pop(path, None)
        plan.delete_app.extend(selfheal_deletes)
        known = {f"{ns}/{n}" for n in repo_apps} | {f"{ns}/{n}" for n in names}
        for key in [k for k in st.missing if k.startswith(f"{ns}/") and k not in known]:
            del st.missing[key]
        st.seen -= {k for k in st.seen if k.startswith(f"{ns}/") and k not in known}
        for key in [k for k in st.exports if k.startswith(f"{ns}/") and k not in known
                    and k not in {f"{a[0]}/{a[1]}" for a in plan.add_dir}]:
            del st.exports[key]
    return plan


def _plan_adopt(inp: Inputs, cfg: MirrorConfig, plan: Plan, ns: str, repo_apps: list[str]) -> None:
    """Synced mirror apps whose live Deployment drifted from git: commit clean(live) (the cluster wins)."""
    for name in repo_apps:
        path = f"{ns}/{name}"
        info = inp.apps_by_path.get(path)
        if inp.skipped.get(path) == "foreign_tracked":
            plan.sync_skipped[path] = "foreign_tracked"
            continue
        if not (info and info.synced_ever):
            continue  # never synced: refresh / first sync handle it
        live, repo_doc = inp.live_docs.get(path), inp.repo_docs.get(path)
        if live is None or repo_doc is None:
            continue
        changes = semantic_diff(repo_doc, live, cfg.adopt_ignore_annotation_prefixes)
        if not changes:
            continue
        if not info.managed:
            plan.sync_skipped[path] = "not_managed"
        elif info.operation_pending:
            plan.sync_skipped[path] = "operation_pending"
        else:
            text = to_yaml(live)
            plan.adopt_dir.append((ns, name, text, changes, list(inp.unsynced_commits.get(path, []))))
            plan.state.exports[path] = content_hash(text)


def _plan_first_sync(inp: Inputs, cfg: MirrorConfig, plan: Plan, ns: str, repo_apps: list[str]) -> None:
    """Never-synced mirror apps whose git content provably equals live get one SHA-pinned first sync.

    Self-heal creates (a repo dir without an Application) never get one: their content may be human-written."""
    for a_ns, name, _ in plan.add_dir:
        if a_ns == ns and (ns, name) in plan.create_app:
            plan.first_sync.append((ns, name, "create"))
    refreshed = {name for r_ns, name, _ in plan.refresh_dir if r_ns == ns}
    for name in repo_apps:
        path = f"{ns}/{name}"
        info = inp.apps_by_path.get(path)
        if info is None or info.synced_ever:
            continue  # self-heal create, or first sync already done
        live, repo_doc = inp.live_docs.get(path), inp.repo_docs.get(path)
        if inp.skipped.get(path) == "foreign_tracked":
            reason = "foreign_tracked"
        elif not info.managed or info.multi_source:
            reason = "not_managed"
        elif info.operation_pending:
            reason = "operation_pending"
        elif info.has_finalizers or info.has_resources_finalizer:
            reason = "finalizer"
        elif live is None:
            continue  # not live (missing detection handles it)
        elif name in refreshed:
            reason = None  # the mirror wrote clean(live) in this run
        elif not inp.last_commit_is_mirror.get(path):
            reason = "human_last_commit"
        elif repo_doc is None or semantic_diff(repo_doc, live, cfg.adopt_ignore_annotation_prefixes):
            reason = "live_differs"
        else:
            reason = None
        if reason:
            plan.sync_skipped[path] = reason
        else:
            plan.first_sync.append((ns, name, "recreate"))
