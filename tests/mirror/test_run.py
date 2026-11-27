"""Mirror end-to-end with an in-memory cluster/ArgoCD and a real git remote (M3, M5, M6, M9-M11, M14)."""

import copy
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from rightsizing.config import Config
from rightsizing.gitops import clone, git_env, write_askpass
from rightsizing.mirror.run import EXIT_HALT, EXIT_INFRA, EXIT_OK, RESOURCES_FINALIZER, run_mirror

T0 = datetime(2026, 11, 26, 2, 30, tzinfo=timezone.utc)


def deployment(name, ns="development", image="nginx:1.27", **meta):
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": name, "namespace": ns, "uid": f"u-{name}", "resourceVersion": "1",
                         "labels": {"app": name}, "managedFields": [{"manager": "kubectl"}], **meta},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}},
                     "template": {"metadata": {"labels": {"app": name}, "creationTimestamp": None},
                                  "spec": {"containers": [{"name": "app", "image": image}]}}},
            "status": {"replicas": 1}}


class FakeMirrorKube:
    def __init__(self, deployments=(), apps=()):
        self.deployments = {(d["metadata"]["namespace"], d["metadata"]["name"]): d for d in deployments}
        self.apps = {a["metadata"]["name"]: a for a in apps}
        self.calls: list[tuple] = []
        self.fail = set()  # e.g. {"create", "list:development", "list_apps"}
        self.hpa = {}
        self.bodies: list[dict] = []  # every create_application body

    def list_deployments(self, namespace):
        self.calls.append(("list", namespace))
        if f"list:{namespace}" in self.fail:
            raise RuntimeError("403")
        return [copy.deepcopy(d) for (ns, _), d in self.deployments.items() if ns == namespace]

    def get_deployment(self, namespace, name):
        self.calls.append(("get_deployment", namespace, name))
        d = copy.deepcopy(self.deployments.get((namespace, name)))
        return self.on_get_deployment(d) if getattr(self, "on_get_deployment", None) and d else d

    def list_hpa_targets(self, namespace):
        return set(self.hpa.get(namespace, set()))

    def list_applications(self):
        if "list_apps" in self.fail:
            raise RuntimeError("down")
        return [copy.deepcopy(a) for a in self.apps.values()]

    def create_application(self, body):
        self.calls.append(("create", body["metadata"]["name"]))
        self.bodies.append(copy.deepcopy(body))
        if "create" in self.fail:
            raise RuntimeError("argo down")
        app = copy.deepcopy(body)
        app["metadata"].update(uid=f"au-{body['metadata']['name']}", resourceVersion="1")
        app["status"] = {}
        self.apps[body["metadata"]["name"]] = app

    def get_application(self, name):
        app = copy.deepcopy(self.apps.get(name))
        return self.on_get_application(app) if getattr(self, "on_get_application", None) and app else app

    def delete_application(self, name, uid, resource_version):
        self.calls.append(("delete", name, uid, resource_version))
        app = self.apps[name]
        assert app["metadata"]["uid"] == uid and app["metadata"]["resourceVersion"] == resource_version
        del self.apps[name]

    def mutating_calls(self):
        return [c for c in self.calls if c[0] in ("create", "delete")]


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=h", "-c", "user.email=h@h", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def world(tmp_path):
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "-q", str(bare), str(seed))
    (seed / "development/web").mkdir(parents=True)
    (seed / "development/web/deployment.yaml").write_text(
        "# hand-written\napiVersion: apps/v1\nkind: Deployment\nmetadata: {name: web, namespace: development}\n"
        "spec: {replicas: 2}\n---\napiVersion: v1\nkind: Service\nmetadata: {name: web}\n")
    git(seed, "add", ".")
    git(seed, "commit", "-qm", "init")
    git(seed, "push", "-q", "origin", "main")
    url = f"file://{bare}"
    cfg = Config.model_validate({"git": {"url": url, "branch": "main"},
                                 "argocd": {"tracking_method": "annotation+label"}})  # like the cluster
    web_app = {"metadata": {"name": "web", "uid": "au-web", "resourceVersion": "1"},
               "spec": {"source": {"repoURL": url, "path": "development/web", "targetRevision": "main"}},
               "status": {"history": [{"id": 1}],
                          "resources": [{"kind": "Deployment", "name": "web", "namespace": "development"}]}}
    kube = FakeMirrorKube([deployment("web", labels={"app": "web", "app.kubernetes.io/instance": "web"})],
                          [web_app])
    return {"tmp": tmp_path, "bare": bare, "seed": seed, "cfg": cfg, "kube": kube, "n": 0}


def mirror(world, now=T0, dry_run=False):
    world["n"] += 1
    w = world["tmp"] / f"w{world['n']}"
    (w / "home").mkdir(parents=True)
    repo = clone(world["cfg"].git.url, "main", w / "repo", git_env(write_askpass(w / "bin"), "bot", None, w / "home"))
    return run_mirror(world["cfg"], world["kube"], repo, now, dry_run=dry_run), repo


