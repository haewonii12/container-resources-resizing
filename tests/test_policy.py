import pytest

from rightsizing.config import Config
from rightsizing.models import ContainerResources, ContainerUsage, LimitRangeBounds, Usage
from rightsizing.policy import PolicyParams, recommend_container
from rightsizing.quantity import GI, MI, round_cpu_up, round_mem_up

CFG = Config()
INF = CFG.policies["inference"]
DEV = CFG.policies["development"]


def res(cpu_req=None, cpu_lim=None, mem_req=None, mem_lim=None):
    return ContainerResources({"cpu.request": cpu_req, "cpu.limit": cpu_lim,
                               "memory.request": mem_req, "memory.limit": mem_lim})


def usage(cpu_p95=1000, cpu_max=1500, mem_p95=4 * GI, mem_max=6 * GI, hours=168, oom=False, throttle=0.0):
    return ContainerUsage(cpu=Usage(cpu_p95, cpu_max), memory=Usage(mem_p95, mem_max), coverage_hours=hours,
                          oom=oom, restarts=0, throttle_ratio=throttle)


def changes(rec):
    return {c.field: c for c in rec.changes}


@pytest.mark.parametrize("policy,k,kl", [(INF, 1.3, 1.5), (DEV, 1.1, 1.2)])
def test_namespace_multipliers(policy, k, kl):
    cur = res(cpu_req=1500, cpu_lim=4000, mem_req=7 * GI, mem_lim=10 * GI)
    rec = recommend_container("app", cur, usage(cpu_p95=1000, mem_p95=4.2 * GI, mem_max=6 * GI), policy)
    ch = changes(rec)
    assert ch["cpu.request"].new == round_cpu_up(1000 * k)
    assert ch["memory.request"].new == round_mem_up(4.2 * GI * k)
    expected_lim = max(round_mem_up(6 * GI * kl), 10 * GI * (1 - policy.max_shrink))
    assert ch["memory.limit"].new == pytest.approx(round_mem_up(expected_lim))
    assert "cpu.limit" not in ch and rec.skipped["cpu.limit"] == "cpu_limit_kept"


@pytest.mark.parametrize("policy,max_shrink", [(INF, 0.25), (DEV, 0.50)])
def test_shrink_clamped(policy, max_shrink):
    cur = res(cpu_req=4000, cpu_lim=8000, mem_req=16 * GI, mem_lim=32 * GI)
    rec = recommend_container("app", cur, usage(cpu_p95=100, mem_p95=1 * GI, mem_max=1 * GI), policy)
    ch = changes(rec)
    assert ch["cpu.request"].new == round_cpu_up(4000 * (1 - max_shrink))
    assert ch["memory.request"].new == round_mem_up(16 * GI * (1 - max_shrink))
    assert "축소 상한" in ch["cpu.request"].basis


def test_grow_clamped_by_max_increase():
    cur = res(cpu_req=100, cpu_lim=8000, mem_req=256 * MI, mem_lim=8 * GI)
    rec = recommend_container("app", cur, usage(cpu_p95=1000, mem_p95=1 * GI, mem_max=1 * GI), DEV)
    ch = changes(rec)
    assert ch["cpu.request"].new == 200 and ch["cpu.request"].direction == "grow"
    assert ch["memory.request"].new == 512 * MI


def test_insufficient_coverage_skips_everything():
    rec = recommend_container("app", res(1000, 2000, GI, 2 * GI), usage(hours=71), INF)
    assert rec.changes == [] and rec.skipped == {"*": "insufficient_data"}


def test_below_min_change_dropped():
    cur = res(cpu_req=1100, cpu_lim=4000, mem_req=int(round_mem_up(4 * GI * 1.1)), mem_lim=None)
    rec = recommend_container("app", cur, usage(cpu_p95=950, mem_p95=4 * GI), DEV)  # 950*1.1=1045 -> -5%
    assert "cpu.request" not in changes(rec)
    assert rec.skipped["cpu.request"] == "below_min_change"


def test_unknown_usage_blocks_field():
    u = usage()
    u.cpu = Usage(None, None)
    rec = recommend_container("app", res(4000, 8000, 8 * GI, 16 * GI), u, DEV)
    assert rec.skipped["cpu.request"] == "unknown_usage"
    assert "memory.request" in changes(rec)


@pytest.mark.parametrize("oom,reason", [(True, "oom_recent"), (None, "oom_unknown")])
def test_oom_blocks_memory_shrink(oom, reason):
    rec = recommend_container("app", res(4000, 8000, 16 * GI, 32 * GI), usage(oom=oom), DEV)
    assert rec.skipped["memory.request"] == reason and rec.skipped["memory.limit"] == reason
    assert "cpu.request" in changes(rec)


def test_oom_does_not_block_memory_grow():
    rec = recommend_container("app", res(1000, 8000, 1 * GI, 2 * GI), usage(mem_p95=3 * GI, mem_max=3 * GI, oom=True),
                              DEV)
    assert changes(rec)["memory.request"].direction == "grow"


