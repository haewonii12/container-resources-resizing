"""Rightsizing after a mirror adopt commit: superseded, not suppressed, invisible to History (US-008)."""

import subprocess

from ruamel.yaml import YAML

from rightsizing.gitops import SUPERSEDED, GitRepo, History, pending_state
from rightsizing.mirror.clean import to_yaml
from rightsizing.mirror.run import _message
from rightsizing.models import ContainerResources

PROPOSAL = ("rightsizing(development/web): app cpu.request 500m→100m\n\nRightsizing-Id: 20261126-development-"
            "deployment-web\nRightsizing-Workload: development/Deployment/web\n"
            "Rightsizing-Change: app.cpu.request=500m->100m,shrink\nRightsizing-Run: r1\n")


def manifest(cpu):
    return to_yaml({"apiVersion": "apps/v1", "kind": "Deployment",
                    "metadata": {"name": "web", "namespace": "development"},
                    "spec": {"template": {"spec": {"containers": [
                        {"name": "app", "image": "nginx", "resources": {"requests": {"cpu": cpu}}}]}}}})


def resources(text):
    doc = YAML(typ="safe").load(text)
    return {c["name"]: ContainerResources.from_k8s(c.get("resources"))
            for c in doc["spec"]["template"]["spec"]["containers"]}


def commit(repo, text, message):
    path = repo.workdir / "development/web/deployment.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return repo.commit(["development/web/deployment.yaml"], message, "bot", "bot@x", allowed_prefix="development/web")


def test_adopt_over_pending_proposal_is_superseded_and_not_suppressed(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    repo = GitRepo(tmp_path, {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1"})
    commit(repo, manifest("500m"), _message("add", "development", "web"))
    commit(repo, manifest("100m"), PROPOSAL)  # pending: never synced, live still 500m
    live = resources(manifest("750m"))  # somebody ran kubectl set resources
    commit(repo, manifest("750m"), _message("adopt", "development", "web", "\n\n변경:\n- cpu.request[app] 100m→750m",
                                            "Mirror-Overwrote: x\n"))

    hist = History.scan(repo)
    assert [c.kind for c in hist.commits] == ["propose"]  # mirror add/adopt commits are ignored
    proposal = hist.latest_unreverted("development/Deployment/web")
    head = resources((tmp_path / "development/web/deployment.yaml").read_text())
    assert pending_state(proposal, head, live, "Synced") == SUPERSEDED
    assert not hist.is_suppressed("development/Deployment/web", "app", "cpu.request", "shrink", 14)
    assert hist.reverts == {}
