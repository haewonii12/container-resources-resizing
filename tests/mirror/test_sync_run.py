"""Apply: adopt commits and the SHA-pinned first sync with its re-check (US-003, US-005, US-007)."""

import json
import subprocess
from datetime import timedelta

import pytest

from rightsizing.config import Config, ReportConfig
from rightsizing.mirror import __main__ as mirror_cli
from rightsizing.mirror.run import (EXIT_INFRA, EXIT_OK, MirrorReport, _apply_recreate, _last_commit_is_mirror,
                                    application_body, merge_mirror_records, mirror_record)
from tests.mirror.test_run import T0, deployment, git, mirror, remote, remote_file, world  # noqa: F401

SHA_LEN = 40


def head(world):
    return remote(world, "rev-parse", "main").strip()


def argo_syncs(kube, name):
    """What ArgoCD does with a requested first sync: run it, clear operation, record history, track the live
    Deployment (instance label)."""
    app = kube.apps[name]
    app.pop("operation", None)
    app["status"] = {"history": [{"id": 1}], "operationState": {"phase": "Succeeded"},
                     "resources": [{"kind": "Deployment", "name": name, "namespace": "development"}]}
    kube.deployments[("development", name)]["metadata"]["labels"]["app.kubernetes.io/instance"] = name


def auto(world):
    world["cfg"].mirror.auto_first_sync_namespaces = ["development"]


def adopt(world):
    world["cfg"].mirror.adopt_drift_namespaces = ["development"]


# ------------------------------------------------------------------ first sync

def test_application_body_operation_is_pinned_to_a_sha():
    cfg = Config()
    assert "operation" not in application_body(cfg, "development", "web")
    body = application_body(cfg, "development", "web", "a" * SHA_LEN)
    assert body["operation"] == {"initiatedBy": {"username": "gitops-mirror"},
                                 "sync": {"revision": "a" * SHA_LEN, "syncStrategy": {"apply": {}}}}
    with pytest.raises(ValueError):
        application_body(cfg, "development", "web", "main")


def test_new_deployment_is_created_and_first_synced_in_one_run(world):
    auto(world)
    world["kube"].deployments[("development", "demo-x")] = deployment("demo-x")
    report, _ = mirror(world)
    assert report.exit_code == EXIT_OK and report.first_synced == ["development/demo-x"]
    app = world["kube"].apps["demo-x"]
    assert app["operation"]["sync"]["revision"] == head(world) and len(head(world)) == SHA_LEN
    assert app["spec"]["syncPolicy"] == {}
    assert ("get_deployment", "development", "demo-x") in world["kube"].calls  # re-read right before


def test_live_changed_before_create_means_plain_create(world):
    auto(world)
    kube = world["kube"]
    kube.deployments[("development", "demo-x")] = deployment("demo-x")

    def scaled(d):
        d["spec"]["replicas"] = 4  # somebody scaled it between the listing and the sync
        return d
    kube.on_get_deployment = scaled
    report, _ = mirror(world)
    assert report.app_results == {"demo-x": "created"} and report.first_synced == []
    assert "operation" not in kube.apps["demo-x"] and report.sync_skipped["development/demo-x"] == "live_changed"
    rec = mirror_record(world["cfg"], report, T0)
    assert {"path": "development/demo-x", "reason": "live_changed"} in rec["skipped"]


def test_never_synced_app_is_recreated_with_operation(world):
    kube = world["kube"]
    kube.deployments[("development", "demo-x")] = deployment("demo-x")
    mirror(world)  # lists empty: plain app, needs a human first sync today
    assert "operation" not in kube.apps["demo-x"]
    auto(world)
    report, _ = mirror(world, now=T0 + timedelta(days=1))
    assert report.app_results == {"demo-x": "recreated"} and report.first_synced == ["development/demo-x"]
    assert ("delete", "demo-x", "au-demo-x", "1") in kube.calls
    assert kube.apps["demo-x"]["operation"]["sync"]["revision"] == head(world)
    argo_syncs(kube, "demo-x")
    again, _ = mirror(world, now=T0 + timedelta(days=2))
    assert again.commits == [] and kube.mutating_calls()[-1][0] == "create" and again.first_synced == []
    assert len([c for c in kube.calls if c[0] == "delete"]) == 1


