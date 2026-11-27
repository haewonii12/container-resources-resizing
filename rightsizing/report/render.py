"""Markdown and JSON rendering of the daily report."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from ..quantity import GI, format_cpu, human
from .changes import ChangeTable
from .nodes import FailoverRow, NodeRow

LIMITATION = ("N-1 판정은 같은 role(node-role.kubernetes.io/<role>) 노드끼리만, CPU·메모리·GPU request와 "
              "pod 수, taint/toleration으로 큰 pod부터 배치해 봅니다(cordon·NotReady 노드는 받는 쪽에서 제외). nodeSelector·affinity·topology spread·"
              "PDB·우선순위는 반영하지 않습니다.")
_LABEL = {"cpu.request": "cpu.req", "cpu.limit": "cpu.lim", "memory.request": "mem.req", "memory.limit": "mem.lim"}


@dataclass
class DailyReport:
    date: str
    generated_at: str
    namespaces: list[str]
    record: dict | None  # header info of the run record used (no workloads)
    banners: list[str]
    table: ChangeTable
    nodes: list[NodeRow]
    failover: list[FailoverRow]
    metrics_available: bool = True
    window_days: int = 7  # Prometheus window behind the usage column (Config.window_days)
    notes: list[str] = field(default_factory=list)
    mirror: dict | None = None  # last night's gitops-mirror record (mirror/YYYY/MM/<date>.json)


def _q(fname: str, v: float | None) -> str:
    """CPU in Kubernetes notation (2, 1100m), memory as Gi/Mi."""
    if v is None:
        return "-"
    return format_cpu(v) if fname.startswith("cpu") else human("memory", v)


def _cores(m: float) -> str:
    return f"{m / 1000:.1f}"


def _gi(b: float) -> str:
    return f"{b / GI:.1f}Gi"


def _cell(text: str) -> str:
    return str(text).replace("|", "/").replace("\n", " ")


_OVERWROTE = {"rightsizing": "pending proposal", "human": "human edit"}


def _overwrote(entries: list[dict]) -> str:
    return "<br>".join(f"overwrote {_OVERWROTE.get(o.get('kind'), o.get('kind'))} {str(o.get('sha'))[:10]}"
                       for o in entries) or "-"


_MIRROR_REASONS = {
    "admission_denied": "admission policy가 첫 Sync를 거부함 (out/20-rightsizing-base.yaml을 다시 apply했는지 확인)",
    "live_changed": "Sync 직전에 클러스터 값이 git과 달라짐 (다음 실행에서 다시 시도)",
    "human_last_commit": "마지막 commit이 사람 수정이라 자동 첫 Sync 안 함",
    "git_failed": "git push 실패로 이번 실행에서 반영 안 됨",
    "recreate_failed": "첫 Sync를 위해 지운 뒤 다시 만들기 실패 (다음 실행이 Sync 없이 다시 만듦)",
}


def _mirror_reason(reason: str) -> str:
    return f"{reason}: {_MIRROR_REASONS[reason]}" if reason in _MIRROR_REASONS else reason


def _mirror_section(m: dict | None) -> list[str]:
    out = ["## 어제 밤 mirror", ""]
    if m is None:
        return out + ["> ⚠️ mirror 기록 없음", ""]
    out += [f"등록 {len(m.get('created') or [])} · 첫 Sync {len(m.get('first_synced') or [])} · "
            f"클러스터 반영(adopt) {len(m.get('adopted') or [])} · 정리 {len(m.get('deleted') or [])} · "
            f"건너뜀 {len(m.get('skipped') or [])} (exit {m.get('exit_code')})", ""]
    if m.get("first_synced"):
        out += ["### 첫 Sync (mirror가 자동 실행, pod 재시작 없음)", "", "| 앱 |", "|---|"]
        out += [f"| {_cell(p)} |" for p in m["first_synced"]] + [""]
    refreshed = [x for x in m.get("refreshed") or [] if x.get("overwrote")]  # never-synced, human edit overwritten
    if m.get("adopted") or refreshed:
        out += ["### 클러스터 변경을 git에 반영 (adopt)", "", "| 앱 | 변경 | 덮어씀 |", "|---|---|---|"]
        out += [f"| {_cell(a['path'])} | {_cell('<br>'.join(a.get('changes') or [])) or '-'} "
                f"| {_overwrote(a.get('overwrote') or [])} |" for a in m.get("adopted") or []]
        out += [f"| {_cell(x['path'])} | refresh (첫 Sync 전) | {_overwrote(x['overwrote'])} |" for x in refreshed]
        out.append("")
    if m.get("skipped"):
        out += ["### 건너뜀", "", "| 앱 | 사유 |", "|---|---|"]
        out += [f"| {_cell(s['path'])} | {_cell(_mirror_reason(s['reason']))} |" for s in m["skipped"]] + [""]
    return out


def render_markdown(r: DailyReport) -> str:
    t = r.table
    out = [f"# Daily report {r.date}", ""]
    out += [f"> ⚠️ {b}" for b in r.banners] + ([""] if r.banners else [])
    rec = r.record
    run = (f"rightsizing 실행 기록: {rec['date']} · run `{rec['run_id']}` · {rec['run_status']} · "
           f"manifest push {'켜짐' if rec.get('push_enabled') else '꺼짐'}") if rec else "rightsizing 실행 기록: 없음"
    out += [f"생성: {r.generated_at} · 대상 namespace: {', '.join(r.namespaces)}", "", run, ""]
    out += [f"**요약:** Deployment {t.deployments} · 변경 제안 {t.with_proposals} · "
            f"예상 절감 CPU {_cores(t.savings_cpu_m)} core / Mem {_gi(t.savings_mem_b)} (requests × replicas)", ""]

    out += ["## 변경 예정", ""]
    if t.rows:
        out += [f"| Namespace | Deployment | 컨테이너 | 항목 | 현재 | 제안 | 변화 | {r.window_days}일 사용량 | 상태 | LLM |",
                "|---|---|---|---|---|---|---|---|---|---|"]
        for row in t.rows:
            pct = "-" if row.change_pct is None else f"{row.change_pct:+.0f}%"
            out.append(f"| {row.namespace} | {row.deployment} | {row.container} | {_LABEL.get(row.field, row.field)} "
                       f"| {_q(row.field, row.current)} | {_q(row.field, row.proposed)} | {pct} "
                       f"| {_q(row.field, row.p95)} | {_cell(row.status)} | {_cell(row.llm)} |")
        out += ["", f"사용량: 최근 {r.window_days}일 P95 (mem.lim은 최대값)."]
    else:
        out.append("변경 제안이 없습니다.")
    out.append("")
    if t.skips:
        out += [f"<details><summary>변경 없음 / 건너뜀 {len(t.skips)}건</summary>", "",
                "| Namespace | Deployment | 사유 |", "|---|---|---|"]
        out += [f"| {s.namespace} | {s.deployment} | {_cell(s.reason)} |" for s in t.skips]
        out += ["", "</details>", ""]

    out += _mirror_section(r.mirror)
    out += ["## 노드 용량", ""]
    if not r.metrics_available:
        out += ["(metrics.k8s.io를 읽지 못해 실사용량은 n/a로 표시합니다)", ""]
    out += ["| 노드 | role | CPU 사용 / request / allocatable | Mem 사용 / request / allocatable | GPU request / alloc "
            "| Pods | 비고 |", "|---|---|---|---|---|---|---|"]
    for n in r.nodes:
        use_cpu = _cores(n.usage.cpu) if n.usage else "n/a"
        use_mem = _gi(n.usage.memory) if n.usage else "n/a"
        notes = [x for x in (("N-1 제외 (taint 있는 control-plane)" if not n.analyzed else ""),
                             ("cordon" if n.cordoned else ""), ("NotReady" if not n.ready else ""),
                             ", ".join(n.taints)) if x]
        out.append(f"| {n.name} | {n.role} | {use_cpu} / {_cores(n.requests.cpu)} / {_cores(n.allocatable.cpu)} "
                   f"({n.requests.cpu / n.allocatable.cpu * 100 if n.allocatable.cpu else 0:.0f}%) "
                   f"| {use_mem} / {_gi(n.requests.memory)} / {_gi(n.allocatable.memory)} "
                   f"({n.requests.memory / n.allocatable.memory * 100 if n.allocatable.memory else 0:.0f}%) "
                   f"| {n.requests.gpu:g} / {n.allocatable.gpu:g} | {n.pods} / {n.max_pods} | {'; '.join(notes) or '-'} |")
    out.append("")

    out += ["## 노드 장애 시 (N-1)", ""]
    if r.failover:
        out += ["| 죽는 노드 | role | 옮겨 갈 노드 | 결과 | 필요 (CPU / Mem / GPU) | 남은 여유 합계 | 못 들어가는 pod |",
                "|---|---|---|---|---|---|---|"]
        icon = {"가능": "✅ 가능", "불가": "❌ 불가", "단일 노드": "❌ 단일 노드: 장애 시 전체 중단"}
        for f in r.failover:
            unplaced = "<br>".join(f.unplaced[:10]) + (f"<br>외 {len(f.unplaced) - 10}개" if len(f.unplaced) > 10 else "")
            out.append(f"| {f.node} | {f.role} | {', '.join(f.targets) or '(없음)'} | {icon.get(f.result, f.result)} "
                       f"| {_cores(f.need.cpu)} / {_gi(f.need.memory)} / {f.need.gpu:g} "
                       f"| {_cores(f.free.cpu)} / {_gi(f.free.memory)} / {f.free.gpu:g} | {unplaced or '-'} |")
    else:
        out.append("분석할 노드가 없습니다 (모두 taint 있는 control-plane).")
    out += ["", f"_{LIMITATION}_", ""]
    out += [f"- {n}" for n in r.notes]
    return "\n".join(out).rstrip() + "\n"


def render_json(r: DailyReport) -> str:
    return json.dumps(asdict(r), ensure_ascii=False, indent=1, sort_keys=True, default=str) + "\n"
