import pytest

from rightsizing.config import Config
from rightsizing.discovery import discover, hpa_blocked, limitrange_bounds, normalize_repo, resolve_app
from rightsizing.models import WorkloadRef
from rightsizing.quantity import GI
from tests.kube_fixtures import REPO, FakeKube, application, deployment


def cfg(**kw):
    c = Config.model_validate({"git": {"url": REPO, "branch": "main"}, **kw})
    return c


def reasons(skipped):
    return {s.ref.name: s.reason for s in skipped}


def test_only_allowed_namespaces_are_listed():
    kube = FakeKube([deployment("web"), deployment("sys", ns="kube-system")], [application()])
    cands, _ = discover(kube, cfg(namespaces=["development"]))
    assert [c.ref.key for c in cands] == ["development/Deployment/web"]
    assert {ns for _, ns in kube.calls} == {"development"}


def test_resolves_tracking_id_and_ns_prefixed_form():
    c = cfg()
    w = deployment("web")
    assert resolve_app(w, c) == ("argocd", "web")
    w["metadata"]["annotations"]["argocd.argoproj.io/tracking-id"] = "team-a_web:apps/Deployment:development/web"
    assert resolve_app(w, c) == ("team-a", "web")
    # a copied annotation that names another resource is ignored
    w["metadata"]["annotations"]["argocd.argoproj.io/tracking-id"] = "web:apps/Deployment:development/other"
    assert resolve_app(w, c) is None


def test_label_fallback_only_in_label_mode():
    w = deployment("web", annotate=False)
    assert resolve_app(w, cfg()) is None
    assert resolve_app(w, cfg(argocd={"tracking_method": "label"})) == ("argocd", "web")


@pytest.mark.parametrize("app,reason", [
    (application(automated=True), "auto_sync_enabled"),
    (application(spec_extra={"sources": [{"repoURL": REPO}, {"repoURL": REPO}]}), "multi_source"),
    (application(rev="release-1"), "target_revision_mismatch"),
    (application(repo="https://github.com/other/repo"), "foreign_repo"),
    (application(spec_extra={"ignoreDifferences": [
        {"group": "apps", "kind": "Deployment", "jsonPointers": ["/spec/template/spec/containers/0/resources"]}]}),
     "unsupported_ignore_differences"),
])
def test_application_skip_reasons(app, reason):
    _, skipped = discover(FakeKube([deployment("web")], [app]), cfg())
    assert reasons(skipped) == {"web": reason}


def test_missing_app_and_untracked():
    kube = FakeKube([deployment("web"), deployment("lonely", annotate=False)], [])
    _, skipped = discover(kube, cfg())
    assert reasons(skipped) == {"web": "app_not_found", "lonely": "no_argocd_app"}


def test_head_revision_and_repo_normalization():
    assert normalize_repo("https://Gitea.example/org/manifests.git/") == normalize_repo("https://gitea.example/org/manifests")
    cands, _ = discover(FakeKube([deployment("web")], [application(rev="HEAD")]), cfg())
    assert len(cands) == 1


def test_hpa_utilization_blocks_resources():
    ref = WorkloadRef("development", "Deployment", "web")
    hpa = {"metadata": {"namespace": "development"}, "spec": {
        "scaleTargetRef": {"kind": "Deployment", "name": "web"},
        "metrics": [{"type": "Resource", "resource": {"name": "cpu", "target": {"type": "Utilization"}}},
                    {"type": "Resource", "resource": {"name": "memory", "target": {"type": "AverageValue"}}}]}}
    assert hpa_blocked([hpa], ref) == {"cpu"}
    v1 = {"metadata": {"namespace": "development"},
          "spec": {"scaleTargetRef": {"kind": "Deployment", "name": "web"}, "targetCPUUtilizationPercentage": 80}}
    assert hpa_blocked([v1], ref) == {"cpu"}
    assert hpa_blocked([hpa], WorkloadRef("development", "Deployment", "other")) == set()
    cands, _ = discover(FakeKube([deployment("web")], [application()], hpas=[hpa]), cfg())
    assert cands[0].blocked_resources == {"cpu"}


def test_limitrange_parsing():
    lr = {"metadata": {"namespace": "development"}, "spec": {"limits": [
        {"type": "Container", "max": {"memory": "32Gi"}, "maxLimitRequestRatio": {"cpu": "4"}},
        {"type": "Pod", "max": {"memory": "1Gi"}}]}}
    b = limitrange_bounds([lr])
    assert b.max == {"memory": 32 * GI} and b.max_ratio == {"cpu": 4.0}
    assert limitrange_bounds([]) is None


def test_candidate_fields():
    app = application(resources=[{"kind": "Deployment", "name": "web", "namespace": "development",
                                  "status": "OutOfSync"}])
    cands, _ = discover(FakeKube([deployment("web")], [app]), cfg())
    c = cands[0]
    assert c.argo_status == "OutOfSync" and c.source.path == "apps/web" and c.source.dest_namespace == "development"
    assert c.containers["web"].get("cpu.request") == 2000 and c.replicas == 2


class Forbidden(Exception):
    status = 403


def test_unreadable_app_is_skipped_not_fatal():
    class Kube(FakeKube):
        def get_application(self, namespace, name):
            if name == "web":
                raise Forbidden("forbidden")
            return super().get_application(namespace, name)

    kube = Kube([deployment("web"), deployment("api", app="api")], [application("api", "apps/api")])
    cands, skipped = discover(kube, cfg())
    assert [c.ref.name for c in cands] == ["api"]
    assert reasons(skipped) == {"web": "app_unreadable"}


def test_app_outside_argocd_namespace_is_skipped():
    w = deployment("web")
    w["metadata"]["annotations"]["argocd.argoproj.io/tracking-id"] = "team-a_web:apps/Deployment:development/web"
    _, skipped = discover(FakeKube([w], [application()]), cfg())
    assert reasons(skipped) == {"web": "app_namespace_unsupported"}
