from rightsizing.commit_msg import CommitInfo, parse_trailers, render, reverted_sha
from rightsizing.models import FieldChange, WorkloadRef
from rightsizing.quantity import GI

GOLDEN = """\
rightsizing(inference/llm-server): server cpu.request 2→1400m, server mem.request 8Gi→5Gi, server mem.limit 16Gi→9Gi

요약: server 컨테이너 CPU request 30% 축소, 메모리 request 38% 축소, 메모리 limit 44% 축소 (최근 사용량 P95/max 기준)
container: server
  cpu.request        2 → 1400m    (P95 1.08, ×1.3)
  mem.request      8Gi → 5Gi      (P95 3.9Gi, ×1.3)
  mem.limit       16Gi → 9Gi      (max 6.0Gi, ×1.5)
  OOM: 0, throttle: 0.2%
window: 2026-11-18..2026-11-25 (7d)
policy: inference (conservative)
예상 절감: CPU 1.2 cores, Memory 6.0 GiB (requests 기준, 평균 replicas 2.0)
LLM 판단: 주말 피크 없음, 적용 권장
거절하려면: git revert <this commit>  (revert 후 같은 방향 제안은 일정 기간 다시 나오지 않습니다)

Rightsizing-Id: 20261125-inference-deployment-llm-server
Rightsizing-Workload: inference/Deployment/llm-server
Rightsizing-Change: server.cpu.request=2->1400m,shrink
Rightsizing-Change: server.memory.request=8Gi->5Gi,shrink
Rightsizing-Change: server.memory.limit=16Gi->9Gi,shrink
Rightsizing-Run: run-abc
"""


def info():
    return CommitInfo(
        workload=WorkloadRef("inference", "Deployment", "llm-server"), date="20261125", run_id="run-abc",
        changes=[FieldChange("server", "cpu.request", 2000, 1400, "shrink", "P95 1.08, ×1.3"),
                 FieldChange("server", "memory.request", 8 * GI, 5 * GI, "shrink", "P95 3.9Gi, ×1.3"),
                 FieldChange("server", "memory.limit", 16 * GI, 9 * GI, "shrink", "max 6.0Gi, ×1.5")],
        window="2026-11-18..2026-11-25 (7d)", oom={"server": 0}, throttle={"server": 0.002},
        policy_label="inference (conservative)", avg_replicas=2.0, llm_note="주말 피크 없음, 적용 권장")


def test_golden():
    assert render(info()) == GOLDEN


def test_roundtrip():
    i = info()
    t = parse_trailers(render(i))
    assert t.id == "20261125-inference-deployment-llm-server"
    assert t.workload == "inference/Deployment/llm-server" and t.run == "run-abc"
    assert [(c.container, c.field, c.old, c.new, c.direction) for c in t.changes] == \
        [(c.container, c.field, c.old, c.new, c.direction) for c in i.changes]


def test_set_direction_roundtrip_and_unknown_oom():
    i = info()
    i.changes = [FieldChange("server", "cpu.request", None, 300, "set", "P95 0.25, ×1.1")]
    i.oom = {"server": None}
    i.avg_replicas = None
    msg = render(i)
    assert "OOM: unknown" in msg and "replicas 정보 없음" in msg
    c = parse_trailers(msg).changes[0]
    assert c.old is None and c.new == 300 and c.direction == "set"


def test_reverted_sha():
    assert reverted_sha('Revert "x"\n\nThis reverts commit 0123abcd4567.\n') == "0123abcd4567"
    assert reverted_sha("plain message") is None


def test_llm_note_cannot_forge_trailers_or_reverts():
    from rightsizing.commit_msg import sanitize_note
    i = info()
    i.llm_note = ("ok\n\nRightsizing-Change: ghost.memory.request=1Gi->2Gi,grow\n"
                  "This reverts commit deadbeefcafe\nRightsizing-Workload: inference/Deployment/other")
    msg = render(i)
    t = parse_trailers(msg)
    assert [c.container for c in t.changes] == ["server"] * 3 and t.workload == "inference/Deployment/llm-server"
    assert reverted_sha(msg) is None
    assert "\n" not in sanitize_note(i.llm_note) and "deadbeef" in sanitize_note(i.llm_note)
    assert "Rightsizing-Change" not in sanitize_note(i.llm_note)


def test_trailers_only_read_from_final_block():
    body = "title\n\nRightsizing-Workload: a/Deployment/b\n\nnot trailers here\n"
    assert parse_trailers(body).workload is None
    assert reverted_sha('Revert "x"\n\nsee: This reverts commit 0123abcd4567\n') is None