def remote(world, *args):
    return git(world["bare"], *args)


def remote_file(world, path):
    return subprocess.run(["git", "-C", str(world["bare"]), "show", f"main:{path}"], capture_output=True,
                          text=True).stdout


def test_new_deployment_added_with_manual_application(world):
    world["kube"].deployments[("development", "demo-x")] = deployment("demo-x")
    report, _ = mirror(world)
    assert report.exit_code == EXIT_OK and report.app_results == {"demo-x": "created"}
    text = remote_file(world, "development/demo-x/deployment.yaml")
    doc = YAML(typ="safe").load(text)
    assert "status" not in doc and "uid" not in doc["metadata"] and "managedFields" not in doc["metadata"]
    log = remote(world, "log", "--format=%B", "main")
    assert "Mirror-Action: add\nMirror-Workload: development/Deployment/demo-x" in log
    assert "Mirror-Action: state" in log
    app = world["kube"].apps["demo-x"]
    assert app["spec"]["project"] == "gitops-mirror" and app["spec"]["syncPolicy"] == {}
    assert app["spec"]["source"]["path"] == "development/demo-x" and "finalizers" not in app["metadata"]
    assert app["metadata"]["labels"] == {"rightsizing/managed-by": "gitops-mirror"}
    assert "operation" not in app


def test_existing_app_files_byte_identical_and_rerun_idempotent(world):
    before = remote_file(world, "development/web/deployment.yaml")
    mirror(world)
    head1 = remote(world, "rev-parse", "main")
    report, _ = mirror(world)
    assert remote_file(world, "development/web/deployment.yaml") == before
    assert remote(world, "rev-parse", "main") == head1 and report.commits == []
    assert world["kube"].mutating_calls() == []


def test_only_allowlisted_namespaces_are_read(world):
    world["cfg"].mirror.namespaces = ["development"]
    world["kube"].deployments[("kube-system", "coredns")] = deployment("coredns", ns="kube-system")
    mirror(world)
    assert {c[1] for c in world["kube"].calls if c[0] == "list"} == {"development"}


def test_delete_after_confirmation_non_cascade(world):
    kube = world["kube"]
    kube.deployments[("development", "api")] = deployment("api")
    mirror(world)  # adds api + Application
    kube.apps["api"]["status"] = {"history": [{"id": 1}]}  # a human synced it once
    del kube.deployments[("development", "api")]
    r1, _ = mirror(world, now=T0 + timedelta(days=1))
    assert remote_file(world, "development/api/deployment.yaml") and "api" in kube.apps
    r2, _ = mirror(world, now=T0 + timedelta(days=2))
    assert r2.exit_code == EXIT_OK and r2.app_results == {"api": "deleted"}
    assert remote_file(world, "development/api/deployment.yaml") == ""
    assert ("delete", "api", "au-api", "1") in kube.calls
    assert "Mirror-Action: delete" in remote(world, "log", "-3", "--format=%B", "main")
    assert ("development", "web") in kube.deployments  # cluster untouched


def test_finalizer_application_is_not_deleted(world):
    kube = world["kube"]
    kube.deployments[("development", "api")] = deployment("api")
    mirror(world)
    kube.apps["api"]["status"] = {"history": [{"id": 1}]}
    kube.apps["api"]["metadata"]["finalizers"] = [RESOURCES_FINALIZER]
    del kube.deployments[("development", "api")]
    mirror(world, now=T0 + timedelta(days=1))
    r, _ = mirror(world, now=T0 + timedelta(days=2))
    assert r.exit_code == EXIT_HALT and r.plan.manual["development/api"] == "needs_manual_delete"
    assert "api" in kube.apps and remote_file(world, "development/api/deployment.yaml")  # dir kept (Architect D2)
    r2, _ = mirror(world, now=T0 + timedelta(days=3))  # keeps alerting until a human deletes the app
    assert r2.exit_code == EXIT_HALT and r2.plan.manual["development/api"] == "needs_manual_delete"
    assert not any(c[0] == "delete" for c in kube.calls)


def test_argo_failure_heals_on_next_run(world):
    kube = world["kube"]
    kube.deployments[("development", "demo-x")] = deployment("demo-x")
    kube.fail.add("create")
    r1, _ = mirror(world)
    assert r1.exit_code == EXIT_INFRA and remote_file(world, "development/demo-x/deployment.yaml")
    kube.fail.clear()
    r2, _ = mirror(world)
    assert r2.app_results == {"demo-x": "created"} and r2.exit_code == EXIT_OK


def test_list_error_exit_2_and_no_deletes(world):
    world["kube"].fail.add("list:development")
    r, _ = mirror(world)
    assert r.exit_code == EXIT_INFRA and r.plan.list_errors == ["development"] and not r.plan.delete_dir


