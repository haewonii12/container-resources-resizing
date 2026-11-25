import shutil
import textwrap
from pathlib import Path

import pytest

from rightsizing.manifest import AppSource, SkipEdit
from rightsizing.manifest.detect import detect_format
from rightsizing.manifest.editors import edit_helm, edit_kustomize, edit_plain
from rightsizing.manifest.render import (Renderer, check_drift, container_resources, nondeterministic_mask,
                                         validate_edit)
from rightsizing.config import ManualMapping
from rightsizing.models import ContainerResources
from rightsizing.quantity import GI, MI

needs_bins = pytest.mark.skipif(not (shutil.which("helm") and shutil.which("kustomize")),
                                reason="helm/kustomize not installed")

DEPLOY = textwrap.dedent("""\
    # web service
    apiVersion: v1
    kind: Service
    metadata:
      name: web
    spec:
      ports:
        - port: 80
    ---
    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: web
      namespace: development
    spec:
      replicas: 2
      template:
        spec:
          containers:
            - name: web
              image: nginx:1.27
              resources:
                requests:
                  cpu: "2"        # generous on purpose
                  memory: 8Gi
                limits:
                  cpu: "4"
                  memory: 16Gi
                  nvidia.com/gpu: 1
            - name: sidecar
              image: busybox
              resources:
                requests:
                  cpu: 100m
                  memory: 64Mi
    """)


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def src(path, **kw):
    return AppSource(app_name=kw.pop("app_name", "web"), repo_url="https://gitea/x.git", path=path,
                     target_revision="main", dest_namespace=kw.pop("ns", "development"), **kw)


def expected_after(before: ContainerResources, changes: dict) -> ContainerResources:
    vals = dict(before.values)
    vals.update(changes)
    return ContainerResources(vals)


# ------------------------------------------------------------------ detect


@pytest.mark.parametrize("kw,reason", [
    ({"plugin": {"name": "x"}}, "unsupported_source"),
    ({"directory": {"jsonnet": {"libs": []}}}, "unsupported_source"),
    ({"directory": {"recurse": True}}, "unsupported_source"),
    ({"directory": {"include": "*.yaml"}}, "unsupported_source"),
    ({"chart": "nginx"}, "unsupported_source"),
])
def test_detect_unsupported(tmp_path, kw, reason):
    (tmp_path / "app").mkdir()
    with pytest.raises(SkipEdit) as exc:
        detect_format(src("app", **kw), tmp_path)
    assert exc.value.reason == reason


def test_detect_formats(tmp_path):
    write(tmp_path, "p/deploy.yaml", DEPLOY)
    write(tmp_path, "h/Chart.yaml", "apiVersion: v2\nname: x\nversion: 0.1.0\n")
    write(tmp_path, "k/kustomization.yaml", "resources: []\n")
    assert detect_format(src("p"), tmp_path) == "plain"
    assert detect_format(src("h"), tmp_path) == "helm"
    assert detect_format(src("k"), tmp_path) == "kustomize"
    assert detect_format(src("p"), tmp_path, [ManualMapping(workload="a/b/c", format="kustomize")]) == "kustomize"


def test_detect_path_escape(tmp_path):
    with pytest.raises(SkipEdit) as exc:
        detect_format(src("../../etc"), tmp_path)
    assert exc.value.reason == "app_path_missing"


# ------------------------------------------------------------------ plain


def test_plain_edit_only_target_and_preserves_comments(tmp_path):
    path = write(tmp_path, "apps/web/deploy.yaml", DEPLOY)
    s = src("apps/web")
    r = Renderer()
    before = r.render("plain", tmp_path, s)
    key = ("Deployment", "development", "web")
    cur = container_resources(before, key, "web")
    changes = {"cpu.request": 1400.0, "memory.request": 5 * GI, "memory.limit": 9 * GI}
    files = edit_plain(tmp_path, s, "Deployment", "web", "development", "web", changes)
    assert files == ["apps/web/deploy.yaml"]
    text = path.read_text()
    assert "# web service" in text and "# generous on purpose" in text
    assert 'cpu: "1400m"' in text and "memory: 5Gi" in text and "memory: 9Gi" in text  # quote style kept
    assert not text.startswith("---")
    assert "nvidia.com/gpu: 1" in text and "cpu: 100m" in text and 'cpu: "4"' in text
    after = r.render("plain", tmp_path, s)
    validate_edit(before, after, set(), key, {"web": expected_after(cur, changes)})


