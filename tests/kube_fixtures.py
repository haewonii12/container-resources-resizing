"""In-memory KubeAPI with fixture objects shaped like the real API JSON."""

from __future__ import annotations

import copy

REPO = "https://gitea.example/org/manifests.git"


def deployment(name, ns="development", app="web", resources=None, containers=None, annotate=True, replicas=2,
               kind="Deployment"):
    meta = {"name": name, "namespace": ns, "labels": {"app.kubernetes.io/instance": app}}
    if annotate:
        meta["annotations"] = {"argocd.argoproj.io/tracking-id": f"{app}:apps/{kind}:{ns}/{name}"}
    if containers is None:
        containers = [{"name": name, "image": "x", "resources": resources if resources is not None else {
            "requests": {"cpu": "2", "memory": "8Gi"}, "limits": {"cpu": "4", "memory": "16Gi"}}}]
    return {"kind": kind, "metadata": meta, "spec": {"replicas": replicas, "template": {"spec": {
        "containers": containers}}}}


def application(name="web", path="apps/web", ns="development", automated=False, repo=REPO, rev="main", **extra):
    spec = {"source": {"repoURL": repo, "path": path, "targetRevision": rev},
            "destination": {"namespace": ns, "server": "https://kubernetes.default.svc"},
            "syncPolicy": {"automated": {"prune": True}} if automated else {}}
    spec.update(extra.pop("spec_extra", {}))
    app = {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
           "metadata": {"name": name, "namespace": "argocd"}, "spec": spec,
           "status": {"resources": extra.pop("resources", [])}}
    return app


class FakeKube:
    def __init__(self, workloads=None, apps=None, hpas=None, limitranges=None):
        self.workloads = workloads or []
        self.apps = {(a["metadata"]["namespace"], a["metadata"]["name"]): a for a in (apps or [])}
        self.hpas = hpas or []
        self.limitranges = limitranges or []
        self.calls: list[tuple] = []

    def list_workloads(self, namespace):
        self.calls.append(("list_workloads", namespace))
        return [copy.deepcopy(w) for w in self.workloads if w["metadata"]["namespace"] == namespace]

    def list_hpas(self, namespace):
        return [h for h in self.hpas if h["metadata"]["namespace"] == namespace]

    def list_limitranges(self, namespace):
        return [x for x in self.limitranges if x["metadata"]["namespace"] == namespace]

    def get_application(self, namespace, name):
        return copy.deepcopy(self.apps.get((namespace, name)))

    def version(self):
        return "v1.31.0"

    def api_versions(self):
        return ["v1", "apps/v1", "autoscaling/v2"]