def test_recreate_guard_reasserted_on_fresh_application(world):
    kube = world["kube"]
    kube.deployments[("development", "demo-x")] = deployment("demo-x")
    mirror(world)
    auto(world)

    def human_synced(app):
        app["status"] = {"history": [{"id": 1}]}  # a human clicked Sync after the listing
        return app
    kube.on_get_application = human_synced
    report, _ = mirror(world, now=T0 + timedelta(days=1))
    assert report.sync_skipped["development/demo-x"] == "already_synced"
    assert not any(c[0] == "delete" for c in kube.calls)


def test_create_failure_after_delete_is_recreate_failed_and_selfheals(world):
    kube = world["kube"]
    kube.deployments[("development", "demo-x")] = deployment("demo-x")
    mirror(world)
    auto(world)
    kube.fail.add("create")
    report, _ = mirror(world, now=T0 + timedelta(days=1))
    assert report.exit_code == EXIT_INFRA and report.recreate_failed == ["development/demo-x"]
    assert report.app_results["demo-x"] == "recreate_failed" and "demo-x" not in kube.apps
    assert mirror_record(world["cfg"], report, T0)["recreate_failed"] == ["development/demo-x"]
    kube.fail.clear()
    healed, _ = mirror(world, now=T0 + timedelta(days=2))
    assert healed.app_results == {"demo-x": "created"} and "operation" not in kube.apps["demo-x"]  # self-heal


def test_human_last_commit_blocks_recreate(world):
    kube = world["kube"]
    kube.deployments[("development", "demo-x")] = deployment("demo-x")
    mirror(world)
    git(world["seed"], "pull", "-q", "origin", "main")
    f = world["seed"] / "development/demo-x/deployment.yaml"
    f.write_text(f.read_text() + "# reviewed\n")  # semantically equal, but a human touched it
    git(world["seed"], "commit", "-qam", "human note")
    git(world["seed"], "push", "-q", "origin", "main")
    auto(world)
    report, _ = mirror(world, now=T0 + timedelta(days=1))
    assert report.sync_skipped["development/demo-x"] == "human_last_commit" and report.first_synced == []


# ------------------------------------------------------------------ adopt

def _synced_demo(world):
    kube = world["kube"]
    kube.deployments[("development", "demo-x")] = deployment("demo-x")
    mirror(world)
    argo_syncs(kube, "demo-x")
    adopt(world)
    return kube


def test_kubectl_drift_is_adopted_once(world):
    kube = _synced_demo(world)
    d = kube.deployments[("development", "demo-x")]
    d["spec"]["replicas"] = 3
    c = d["spec"]["template"]["spec"]["containers"][0]
    c["image"] = "nginx:1.28"
    c["env"] = [{"name": "TOKEN", "value": "s3cr3t"}]
    c["resources"] = {"requests": {"cpu": "250m"}}
    before = len(kube.mutating_calls())
    report, _ = mirror(world, now=T0 + timedelta(days=1))
    assert [a[:2] for a in report.plan.adopt_dir] == [("development", "demo-x")]
    body = remote(world, "log", "-1", "--format=%B", "main", "--", "development/demo-x")
    assert "Mirror-Action: adopt" in body and "s3cr3t" not in body
    for line in ("- replicas 1→3", "- env[app] TOKEN added", "- image[app] nginx:1.27→nginx:1.28",
                 "- cpu.request[app] -→250m"):
        assert line in body
    assert "replicas: 3" in remote_file(world, "development/demo-x/deployment.yaml")
    assert len(kube.mutating_calls()) == before  # git only: no ArgoCD write, no sync
    again, _ = mirror(world, now=T0 + timedelta(days=2))
    assert again.commits == [] and again.plan.adopt_dir == []


