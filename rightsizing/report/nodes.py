"""Node capacity and N-1 failover simulation (pure functions over Kubernetes objects as dicts).

Operating model: pods only move between nodes of the same role (node-role.kubernetes.io/<role> labels).
A failed node's movable pods are placed first-fit-decreasing on the other eligible nodes of its role group.
Not simulated: nodeSelector/affinity/topology spread beyond the role, PodDisruptionBudgets, priorities.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..quantity import parse_cpu, parse_mem, parse_number

GPU = "nvidia.com/gpu"
CP_ROLES = {"control-plane", "master"}
HARD_EFFECTS = {"NoSchedule", "NoExecute"}
RESOURCES = ("cpu", "memory", "gpu")


@dataclass(frozen=True)
class Res:
    cpu: float = 0.0  # millicores
    memory: float = 0.0  # bytes
    gpu: float = 0.0

    def __add__(self, o: "Res") -> "Res":
        return Res(self.cpu + o.cpu, self.memory + o.memory, self.gpu + o.gpu)

    def __sub__(self, o: "Res") -> "Res":
        return Res(self.cpu - o.cpu, self.memory - o.memory, self.gpu - o.gpu)

    def fits_in(self, free: "Res") -> bool:
        return all(getattr(self, r) <= getattr(free, r) + 1e-9 for r in RESOURCES)

    @staticmethod
    def max(a: "Res", b: "Res") -> "Res":
        return Res(max(a.cpu, b.cpu), max(a.memory, b.memory), max(a.gpu, b.gpu))


def parse_res(values: dict | None) -> Res:
    values = values or {}
    cpu = parse_cpu(values["cpu"]) if "cpu" in values else 0.0
    mem = parse_mem(values["memory"]) if "memory" in values else 0.0
    gpu = float(parse_number(values[GPU])) if GPU in values else 0.0
    return Res(cpu, mem, gpu)


def _container_req(c: dict) -> Res:
    return parse_res((c.get("resources") or {}).get("requests"))


def pod_requests(pod: dict) -> Res:
    """Effective request as kube-scheduler computes it (sidecars = init containers with restartPolicy Always)."""
    spec = pod.get("spec") or {}
    total = Res()
    for c in spec.get("containers") or []:
        total = total + _container_req(c)
    sidecars, init_peak = Res(), Res()
    for c in spec.get("initContainers") or []:
        req = _container_req(c)
        if c.get("restartPolicy") == "Always":
            sidecars = sidecars + req
            init_peak = Res.max(init_peak, sidecars)
        else:
            init_peak = Res.max(init_peak, sidecars + req)
    effective = Res.max(total + sidecars, init_peak)
    return effective + parse_res(spec.get("overhead"))


def is_active(pod: dict) -> bool:
    return bool((pod.get("spec") or {}).get("nodeName")) and \
        (pod.get("status") or {}).get("phase") not in ("Succeeded", "Failed")


def is_movable(pod: dict) -> bool:
    meta = pod.get("metadata") or {}
    if "kubernetes.io/config.mirror" in (meta.get("annotations") or {}):
        return False  # static pod: lives and dies with its node
    return not any(o.get("kind") == "DaemonSet" for o in meta.get("ownerReferences") or [])


def roles(node: dict, prefix: str) -> list[str]:
    labels = (node.get("metadata") or {}).get("labels") or {}
    found = sorted(k[len(prefix):] for k in labels if k.startswith(prefix) and k[len(prefix):])
    workload_roles = [r for r in found if r not in CP_ROLES]
    return workload_roles or found


def role_key(node: dict, prefix: str) -> str:
    return ",".join(roles(node, prefix)) or "<none>"


def _taints(node: dict) -> list[dict]:
    return [t for t in (node.get("spec") or {}).get("taints") or [] if t.get("effect") in HARD_EFFECTS]


def is_tainted_control_plane(node: dict, prefix: str) -> bool:
    labels = (node.get("metadata") or {}).get("labels") or {}
    cp = any(f"{prefix}{r}" in labels for r in CP_ROLES)
    cp_taint = any(t.get("key") in (f"{prefix}{r}" for r in CP_ROLES) for t in _taints(node))
    return (cp and bool(_taints(node))) or cp_taint


def tolerates(pod: dict, node: dict) -> bool:
    tolerations = (pod.get("spec") or {}).get("tolerations") or []
    for taint in _taints(node):
        ok = False
        for tol in tolerations:
            if tol.get("effect") and tol.get("effect") != taint.get("effect"):
                continue
            if tol.get("operator") == "Exists" and not tol.get("key"):
                ok = True
            elif tol.get("key") == taint.get("key") and (
                    tol.get("operator") == "Exists" or tol.get("value", "") == taint.get("value", "")):
                ok = True
            if ok:
                break
        if not ok:
            return False
    return True


@dataclass
class NodeRow:
    name: str
    role: str
    allocatable: Res
    requests: Res
    usage: Res | None
    pods: int
    max_pods: int
    analyzed: bool  # False: tainted control-plane (excluded from N-1)
    cordoned: bool
    ready: bool = True
    taints: list[str] = field(default_factory=list)


@dataclass
class FailoverRow:
    node: str
    role: str
    targets: list[str]
    result: str  # 가능 | 불가 | 단일 노드
    unplaced: list[str] = field(default_factory=list)  # "ns/pod (cpu, mem, gpu)"
    moved: int = 0
    need: Res = field(default_factory=Res)
    free: Res = field(default_factory=Res)


def _name(obj: dict) -> str:
    return (obj.get("metadata") or {}).get("name", "")


def _ready(node: dict) -> bool:
    conds = (node.get("status") or {}).get("conditions") or []
    ready = next((c for c in conds if c.get("type") == "Ready"), None)
    return ready is None or ready.get("status") == "True"  # no conditions (tests/fakes) counts as Ready


def node_rows(nodes: list[dict], pods: list[dict], usage: dict[str, Res] | None, prefix: str) -> list[NodeRow]:
    rows = []
    for node in sorted(nodes, key=_name):
        name = _name(node)
        on = [p for p in pods if is_active(p) and p["spec"]["nodeName"] == name]
        alloc = (node.get("status") or {}).get("allocatable") or {}
        rows.append(NodeRow(
            name=name, role=role_key(node, prefix), allocatable=parse_res(alloc),
            requests=sum((pod_requests(p) for p in on), Res()), usage=(usage or {}).get(name),
            pods=len(on), max_pods=int(parse_number(alloc.get("pods", "110"))),
            analyzed=not is_tainted_control_plane(node, prefix),
            cordoned=bool((node.get("spec") or {}).get("unschedulable")),
            ready=_ready(node),
            taints=[f"{t.get('key')}:{t.get('effect')}" for t in _taints(node)]))
    return rows


def simulate_failover(nodes: list[dict], pods: list[dict], prefix: str) -> list[FailoverRow]:
    by_name = {_name(n): n for n in nodes}
    rows = node_rows(nodes, pods, None, prefix)
    active = [p for p in pods if is_active(p)]
    out = []
    for row in rows:
        if not row.analyzed:
            continue
        targets = [r for r in rows if r.analyzed and not r.cordoned and r.ready
                   and r.name != row.name and r.role == row.role]
        movable = sorted((p for p in active if p["spec"]["nodeName"] == row.name and is_movable(p)),
                         key=lambda p: (pod_requests(p).gpu, pod_requests(p).memory, pod_requests(p).cpu),
                         reverse=True)  # largest first: placement order and the order unplaced pods are listed
        need = sum((pod_requests(p) for p in movable), Res())
        free = {t.name: t.allocatable - t.requests for t in targets}
        slots = {t.name: t.max_pods - t.pods for t in targets}
        fr = FailoverRow(node=row.name, role=row.role, targets=[t.name for t in targets], result="",
                         need=need, free=sum(free.values(), Res()))
        if not targets:
            fr.result = "단일 노드"
            fr.unplaced = [_pod_label(p) for p in movable]
            out.append(fr)
            continue
        for pod in movable:
            req = pod_requests(pod)
            dest = next((t for t in fr.targets if slots[t] >= 1 and req.fits_in(free[t])
                         and tolerates(pod, by_name[t])), None)
            if dest is None:
                fr.unplaced.append(_pod_label(pod))
                continue
            free[dest] = free[dest] - req
            slots[dest] -= 1
            fr.moved += 1
        fr.result = "불가" if fr.unplaced else "가능"
        out.append(fr)
    return out


def _pod_label(pod: dict) -> str:
    meta = pod.get("metadata") or {}
    r = pod_requests(pod)
    gpu = f", gpu {r.gpu:g}" if r.gpu else ""
    return f"{meta.get('namespace')}/{meta.get('name')} (cpu {r.cpu:.0f}m, mem {r.memory / 2 ** 30:.1f}Gi{gpu})"