@pytest.mark.parametrize("throttle,reason", [(0.2, "throttled"), (None, "throttle_unknown")])
def test_throttle_blocks_cpu_shrink(throttle, reason):
    rec = recommend_container("app", res(4000, 8000, 16 * GI, 32 * GI), usage(throttle=throttle), DEV)
    assert rec.skipped["cpu.request"] == reason
    assert "memory.request" in changes(rec)


def test_gpu_and_unrelated_keys_untouched():
    rec = recommend_container("app", res(4000, 8000, 16 * GI, 32 * GI), usage(), DEV)
    assert all(c.field.split(".")[0] in ("cpu", "memory") for c in rec.changes)


def test_hpa_blocked_resource():
    rec = recommend_container("app", res(4000, 8000, 16 * GI, 32 * GI), usage(), DEV, blocked_resources={"cpu"})
    assert rec.skipped["cpu.request"] == "hpa_utilization"
    assert "memory.request" in changes(rec)


def test_rounding_before_invariant_cpu_request_exceeds_kept_limit():
    # P95 1000 * 1.3 = 1300 -> exceeds kept limit 1200: skip, never silently cap
    rec = recommend_container("app", res(700, 1200, 4 * GI, 8 * GI), usage(cpu_p95=1000), INF)
    assert rec.skipped["cpu.request"] == "exceeds_cpu_limit"
    assert "cpu.request" not in changes(rec)


def test_rounding_happens_before_invariant():
    # 917*1.3 = 1192.1 -> rounds up to 1200 == limit 1200: allowed (not >)
    rec = recommend_container("app", res(1500, 1200, 4 * GI, 8 * GI), usage(cpu_p95=917), INF)
    assert changes(rec)["cpu.request"].new == 1200


def test_memory_limit_raised_to_request():
    # request grows to 5.2Gi*1.3, limit kept by OOM guard? -> use a case where limit target < request
    cur = res(1000, 4000, 4 * GI, 5 * GI)
    rec = recommend_container("app", cur, usage(mem_p95=4.5 * GI, mem_max=4.6 * GI), INF)
    ch = changes(rec)
    assert ch["memory.limit"].new >= ch["memory.request"].new


def test_memory_limit_unset_not_introduced():
    rec = recommend_container("app", res(4000, None, 16 * GI, None), usage(), DEV)
    assert rec.skipped["memory.limit"] == "limit_unset_kept"
    assert "memory.request" in changes(rec)


def test_missing_request_is_set():
    rec = recommend_container("app", res(None, None, None, None), usage(), DEV)
    ch = changes(rec)
    assert ch["cpu.request"].direction == "set" and ch["memory.request"].direction == "set"


def test_guaranteed_memory_rule_and_cpu_kept():
    cur = res(2000, 2000, 8 * GI, 8 * GI)
    rec = recommend_container("app", cur, usage(mem_p95=3 * GI, mem_max=4 * GI), DEV)
    ch = changes(rec)
    assert ch["memory.request"].new == ch["memory.limit"].new
    expected = round_mem_up(max(3 * GI * 1.1, 4 * GI * 1.2))
    assert ch["memory.request"].new == expected
    assert rec.skipped["cpu.request"] == "guaranteed_cpu_limit_kept"


def test_guaranteed_cpu_adjust_mode_moves_pair():
    cur = res(2000, 2000, 8 * GI, 8 * GI)
    rec = recommend_container("app", cur, usage(cpu_p95=1000), DEV, PolicyParams(cpu_limit_mode="adjust"))
    ch = changes(rec)
    assert ch["cpu.request"].new == ch["cpu.limit"].new == 1100


def test_burstable_never_becomes_guaranteed():
    # cpu req/limit already equal; memory request would become equal to limit via raise-to-request
    cur = res(1000, 1000, 4 * GI, int(round_mem_up(4.5 * GI * 1.3)))
    rec = recommend_container("app", cur, usage(cpu_p95=900, mem_p95=4.5 * GI, mem_max=1 * GI), INF)
    after = {f: v for f, v in cur.values.items()}
    for c in rec.changes:
        after[c.field] = c.new
    assert not ContainerResources(after).is_guaranteed()


def test_limitrange_max_and_ratio_skip():
    lr = LimitRangeBounds(max={"memory": 6 * GI})
    rec = recommend_container("app", res(1000, 4000, 4 * GI, 5 * GI), usage(mem_p95=4.5 * GI, mem_max=4.6 * GI), INF,
                              limitrange=lr)
    assert rec.skipped["memory.request"] == "limitrange"
    lr2 = LimitRangeBounds(max_ratio={"cpu": 2})
    rec2 = recommend_container("app", res(4000, 8000, 16 * GI, 32 * GI), usage(cpu_p95=1000), DEV, limitrange=lr2)
    assert rec2.skipped["cpu.request"] == "limitrange"



def test_min_days_zero_accepts_sparse_data():
    rec = recommend_container("app", res(4000, 8000, 16 * GI, 32 * GI), usage(hours=5), DEV, PolicyParams(min_days=0))
    assert rec.changes and "*" not in rec.skipped
    assert recommend_container("app", res(4000, 8000, 16 * GI, 32 * GI), usage(hours=5), DEV,
                               PolicyParams(min_days=1)).skipped == {"*": "insufficient_data"}