def test_adopt_overwrites_pending_proposal_and_human_edit(world):
    kube = _synced_demo(world)
    seed = world["seed"]
    git(seed, "pull", "-q", "origin", "main")
    f = seed / "development/demo-x/deployment.yaml"
    f.write_text(f.read_text().replace("image: nginx:1.27",
                                       "image: nginx:1.27\n          resources: {requests: {cpu: 100m}}"))
    git(seed, "commit", "-qam", "rightsizing(development/demo-x): cpu\n\nRightsizing-Id: x\n"
        "Rightsizing-Workload: development/Deployment/demo-x\nRightsizing-Change: app.cpu.request=->100m,set\n"
        "Rightsizing-Run: r1")
    proposal = git(seed, "rev-parse", "HEAD").strip()
    f.write_text(f.read_text().replace("replicas: 1", "replicas: 2"))
    git(seed, "commit", "-qam", "human edit in gitea")
    human = git(seed, "rev-parse", "HEAD").strip()
    git(seed, "push", "-q", "origin", "main")
    report, repo = mirror(world, now=T0 + timedelta(days=1))
    (item,) = report.plan.adopt_dir
    assert item[4] == [(human, "human"), (proposal, "rightsizing")]
    body = remote(world, "log", "-1", "--format=%B", "main", "--", "development/demo-x")
    assert f"overwrote human edit {human}" in body and f"overwrote pending proposal {proposal}" in body
    assert f"Mirror-Overwrote: {human},{proposal}" in body
    repo.run("pull", "-q", "origin", "main")
    assert _last_commit_is_mirror(repo, "development/demo-x")
    rec = mirror_record(world["cfg"], report, T0)
    assert rec["adopted"][0]["overwrote"] == [{"sha": human, "kind": "human"},
                                              {"sha": proposal, "kind": "rightsizing"}]
    assert kube.apps["demo-x"]["status"]["history"]  # no ArgoCD write


def test_foreign_tracked_deployment_is_not_adopted(world):
    adopt(world)
    kube = world["kube"]
    # web is tracked by the non-managed Application "web": never adopted, even with drift
    kube.deployments[("development", "web")]["spec"]["replicas"] = 5
    report, _ = mirror(world)
    assert report.plan.skipped["development/web"] == "foreign_tracked" and report.plan.adopt_dir == []


# ------------------------------------------------------------------ mirror record

