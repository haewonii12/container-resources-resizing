"""Pending proposals: supersede on material change, keep on jitter, withdraw when no longer needed (M15-M17)."""

import subprocess
from datetime import datetime, timezone

from rightsizing import commit_msg
from rightsizing.models import ContainerRec, ContainerResources, FieldChange, WorkloadRef
from rightsizing.pending import decide_pending, expected_head
from rightsizing.quantity import GI, MI
from tests.integration.test_pipeline import PROM_RULES, git, remote_log, run
from tests.test_prom import vec

LIVE = {"web": ContainerResources({"cpu.request": 2000.0, "cpu.limit": 4000.0,
                                   "memory.request": 8.0 * GI, "memory.limit": 16.0 * GI})}
PENDING = {("web", "cpu.request"): 1100.0, ("web", "memory.request"): 4512.0 * MI, ("web", "memory.limit"): 8.0 * GI}
HEAD = expected_head(LIVE, PENDING)


def rec(**proposed):
    """A ContainerRec whose changes are the proposed values that differ from live by >= 10%."""
    r = ContainerRec("web")
    for f, v in proposed.items():
        f = f.replace("_", ".").replace("memory.", "memory.")
        r.proposed[f] = v
        old = LIVE["web"].get(f)
        if abs(v - old) / old >= 0.10:
            r.changes.append(FieldChange("web", f, old, v, "shrink" if v < old else "grow", "test"))
    return r


def test_jitter_keeps_pending():
    d = decide_pending(LIVE, HEAD, PENDING,
                       [rec(cpu_request=1160.0, memory_request=4528.0 * MI, memory_limit=8.0 * GI)], 0.10)
    assert d.kind == "same" and not d.edits


def test_direction_flip_supersedes_and_keeps_other_pending_fields():
    d = decide_pending(LIVE, HEAD, PENDING,
                       [rec(cpu_request=4000.0, memory_request=4528.0 * MI, memory_limit=8.0 * GI)], 0.10)
    assert d.kind == "supersede"
    assert d.edits == {"web": {"cpu.request": 4000.0}}
    changes = {c.field: c for c in d.changes}
    assert changes["cpu.request"].old == 2000.0 and changes["cpu.request"].direction == "grow"
    assert changes["memory.request"].new == 4512.0 * MI  # pending value kept, recorded relative to live


def test_material_move_same_direction_supersedes():
    d = decide_pending(LIVE, HEAD, PENDING,
                       [rec(cpu_request=1500.0, memory_request=4512.0 * MI, memory_limit=8.0 * GI)], 0.10)
    assert d.kind == "supersede" and d.edits == {"web": {"cpu.request": 1500.0}}


def test_back_to_live_withdraws():
    d = decide_pending(LIVE, HEAD, PENDING,
                       [rec(cpu_request=2000.0, memory_request=8.0 * GI, memory_limit=16.0 * GI)], 0.10)
    assert d.kind == "withdraw" and not d.changes
    assert d.edits == {"web": {"cpu.request": 2000.0, "memory.request": 8.0 * GI, "memory.limit": 16.0 * GI}}


def test_hysteresis_band_between_half_and_full_min_change_keeps_pending_withdraw_only_near_live():
    # fresh cpu 1900 is 5% from live (== min_change/2): not "near live" (< 5%), same direction as pending shrink,
    # but 73% away from pending 1100 -> moves materially -> supersede to 1900
    d = decide_pending(LIVE, HEAD, PENDING,
                       [rec(cpu_request=1900.0, memory_request=4512.0 * MI, memory_limit=8.0 * GI)], 0.10)
    assert d.kind == "supersede" and d.edits["web"]["cpu.request"] == 1900.0
    d2 = decide_pending(LIVE, HEAD, PENDING,
                        [rec(cpu_request=1960.0, memory_request=4512.0 * MI, memory_limit=8.0 * GI)], 0.10)
    assert d2.edits["web"]["cpu.request"] == 2000.0  # within min_change/2 of live -> withdrawn to live


