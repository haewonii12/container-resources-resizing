from rightsizing.report.changes import ChangeRow, ChangeTable, SkipRow
from rightsizing.report.nodes import FailoverRow, NodeRow, Res
from rightsizing.report.render import DailyReport, render_json, render_markdown

GI = 2 ** 30


def report(**kw):
    table = ChangeTable(rows=[ChangeRow("default", "litellm", "litellm", "cpu.request", 1000, 600, -40.0, 545,
                                        "제안 가능 (Sync 전)", "LLM 검토 전 (Sync 필요)")],
                        skips=[SkipRow("default", "postgres", "no_change: below_min_change")],
                        deployments=2, with_proposals=1, savings_cpu_m=400, savings_mem_b=0)
    node = NodeRow("promax", "control-plane,master", Res(20000, 119 * GI, 1), Res(6885, 22 * GI, 0),
                   Res(3100, 41 * GI, 0), 63, 110, True, False)
    fo = FailoverRow("promax", "control-plane,master", [], "단일 노드", ["default/vllm (cpu 2000m, mem 8.0Gi, gpu 1)"],
                     need=Res(6000, 20 * GI, 1), free=Res())
    base = dict(date="2026-11-26", generated_at="2026-11-26 05:00 KST", namespaces=["default"],
                record={"date": "2026-11-26", "run_id": "r1", "run_status": "ok", "push_enabled": False},
                banners=[], table=table, nodes=[node], failover=[fo])
    base.update(kw)
    return DailyReport(**base)


def test_markdown_layout():
    md = render_markdown(report())
    order = ["# Daily report 2026-11-26", "**요약:** Deployment 2 · 변경 제안 1 · 예상 절감 CPU 0.4 core",
             "## 변경 예정", "| default | litellm | litellm | cpu.req | 1 | 600m | -40% | 545m | 제안 가능 (Sync 전) "
             "| LLM 검토 전 (Sync 필요) |", "<details><summary>변경 없음 / 건너뜀 1건</summary>", "## 노드 용량",
             "| promax | control-plane,master | 3.1 / 6.9 / 20.0 (34%)", "## 노드 장애 시 (N-1)",
             "❌ 단일 노드: 장애 시 전체 중단", "default/vllm (cpu 2000m, mem 8.0Gi, gpu 1)", "N-1 판정은"]
    pos = [md.index(s) for s in order]
    assert pos == sorted(pos)
    assert "run `r1`" in md and "manifest push 꺼짐" in md


def test_banners_and_missing_record_and_metrics():
    md = render_markdown(report(record=None, banners=["오늘 rightsizing 실행 기록이 없습니다"], metrics_available=False,
                                nodes=[NodeRow("n", "worker", Res(1000, GI, 0), Res(), None, 0, 110, True, False)]))
    assert "> ⚠️ 오늘 rightsizing 실행 기록이 없습니다" in md and "rightsizing 실행 기록: 없음" in md
    assert "n/a / 0.0 / 1.0" in md and "실사용량은 n/a" in md


def test_empty_table_and_json_stable():
    r = report(table=ChangeTable(deployments=0))
    assert "변경 제안이 없습니다." in render_markdown(r)
    assert render_json(r) == render_json(r) and '"date": "2026-11-26"' in render_json(r)


def test_usage_column_follows_window_days():
    md = render_markdown(report(window_days=3))
    assert "| 3일 사용량 |" in md and "최근 3일 P95 (mem.lim은 최대값)" in md
    assert "7일" not in md


MIRROR = {"record_version": 1, "date": "2026-11-26", "exit_code": 0, "created": ["development/new"],
          "first_synced": ["development/new"], "deleted": [], "recreate_failed": [],
          "adopted": [{"path": "development/web", "changes": ["replicas 1→3", "env[app] FOO changed"],
                       "overwrote": [{"sha": "a" * 40, "kind": "rightsizing"}, {"sha": "b" * 40, "kind": "human"}]}],
          "refreshed": [], "skipped": [{"path": "development/api", "reason": "live_changed"}]}


def test_mirror_section_tables():
    md = render_markdown(report(mirror=MIRROR))
    order = ["## 변경 예정", "## 어제 밤 mirror", "### 첫 Sync", "| development/new |", "### 클러스터 변경을 git에 반영 (adopt)",
             "| development/web | replicas 1→3<br>env[app] FOO changed | overwrote pending proposal aaaaaaaaaa"
             "<br>overwrote human edit bbbbbbbbbb |", "### 건너뜀", "| development/api | live_changed: Sync 직전에 클러스터 값이 git과 달라짐", "## 노드 용량"]
    pos = [md.index(s) for s in order]
    assert pos == sorted(pos)
    assert '"first_synced": [' in render_json(report(mirror=MIRROR))


def test_mirror_section_missing_record_banner():
    md = render_markdown(report())
    assert "## 어제 밤 mirror\n\n> ⚠️ mirror 기록 없음" in md
    assert '"mirror": null' in render_json(report())
