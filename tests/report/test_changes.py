from rightsizing.commit_msg import requests_savings
from rightsizing.config import Config
from rightsizing.models import ContainerResources, ContainerUsage, Usage
from rightsizing.policy import PolicyParams, recommend_container
from rightsizing.report.changes import build_table, compute_proposal, needs_compute
from rightsizing.runrecord import proposal_rows
from tests.kube_fixtures import deployment

GI = 2 ** 30
RES = {"requests": {"cpu": "2", "memory": "8Gi"}, "limits": {"cpu": "4", "memory": "16Gi"}}
CFG = Config.model_validate({"namespaces": ["development"]})


def usage():
    return ContainerUsage(cpu=Usage(1000, 1500), memory=Usage(4 * GI, 6 * GI), coverage_hours=168, oom=False,
                          restarts=0, throttle_ratio=0.0)


def row(field, cur, new, changed=True, reason=None):
    return {"container": "c", "field": field, "current": cur, "proposed": new, "changed": changed,
            "direction": "shrink" if changed else None, "basis": "b", "reason": reason, "p95": 1000, "max": 1500}


def dep(name, replicas=2, kind="Deployment"):
    return deployment(name, resources=RES, replicas=replicas, kind=kind)


def entry(name, status, **kw):
    return {"workload": f"development/Deployment/{name}", "status": status, "detail": kw.pop("detail", ""),
            "commit": kw.pop("commit", None), "llm_decision": kw.pop("llm_decision", None),
            "llm_notes": kw.pop("llm_notes", ""), "proposal": kw.pop("proposal", []), "pending": kw.pop("pending", None)}


def table_for(entries, deps, record_extra=None, computed=None):
    record = {"pushed": True, "push_enabled": True, "workloads": entries, **(record_extra or {})}
    return build_table(deps, record, computed or {})


def status_of(t, name):
    return {r.deployment: r.status for r in t.rows}.get(name)


def test_compute_matches_policy_exactly():
    d = dep("web")
    containers = {"web": ContainerResources.from_k8s(RES)}
    params = PolicyParams(CFG.min_days, CFG.min_change, CFG.throttle_threshold, CFG.cpu_limit_mode)
    recs = [recommend_container("web", containers["web"], usage(), CFG.policy_for("development"), params, None,
                                set())]
    assert compute_proposal(CFG, d, {"web": usage()}, [], []) == proposal_rows(containers, recs, {"web": usage()})
    assert any(r["changed"] and r["field"] == "cpu.request" and r["proposed"] == 1100
               for r in compute_proposal(CFG, d, {"web": usage()}, [], []))


def test_status_mapping_from_record():
    p = [row("cpu.request", 2000, 1100)]
    entries = [entry("a", "committed", commit="abc1234def", llm_decision="apply", llm_notes="이상 없음", proposal=p),
               entry("b", "superseded", proposal=p),
               entry("c", "held:llm_hold", llm_decision="hold", llm_notes="주간 배치", proposal=p),
               entry("d", "no_change", detail="pending_same", proposal=p,
                     pending={"kind": "same", "age_days": 3.2, "sha": "fedcba987"}),
               entry("e", "withdrawn", pending={"kind": "withdraw", "sha": "x", "age_days": 1})]
    t = table_for(entries, [dep(n) for n in "abcde"])
    assert status_of(t, "a") == "승인 대기 (commit abc1234)"
    assert {r.deployment: r.llm for r in t.rows}["a"] == "apply: 이상 없음"
    assert status_of(t, "b") == "제안 교체"
    assert status_of(t, "c") == "보류 (llm_hold)" and {r.deployment: r.llm for r in t.rows}["c"] == "hold: 주간 배치"
    assert status_of(t, "d") == "승인 대기 (3일, fedcba9)"
    assert [s.deployment for s in t.skips] == ["e"]


def test_push_states():
    p = [row("cpu.request", 2000, 1100)]
    t = table_for([entry("a", "committed", commit="abc1234", proposal=p)], [dep("a")],
                  {"pushed": False, "push_enabled": False})
    assert status_of(t, "a") == "commit됨 (push 꺼짐)"
    t = table_for([entry("a", "committed", commit="abc1234", proposal=p)], [dep("a")],
                  {"pushed": False, "push_enabled": True})
    assert status_of(t, "a") == "push 실패"


def test_unjudged_deployments_are_computed():
    computed = {f"development/Deployment/{n}": [row("memory.request", 8 * GI, 4 * GI)] for n in ("s", "u", "m")}
    entries = [entry("s", "skipped:no_argocd_app"), entry("u", "skipped:target_revision_mismatch"),
               entry("x", "skipped:drift", detail="HEAD differs")]
    t = table_for(entries, [dep("s"), dep("u"), dep("m"), dep("x")], computed=computed)
    assert status_of(t, "s") == "제안 가능 (Sync 전)"
    assert {r.deployment: r.llm for r in t.rows}["s"] == "LLM 검토 전 (Sync 필요)"
    assert status_of(t, "u") == "제안 가능 (지원 안 됨: target_revision_mismatch)"
    assert status_of(t, "m") == "제안 가능 (기록 없음)" and {r.deployment: r.llm for r in t.rows}["m"] == "실행 기록 없음"
    assert [(s.deployment, s.reason) for s in t.skips] == [("x", "skipped:drift: HEAD differs")]
    assert needs_compute(None) and needs_compute(entries[0]) and not needs_compute(entries[2])


def test_no_change_goes_to_skip_list_and_statefulsets_filtered():
    entries = [entry("a", "no_change", detail="below_min_change",
                     proposal=[row("cpu.request", 2000, 1900, changed=False, reason="below_min_change")])]
    t = table_for(entries, [dep("a"), dep("db", kind="StatefulSet")])
    assert not t.rows and t.deployments == 1 and t.skips[0].reason == "no_change: below_min_change"


def test_summary_savings_requests_times_replicas():
    p = [row("cpu.request", 2000, 1100), row("memory.request", 8 * GI, 4 * GI), row("memory.limit", 16 * GI, 8 * GI)]
    t = table_for([entry("a", "committed", commit="abc", proposal=p)], [dep("a", replicas=3)])
    assert t.with_proposals == 1 and len(t.rows) == 3
    assert t.savings_cpu_m == 2700 and t.savings_mem_b == 12 * GI
    assert requests_savings(p, 3) == (2700, 12 * GI)


def test_insufficient_data_reason_shows_collected_hours():
    rows = [{"container": "c", "field": "*", "current": None, "proposed": None, "changed": False, "direction": None,
             "basis": None, "reason": "insufficient_data", "p95": None, "max": None, "coverage_h": 10.0}]
    t = build_table([dep("a")], {"workloads": [entry("a", "skipped:no_argocd_app")]},
                    {"development/Deployment/a": rows}, min_days=3)
    assert t.skips[0].reason == "no_change: insufficient_data (수집 10h / 필요 72h)"