def test_oom_guard_withdraws_pending_shrink():
    r = ContainerRec("web")
    r.skipped.update({"memory.request": "oom_recent", "memory.limit": "oom_recent"})
    r.proposed["cpu.request"] = 1100.0
    r.changes.append(FieldChange("web", "cpu.request", 2000.0, 1100.0, "shrink", "t"))
    d = decide_pending(LIVE, HEAD, PENDING, [r], 0.10)
    assert d.kind == "supersede"
    assert d.edits == {"web": {"memory.request": 8.0 * GI, "memory.limit": 16.0 * GI}}


def test_suppressed_field_keeps_pending_value():
    r = ContainerRec("web")
    r.skipped["cpu.request"] = "suppressed_by_revert"
    r.proposed.update({"memory.request": 4512.0 * MI, "memory.limit": 8.0 * GI})
    d = decide_pending(LIVE, HEAD, PENDING, [r], 0.10)
    assert d.kind == "same"


def test_conflict_when_merged_request_exceeds_limit():
    live = {"web": ContainerResources({"cpu.request": 1000.0, "cpu.limit": 2000.0,
                                       "memory.request": 1.0 * GI, "memory.limit": 2.0 * GI})}
    pending = {("web", "memory.limit"): 1.0 * GI}
    r = ContainerRec("web")
    r.proposed.update({"memory.request": 1.5 * GI})
    r.changes.append(FieldChange("web", "memory.request", 1.0 * GI, 1.5 * GI, "grow", "t"))
    d = decide_pending(live, expected_head(live, pending), pending, [r], 0.10)
    assert d.kind == "conflict"


def test_trailers_roundtrip_supersede_and_withdraw():
    ref = WorkloadRef("development", "Deployment", "web")
    info = commit_msg.CommitInfo(ref, "20261126", "r2", [FieldChange("web", "cpu.request", 2000, 4000, "grow", "b")],
                                 "w", {}, {}, "development", 2.0, "", supersedes="a" * 40)
    t = commit_msg.parse_trailers(commit_msg.render(info))
    assert t.workload == ref.key and t.supersedes == "a" * 40 and t.kind == "supersede" and len(t.changes) == 1
    w = commit_msg.parse_trailers(commit_msg.render_withdraw(ref, "20261126", "r3", "b" * 40, [], "x"))
    assert w.workload == ref.key and w.withdraw == "b" * 40 and w.kind == "withdraw" and w.changes == []
    assert commit_msg.reverted_sha(commit_msg.render_withdraw(ref, "d", "r", "c" * 40, [], "x")) is None


# ------------------------------------------------------------------ integration (real git flow)

def spike_rules(cpu_cores):
    rules = [r for r in PROM_RULES if "rate(container_cpu" not in r[0] or "count_over_time" in r[0]]
    rules.insert(0, ("quantile_over_time(0.95, (max by (namespace, workload, container) (rate(container_cpu",
                     vec(web=cpu_cores, api=cpu_cores)))
    rules.insert(0, ("max_over_time((max by (namespace, workload, container) (rate(container_cpu",
                     vec(web=cpu_cores * 1.2, api=cpu_cores * 1.2)))
    return rules


def flat_rules():
    """Usage that recommends exactly the live values (2 cores / 8Gi / 16Gi limit in development policy)."""
    rules = [r for r in spike_rules(2.0 / 1.1) if "container_memory" not in r[0]]
    rules.insert(0, ("quantile_over_time(0.95, (max by (namespace, workload, container) (container_memory",
                     vec(web=8 * GI / 1.1, api=8 * GI / 1.1)))
    rules.insert(0, ("max_over_time((max by (namespace, workload, container) (container_memory",
                     vec(web=16 * GI / 1.2, api=16 * GI / 1.2)))
    return rules


def bodies(env):
    return [c.split("\x1f", 1)[1] for c in remote_log(env)]


