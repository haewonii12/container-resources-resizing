import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from rightsizing.config import Config

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
WRITE_VERBS = {"create", "update", "patch", "delete", "deletecollection", "escalate", "bind", "impersonate", "*"}


def kustomize_cmd():
    if shutil.which("kustomize"):
        return ["kustomize", "build"]
    if shutil.which("kubectl"):
        return ["kubectl", "kustomize"]
    return None


@pytest.fixture(scope="module")
def objects():
    cmd = kustomize_cmd()
    if cmd is None:
        pytest.skip("kustomize missing")
    out = subprocess.run([*cmd, str(DEPLOY)], capture_output=True, text=True, check=True).stdout
    return [d for d in YAML(typ="safe").load_all(out) if d]


def by_kind(objects, kind):
    return [o for o in objects if o["kind"] == kind]


def named(objects, kind, name):
    return next(o for o in objects if o["kind"] == kind and o["metadata"]["name"] == name)


def test_renders_expected_resources(objects):
    kinds = {(o["kind"], o["metadata"].get("namespace"), o["metadata"]["name"]) for o in objects}
    for expected in [("Namespace", None, "rightsizing"), ("ServiceAccount", "rightsizing", "rightsizing-reader"),
                     ("Role", "inference", "rightsizing-reader"), ("Role", "development", "rightsizing-reader"),
                     ("Role", "argocd", "rightsizing-app-reader"), ("ConfigMap", "rightsizing", "rightsizing-config"),
                     ("CronJob", "rightsizing", "rightsizing"), ("NetworkPolicy", "rightsizing", "rightsizing-agent")]:
        assert expected in kinds


def test_rbac_is_read_only_and_scoped(objects):
    roles = [r for r in by_kind(objects, "Role") if r["metadata"]["name"] != "gitops-mirror-apps"]  # see mirror test
    # the only cluster-wide grant: the daily report (see test_daily_report_rbac)
    assert [c["metadata"]["name"] for c in by_kind(objects, "ClusterRole")] == ["rightsizing-daily-report"]
    for role in roles:
        for rule in role["rules"]:
            assert not set(rule["verbs"]) & WRITE_VERBS, (role["metadata"], rule)
            assert "secrets" not in rule["resources"]
    assert {r["metadata"]["namespace"] for r in roles} == {"inference", "development", "argocd"}


def test_cronjob_security_and_lifecycle(objects):
    cj = named(objects, "CronJob", "rightsizing")
    spec, job = cj["spec"], cj["spec"]["jobTemplate"]["spec"]
    pod = job["template"]["spec"]
    assert spec["concurrencyPolicy"] == "Forbid" and spec["startingDeadlineSeconds"] == 3600
    assert job["backoffLimit"] == 0 and job["activeDeadlineSeconds"] == 5700 and job["ttlSecondsAfterFinished"] == 86400
    assert spec["failedJobsHistoryLimit"] == 1
    sidecar = pod["initContainers"][0]
    assert sidecar["name"] == "kubectl-ai" and sidecar["restartPolicy"] == "Always"
    assert sidecar["startupProbe"]["tcpSocket"]["port"] == 9080
    agent = pod["containers"][0]
    secret_vols = {v["name"] for v in pod["volumes"] if "secret" in v}
    assert secret_vols == {"secrets"}
    assert not {m["name"] for m in sidecar.get("volumeMounts", [])} & secret_vols
    assert not any("secretKeyRef" in json.dumps(e) for e in sidecar.get("env", []))
    assert {m["name"] for m in agent["volumeMounts"]} >= {"secrets", "config", "work", "tmp", "home"}
    for c in (sidecar, agent):
        sc = c["securityContext"]
        assert sc["readOnlyRootFilesystem"] and not sc["allowPrivilegeEscalation"] and sc["capabilities"]["drop"] == ["ALL"]
    assert sidecar["securityContext"]["runAsUser"] != agent["securityContext"]["runAsUser"]
    assert pod["shareProcessNamespace"] is False
    assert agent["image"].endswith("rightsizing-agent:0.2.3") and sidecar["image"].endswith("rightsizing-kubectl-ai:v0.0.31")


