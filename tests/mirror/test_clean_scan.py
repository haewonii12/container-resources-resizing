import copy
import shutil
import subprocess
import textwrap

import pytest
from ruamel.yaml import YAML

from rightsizing.mirror.clean import clean_deployment, content_hash, skip_reason, to_yaml
from rightsizing.mirror.scan import scan_repo

LIVE = {  # shape of `kubectl get deploy -o json` / python client sanitize_for_serialization
    "apiVersion": "apps/v1", "kind": "Deployment",
    "metadata": {
        "name": "demo-x", "namespace": "development", "uid": "abc", "resourceVersion": "123", "generation": 4,
        "creationTimestamp": "2026-11-25T01:00:00Z",
        "labels": {"app": "demo-x", "app.kubernetes.io/instance": "demo-x", "team": "a"},
        "annotations": {"deployment.kubernetes.io/revision": "3",
                        "kubectl.kubernetes.io/last-applied-configuration": "{...}",
                        "argocd.argoproj.io/tracking-id": "x", "owner": "team-a"},
        "managedFields": [{"manager": "kubectl", "operation": "Update"}],
    },
    "spec": {"replicas": 3, "selector": {"matchLabels": {"app": "demo-x"}},
             "progressDeadlineSeconds": 600, "revisionHistoryLimit": 10,
             "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": "25%", "maxUnavailable": "25%"}},
             "template": {"metadata": {"creationTimestamp": None, "labels": {"app": "demo-x"},
                                       "annotations": {"kubectl.kubernetes.io/restartedAt": "2026-11-24T00:00:00Z"}},
                          "spec": {"containers": [{"name": "app", "image": "nginx:1.27",
                                                   "resources": {"requests": {"cpu": "500m"}},
                                                   "terminationMessagePath": "/dev/termination-log",
                                                   "terminationMessagePolicy": "File",
                                                   "imagePullPolicy": "IfNotPresent"}],
                                   "dnsPolicy": "ClusterFirst", "restartPolicy": "Always",
                                   "schedulerName": "default-scheduler", "securityContext": {},
                                   "terminationGracePeriodSeconds": 30}}},
    "status": {"replicas": 3, "readyReplicas": 3},
}


def test_clean_drops_runtime_fields_and_keeps_spec():
    out = clean_deployment(LIVE, app_name="demo-x")
    assert set(out) == {"apiVersion", "kind", "metadata", "spec"}
    assert out["metadata"] == {"name": "demo-x", "namespace": "development",
                               "labels": {"app": "demo-x", "team": "a"}, "annotations": {"owner": "team-a"}}
    expected_spec = copy.deepcopy(LIVE["spec"])
    del expected_spec["template"]["metadata"]["creationTimestamp"]
    assert out["spec"] == expected_spec  # everything else verbatim, incl. restartedAt + server defaults
    assert out["spec"]["selector"] == LIVE["spec"]["selector"]
    assert LIVE["metadata"]["labels"]["app.kubernetes.io/instance"] == "demo-x"  # input not mutated


def test_instance_label_kept_when_not_the_app_name():
    out = clean_deployment(LIVE, app_name="other")
    assert out["metadata"]["labels"]["app.kubernetes.io/instance"] == "demo-x"


def test_hpa_targeted_drops_replicas():
    assert "replicas" not in clean_deployment(LIVE, hpa_targeted=True)["spec"]
    assert clean_deployment(LIVE)["spec"]["replicas"] == 3


@pytest.mark.skipif(not shutil.which("kubeconform"), reason="kubeconform missing")
def test_clean_output_passes_kubeconform_strict(tmp_path):
    path = tmp_path / "d.yaml"
    path.write_text(to_yaml(clean_deployment(LIVE, app_name="demo-x")))
    proc = subprocess.run(["kubeconform", "-strict", "-summary", "-kubernetes-version", "1.32.0", str(path)],
                          capture_output=True, text=True)
    if "could not find schema" in proc.stdout + proc.stderr:
        pytest.skip("schemas unavailable offline")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_yaml_roundtrip_and_hash_stable():
    text = to_yaml(clean_deployment(LIVE, app_name="demo-x"))
    assert YAML(typ="safe").load(text)["metadata"]["name"] == "demo-x"
    assert content_hash(text) == content_hash(to_yaml(clean_deployment(LIVE, app_name="demo-x")))


def _obj(**meta):
    o = copy.deepcopy(LIVE)
    o["metadata"].update(meta)
    return o


@pytest.mark.parametrize("obj,expected", [
    (_obj(ownerReferences=[{"kind": "Rollout", "name": "r"}]), "owned"),
    (_obj(labels={"app.kubernetes.io/managed-by": "Helm"}), "helm_managed"),
    (_obj(annotations={"meta.helm.sh/release-name": "rel"}), "helm_managed"),
])
def test_skip_reasons(obj, expected):
    assert skip_reason(obj, lambda o: None, lambda ns, n: None) == expected


def test_already_tracked_requires_application_listing_the_resource():
    listed = {"status": {"resources": [{"kind": "Deployment", "name": "demo-x", "namespace": "development"}]}}
    unlisted = {"status": {"resources": []}}
    resolve = lambda o: ("argocd", "demo-x")  # noqa: E731
    assert skip_reason(LIVE, resolve, lambda ns, n: listed) == "already_tracked"
    assert skip_reason(LIVE, resolve, lambda ns, n: unlisted) is None  # instance label set by a human only
    assert skip_reason(LIVE, resolve, lambda ns, n: None) is None
    assert skip_reason(_obj(labels={"app.kubernetes.io/managed-by": "Helm"}), resolve, lambda ns, n: listed,
                       skip_helm=False) == "already_tracked"


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text))


DEP = "apiVersion: apps/v1\nkind: Deployment\nmetadata: {{name: {name}, namespace: development}}\nspec: {{}}\n"


def test_scan_classifies_apps_and_foreign(tmp_path):
    write(tmp_path, "development/web/deployment.yaml", DEP.format(name="web") + "---\nkind: Service\nmetadata: {name: web}\n")
    write(tmp_path, "development/api/d.yml", DEP.format(name="api"))
    write(tmp_path, "development/renamed/deployment.yaml", DEP.format(name="other"))
    write(tmp_path, "development/two/a.yaml", DEP.format(name="two") + "---\n" + DEP.format(name="two2"))
    write(tmp_path, "development/kust/kustomization.yaml", "resources: []\n")
    write(tmp_path, "development/broken/deployment.yaml", "kind: Deployment\n  bad: [indent\n")
    write(tmp_path, "development/.mirror/x.yaml", "a: 1\n")
    write(tmp_path, "kube-system/evil/deployment.yaml", DEP.format(name="evil"))
    s = scan_repo(tmp_path, ["development", "inference"])
    assert s.apps == {("development", "web"): "development/web", ("development", "api"): "development/api"}
    assert set(s.foreign) == {"development/renamed", "development/two", "development/kust", "development/broken"}
    assert s.foreign["development/broken"].startswith("foreign_layout:parse_error")
    assert s.present("development", "renamed") and s.present("development", "web")
    assert not s.present("development", "new")
    assert s.present("development", "new", {"development/new"})  # an Application already points there