def test_spike_supersedes_with_llm_review(env):
    run(env)  # initial shrink proposals
    box = []
    report, out, _ = run(env, rules=spike_rules(3.0), llm_box=box)
    assert out["web"].status == "superseded" and report.pushed == 2
    web_sup = next(b for b in bodies(env) if "Rightsizing-Supersedes" in b and "Deployment/web" in b)
    assert "web.cpu.request=2->3300m,grow" in web_sup  # direction flipped: pending shrink -> grow
    assert "web.memory.request=8Gi->4512Mi,shrink" in web_sup  # unchanged pending field kept, old = live
    assert "대체: 승인 대기 중이던 제안" in web_sup
    assert any("pending_proposal" in m["content"] for r in box[0].requests for m in r["messages"] if m["role"] == "user")
    # the new proposal is now the pending one; same data again -> no further commit
    _, out, _ = run(env, rules=spike_rules(3.0))
    assert out["web"].detail.startswith("pending_same")


def test_jitter_creates_no_commit_and_no_llm_call(env):
    run(env)
    n = len(remote_log(env))
    box = []
    _, out, _ = run(env, rules=spike_rules(1.05), llm_box=box)
    assert out["web"].detail.startswith("pending_same") and len(remote_log(env)) == n
    assert box[0].requests == []


def test_flat_usage_withdraws_then_nothing_is_pending(env):
    run(env)
    _, out, _ = run(env, rules=flat_rules())
    assert out["web"].status == "withdrawn"
    newest = next(b for b in bodies(env) if "Rightsizing-Withdraw" in b and "Deployment/web" in b)
    assert "Rightsizing-Change" not in newest
    repo = env["tmp"] / f"work{env['runs']}" / "repo"
    assert 'cpu: "2"' in subprocess.run(["git", "-C", str(repo), "show", "HEAD:apps/web/deploy.yaml"],
                                       capture_output=True, text=True).stdout
    _, out, _ = run(env, rules=flat_rules())
    assert out["web"].status == "no_change" and not out["web"].detail.startswith("pending")


def test_human_revert_of_supersede_is_endorsed(env):
    run(env)
    run(env, rules=spike_rules(3.0))
    web_sup = next(c for c in remote_log(env) if "Rightsizing-Supersedes" in c and "Deployment/web" in c)
    git(env["seed"], "pull", "-q", "origin", "main")
    git(env["seed"], "revert", "--no-edit", web_sup.split("\x1f")[0].strip())
    git(env["seed"], "push", "-q", "origin", "main")
    _, out, _ = run(env, rules=spike_rules(3.0))
    assert out["web"].status == "no_change" and out["web"].detail.startswith("pending_endorsed")


def test_manual_git_edit_while_pending_is_drift(env):
    run(env)
    git(env["seed"], "pull", "-q", "origin", "main")
    path = env["seed"] / "apps/web/deploy.yaml"
    text = path.read_text()
    assert 'cpu: "1100m"' in text
    path.write_text(text.replace('cpu: "1100m"', 'cpu: "900m"'))
    git(env["seed"], "commit", "-qam", "hand edit")
    git(env["seed"], "push", "-q", "origin", "main")
    _, out, _ = run(env, rules=spike_rules(3.0))
    # the hand edit removed the pending values from HEAD -> the proposal counts as superseded by a human,
    # and the normal drift check (git != live) stops the bot from touching the workload
    assert out["web"].status == "skipped:drift" and "900.0" in out["web"].detail