def test_plain_inserts_missing_limits(tmp_path):
    write(tmp_path, "a/d.yaml", """\
        apiVersion: apps/v1
        kind: Deployment
        metadata: {name: api, namespace: development}
        spec:
          template:
            spec:
              containers:
                - name: api
                  image: x
        """)
    edit_plain(tmp_path, src("a"), "Deployment", "api", "development", "api",
               {"cpu.request": 300.0, "memory.limit": 512 * MI})
    out = Renderer().render("plain", tmp_path, src("a"))
    got = container_resources(out, ("Deployment", "development", "api"), "api")
    assert got.get("cpu.request") == 300 and got.get("memory.limit") == 512 * MI


def test_plain_not_found_and_ambiguous(tmp_path):
    write(tmp_path, "a/d.yaml", DEPLOY)
    with pytest.raises(SkipEdit) as exc:
        edit_plain(tmp_path, src("a"), "Deployment", "nope", "development", "web", {"cpu.request": 1.0})
    assert exc.value.reason == "workload_not_in_repo"
    write(tmp_path, "a/d2.yaml", DEPLOY)
    with pytest.raises(SkipEdit) as exc:
        edit_plain(tmp_path, src("a"), "Deployment", "web", "development", "web", {"cpu.request": 1.0})
    assert exc.value.reason == "ambiguous_manifest"


def test_validate_rejects_unexpected_change(tmp_path):
    path = write(tmp_path, "a/d.yaml", DEPLOY)
    r, s, key = Renderer(), src("a"), ("Deployment", "development", "web")
    before = r.render("plain", tmp_path, s)
    cur = container_resources(before, key, "web")
    path.write_text(DEPLOY.replace("replicas: 2", "replicas: 3").replace('cpu: "2"', "cpu: 1400m"))
    after = r.render("plain", tmp_path, s)
    with pytest.raises(SkipEdit) as exc:
        validate_edit(before, after, set(), key, {"web": expected_after(cur, {"cpu.request": 1400.0})})
    assert exc.value.reason == "render_mismatch" and "replicas" in exc.value.detail


def test_validate_multi_container(tmp_path):
    write(tmp_path, "a/d.yaml", DEPLOY)
    r, s, key = Renderer(), src("a"), ("Deployment", "development", "web")
    before = r.render("plain", tmp_path, s)
    web, side = container_resources(before, key, "web"), container_resources(before, key, "sidecar")
    edit_plain(tmp_path, s, "Deployment", "web", "development", "web", {"cpu.request": 1400.0})
    edit_plain(tmp_path, s, "Deployment", "web", "development", "sidecar", {"memory.request": 32 * MI})
    after = r.render("plain", tmp_path, s)
    validate_edit(before, after, set(), key, {"web": expected_after(web, {"cpu.request": 1400.0}),
                                              "sidecar": expected_after(side, {"memory.request": 32 * MI})})
    with pytest.raises(SkipEdit):  # sidecar change not declared -> unexpected
        validate_edit(before, after, set(), key, {"web": expected_after(web, {"cpu.request": 1400.0})})


def test_validate_rejects_wrong_values(tmp_path):
    path = write(tmp_path, "a/d.yaml", DEPLOY)
    r, s, key = Renderer(), src("a"), ("Deployment", "development", "web")
    before = r.render("plain", tmp_path, s)
    cur = container_resources(before, key, "web")
    path.write_text(DEPLOY.replace('cpu: "2"', "cpu: 1500m"))
    with pytest.raises(SkipEdit) as exc:
        validate_edit(before, r.render("plain", tmp_path, s), set(), key,
                      {"web": expected_after(cur, {"cpu.request": 1400.0})})
    assert exc.value.reason == "render_mismatch"


def test_drift_detected(tmp_path):
    write(tmp_path, "a/d.yaml", DEPLOY)
    rendered = Renderer().render("plain", tmp_path, src("a"))
    key = ("Deployment", "development", "web")
    live_same = ContainerResources.from_k8s({"requests": {"cpu": "2000m", "memory": "8192Mi"},
                                             "limits": {"cpu": "4", "memory": "16Gi"}})
    check_drift(rendered, key, "web", live_same)  # normalized equal -> no drift
    live_diff = ContainerResources.from_k8s({"requests": {"cpu": "1", "memory": "8Gi"},
                                             "limits": {"cpu": "4", "memory": "16Gi"}})
    with pytest.raises(SkipEdit) as exc:
        check_drift(rendered, key, "web", live_diff)
    assert exc.value.reason == "drift"


# ------------------------------------------------------------------ helm

CHART_DEPLOY = """\
    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: {{ .Release.Name }}-web
      annotations:
        nonce: {{ .Values.nonce | default "" }}{{ if .Values.random }}{{ randAlphaNum 8 }}{{ end }}
    spec:
      replicas: {{ .Values.replicas }}
      template:
        spec:
          containers:
            - name: web
              image: nginx
              resources: {{- toYaml .Values.web.resources | nindent 16 }}
            - name: sidecar
              image: busybox
              resources: {{- toYaml .Values.sidecar.resources | nindent 16 }}
    """


