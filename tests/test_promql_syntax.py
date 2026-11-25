"""Parse every generated PromQL expression with the real Prometheus parser (promtool check rules)."""

import json
import shutil
import subprocess

import pytest

from rightsizing import prom_queries as pq

pytestmark = pytest.mark.skipif(not shutil.which("promtool"), reason="promtool not installed")


def all_queries():
    out = []
    for ns, kind, name in [("development", "Deployment", "web"), ("inference", "StatefulSet", "llm")]:
        for inner in (pq.cpu_inline(ns, kind, name), pq.mem_inline(ns, kind, name),
                      pq.recorded(pq.RR_CPU, ns, kind, name), pq.recorded(pq.RR_MEM, ns, kind, name)):
            out += [pq.quantile(inner, 7, "15m"), pq.maximum(inner, 7, "5m"), pq.coverage_hours(inner, 7)]
        out += [pq.throttle_ratio(ns, kind, name, 7), pq.oom(ns, kind, name, 7), pq.restarts(ns, kind, name, 7),
                pq.avg_replicas(ns, kind, name, 7)]
    return out


def test_generated_queries_parse(tmp_path):
    rules = [{"record": f"test:q{i}", "expr": q} for i, q in enumerate(all_queries())]
    rules += pq.recording_rules()
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({"groups": [{"name": "t", "rules": rules}]}))
    proc = subprocess.run(["promtool", "check", "rules", str(path)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_ownership_join_semantics(tmp_path):
    """Synthetic series: web (1 core) and a decoy webapi (2 cores). The join must attribute only web's pod."""
    series = [
        ('kube_pod_owner{namespace="development",pod="web-abc-1",owner_kind="ReplicaSet",owner_name="web-abc"}', "1x40"),
        ('kube_pod_owner{namespace="development",pod="webapi-x-1",owner_kind="ReplicaSet",owner_name="webapi-x"}', "1x40"),
        ('kube_pod_owner{namespace="inference",pod="llm-0",owner_kind="StatefulSet",owner_name="llm"}', "1x40"),
        ('kube_replicaset_owner{namespace="development",replicaset="web-abc",owner_kind="Deployment",owner_name="web"}', "1x40"),
        ('kube_replicaset_owner{namespace="development",replicaset="webapi-x",owner_kind="Deployment",owner_name="webapi"}', "1x40"),
        ('container_cpu_usage_seconds_total{namespace="development",pod="web-abc-1",container="web",image="nginx",id="/a"}', "0+60x40"),
        ('container_cpu_usage_seconds_total{namespace="development",pod="web-abc-1",container="POD",image="pause",id="/p"}', "0+600x40"),
        ('container_cpu_usage_seconds_total{namespace="development",pod="webapi-x-1",container="web",image="nginx",id="/b"}', "0+120x40"),
        ('container_cpu_usage_seconds_total{namespace="inference",pod="llm-0",container="server",image="vllm",id="/c"}', "0+30x40"),
    ]
    tests = {"rule_files": ["rules.json"], "evaluation_interval": "1m", "tests": [{
        "interval": "1m",
        "input_series": [{"series": s, "values": v} for s, v in series],
        "promql_expr_test": [
            {"expr": pq.cpu_inline("development", "Deployment", "web"), "eval_time": "30m",
             "exp_samples": [{"labels": '{container="web", namespace="development", workload="web"}', "value": 1}]},
            {"expr": pq.cpu_inline("inference", "StatefulSet", "llm"), "eval_time": "30m",
             "exp_samples": [{"labels": '{container="server", namespace="inference", workload="llm"}', "value": 0.5}]},
            {"expr": pq.RR_CPU + '{workload="web"}', "eval_time": "30m",
             "exp_samples": [{"labels": pq.RR_CPU + '{container="web", namespace="development", workload="web", '
                                                    'workload_kind="Deployment"}', "value": 1}]},
        ]}]}
    (tmp_path / "rules.json").write_text(json.dumps({"groups": [{"name": "rs", "interval": "1m",
                                                                  "rules": pq.recording_rules()}]}))
    (tmp_path / "test.json").write_text(json.dumps(tests))
    proc = subprocess.run(["promtool", "test", "rules", "test.json"], cwd=tmp_path, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr



def test_throttle_ratio_is_zero_not_nan_for_idle_containers(tmp_path):
    series = [
        ('kube_pod_owner{namespace="development",pod="web-abc-1",owner_kind="ReplicaSet",owner_name="web-abc"}', "1x40"),
        ('kube_replicaset_owner{namespace="development",replicaset="web-abc",owner_kind="Deployment",owner_name="web"}', "1x40"),
        # idle: counters never move -> rate 0 for both
        ('container_cpu_cfs_periods_total{namespace="development",pod="web-abc-1",container="web",image="nginx"}', "8x40"),
        ('container_cpu_cfs_throttled_periods_total{namespace="development",pod="web-abc-1",container="web",image="nginx"}', "0x40"),
    ]
    tests = {"rule_files": [], "evaluation_interval": "1m", "tests": [{
        "interval": "1m", "input_series": [{"series": s, "values": v} for s, v in series],
        "promql_expr_test": [{"expr": pq.throttle_ratio("development", "Deployment", "web", 1), "eval_time": "30m",
                              "exp_samples": [{"labels": '{container="web", namespace="development", workload="web"}',
                                               "value": 0}]}]}]}
    (tmp_path / "t.json").write_text(json.dumps(tests))
    proc = subprocess.run(["promtool", "test", "rules", "t.json"], cwd=tmp_path, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