def test_multi_container_supersede_passes_render_validation(tmp_path):
    from rightsizing.manifest import AppSource
    from rightsizing.manifest.editors import edit_plain
    from rightsizing.manifest.render import Renderer, container_resources, validate_edit
    from tests.test_manifest import DEPLOY, write

    write(tmp_path, "a/d.yaml", DEPLOY)
    src = AppSource("web", "r", "a", "main", "development")
    key = ("Deployment", "development", "web")
    live = {c: container_resources(Renderer().render("plain", tmp_path, src), key, c) for c in ("web", "sidecar")}
    pending = {("web", "cpu.request"): 1400.0, ("sidecar", "memory.request"): 32.0 * MI}
    for (c, f), v in pending.items():  # HEAD carries the pending proposal
        edit_plain(tmp_path, src, "Deployment", "web", "development", c, {f: v})
    before = Renderer().render("plain", tmp_path, src)
    head = {c: container_resources(before, key, c) for c in live}
    web, side = ContainerRec("web"), ContainerRec("sidecar")
    web.proposed["cpu.request"] = 4000.0
    web.changes.append(FieldChange("web", "cpu.request", 2000.0, 4000.0, "grow", "spike"))
    side.proposed["memory.request"] = 64.0 * MI  # back to live -> withdraw that container's pending field
    d = decide_pending(live, head, pending, [web, side], 0.10)
    assert d.kind == "supersede" and set(d.edits) == {"web", "sidecar"}
    for c, values in d.edits.items():
        edit_plain(tmp_path, src, "Deployment", "web", "development", c, values)
    validate_edit(before, Renderer().render("plain", tmp_path, src), set(), key, {c: d.target[c] for c in d.edits})


def test_hand_edit_of_non_pending_field_while_pending_is_drift(env):
    run(env)  # pending proposal changes cpu.request / memory.request / memory.limit, not cpu.limit
    git(env["seed"], "pull", "-q", "origin", "main")
    path = env["seed"] / "apps/web/deploy.yaml"
    text = path.read_text()
    assert 'cpu: "4"' in text
    path.write_text(text.replace('cpu: "4"', 'cpu: "3"'))
    git(env["seed"], "commit", "-qam", "hand edit cpu limit")
    git(env["seed"], "push", "-q", "origin", "main")
    _, out, _ = run(env, rules=spike_rules(3.0))
    assert out["web"].status == "skipped:drift" and "HEAD differs from pending proposal" in out["web"].detail



def test_suppressed_field_keeps_pending_value_with_production_shaped_recs(tmp_path, monkeypatch):
    """A human reverted a cpu shrink; a grow is pending; usage drops again -> the fresh (suppressed) shrink must
    not be committed through the pending path (Architect D1)."""
    from rightsizing.config import Config
    from rightsizing.main import Runner
    from rightsizing.models import ContainerUsage, Usage
    from rightsizing.policy import PolicyParams, recommend_container

    cfg = Config()
    live = {"web": ContainerResources({"cpu.request": 1000.0, "cpu.limit": 4000.0,
                                       "memory.request": 1.0 * GI, "memory.limit": 2.0 * GI})}
    usage = ContainerUsage(cpu=Usage(636.0, 800.0), memory=Usage(0.95 * GI, 1.6 * GI), coverage_hours=168,
                           oom=False, restarts=0, throttle_ratio=0.0)
    rec_ = recommend_container("web", live["web"], usage, cfg.policies["development"], PolicyParams())
    assert any(c.field == "cpu.request" and c.direction == "shrink" for c in rec_.changes)  # fresh 700m shrink

    class Hist:
        def is_suppressed(self, workload, container, field, direction, days, now):
            return field == "cpu.request" and direction == "shrink"

    runner = Runner.__new__(Runner)
    runner.history, runner.cfg = Hist(), cfg
    runner.deps = type("D", (), {"now": datetime(2026, 11, 26, tzinfo=timezone.utc)})()
    runner._apply_suppression("development/Deployment/web", [rec_])
    assert "cpu.request" not in rec_.proposed and rec_.skipped["cpu.request"] == "suppressed_by_revert"
    pending = {("web", "cpu.request"): 1300.0}
    d = decide_pending(live, expected_head(live, pending), pending, [rec_], cfg.min_change)
    assert d.target["web"].get("cpu.request") == 1300.0  # pending grow kept, rejected shrink not re-proposed
    assert all(not (c.field == "cpu.request" and c.direction == "shrink") for c in d.changes)
