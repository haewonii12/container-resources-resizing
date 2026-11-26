"""PromQL builders. Workload membership comes from kube-state-metrics ownership (no pod-name regexes).

Every series is joined to its workload through OWN (pod -> ReplicaSet -> Deployment, or pod -> StatefulSet)
and aggregated `max by (namespace, workload, container)`, so replicas and rollouts form one series per container.
"""

from __future__ import annotations

import re

from . import namespace_error

RR_CPU = "rightsizing:container_cpu_usage:rate5m"
RR_MEM = "rightsizing:container_memory_working_set:bytes"
_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
_CONTAINER_FILTER = 'container!="",container!="POD",image!=""'


def _check(namespace: str, name: str) -> None:
    if namespace_error(namespace) or not _NAME.match(name):
        raise ValueError(f"refusing to build query for {namespace}/{name}")


def own(namespace: str, kind: str, name: str) -> str:
    """Series with labels (namespace, pod, workload) and value 1 for every pod owned by the workload."""
    _check(namespace, name)
    if kind == "Deployment":
        return (
            'max by (namespace, pod, workload) (label_replace(label_replace('
            f'kube_pod_owner{{namespace="{namespace}",owner_kind="ReplicaSet"}}, "replicaset", "$1", "owner_name", "(.*)")'
            ' * on (namespace, replicaset) group_left (owner_name) max by (namespace, replicaset, owner_name) ('
            f'kube_replicaset_owner{{namespace="{namespace}",owner_kind="Deployment",owner_name="{name}"}}),'
            ' "workload", "$1", "owner_name", "(.*)"))'
        )
    if kind == "StatefulSet":
        return (
            'max by (namespace, pod, workload) (label_replace('
            f'kube_pod_owner{{namespace="{namespace}",owner_kind="StatefulSet",owner_name="{name}"}},'
            ' "workload", "$1", "owner_name", "(.*)"))'
        )
    raise ValueError(f"unsupported kind {kind}")


def _joined(series: str, namespace: str, kind: str, name: str) -> str:
    return (f"max by (namespace, workload, container) ({series} "
            f"* on (namespace, pod) group_left (workload) {own(namespace, kind, name)})")


def cpu_inline(namespace: str, kind: str, name: str) -> str:
    return _joined(f'rate(container_cpu_usage_seconds_total{{namespace="{namespace}",{_CONTAINER_FILTER}}}[5m])',
                   namespace, kind, name)


def mem_inline(namespace: str, kind: str, name: str) -> str:
    return _joined(f'container_memory_working_set_bytes{{namespace="{namespace}",{_CONTAINER_FILTER}}}',
                   namespace, kind, name)


def recorded(metric: str, namespace: str, kind: str, name: str) -> str:
    _check(namespace, name)
    return (f'max by (namespace, workload, container) '
            f'({metric}{{namespace="{namespace}",workload_kind="{kind}",workload="{name}"}})')


def quantile(inner: str, days: int, step: str, q: float = 0.95) -> str:
    return f"quantile_over_time({q}, ({inner})[{days}d:{step}])"


def maximum(inner: str, days: int, step: str) -> str:
    return f"max_over_time(({inner})[{days}d:{step}])"


def coverage_hours(inner: str, days: int) -> str:
    return f"count_over_time(({inner})[{days}d:1h])"


def throttle_ratio(namespace: str, kind: str, name: str, days: int) -> str:
    sel = f'{{namespace="{namespace}",{_CONTAINER_FILTER}}}'
    throttled = _sum_joined(f"rate(container_cpu_cfs_throttled_periods_total{sel}[5m])", namespace, kind, name)
    periods = _sum_joined(f"rate(container_cpu_cfs_periods_total{sel}[5m])", namespace, kind, name)
    # An idle container accrues no CFS periods: 0/0 = NaN, and one NaN step poisons avg_over_time. No periods means
    # no throttling, so the denominator is clamped (throttled is 0 whenever periods is 0).
    return f"avg_over_time(({throttled} / clamp_min({periods}, 1e-9))[{days}d:15m])"


def _sum_joined(series: str, namespace: str, kind: str, name: str) -> str:
    return (f"sum by (namespace, workload, container) ({series} "
            f"* on (namespace, pod) group_left (workload) {own(namespace, kind, name)})")


def oom(namespace: str, kind: str, name: str, days: int) -> str:
    inner = _joined(f'kube_pod_container_status_last_terminated_reason{{namespace="{namespace}",reason="OOMKilled"}}',
                    namespace, kind, name)
    return f"max_over_time(({inner})[{days}d:5m])"


def restarts(namespace: str, kind: str, name: str, days: int) -> str:
    """Also the presence check for kube-state-metrics container series (absent -> OOM unknown)."""
    return _sum_joined(f'increase(kube_pod_container_status_restarts_total{{namespace="{namespace}"}}[{days}d])',
                       namespace, kind, name)


def avg_replicas(namespace: str, kind: str, name: str, days: int) -> str:
    _check(namespace, name)
    if kind == "Deployment":
        return f'avg_over_time(kube_deployment_spec_replicas{{namespace="{namespace}",deployment="{name}"}}[{days}d])'
    return f'avg_over_time(kube_statefulset_replicas{{namespace="{namespace}",statefulset="{name}"}}[{days}d])'


# Recording rules shipped in deploy/alerts (kept here so tests can check they match the client's metric names).
def recording_rules(namespaces: list[str] | tuple[str, ...]) -> list[dict]:
    errors = [e for e in (namespace_error(ns) for ns in namespaces) if e]
    if errors or not namespaces:
        raise ValueError(f"refusing to build recording rules: {errors or 'no namespaces'}")
    ns_re = "|".join(sorted(namespaces))
    owner_deploy = (
        'max by (namespace, pod, workload, workload_kind) (label_replace(label_replace(label_replace('
        f'kube_pod_owner{{namespace=~"{ns_re}",owner_kind="ReplicaSet"}}, "replicaset", "$1", "owner_name", "(.*)")'
        ' * on (namespace, replicaset) group_left (owner_name) max by (namespace, replicaset, owner_name) ('
        'kube_replicaset_owner{owner_kind="Deployment"}), "workload", "$1", "owner_name", "(.*)"),'
        ' "workload_kind", "Deployment", "", ""))'
    )
    owner_sts = (
        'max by (namespace, pod, workload, workload_kind) (label_replace(label_replace('
        f'kube_pod_owner{{namespace=~"{ns_re}",owner_kind="StatefulSet"}}, "workload", "$1", "owner_name", "(.*)"),'
        ' "workload_kind", "StatefulSet", "", ""))'
    )
    sel = f'{{namespace=~"{ns_re}",{_CONTAINER_FILTER}}}'
    join = "* on (namespace, pod) group_left (workload, workload_kind) rightsizing:pod_workload:owner"
    return [
        {"record": "rightsizing:pod_workload:owner", "expr": owner_deploy},
        {"record": "rightsizing:pod_workload:owner", "expr": owner_sts},
        {"record": RR_CPU, "expr": f"max by (namespace, workload_kind, workload, container) "
                                   f"(rate(container_cpu_usage_seconds_total{sel}[5m]) {join})"},
        {"record": RR_MEM, "expr": f"max by (namespace, workload_kind, workload, container) "
                                   f"(container_memory_working_set_bytes{sel} {join})"},
    ]