def make_chart(root: Path, sidecar_same=False, random=False):
    write(root, "charts/web/Chart.yaml", "apiVersion: v2\nname: web\nversion: 0.1.0\n")
    write(root, "charts/web/templates/deploy.yaml", CHART_DEPLOY)
    cpu, mem = ("500m", "1Gi") if sidecar_same else ("50m", "64Mi")
    values = (f"replicas: 1\nrandom: {str(random).lower()}\n"
              "web:\n  # tuned by hand\n  resources:\n    requests:\n      cpu: 500m\n      memory: 1Gi\n"
              "    limits:\n      memory: 2Gi\n"
              f"sidecar:\n  resources:\n    requests:\n      cpu: {cpu}\n      memory: {mem}\n")
    (root / "charts/web").mkdir(parents=True, exist_ok=True)
    (root / "charts/web/values.yaml").write_text(values)
    write(root, "charts/web/values-dev.yaml", "replicas: 2\n")


HKEY = ("Deployment", "development", "rel-web")


@needs_bins
def test_helm_render_mirrors_app_params(tmp_path):
    make_chart(tmp_path)
    s = src("charts/web", helm={"releaseName": "rel", "valueFiles": ["values-dev.yaml"],
                                "parameters": [{"name": "nonce", "value": "abc", "forceString": True}]})
    out = Renderer().render("helm", tmp_path, s)
    obj = out[HKEY]
    assert obj["spec"]["replicas"] == 2 and obj["metadata"]["annotations"]["nonce"] == "abc"


@needs_bins
def test_helm_edit_single_match_and_validate(tmp_path):
    make_chart(tmp_path)
    s = src("charts/web", helm={"releaseName": "rel", "valueFiles": ["values-dev.yaml"]})
    r = Renderer()
    before = r.render("helm", tmp_path, s)
    cur = container_resources(before, HKEY, "web")
    changes = {"cpu.request": 300.0, "memory.limit": 1536 * MI}
    files = edit_helm(tmp_path, s, "web", cur, changes)
    assert files == ["charts/web/values.yaml"]
    text = (tmp_path / "charts/web/values.yaml").read_text()
    assert "# tuned by hand" in text and "cpu: 300m" in text and "memory: 1536Mi" in text
    after = r.render("helm", tmp_path, s)
    validate_edit(before, after, set(), HKEY, {"web": expected_after(cur, changes)})


@needs_bins
def test_helm_ambiguous_and_inline_and_manual(tmp_path):
    make_chart(tmp_path, sidecar_same=True)
    s = src("charts/web", helm={"releaseName": "rel"})
    before = Renderer().render("helm", tmp_path, s)
    cur = container_resources(before, HKEY, "sidecar")
    # sidecar block {req 500m/1Gi} vs web block {req 500m/1Gi, lim 2Gi}: only sidecar block matches exactly
    assert edit_helm(tmp_path, s, "sidecar", cur, {"cpu.request": 200.0}) == ["charts/web/values.yaml"]

    make_chart(tmp_path / "amb", sidecar_same=True)
    text = (tmp_path / "amb/charts/web/values.yaml").read_text()
    (tmp_path / "amb/charts/web/values.yaml").write_text(text.replace("    limits:\n      memory: 2Gi\n", ""))
    s2 = src("charts/web", helm={"releaseName": "rel"})
    before2 = Renderer().render("helm", tmp_path / "amb", s2)
    with pytest.raises(SkipEdit) as exc:
        edit_helm(tmp_path / "amb", s2, "web", container_resources(before2, HKEY, "web"), {"cpu.request": 200.0})
    assert exc.value.reason == "helm_ambiguous"
    manual = [ManualMapping(workload="development/Deployment/rel-web", values_key="web.resources")]
    assert edit_helm(tmp_path / "amb", s2, "web", container_resources(before2, HKEY, "web"),
                     {"cpu.request": 200.0}, manual) == ["charts/web/values.yaml"]

    s3 = src("charts/web", helm={"values": "web: {}"})
    with pytest.raises(SkipEdit) as exc:
        edit_helm(tmp_path, s3, "web", cur, {"cpu.request": 200.0})
    assert exc.value.reason == "helm_inline_values"


@needs_bins
def test_helm_no_match(tmp_path):
    make_chart(tmp_path)
    s = src("charts/web", helm={"releaseName": "rel"})
    bogus = ContainerResources.from_k8s({"requests": {"cpu": "9"}})
    with pytest.raises(SkipEdit) as exc:
        edit_helm(tmp_path, s, "web", bogus, {"cpu.request": 200.0})
    assert exc.value.reason == "helm_no_match"