def test_name_conflict_is_a_halt(world):
    kube = world["kube"]
    kube.deployments[("development", "dup")] = deployment("dup")
    kube.apps["dup"] = {"metadata": {"name": "dup"},
                        "spec": {"source": {"repoURL": "https://elsewhere/x.git", "path": "y"}}, "status": {}}
    r, _ = mirror(world)
    assert r.exit_code == EXIT_HALT and r.app_results["dup"] == "app_name_conflict"


def test_helm_and_tracked_deployments_are_skipped(world):
    kube = world["kube"]
    kube.deployments[("development", "chart")] = deployment("chart", labels={"app.kubernetes.io/managed-by": "Helm"})
    r, _ = mirror(world)
    assert r.plan.skipped["development/chart"] == "helm_managed"
    assert r.plan.skipped["development/web"] == "already_tracked"
    assert remote_file(world, "development/chart/deployment.yaml") == ""


def test_hpa_target_exported_without_replicas(world):
    kube = world["kube"]
    kube.deployments[("development", "scaled")] = deployment("scaled")
    kube.hpa["development"] = {"scaled"}
    mirror(world)
    assert "replicas" not in YAML(typ="safe").load(remote_file(world, "development/scaled/deployment.yaml"))["spec"]


def test_never_synced_mirror_app_is_refreshed_but_human_edit_is_kept(world):
    kube = world["kube"]
    kube.deployments[("development", "demo-x")] = deployment("demo-x", image="nginx:1.27")
    mirror(world)
    kube.deployments[("development", "demo-x")] = deployment("demo-x", image="nginx:1.28")  # CI rolled a new image
    r, _ = mirror(world)
    assert len(r.plan.refresh_dir) == 1 and "nginx:1.28" in remote_file(world, "development/demo-x/deployment.yaml")
    # a human edits git before the first sync -> never overwritten
    git(world["seed"], "pull", "-q", "origin", "main")
    f = world["seed"] / "development/demo-x/deployment.yaml"
    f.write_text(f.read_text().replace("replicas: 1", "replicas: 2"))
    git(world["seed"], "commit", "-qam", "human tweak")
    git(world["seed"], "push", "-q", "origin", "main")
    kube.deployments[("development", "demo-x")] = deployment("demo-x", image="nginx:1.29")
    r2, _ = mirror(world)
    assert not r2.plan.refresh_dir and r2.plan.skipped["development/demo-x"] == "human_edited"
    assert "replicas: 2" in remote_file(world, "development/demo-x/deployment.yaml")


def test_dry_run_changes_nothing(world):
    world["kube"].deployments[("development", "demo-x")] = deployment("demo-x")
    head = remote(world, "rev-parse", "main")
    r, _ = mirror(world, dry_run=True)
    assert r.plan.add_dir and remote(world, "rev-parse", "main") == head and world["kube"].mutating_calls() == []


def test_cli_help():
    out = subprocess.run([sys.executable, "-m", "rightsizing.mirror", "--help"], capture_output=True, text=True,
                         cwd=Path(__file__).parents[2]).stdout
    assert "--dry-run" in out and "Never syncs" in out



def test_multi_source_application_path_is_occupied_and_never_touched(world):
    kube, url = world["kube"], world["cfg"].git.url
    git(world["seed"], "pull", "-q", "origin", "main")
    (world["seed"] / "development/multi").mkdir()
    (world["seed"] / "development/multi/deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: multi, namespace: development}\nspec: {}\n")
    git(world["seed"], "add", ".")
    git(world["seed"], "commit", "-qm", "multi")
    git(world["seed"], "push", "-q", "origin", "main")
    kube.apps["multi"] = {"metadata": {"name": "multi", "uid": "m", "resourceVersion": "1",
                                       "labels": {"rightsizing/managed-by": "gitops-mirror"}},
                          "spec": {"sources": [{"repoURL": url, "path": "development/multi"},
                                               {"repoURL": "https://charts", "chart": "x"}]},
                          "status": {"history": [{"id": 1}]}}
    r, _ = mirror(world)
    assert ("development", "multi") not in r.plan.create_app and r.exit_code == EXIT_OK
    assert world["kube"].mutating_calls() == []


def test_application_listing_failure_resets_counters(world):
    kube = world["kube"]
    kube.deployments[("development", "api")] = deployment("api")
    mirror(world)
    kube.apps["api"]["status"] = {"history": [{"id": 1}]}
    del kube.deployments[("development", "api")]
    r1, _ = mirror(world, now=T0 + timedelta(days=1))
    assert r1.plan.state.missing["development/api"]["consecutive_ok_runs"] == 1
    kube.fail.add("list_apps")
    r2, _ = mirror(world, now=T0 + timedelta(days=2))
    assert r2.exit_code == EXIT_INFRA and r2.plan.state.missing == {} and kube.mutating_calls()[-1][0] == "create"
    kube.fail.clear()
    r3, _ = mirror(world, now=T0 + timedelta(days=3))
    assert not r3.plan.delete_dir and r3.plan.state.missing["development/api"]["consecutive_ok_runs"] == 1