def _bare(tmp, name):
    bare = tmp / f"{name}.git"
    git(tmp, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp / f"{name}-seed"
    git(tmp, "clone", "-q", str(bare), str(seed))
    (seed / "README.md").write_text("x\n")
    git(seed, "add", ".")
    git(seed, "commit", "-qm", "init")
    git(seed, "push", "-q", "origin", "main")
    return bare


def test_mirror_record_written_to_report_repo(world):
    auto(world)
    world["kube"].deployments[("development", "demo-x")] = deployment("demo-x")
    report, _ = mirror(world)
    cfg = world["cfg"].model_copy(update={"report": ReportConfig(git_url=f"file://{_bare(world['tmp'], 'report')}")})
    mirror_cli.write_mirror_record(cfg, report, T0, world["tmp"] / "rec", None)
    text = subprocess.run(["git", "-C", str(world["tmp"] / "report.git"), "show",
                           "main:mirror/2026/11/2026-11-26.json"], capture_output=True, text=True).stdout
    rec = json.loads(text)
    assert rec["created"] == ["development/demo-x"] and rec["first_synced"] == ["development/demo-x"]
    assert set(rec) >= {"adopted", "skipped", "deleted", "recreate_failed", "date"}


def test_second_run_same_day_keeps_earlier_actions(world):
    auto(world)
    world["kube"].deployments[("development", "demo-x")] = deployment("demo-x")
    first, _ = mirror(world)
    second, _ = mirror(world)  # nothing to do: an empty run must not wipe the first run's actions
    cfg = world["cfg"].model_copy(update={"report": ReportConfig(git_url=f"file://{_bare(world['tmp'], 'report')}")})
    mirror_cli.write_mirror_record(cfg, first, T0, world["tmp"] / "rec1", None)
    mirror_cli.write_mirror_record(cfg, second, T0, world["tmp"] / "rec2", None)
    text = subprocess.run(["git", "-C", str(world["tmp"] / "report.git"), "show",
                           "main:mirror/2026/11/2026-11-26.json"], capture_output=True, text=True).stdout
    rec = json.loads(text)
    assert rec["created"] == ["development/demo-x"] and rec["first_synced"] == ["development/demo-x"]
    assert rec["runs"] == 2


def test_merge_mirror_records_later_entry_wins_and_other_days_replace():
    from rightsizing.mirror.run import merge_mirror_records
    old = {"record_version": 1, "date": "2026-11-26", "exit_code": 2, "created": ["a/x"],
           "skipped": [{"path": "a/y", "reason": "live_changed"}]}
    new = {"record_version": 1, "date": "2026-11-26", "exit_code": 0, "created": ["a/z"],
           "skipped": [{"path": "a/y", "reason": "operation_pending"}]}
    out = merge_mirror_records(old, new)
    assert out["created"] == ["a/x", "a/z"] and out["skipped"] == [{"path": "a/y", "reason": "operation_pending"}]
    assert out["exit_code"] == 2 and out["runs"] == 2
    assert merge_mirror_records(dict(old, date="2026-11-25"), new) == new


def test_mirror_record_failure_is_only_a_warning(world, caplog):
    report, _ = mirror(world)
    cfg = world["cfg"].model_copy(update={"report": ReportConfig(git_url="file:///nonexistent/report.git")})
    mirror_cli.write_mirror_record(cfg, report, T0, world["tmp"] / "rec", None)  # does not raise
    assert any("mirror record not written" in r.getMessage() for r in caplog.records)


def test_cli_dry_run_and_no_report_section_skip_the_record(world, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(mirror_cli, "write_mirror_record", lambda *a: calls.append(a))
    monkeypatch.setattr(mirror_cli, "KubeMirrorClient", lambda cfg: world["kube"])
    cfg = tmp_path / "config.yaml"
    url = world["cfg"].git.url
    cfg.write_text(f"git: {{url: '{url}'}}\nsecret_dir: {tmp_path}\n"
                   f"report: {{git_url: 'file://{tmp_path}/r.git'}}\n")
    assert mirror_cli.cli(["--config", str(cfg), "--workdir", str(tmp_path / "w1"), "--dry-run"]) == EXIT_OK
    assert calls == []
    assert mirror_cli.cli(["--config", str(cfg), "--workdir", str(tmp_path / "w2")]) == EXIT_OK
    assert len(calls) == 1
    cfg.write_text(f"git: {{url: '{url}'}}\nsecret_dir: {tmp_path}\n")
    mirror_cli.cli(["--config", str(cfg), "--workdir", str(tmp_path / "w3")])
    assert len(calls) == 1


def test_record_after_git_failure_lists_planned_items_as_git_failed(world):
    world["kube"].deployments[("development", "demo-x")] = deployment("demo-x")
    report, _ = mirror(world, dry_run=True)  # a plan with one add, as a failed git phase would leave it
    report.git_applied, report.exit_code = False, EXIT_INFRA
    rec = mirror_record(world["cfg"], report, T0)
    assert rec["created"] == [] and rec["first_synced"] == []
    assert {"path": "development/demo-x", "reason": "git_failed"} in rec["skipped"]


def test_recreate_refuses_a_non_sha_revision_before_deleting(world):
    kube = world["kube"]
    kube.deployments[("development", "demo-x")] = deployment("demo-x")
    mirror(world)

    class Bad:
        pushed = "main"

    from rightsizing.mirror.plan import Plan
    plan = Plan(first_sync=[("development", "demo-x", "recreate")])
    with pytest.raises(ValueError):
        _apply_recreate(world["cfg"], kube, plan, MirrorReport(0, plan), Bad())
    assert "demo-x" in kube.apps  # nothing deleted


def test_merge_drops_failures_that_succeeded_later_the_same_day():
    old = {"record_version": 1, "date": "2026-11-26", "exit_code": 2, "first_synced": [], "adopted": [],
           "skipped": [{"path": "a/x", "reason": "live_changed"}], "recreate_failed": ["a/y"]}
    new = {"record_version": 1, "date": "2026-11-26", "exit_code": 0, "first_synced": ["a/x", "a/y"],
           "adopted": [], "skipped": [], "recreate_failed": []}
    out = merge_mirror_records(old, new)
    assert out["skipped"] == [] and out["recreate_failed"] == [] and out["first_synced"] == ["a/x", "a/y"]
