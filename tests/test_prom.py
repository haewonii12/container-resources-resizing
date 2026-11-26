from urllib.parse import parse_qs

import httpx
import pytest

from rightsizing import prom_queries as pq
from rightsizing.models import WorkloadRef
from rightsizing.prom import PromClient, collect_usage

REF = WorkloadRef("development", "Deployment", "web")


def vec(**containers):
    return {"status": "success", "data": {"resultType": "vector", "result": [
        {"metric": {"namespace": "development", "workload": "web", "container": c}, "value": [0, str(v)]}
        for c, v in containers.items()]}}


class FakeProm:
    """Routes queries by the first matching (substring -> response) rule; records every query."""

    def __init__(self, rules):
        self.rules = rules
        self.queries = []

    def __call__(self, request):
        q = parse_qs(request.content.decode())["query"][0]
        self.queries.append(q)
        for needle, resp in self.rules:
            if needle in q:
                if isinstance(resp, Exception):
                    raise resp
                return httpx.Response(200, json=resp)
        return httpx.Response(200, json=vec())

    def client(self):
        return PromClient("http://prom", transport=httpx.MockTransport(self))


FULL = [
    ("count_over_time((max by (namespace, workload, container) (rightsizing:", vec()),
    ("quantile_over_time(0.95, (max by (namespace, workload, container) (rate(container_cpu", vec(web=1.08)),
    ("max_over_time((max by (namespace, workload, container) (rate(container_cpu", vec(web=1.9)),
    ("quantile_over_time(0.95, (max by (namespace, workload, container) (container_memory", vec(web=4.0e9)),
    ("max_over_time((max by (namespace, workload, container) (container_memory", vec(web=6.0e9)),
    ("count_over_time((max by (namespace, workload, container) (rate(container_cpu", vec(web=168)),
    ("container_cpu_cfs_throttled_periods_total", vec(web=0.002)),
    ("kube_pod_container_status_restarts_total", vec(web=0)),
    ("OOMKilled", vec()),
    ("kube_deployment_spec_replicas", vec(**{"": 2.0})),
]


def test_full_usage_inline_when_recording_rules_empty():
    fake = FakeProm(FULL)
    usage, replicas, source = collect_usage(fake.client(), REF, {"web": True}, 7)
    u = usage["web"]
    assert source == "inline"
    assert u.cpu.p95 == pytest.approx(1080) and u.cpu.max == pytest.approx(1900)
    assert u.memory.p95 == 4.0e9 and u.memory.max == 6.0e9
    assert u.coverage_hours == 168 and u.throttle_ratio == 0.002 and u.oom is False and replicas == 2.0
    assert any("[7d:15m]" in q for q in fake.queries)


def test_recording_rules_used_when_covering_window():
    rules = [("count_over_time((max by (namespace, workload, container) (rightsizing:", vec(web=168))] + FULL[1:]
    fake = FakeProm(rules)
    _, _, source = collect_usage(fake.client(), REF, {"web": True}, 7)
    assert source == "recording_rules"
    assert any(pq.RR_CPU in q and "quantile_over_time" in q for q in fake.queries)


def test_recording_rules_partial_coverage_falls_back():
    rules = [("count_over_time((max by (namespace, workload, container) (rightsizing:", vec(web=40))] + FULL[1:]
    _, _, source = collect_usage(FakeProm(rules).client(), REF, {"web": True}, 7)
    assert source == "inline"


def test_oom_present():
    rules = [r for r in FULL if r[0] != "OOMKilled"] + [("OOMKilled", vec(web=1))]
    usage, _, _ = collect_usage(FakeProm(rules).client(), REF, {"web": True}, 7)
    assert usage["web"].oom is True


def test_absent_series_are_unknown():
    rules = [r for r in FULL if r[0] not in ("kube_pod_container_status_restarts_total",
                                             "container_cpu_cfs_throttled_periods_total")]
    usage, _, _ = collect_usage(FakeProm(rules).client(), REF, {"web": True}, 7)
    assert usage["web"].oom is None and usage["web"].throttle_ratio is None


def test_no_cpu_limit_means_no_throttling():
    rules = [r for r in FULL if r[0] != "container_cpu_cfs_throttled_periods_total"]
    usage, _, _ = collect_usage(FakeProm(rules).client(), REF, {"web": False}, 7)
    assert usage["web"].throttle_ratio == 0.0


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("slow"),
                                     {"status": "error", "errorType": "timeout", "error": "query timed out"}])
def test_errors_and_timeouts_are_unknown(failure):
    rules = [("quantile_over_time(0.95, (max by (namespace, workload, container) (rate(container_cpu", failure),
             ("OOMKilled", failure)] + FULL
    usage, _, _ = collect_usage(FakeProm(rules).client(), REF, {"web": True}, 7)
    assert usage["web"].cpu.p95 is None and usage["web"].oom is None
    assert usage["web"].memory.p95 == 4.0e9


def test_queries_use_explicit_ownership_join():
    dep = pq.own("development", "Deployment", "web")
    assert 'kube_pod_owner{namespace="development",owner_kind="ReplicaSet"}' in dep
    assert 'kube_replicaset_owner{namespace="development",owner_kind="Deployment",owner_name="web"}' in dep
    assert "on (namespace, replicaset) group_left (owner_name)" in dep
    sts = pq.own("inference", "StatefulSet", "llm")
    assert 'owner_kind="StatefulSet",owner_name="llm"' in sts
    t = pq.throttle_ratio("development", "Deployment", "web", 7)
    assert "container_cpu_cfs_throttled_periods_total" in t and "container_cpu_cfs_periods_total" in t
    assert "on (namespace, pod) group_left (workload)" in pq.oom("development", "Deployment", "web", 7)


def test_query_builder_refuses_other_namespaces():
    with pytest.raises(ValueError):
        pq.own("kube-system", "Deployment", "x")
    with pytest.raises(ValueError):
        pq.own("development", "Deployment", 'x"}or{a="')


def test_recording_rule_names_match_client():
    names = {r["record"] for r in pq.recording_rules(["inference", "development"])}
    assert {pq.RR_CPU, pq.RR_MEM, "rightsizing:pod_workload:owner"} <= names


def test_workload_stats_templated():
    from rightsizing.prom import workload_stats

    fake = FakeProm(FULL)
    stats = workload_stats(fake.client(), REF, "cpu", 14, 0.99)
    assert stats == {"web": {"p99": None, "max": pytest.approx(1900)}}  # fake routes only the 0.95 quantile
    assert any("quantile_over_time(0.99" in q and "[14d:15m]" in q and 'owner_name="web"' in q for q in fake.queries)
    assert workload_stats(fake.client(), REF, "restarts", 7) == {"web": 0}
    down = FakeProm([("", httpx.ConnectError("x"))])
    assert workload_stats(down.client(), REF, "oom", 7) == {"unavailable": True}