@needs_bins
def test_nondeterministic_mask(tmp_path):
    make_chart(tmp_path, random=True)
    s = src("charts/web", helm={"releaseName": "rel"})
    r = Renderer()
    b1, b2 = r.render("helm", tmp_path, s), r.render("helm", tmp_path, s)
    mask = nondeterministic_mask(b1, b2)
    assert any("annotations" in p for p in mask)
    cur = container_resources(b1, HKEY, "web")
    edit_helm(tmp_path, s, "web", cur, {"cpu.request": 300.0})
    validate_edit(b1, r.render("helm", tmp_path, s), mask, HKEY, {"web": expected_after(cur, {"cpu.request": 300.0})})


# ------------------------------------------------------------------ kustomize


def make_kustomize(root: Path):
    write(root, "base/deploy.yaml", """\
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: api
        spec:
          template:
            spec:
              containers:
                - name: api
                  image: api:1
                  resources:
                    requests: {cpu: "1", memory: 2Gi}
                    limits: {memory: 4Gi}
        """)
    write(root, "base/kustomization.yaml", "resources:\n  - deploy.yaml\n")
    write(root, "overlays/dev/kustomization.yaml", """\
        # dev overlay
        namespace: development
        namePrefix: dev-
        resources:
          - ../../base
        """)


KKEY = ("Deployment", "development", "dev-api")


@needs_bins
def test_kustomize_patch_and_validate(tmp_path):
    make_kustomize(tmp_path)
    s = src("overlays/dev")
    r = Renderer()
    before = r.render("kustomize", tmp_path, s)
    cur = container_resources(before, KKEY, "api")
    changes = {"cpu.request": 600.0, "memory.request": 1536 * MI}
    files = edit_kustomize(tmp_path, s, "Deployment", "dev-api", "api", changes)
    assert files == ["overlays/dev/rightsizing-patch-deployment-api.yaml", "overlays/dev/kustomization.yaml"]
    ktext = (tmp_path / "overlays/dev/kustomization.yaml").read_text()
    assert "# dev overlay" in ktext and "rightsizing-patch-deployment-api.yaml" in ktext
    after = r.render("kustomize", tmp_path, s)
    validate_edit(before, after, set(), KKEY, {"api": expected_after(cur, changes)})
    # second edit updates the same patch file and does not re-register it
    files2 = edit_kustomize(tmp_path, s, "Deployment", "dev-api", "api", {"cpu.request": 500.0})
    assert files2 == ["overlays/dev/rightsizing-patch-deployment-api.yaml"]
    assert ktext == (tmp_path / "overlays/dev/kustomization.yaml").read_text()


@needs_bins
def test_kustomize_app_overrides_mirrored(tmp_path):
    make_kustomize(tmp_path)
    s = src("overlays/dev", kustomize={"namePrefix": "argo-", "images": ["api=registry/api:2"]})
    out = Renderer().render("kustomize", tmp_path, s)
    key = ("Deployment", "development", "argo-api")
    assert out[key]["spec"]["template"]["spec"]["containers"][0]["image"] == "registry/api:2"
    # editor strips the effective (override) prefix to find the patch target
    edit_kustomize(tmp_path, s, "Deployment", "argo-api", "api", {"cpu.request": 600.0})
    out2 = Renderer().render("kustomize", tmp_path, s)
    assert container_resources(out2, key, "api").get("cpu.request") == 600
    # the original repo's kustomization is not modified by rendering overrides
    assert "argo-" not in (tmp_path / "overlays/dev/kustomization.yaml").read_text()


def test_render_failure_is_skip(tmp_path):
    write(tmp_path, "k/kustomization.yaml", "resources:\n  - missing.yaml\n")
    if not shutil.which("kustomize"):
        pytest.skip("kustomize missing")
    with pytest.raises(SkipEdit) as exc:
        Renderer().render("kustomize", tmp_path, src("k"))
    assert exc.value.reason == "render_failed"


@needs_bins
def test_helm_value_file_outside_app_path_refused_before_writing(tmp_path):
    make_chart(tmp_path)
    shared = write(tmp_path, "shared/values.yaml", "replicas: 3\n")
    s = src("charts/web", helm={"releaseName": "rel", "valueFiles": ["../../shared/values.yaml"]})
    before = Renderer().render("helm", tmp_path, s)
    with pytest.raises(SkipEdit) as exc:
        edit_helm(tmp_path, s, "web", container_resources(before, HKEY, "web"), {"cpu.request": 300.0})
    assert exc.value.reason == "path_guard"
    assert shared.read_text() == "replicas: 3\n" and "cpu: 500m" in (tmp_path / "charts/web/values.yaml").read_text()
