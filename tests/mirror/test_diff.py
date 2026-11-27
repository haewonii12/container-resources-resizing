"""semantic_diff: git manifest vs clean(live) (US-003)."""

import copy

from ruamel.yaml import YAML

from rightsizing.mirror.clean import clean_deployment, to_yaml
from rightsizing.mirror.diff import semantic_diff


def api_deployment(**over):
    """API shape (sanitize_for_serialization): normalized quantities, server defaults, runtime fields."""
    d = {"apiVersion": "apps/v1", "kind": "Deployment",
         "metadata": {"name": "web", "namespace": "development", "uid": "u", "resourceVersion": "9",
                      "labels": {"app": "web", "app.kubernetes.io/instance": "web"},
                      "annotations": {"deployment.kubernetes.io/revision": "3",
                                      "argocd.argoproj.io/tracking-id": "web:apps/Deployment:development/web"}},
         "spec": {"replicas": 2, "selector": {"matchLabels": {"app": "web"}},
                  "template": {"metadata": {"labels": {"app": "web"}},
                               "spec": {"containers": [{
                                   "name": "app", "image": "nginx:1.27",
                                   "env": [{"name": "FOO", "value": "secret-1"}, {"name": "BAR", "value": "b"}],
                                   "resources": {"requests": {"cpu": "1", "memory": "512Mi"},
                                                 "limits": {"memory": "1Gi"}}}]}}},
         "status": {"replicas": 2}}
    d.update(over)
    return d


def live(**over):
    return clean_deployment(api_deployment(**over), "web")


def test_round_trip_yaml_equals_api_shape():
    git_doc = YAML(typ="safe").load(to_yaml(live()))
    assert semantic_diff(git_doc, live()) == []


def test_quantity_forms_and_key_order_are_normalized():
    git_doc = YAML(typ="safe").load(to_yaml(live()))
    res = git_doc["spec"]["template"]["spec"]["containers"][0]["resources"]
    res["requests"] = {"memory": "536870912", "cpu": "1000m"}  # rightsizing / human formatting
    res["limits"]["memory"] = "1024Mi"
    git_doc["spec"] = dict(reversed(list(git_doc["spec"].items())))
    assert semantic_diff(git_doc, live()) == []


def test_readable_changes_and_env_names_only():
    old = live()
    new = copy.deepcopy(old)
    new["spec"]["replicas"] = 3
    c = new["spec"]["template"]["spec"]["containers"][0]
    c["image"] = "nginx:1.28"
    c["env"] = [{"name": "FOO", "value": "secret-2"}, {"name": "NEW", "value": "secret-3"}]
    c["resources"]["requests"]["cpu"] = "500m"
    changes = semantic_diff(old, new)
    assert changes == ["replicas 2→3", "env[app] BAR removed", "env[app] FOO changed", "env[app] NEW added",
                       "image[app] nginx:1.27→nginx:1.28", "cpu.request[app] 1→500m"]
    assert not any("secret" in c for c in changes)


def test_hpa_owned_replicas_absent_on_both_sides():
    git_doc = clean_deployment(api_deployment(), "web", hpa_targeted=True)
    scaled = api_deployment()
    scaled["spec"]["replicas"] = 7  # the HPA scaled it
    assert semantic_diff(git_doc, clean_deployment(scaled, "web", hpa_targeted=True)) == []


def test_ignore_annotation_prefixes():
    old = live()
    new = copy.deepcopy(old)
    new["metadata"]["annotations"] = {"sidecar.istio.io/status": "x", "team": "a"}
    assert semantic_diff(old, new) == ["annotation.sidecar.istio.io/status added", "annotation.team added"]
    assert semantic_diff(old, new, ["sidecar.istio.io/"]) == ["annotation.team added"]


def test_other_fields_reported_without_values():
    old = live()
    new = copy.deepcopy(old)
    new["spec"]["template"]["spec"]["containers"][0]["args"] = ["--token=abc"]
    new["spec"]["template"]["spec"]["containers"].append({"name": "side", "image": "busybox"})
    assert semantic_diff(old, new) == ["args[app] added", "containers[side] added"]
    assert semantic_diff(None, new)  # no git doc: everything differs
