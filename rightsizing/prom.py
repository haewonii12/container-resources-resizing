"""Prometheus client. Any absent, errored or timed-out series is reported as unknown (None), never as 0."""

from __future__ import annotations

import logging
import math

import httpx

from . import prom_queries as pq
from .models import ContainerUsage, Usage, WorkloadRef

log = logging.getLogger("rightsizing.prom")


class PromClient:
    def __init__(self, url: str, timeout: float = 30, transport: httpx.BaseTransport | None = None):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.http = httpx.Client(timeout=timeout + 5, transport=transport)

    def raw(self, query: str) -> dict:
        resp = self.http.post(f"{self.url}/api/v1/query", data={"query": query, "timeout": f"{int(self.timeout)}s"})
        resp.raise_for_status()
        return resp.json()

    def vector(self, query: str) -> list[dict] | None:
        try:
            body = self.raw(query)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("prometheus query failed", extra={"error": type(exc).__name__})
            return None
        if body.get("status") != "success":
            log.warning("prometheus query error", extra={"error": str(body.get("error"))[:200]})
            return None
        return body.get("data", {}).get("result", [])

    def by_container(self, query: str) -> dict[str, float] | None:
        result = self.vector(query)
        if result is None:
            return None
        out: dict[str, float] = {}
        for sample in result:
            value = float(sample["value"][1])
            if math.isnan(value) or math.isinf(value):
                continue
            out[sample.get("metric", {}).get("container", "")] = value
        return out

    def scalar(self, query: str) -> float | None:
        result = self.vector(query)
        if not result:
            return None
        value = float(result[0]["value"][1])
        return None if math.isnan(value) else value


def _get(values: dict[str, float] | None, container: str, scale: float = 1.0) -> float | None:
    if values is None or container not in values:
        return None
    return values[container] * scale


def collect_usage(prom: PromClient, ref: WorkloadRef, cpu_limited: dict[str, bool], window_days: int,
                  use_recording_rules: bool = True) -> tuple[dict[str, ContainerUsage], float | None, str]:
    """Return (usage per container, 7d average replicas, source label 'recording_rules' | 'inline')."""
    ns, kind, name, days = ref.namespace, ref.kind, ref.name, window_days
    source = "inline"
    cpu_inner, mem_inner, step = pq.cpu_inline(ns, kind, name), pq.mem_inline(ns, kind, name), "15m"
    if use_recording_rules:
        rr_cpu = pq.recorded(pq.RR_CPU, ns, kind, name)
        cov = prom.by_container(pq.coverage_hours(rr_cpu, days))
        needed = days * 24 * 0.95
        if cov and all(cov.get(c, 0) >= needed for c in cpu_limited):
            source = "recording_rules"
            cpu_inner, mem_inner, step = rr_cpu, pq.recorded(pq.RR_MEM, ns, kind, name), "5m"

    cpu_p95 = prom.by_container(pq.quantile(cpu_inner, days, step))
    cpu_max = prom.by_container(pq.maximum(cpu_inner, days, step))
    mem_p95 = prom.by_container(pq.quantile(mem_inner, days, step))
    mem_max = prom.by_container(pq.maximum(mem_inner, days, step))
    coverage = prom.by_container(pq.coverage_hours(cpu_inner, days))
    throttle = prom.by_container(pq.throttle_ratio(ns, kind, name, days))
    restarts = prom.by_container(pq.restarts(ns, kind, name, days))
    oom = prom.by_container(pq.oom(ns, kind, name, days))
    replicas = prom.scalar(pq.avg_replicas(ns, kind, name, days))

    usage: dict[str, ContainerUsage] = {}
    for c, limited in cpu_limited.items():
        restart_count = _get(restarts, c)
        if oom is None or restart_count is None:
            oom_state = None  # kube-state-metrics series missing or query failed -> unknown
        else:
            oom_state = oom.get(c, 0.0) > 0
        if not limited:
            throttle_state = 0.0  # CFS throttling needs a CPU limit
        else:
            throttle_state = _get(throttle, c)
        usage[c] = ContainerUsage(
            cpu=Usage(_get(cpu_p95, c, 1000), _get(cpu_max, c, 1000)),
            memory=Usage(_get(mem_p95, c), _get(mem_max, c)),
            coverage_hours=_get(coverage, c), oom=oom_state, restarts=restart_count, throttle_ratio=throttle_state)
    return usage, replicas, source


def workload_stats(prom: PromClient, ref: WorkloadRef, metric: str, days: int, q: float = 0.95) -> dict:
    """Templated, workload-bound statistics for the LLM's prom_stats tool (no free-form PromQL)."""
    ns, kind, name = ref.namespace, ref.kind, ref.name
    if metric in ("cpu", "memory"):
        inner = pq.cpu_inline(ns, kind, name) if metric == "cpu" else pq.mem_inline(ns, kind, name)
        scale = 1000 if metric == "cpu" else 1
        quant = prom.by_container(pq.quantile(inner, days, "15m", q)) or {}
        peak = prom.by_container(pq.maximum(inner, days, "15m")) or {}
        return {c: {f"p{int(q * 100)}": quant.get(c, 0) * scale if c in quant else None,
                    "max": peak.get(c, 0) * scale if c in peak else None} for c in sorted(set(quant) | set(peak))}
    query = {"throttle": pq.throttle_ratio, "restarts": pq.restarts, "oom": pq.oom}[metric](ns, kind, name, days)
    values = prom.by_container(query)
    return {"unavailable": True} if values is None else values
