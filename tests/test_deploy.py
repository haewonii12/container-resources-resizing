import json
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


@pytest.fixture(scope="module")
def objects():
    if not shutil.which("kustomize"):
        pytest.skip("kustomize missing")
    out = subprocess.run(["kustomize", "build", str(DEPLOY)], capture_output=True, text=True, check=True).stdout
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
    assert not by_kind(objects, "ClusterRole") and not by_kind(objects, "ClusterRoleBinding")
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
    assert 'job_name!~"gitops-mirror.*"' in rules  # rightsizing alert does not double-fire
