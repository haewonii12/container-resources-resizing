"""Change table: one row per proposed field change for every Deployment in the target namespaces.

Deployments rightsizing judged come straight from its run record (same numbers, status and LLM decision).
Deployments it did not judge (no ArgoCD app / unsupported app / no record) are computed here with the same policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..commit_msg import requests_savings
from ..config import Config
from ..discovery import hpa_blocked, limitrange_bounds
from ..models import ContainerResources, WorkloadRef
from ..policy import PolicyParams, recommend_container
from ..runrecord import proposal_rows

# Skips decided by discover() before any policy ran: the report computes these itself.
DISCOVERY_SKIPS = {"no_argocd_app", "app_not_found", "app_namespace_unsupported", "app_unreadable",
                   "multi_source", "auto_sync_enabled", "foreign_repo", "target_revision_mismatch",
                   "unsupported_ignore_differences"}
NOT_SYNCED = {"no_argocd_app", "app_not_found"}


@dataclass
class ChangeRow:
    namespace: str
    deployment: str
    container: str
    field: str
    current: float | None
    proposed: float
    change_pct: float | None
    p95: float | None
    status: str
    llm: str


@dataclass
class SkipRow:
    namespace: str
    deployment: str
    reason: str


@dataclass
class ChangeTable:
    rows: list[ChangeRow] = field(default_factory=list)
    skips: list[SkipRow] = field(default_factory=list)
    deployments: int = 0
    with_proposals: int = 0
    savings_cpu_m: float = 0.0
    savings_mem_b: float = 0.0


def workload_key(dep: dict) -> str:
    meta = dep["metadata"]
    return f"{meta['namespace']}/Deployment/{meta['name']}"


def record_entries(record: dict | None) -> dict[str, dict]:
    return {w["workload"]: w for w in (record or {}).get("workloads") or []}


def needs_compute(entry: dict | None) -> bool:
    if entry is None:
        return True
    status = entry.get("status", "")
    return status.startswith("skipped:") and status.split(":", 1)[1] in DISCOVERY_SKIPS


def containers_of(dep: dict) -> dict[str, ContainerResources]:
    pod = ((dep.get("spec") or {}).get("template") or {}).get("spec") or {}
    return {c["name"]: ContainerResources.from_k8s(c.get("resources")) for c in pod.get("containers") or []}


def compute_proposal(cfg: Config, dep: dict, usage: dict, hpas: list[dict], limitranges: list[dict]) -> list[dict]:
    """Same policy call as rightsizing (main.py process): recommend_container with the same PolicyParams.
    Revert suppression only applies to judged workloads (it needs the manifest repo history)."""
    meta = dep["metadata"]
    ref = WorkloadRef(meta["namespace"], "Deployment", meta["name"])
    containers = containers_of(dep)
    params = PolicyParams(cfg.min_days, cfg.min_change, cfg.throttle_threshold, cfg.cpu_limit_mode)
    policy = cfg.policy_for(ref.namespace)
    recs = [recommend_container(c, containers[c], usage[c], policy, params, limitrange_bounds(limitranges),
                                hpa_blocked(hpas, ref)) for c in containers if c in usage]
    return proposal_rows(containers, recs, usage)


def _status(entry: dict, record: dict) -> str:
    status = entry.get("status", "")
    kind, _, reason = status.partition(":")
    pending = entry.get("pending") or {}
    short = (entry.get("commit") or "")[:7]
    if kind == "committed":
        if record.get("pushed"):
            return f"승인 대기 (commit {short})"
        if record.get("push_enabled"):
            return "push 실패"
        return "commit됨 (push 꺼짐)"
    if kind == "superseded":
        return "제안 교체" + ("" if record.get("pushed") or not record.get("push_enabled") else " (push 실패)")
    if kind == "held":
        return f"보류 ({reason})"
    if pending.get("kind") in ("same", "endorsed"):
        return f"승인 대기 ({pending.get('age_days', 0):.0f}일, {pending.get('sha', '')[:7]})"
    return kind


def _llm(entry: dict | None) -> str:
    if not entry or not entry.get("llm_decision"):
        return "-"
    notes = (entry.get("llm_notes") or "").replace("|", "/").replace("\n", " ")
    notes = notes if len(notes) <= 60 else notes[:59] + "…"
    return f"{entry['llm_decision']}: {notes}" if notes else entry["llm_decision"]


def _reason_text(p: dict, min_days: int | None) -> str:
    reason = p["reason"]
    if reason == "insufficient_data" and p.get("coverage_h") is not None and min_days:
        return f"insufficient_data (수집 {p['coverage_h']:.0f}h / 필요 {min_days * 24}h)"
    return reason


def _pct(cur, new):
    return None if not cur else (new - cur) / cur * 100


def build_table(deployments: list[dict], record: dict | None, computed: dict[str, list[dict]],
                min_days: int | None = None) -> ChangeTable:
    entries = record_entries(record)
    table = ChangeTable()
    for dep in sorted((d for d in deployments if d.get("kind", "Deployment") == "Deployment"),
                      key=workload_key):
        key = workload_key(dep)
        ns, name = dep["metadata"]["namespace"], dep["metadata"]["name"]
        replicas = (dep.get("spec") or {}).get("replicas", 1) or 0
        table.deployments += 1
        entry = entries.get(key)
        if needs_compute(entry):
            reason = entry["status"].split(":", 1)[1] if entry else None
            if reason is None:
                status, llm = "제안 가능 (기록 없음)", "실행 기록 없음"
            elif reason in NOT_SYNCED:
                status, llm = "제안 가능 (Sync 전)", "LLM 검토 전 (Sync 필요)"
            else:
                status, llm = f"제안 가능 (지원 안 됨: {reason})", "LLM 검토 대상 아님"
            proposal = computed.get(key)
            if proposal is None:
                table.skips.append(SkipRow(ns, name, "사용량 조회 실패"))
                continue
        else:
            status, llm, proposal = _status(entry, record), _llm(entry), entry.get("proposal") or []
        changed = [p for p in proposal if p.get("changed")]
        if not changed:
            judged = entry is not None and not needs_compute(entry)
            detail = entry.get("detail") if judged else ""
            reasons = sorted({_reason_text(p, min_days) for p in proposal if p.get("reason")})
            label = entry["status"] if judged else "no_change"
            if label == "withdrawn":  # proposal withdrawn back to live values: nothing left to sync
                sha = ((entry.get("pending") or {}).get("sha") or "")[:7]
                label, detail = "제안 철회", f"승인 대기 중이던 {sha} 철회 — 현재 값 유지" if sha else "현재 값 유지"
            table.skips.append(SkipRow(ns, name, f"{label}: {detail or ', '.join(reasons) or '변경 없음'}"))
            continue
        table.with_proposals += 1
        for p in changed:
            table.rows.append(ChangeRow(ns, name, p["container"], p["field"], p.get("current"), p["proposed"],
                                        _pct(p.get("current"), p["proposed"]),
                                        p.get("max") if p["field"] == "memory.limit" else p.get("p95"),
                                        status, llm))
        cpu, mem = requests_savings(changed, replicas)
        table.savings_cpu_m += cpu
        table.savings_mem_b += mem
    return table
