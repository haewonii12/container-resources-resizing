"""Deterministic rightsizing policy.

Order per container (see plan Step 2):
  coverage -> targets -> per-field guards/clamps -> round -> min-change -> invariants (QoS, request<=limit, LimitRange).

min-change is applied before the invariants (and the invariant pass may still raise a memory limit by
less than min_change), so a dropped small change can never leave an invariant broken. Rounding always
happens before the invariant pass, as the plan requires.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import quantity
from .config import NamespacePolicy
from .models import (FIELDS, ContainerRec, ContainerResources, ContainerUsage, FieldChange, LimitRangeBounds,
                     split_field)


@dataclass
class PolicyParams:
    min_days: int = 3
    min_change: float = 0.10
    throttle_threshold: float = 0.05
    cpu_limit_mode: str = "keep"


def _fmt_mult(value: float) -> str:
    return f"{value:g}"


def _targets(current: ContainerResources, usage: ContainerUsage, policy: NamespacePolicy,
             params: PolicyParams, rec: ContainerRec) -> dict[str, tuple[float | None, str]]:
    k, kl = policy.request_multiplier, policy.mem_limit_multiplier
    cpu_p95, mem_p95, mem_max = usage.cpu.p95, usage.memory.p95, usage.memory.max
    targets: dict[str, tuple[float | None, str]] = {}

    def cpu_req():
        if cpu_p95 is None:
            return None, ""
        return cpu_p95 * k, f"P95 {quantity.human('cpu', cpu_p95)}, ×{_fmt_mult(k)}"

    if current.is_guaranteed():
        if mem_p95 is None or mem_max is None:
            mem = (None, "")
        else:
            mem = (max(mem_p95 * k, mem_max * kl),
                   f"Guaranteed: max(P95 {quantity.human('memory', mem_p95)}×{_fmt_mult(k)}, "
                   f"max {quantity.human('memory', mem_max)}×{_fmt_mult(kl)})")
        targets["memory.request"] = targets["memory.limit"] = mem
        if params.cpu_limit_mode == "adjust":
            value, basis = cpu_req()
            targets["cpu.request"] = targets["cpu.limit"] = (value, f"Guaranteed: {basis}" if basis else "")
        else:
            rec.skipped["cpu.request"] = rec.skipped["cpu.limit"] = "guaranteed_cpu_limit_kept"
        return targets

    targets["cpu.request"] = cpu_req()
    targets["memory.request"] = ((mem_p95 * k, f"P95 {quantity.human('memory', mem_p95)}, ×{_fmt_mult(k)}")
                                 if mem_p95 is not None else (None, ""))
    targets["memory.limit"] = ((mem_max * kl, f"max {quantity.human('memory', mem_max)}, ×{_fmt_mult(kl)}")
                               if mem_max is not None else (None, ""))
    if params.cpu_limit_mode == "keep":
        rec.skipped.setdefault("cpu.limit", "cpu_limit_kept")
    return targets


def _shrink_guard(resource: str, usage: ContainerUsage, params: PolicyParams) -> str | None:
    if resource == "memory":
        if usage.oom is None:
            return "oom_unknown"
        if usage.oom:
            return "oom_recent"
        return None
    if usage.throttle_ratio is None:
        return "throttle_unknown"
    if usage.throttle_ratio > params.throttle_threshold:
        return "throttled"
    return None


def recommend_container(name: str, current: ContainerResources, usage: ContainerUsage, policy: NamespacePolicy,
                        params: PolicyParams | None = None, limitrange: LimitRangeBounds | None = None,
                        blocked_resources: frozenset[str] | set[str] = frozenset()) -> ContainerRec:
    params = params or PolicyParams()
    rec = ContainerRec(name)
    if usage.coverage_hours is None or usage.coverage_hours < params.min_days * 24:
        rec.skipped["*"] = "insufficient_data"
        return rec

    guaranteed = current.is_guaranteed()
    changes: dict[str, FieldChange] = {}
    for fname, (target, basis) in _targets(current, usage, policy, params, rec).items():
        if fname in rec.skipped:
            continue
        resource, kind = split_field(fname)
        if resource in blocked_resources:
            rec.skipped[fname] = "hpa_utilization"
            continue
        if target is None:
            rec.skipped[fname] = "unknown_usage"
            continue
        old = current.get(fname)
        if old is None:
            if kind == "limit":
                rec.skipped[fname] = "limit_unset_kept"
                continue
            direction = "set"
        else:
            direction = "shrink" if target < old else "grow"
            if direction == "shrink":
                guard = _shrink_guard(resource, usage, params)
                if guard:
                    rec.skipped[fname] = guard
                    continue
                floor = old * (1 - policy.max_shrink)
                if target < floor:
                    target = floor
                    basis += f", 축소 상한 -{policy.max_shrink:.0%}"
            elif policy.max_increase is not None and target > old * policy.max_increase:
                target = old * policy.max_increase
                basis += f", 증가 상한 ×{_fmt_mult(policy.max_increase)}"
        new = float(quantity.round_up(resource, target))
        rec.proposed[fname] = new
        if old is not None:
            if abs(new - old) < 1e-6:
                rec.skipped[fname] = "no_change"
                continue
            if abs(new - old) / old < params.min_change:
                rec.skipped[fname] = "below_min_change"
                continue
            direction = "shrink" if new < old else "grow"
        changes[fname] = FieldChange(name, fname, old, new, direction, basis)

    _apply_invariants(rec, current, changes, guaranteed, limitrange)
    rec.changes = [changes[f] for f in FIELDS if f in changes]
    return rec


def _final(current: ContainerResources, changes: dict[str, FieldChange]) -> dict[str, float | None]:
    return {f: (changes[f].new if f in changes else current.get(f)) for f in FIELDS}


def _drop_all(rec: ContainerRec, changes: dict[str, FieldChange], reason: str) -> None:
    for f in list(changes):
        rec.skipped[f] = reason
    changes.clear()


def _apply_invariants(rec: ContainerRec, current: ContainerResources, changes: dict[str, FieldChange],
                      guaranteed: bool, limitrange: LimitRangeBounds | None) -> None:
    # Guaranteed pairs must move together.
    if guaranteed:
        for res in ("cpu", "memory"):
            pair = (f"{res}.request", f"{res}.limit")
            if (pair[0] in changes) != (pair[1] in changes):
                for f in pair:
                    changes.pop(f, None)
                    rec.skipped[f] = "qos_pair"

    final = _final(current, changes)
    # CPU: never silently cap a request at a kept limit -> skip the field.
    if (final["cpu.request"] is not None and final["cpu.limit"] is not None
            and final["cpu.request"] > final["cpu.limit"] + 1e-6 and "cpu.request" in changes):
        changes.pop("cpu.request")
        rec.skipped["cpu.request"] = "exceeds_cpu_limit"

    final = _final(current, changes)
    # Memory: a request above the limit raises the limit to the request.
    if (final["memory.request"] is not None and final["memory.limit"] is not None
            and final["memory.request"] > final["memory.limit"] + 1e-6):
        old = current.get("memory.limit")
        new = final["memory.request"]
        changes["memory.limit"] = FieldChange(rec.container, "memory.limit", old, new,
                                              "shrink" if old is not None and new < old else "grow",
                                              "request 이상으로 상향")
        rec.skipped.pop("memory.limit", None)

    after = ContainerResources(_final(current, changes))
    if guaranteed and changes and not after.is_guaranteed():
        _drop_all(rec, changes, "qos_pair")
        return
    if not guaranteed and changes and after.is_guaranteed():
        _drop_all(rec, changes, "qos_change")
        return

    if limitrange and changes:
        final = _final(current, changes)
        for res in ("cpu", "memory"):
            req, lim = final[f"{res}.request"], final[f"{res}.limit"]
            violated = False
            for value in (req, lim):
                if value is None:
                    continue
                if res in limitrange.min and value < limitrange.min[res] - 1e-6:
                    violated = True
                if res in limitrange.max and value > limitrange.max[res] + 1e-6:
                    violated = True
            if res in limitrange.max_ratio and req and lim and lim / req > limitrange.max_ratio[res] + 1e-9:
                violated = True
            if violated:
                for f in (f"{res}.request", f"{res}.limit"):
                    if f in changes:
                        changes.pop(f)
                        rec.skipped[f] = "limitrange"