def test_networkpolicy_denies_all_ingress(objects):
    np = named(objects, "NetworkPolicy", "rightsizing-agent")["spec"]
    assert "Ingress" in np["policyTypes"] and np["ingress"] == []
    assert np["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "rightsizing-agent"}


def test_configmap_is_a_valid_config(objects):
    cm = by_kind(objects, "ConfigMap")[0]
    cfg = Config.model_validate(YAML(typ="safe").load(cm["data"]["config.yaml"]))
    assert cfg.git.push_enabled is False and cfg.mcp.url == "http://127.0.0.1:9080/mcp"
    assert cfg.policies["inference"].max_shrink == 0.25


def test_alert_files_are_generated_and_current():
    sys.path.insert(0, str(ROOT / "scripts"))
    import gen_alerts

    for name, text in gen_alerts.render().items():
        assert (DEPLOY / "alerts" / name).read_text() == text, f"run scripts/gen_alerts.py ({name} is stale)"


@pytest.mark.skipif(not shutil.which("promtool"), reason="promtool missing")
def test_alert_rules_valid_and_oom_alert_fires(tmp_path):
    rules = DEPLOY / "alerts" / "rules.yaml"
    check = subprocess.run(["promtool", "check", "rules", str(rules)], capture_output=True, text=True)
    assert check.returncode == 0, check.stdout + check.stderr
    shutil.copy(rules, tmp_path / "rules.yaml")
    test = {"rule_files": ["rules.yaml"], "evaluation_interval": "1m", "tests": [{
        "interval": "1m",
        "input_series": [
            {"series": 'kube_pod_container_status_restarts_total{namespace="inference",pod="llm-0",container="server"}',
             "values": "0x20 1x20"},
            {"series": 'kube_pod_container_status_last_terminated_reason{namespace="inference",pod="llm-0",'
                       'container="server",reason="OOMKilled"}', "values": "0x20 1x20"},
            {"series": 'kube_job_status_failed{namespace="rightsizing",job_name="rightsizing-1"}', "values": "1x40"},
            {"series": 'kube_job_status_start_time{namespace="rightsizing",job_name="rightsizing-1"}',
             "values": "0x40"},
        ],
        "alert_rule_test": [
            {"eval_time": "25m", "alertname": "TargetWorkloadOOM", "exp_alerts": [{
                "exp_labels": {"severity": "warning", "namespace": "inference", "pod": "llm-0", "container": "server"},
                "exp_annotations": {
                    "summary": "OOMKilled in inference/llm-0 (server)",
                    "description": "If a rightsizing commit was synced recently: git log --grep "
                                   "'Rightsizing-Workload: inference/' ; git revert <sha> and sync to roll back."}}]},
            {"eval_time": "10m", "alertname": "TargetWorkloadOOM", "exp_alerts": []},
            {"eval_time": "30m", "alertname": "RightsizingJobFailed", "exp_alerts": [{
                "exp_labels": {"severity": "warning", "namespace": "rightsizing", "job_name": "rightsizing-1"},
                "exp_annotations": {
                    "summary": "rightsizing job failed (exit 2 = infrastructure, exit 3 = security refusal)",
                    "description": "Check the job logs: kubectl -n rightsizing logs job/rightsizing-1 -c agent"}}]},
        ]}]}
    (tmp_path / "t.json").write_text(json.dumps(test))
    proc = subprocess.run(["promtool", "test", "rules", "t.json"], cwd=tmp_path, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.skipif(not (shutil.which("kubeconform") and shutil.which("kustomize")), reason="kubeconform missing")
def test_manifests_match_kubernetes_schema():
    rendered = subprocess.run(["kustomize", "build", str(DEPLOY)], capture_output=True, text=True, check=True).stdout
    # CRDs (AppProject) have no built-in schema: skip only those, validate everything else strictly
    proc = subprocess.run(["kubeconform", "-strict", "-summary", "-ignore-missing-schemas",
                           "-kubernetes-version", "1.32.0", "-"], input=rendered, capture_output=True, text=True)
    if "no such host" in proc.stdout + proc.stderr:
        pytest.skip("schemas unavailable offline")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_legacy_sidecar_overlay(tmp_path):
    if not shutil.which("kustomize"):
        pytest.skip("kustomize missing")
    out = subprocess.run(["kustomize", "build", str(ROOT / "deploy-legacy-sidecar")], capture_output=True, text=True,
                         check=True).stdout
    cj = next(d for d in YAML(typ="safe").load_all(out) if d and d["kind"] == "CronJob"
              and d["metadata"]["name"] == "rightsizing")
    pod = cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert "initContainers" not in pod
    names = [c["name"] for c in pod["containers"]]
    assert names == ["agent", "kubectl-ai"]
    side = pod["containers"][1]
    assert side["command"] == ["/usr/local/bin/sidecar-wrapper.sh"] and "restartPolicy" not in side
    assert not {m["name"] for m in side["volumeMounts"]} & {"secrets"}
    assert {"name": "RS_DONE_FILE", "value": "/shared/done"} in pod["containers"][0]["env"]


def test_sidecar_wrapper_exits_when_agent_done(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "kubectl-ai").write_text("#!/bin/sh\nexec sleep 60\n")
    (fake_bin / "kubectl-ai").chmod(0o755)
    done = tmp_path / "done"
    env = {"PATH": f"{fake_bin}:/usr/bin:/bin", "RS_DONE_FILE": str(done)}
    proc = subprocess.Popen([str(ROOT / "scripts/sidecar-wrapper.sh"), "--mcp-server"], env=env)
    import time

    time.sleep(1)
    assert proc.poll() is None
    done.touch()
    assert proc.wait(timeout=10) == 0


def test_agent_writes_done_file_even_on_failure(tmp_path, monkeypatch):
    from rightsizing import main

    done = tmp_path / "done"
    monkeypatch.setenv("RS_DONE_FILE", str(done))
    with pytest.raises(FileNotFoundError):
        main.cli(["--config", str(tmp_path / "missing.yaml")])
    assert done.exists()



# ------------------------------------------------------------------ GitOps mirror (M12, M13)

def test_mirror_rbac_only_reads_workloads_and_creates_deletes_applications(objects):
    mirror_roles = [r for r in by_kind(objects, "Role") if r["metadata"]["name"].startswith("gitops-mirror")]
    by_ns = {r["metadata"]["namespace"]: r for r in mirror_roles}
    assert set(by_ns) == {"inference", "development", "argocd"}
    for ns in ("inference", "development"):
        for rule in by_ns[ns]["rules"]:
            assert set(rule["verbs"]) <= {"get", "list"} and "secrets" not in rule["resources"]
    (argo_rule,) = by_ns["argocd"]["rules"]
    assert argo_rule["resources"] == ["applications"] and set(argo_rule["verbs"]) == {"get", "list", "create", "delete"}
    bindings = [b for b in by_kind(objects, "RoleBinding") if b["metadata"]["name"].startswith("gitops-mirror")]
    assert {b["subjects"][0]["name"] for b in bindings} == {"gitops-mirror"}


def test_mirror_appproject_and_admission_policy(objects):
    proj = named(objects, "AppProject", "gitops-mirror")["spec"]
    assert proj["clusterResourceWhitelist"] == []
    assert proj["namespaceResourceWhitelist"] == [{"group": "apps", "kind": "Deployment"}]
    assert {d["namespace"] for d in proj["destinations"]} == {"inference", "development"}
    vap = named(objects, "ValidatingAdmissionPolicy", "gitops-mirror-applications")["spec"]
    assert vap["failurePolicy"] == "Fail"
    assert vap["matchConstraints"]["resourceRules"][0]["operations"] == ["CREATE"]
    assert "system:serviceaccount:rightsizing:gitops-mirror" in vap["matchConditions"][0]["expression"]
    exprs = " ".join(v["expression"] for v in vap["validations"])
    for needle in ("gitops-mirror", "syncPolicy.automated", "object.operation", "finalizers", "spec.sources"):
        assert needle in exprs
    binding = named(objects, "ValidatingAdmissionPolicyBinding", "gitops-mirror-applications")["spec"]
    assert binding["validationActions"] == ["Deny"]


def test_mirror_cronjob_and_init_pod(objects):
    cj = named(objects, "CronJob", "gitops-mirror")
    assert cj["spec"]["schedule"] == "30 2 * * *" and cj["spec"]["timeZone"] == "Asia/Seoul"
    job = cj["spec"]["jobTemplate"]["spec"]
    assert job["backoffLimit"] == 0 and job["activeDeadlineSeconds"] == 900 and job["ttlSecondsAfterFinished"] == 86400
    pod = job["template"]["spec"]
    assert job["template"]["metadata"]["labels"] == {"app.kubernetes.io/name": "gitops-mirror"}
    assert pod["serviceAccountName"] == "gitops-mirror"
    (container,) = pod["containers"]
    assert container["command"] == ["python", "-m", "rightsizing.mirror"]
    assert container["image"].endswith("rightsizing-agent:0.2.3")
    secret = next(v for v in pod["volumes"] if "secret" in v)["secret"]
    assert [i["key"] for i in secret["items"]] == ["gitea_token"]
    init = YAML(typ="safe").load((DEPLOY / "mirror/pod-init.yaml").read_text())
    assert init["kind"] == "Pod" and init["metadata"]["labels"] == {"app.kubernetes.io/name": "gitops-mirror"}
    init_c = dict(init["spec"]["containers"][0])
    cron_c = dict(container)
    init_c.pop("image"), cron_c.pop("image")
    assert init_c == cron_c and init["spec"]["serviceAccountName"] == pod["serviceAccountName"]

    def norm(vols):  # kustomize renders 0440 as 288; a raw YAML 1.2 parse reads it as 440
        return [{**v, "secret": {**v["secret"], "defaultMode": "0440"}} if "secret" in v else v for v in vols]
    assert norm(init["spec"]["volumes"]) == norm(pod["volumes"])
    assert "pod-init.yaml" not in (DEPLOY / "kustomization.yaml").read_text()


def test_mirror_networkpolicy(objects):
    np = named(objects, "NetworkPolicy", "gitops-mirror")["spec"]
    assert np["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "gitops-mirror"}
    assert np["ingress"] == [] and set(np["policyTypes"]) == {"Ingress", "Egress"}
    ports = sorted(p["port"] for rule in np["egress"] for p in rule.get("ports", []))
    assert ports == [53, 53, 443, 3000, 6443]


def test_mirror_alerts_present():
    rules = (DEPLOY / "alerts/rules.yaml").read_text()
    assert "alert: MirrorJobFailed" in rules and "alert: MirrorJobNotRun" in rules
    assert 'job_name!~"gitops-mirror.*|daily-report.*"' in rules  # rightsizing alert does not double-fire
    assert "alert: DailyReportJobFailed" in rules and "alert: DailyReportJobNotRun" in rules


def test_namespaced_rbac_is_generated_and_current():
    sys.path.insert(0, str(ROOT / "scripts"))
    import gen_namespaces

    for name, text in gen_namespaces.render().items():
        assert (DEPLOY / name).read_text() == text, f"run scripts/gen_namespaces.py ({name} is stale)"


def test_generators_follow_the_configmap_namespaces():
    sys.path.insert(0, str(ROOT / "scripts"))
    import gen_namespaces
    from rightsizing.config import Config

    pol = {"request_multiplier": 1.3, "mem_limit_multiplier": 1.5, "max_shrink": 0.25}
    cfg = Config.model_validate({"namespaces": ["default", "dify"], "policies": {"default": pol, "dify": pol},
                                 "mirror": {"namespaces": ["default"]}})
    out = gen_namespaces.render(cfg)
    yaml = YAML(typ="safe")
    roles = [d for d in yaml.load_all(out["rbac.yaml"]) if d and d["kind"] == "Role"]
    assert {r["metadata"]["namespace"] for r in roles} == {"default", "dify", "argocd"}
    mroles = [d for d in yaml.load_all(out["mirror/rbac.yaml"]) if d and d["kind"] == "Role"]
    assert {r["metadata"]["namespace"] for r in mroles} == {"default", "argocd"}
    proj = yaml.load(out["mirror/appproject.yaml"])
    assert [d["namespace"] for d in proj["spec"]["destinations"]] == ["default"]
    assert proj["spec"]["sourceRepos"] == ["MANIFEST_REPO_URL_PLACEHOLDER"]


def test_daily_report_rbac(objects):
    cr = named(objects, "ClusterRole", "rightsizing-daily-report")
    for rule in cr["rules"]:
        assert set(rule["verbs"]) <= {"get", "list", "watch"}, rule
        assert not {"secrets", "configmaps"} & set(rule["resources"])
    assert {(g, r) for rule in cr["rules"] for g in rule["apiGroups"] for r in rule["resources"]} == {
        ("", "nodes"), ("", "pods"), ("metrics.k8s.io", "nodes"), ("metrics.k8s.io", "pods")}
    bindings = by_kind(objects, "ClusterRoleBinding")
    assert len(bindings) == 1 and bindings[0]["subjects"] == [
        {"kind": "ServiceAccount", "name": "daily-report", "namespace": "rightsizing"}]
    report_roles = [r for r in by_kind(objects, "Role") if r["metadata"]["name"] == "daily-report-reader"]
    assert {r["metadata"]["namespace"] for r in report_roles} == {"inference", "development"}


def test_daily_report_cronjob(objects):
    cj, rs = named(objects, "CronJob", "daily-report"), named(objects, "CronJob", "rightsizing")
    assert cj["spec"]["schedule"] == "0 5 * * *" and cj["spec"]["timeZone"] == "Asia/Seoul"
    pod = cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "daily-report"
    c = pod["containers"][0]
    assert c["command"] == ["python", "-m", "rightsizing.report"] and c["securityContext"]["readOnlyRootFilesystem"]
    secret = next(v for v in pod["volumes"] if v["name"] == "secrets")["secret"]
    assert [i["key"] for i in secret["items"]] == ["gitea_token"]  # never the LLM key
    # starts only after rightsizing's latest possible end (schedule + activeDeadlineSeconds)

    def minutes(cron):
        m, h = cron.split()[:2]
        return int(h) * 60 + int(m)
    rs_end = minutes(rs["spec"]["schedule"]) + rs["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"] / 60
    assert minutes(cj["spec"]["schedule"]) >= rs_end


def test_daily_report_networkpolicy(objects):
    np_ = named(objects, "NetworkPolicy", "daily-report")
    assert np_["spec"]["ingress"] == [] and np_["spec"]["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "daily-report"}
    ports = {p["port"] for rule in np_["spec"]["egress"] for p in rule.get("ports", [])}
    assert ports == {53, 443, 6443, 9090, 3000}


# ------------------------------------------------------------------ first-sync admission allowlist (US-006)

CRD_FIXTURE = ROOT / "tests/fixtures/argocd-v2.13.3-application-operation-schema.yaml"


def _gen():
    sys.path.insert(0, str(ROOT / "scripts"))
    import gen_namespaces

    return gen_namespaces


def test_vap_operation_expression_empty_list_is_plain():
    g = _gen()
    assert g.operation_expression([]) == "!has(object.operation)"
    vap = next(d for d in YAML(typ="safe").load_all(g.render_vap([])) if d["kind"] == "ValidatingAdmissionPolicy")
    assert "!has(object.operation)" in [v["expression"] for v in vap["spec"]["validations"]]


def test_vap_operation_expression_denies_every_item():
    g = _gen()
    expr = g.operation_expression(["development", "inference"])
    assert expr.startswith("!has(object.operation) || (")
    assert "object.spec.destination.namespace in ['development', 'inference']" in expr
    assert "object.operation.sync.revision.matches('^[0-9a-f]{40}$')" in expr
    for k in g.OPERATION_DENIED:
        assert f"!has(object.operation.{k})" in expr
    for k in g.SYNC_DENIED:
        assert f"!has(object.operation.sync.{k})" in expr
    assert "!has(object.operation.sync.syncStrategy.hook)" in expr
    assert "!has(object.operation.sync.syncStrategy.apply.force)" in expr
    assert ".all(" not in expr and ".exists(" not in expr  # no key iteration on CRD objects
    assert expr.count("(") == expr.count(")")
    vap = next(d for d in YAML(typ="safe").load_all(g.render_vap(["development", "inference"]))
               if d["kind"] == "ValidatingAdmissionPolicy")
    rules = [v["expression"] for v in vap["spec"]["validations"]]
    assert " ".join(expr.splitlines()) in rules and len(rules) == 5


def test_vap_allowlist_pinned_to_argocd_crd_properties():
    """A CRD upgrade that adds an operation field must fail here until the allowlist decides about it."""
    g = _gen()
    op = YAML(typ="safe").load(CRD_FIXTURE.read_text())
    sync = op["properties"]["sync"]
    strategy = sync["properties"]["syncStrategy"]
    assert set(op["properties"]) == set(g.OPERATION_ALLOWED) | set(g.OPERATION_DENIED)
    assert set(sync["properties"]) == set(g.SYNC_ALLOWED) | set(g.SYNC_DENIED)
    assert set(strategy["properties"]) == set(g.STRATEGY_ALLOWED) | set(g.STRATEGY_DENIED)
    assert set(strategy["properties"]["apply"]["properties"]) == set(g.APPLY_DENIED)
    for node in (op, sync, strategy, strategy["properties"]["apply"]):  # structural: unknown fields are pruned
        assert not node.get("x-kubernetes-preserve-unknown-fields")


def test_mirror_payload_satisfies_the_allowlist_shape():
    from rightsizing.mirror.run import application_body

    g = _gen()
    body = application_body(Config(), "development", "web", "0" * 40)
    assert set(body["operation"]) <= set(g.OPERATION_ALLOWED)
    assert set(body["operation"]["sync"]) <= set(g.SYNC_ALLOWED)
    assert set(body["operation"]["sync"]["syncStrategy"]) <= set(g.STRATEGY_ALLOWED)
    assert body["operation"]["sync"]["syncStrategy"]["apply"] == {}


@pytest.mark.skipif(not os.environ.get("RS_KUBE_DRYRUN"), reason="set RS_KUBE_DRYRUN=1 to run against a cluster")
def test_installed_vap_server_dry_run():
    """Server dry-run as the mirror SA against the installed (empty-list) policy: plain create admitted,
    any operation rejected (AC5/AC9)."""
    from rightsizing.mirror.run import application_body

    cfg = Config.model_validate({"git": {"url": "http://gitea/a/resource.git"}})

    def dry_run(body):
        return subprocess.run(["kubectl", "--as", "system:serviceaccount:rightsizing:gitops-mirror", "create",
                               "--dry-run=server", "-o", "name", "-f", "-"], input=json.dumps(body),
                              capture_output=True, text=True)
    plain = application_body(cfg, "development", "rs-dryrun")
    assert dry_run(plain).returncode == 0, dry_run(plain).stderr
    synced = application_body(cfg, "development", "rs-dryrun", "0" * 40)
    proc = dry_run(synced)
    assert proc.returncode != 0 and "sync operation" in proc.stderr
