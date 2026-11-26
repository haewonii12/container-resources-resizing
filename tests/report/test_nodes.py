from rightsizing.report.nodes import (Res, is_movable, node_rows, pod_requests, role_key, roles, simulate_failover,
                                      tolerates)

P = "node-role.kubernetes.io/"
GI = 2 ** 30


def node(name, roles_=("worker",), cpu="8", mem="32Gi", gpu=None, taints=None, cordon=False, pods="110"):
    alloc = {"cpu": cpu, "memory": mem, "pods": pods}
    if gpu:
        alloc["nvidia.com/gpu"] = gpu
    return {"metadata": {"name": name, "labels": {f"{P}{r}": "" for r in roles_}},
            "spec": {"taints": taints or [], "unschedulable": cordon}, "status": {"allocatable": alloc}}


def pod(name, on, cpu="1", mem="1Gi", gpu=None, ns="default", owner=None, mirror=False, phase="Running",
        tolerations=None):
    req = {"cpu": cpu, "memory": mem}
    if gpu:
        req["nvidia.com/gpu"] = gpu
    meta = {"name": name, "namespace": ns}
    if owner:
        meta["ownerReferences"] = [{"kind": owner, "name": "x"}]
    if mirror:
        meta["annotations"] = {"kubernetes.io/config.mirror": "abc"}
    return {"metadata": meta, "spec": {"nodeName": on, "containers": [{"resources": {"requests": req}}],
                                       "tolerations": tolerations or []}, "status": {"phase": phase}}


def by_node(rows):
    return {r.node: r for r in rows}


def test_roles_and_groups():
    assert role_key(node("a", ("gpu",)), P) == "gpu"
    assert role_key(node("b", ("control-plane", "master")), P) == "control-plane,master"
    assert roles(node("c", ("control-plane", "infra")), P) == ["infra"]  # cp dropped when another role exists
    assert role_key(node("d", ()), P) == "<none>"
    assert role_key(node("e", ("gpu",)), "example.com/role-") == "<none>"  # configurable prefix


def test_tainted_control_plane_excluded_untainted_included():
    cp_tainted = node("cp1", ("control-plane",), taints=[{"key": f"{P}control-plane", "effect": "NoSchedule"}])
    w1, w2 = node("w1"), node("w2")
    rows = by_node(simulate_failover([cp_tainted, w1, w2], [pod("a", "w1")], P))
    assert "cp1" not in rows and rows["w1"].targets == ["w2"]
    # the current cluster: one untainted control-plane that runs workloads
    single = node("promax", ("control-plane", "master"), cpu="20", mem="119Gi", gpu="1")
    rows = simulate_failover([single], [pod("vllm", "promax", gpu="1"), pod("db", "promax")], P)
    assert [(r.node, r.result) for r in rows] == [("promax", "단일 노드")] and len(rows[0].unplaced) == 2


def test_groups_never_cross_roles():
    rows = by_node(simulate_failover([node("g1", ("gpu",)), node("w1"), node("w2")], [pod("a", "g1")], P))
    assert rows["g1"].result == "단일 노드" and rows["w1"].targets == ["w2"]


def test_aggregate_enough_but_large_pod_fits_nowhere():
    nodes = [node("a", cpu="8", mem="32Gi"), node("b", cpu="8", mem="32Gi"), node("c", cpu="8", mem="32Gi")]
    pods = [pod("big", "a", cpu="1", mem="20Gi"), pod("fill-b", "b", cpu="1", mem="16Gi"),
            pod("fill-c", "c", cpu="1", mem="16Gi")]
    r = by_node(simulate_failover(nodes, pods, P))["a"]
    assert r.free.memory >= r.need.memory  # the sum says yes ...
    assert r.result == "불가" and r.unplaced and r.unplaced[0].startswith("default/big")  # ... placement says no


def test_first_fit_success_and_daemonset_static_not_moved():
    nodes = [node("a"), node("b")]
    pods = [pod("app", "a", cpu="2", mem="4Gi"), pod("ds", "a", cpu="7", owner="DaemonSet"),
            pod("static", "a", cpu="7", mirror=True), pod("done", "a", cpu="7", phase="Succeeded")]
    r = by_node(simulate_failover(nodes, pods, P))["a"]
    assert r.result == "가능" and r.moved == 1 and not r.unplaced
    assert not is_movable(pods[1]) and not is_movable(pods[2])


def test_gpu_dimension():
    nodes = [node("g1", ("gpu",), gpu="1"), node("g2", ("gpu",), gpu="1")]
    pods = [pod("train", "g1", gpu="1"), pod("infer", "g2", gpu="1")]
    assert by_node(simulate_failover(nodes, pods, P))["g1"].result == "불가"


def test_cordoned_is_source_not_target_and_pod_slots():
    nodes = [node("a"), node("b", cordon=True), node("c", pods="1")]
    pods = [pod("x", "a"), pod("y", "c")]
    rows = by_node(simulate_failover(nodes, pods, P))
    assert rows["a"].targets == ["c"] and rows["a"].result == "불가"  # c has no free pod slot
    assert "b" in rows  # cordoned node is still simulated as failing


def test_tolerations():
    tainted = node("t", taints=[{"key": "dedicated", "value": "gpu", "effect": "NoSchedule"}])
    plain, ok = pod("p", "x"), pod("q", "x", tolerations=[{"key": "dedicated", "operator": "Equal", "value": "gpu",
                                                          "effect": "NoSchedule"}])
    assert not tolerates(plain, tainted) and tolerates(ok, tainted)
    assert tolerates(pod("r", "x", tolerations=[{"operator": "Exists"}]), tainted)
    rows = by_node(simulate_failover([node("a"), tainted], [pod("app", "a")], P))
    assert rows["a"].result == "불가"


def test_scheduler_request_formula():
    p = {"spec": {"containers": [{"resources": {"requests": {"cpu": "500m", "memory": "1Gi"}}}],
                  "initContainers": [
                      {"restartPolicy": "Always", "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}}},
                      {"resources": {"requests": {"cpu": "2", "memory": "256Mi"}}}],
                  "overhead": {"cpu": "50m"}}}
    r = pod_requests(p)
    # cpu: max(500+100, 100+2000) + 50 ; memory: max(1Gi+128Mi, 128Mi+256Mi)
    assert r.cpu == 2150 and r.memory == GI + 128 * 2 ** 20


def test_node_rows():
    rows = node_rows([node("a", gpu="1")], [pod("x", "a", cpu="1500m", mem="2Gi", gpu="1"),
                                           pod("gone", "a", phase="Failed")], {"a": Res(700, 3 * GI, 0)}, P)
    r = rows[0]
    assert r.requests == Res(1500, 2 * GI, 1) and r.allocatable == Res(8000, 32 * GI, 1)
    assert r.usage == Res(700, 3 * GI, 0) and r.pods == 1 and r.max_pods == 110
    assert node_rows([node("b")], [], None, P)[0].usage is None


def test_unplaced_pods_listed_largest_first():
    pods = [pod("tiny", "n", cpu="0", mem="0"), pod("big", "n", cpu="4", mem="16Gi"), pod("mid", "n", mem="4Gi")]
    r = simulate_failover([node("n")], pods, P)[0]
    assert r.result == "단일 노드" and [u.split(" ")[0] for u in r.unplaced] == [
        "default/big", "default/mid", "default/tiny"]
