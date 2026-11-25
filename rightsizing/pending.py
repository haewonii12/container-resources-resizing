"""Decide what to do with a workload whose previous rightsizing proposal is still pending (not yet synced).

Recommendations are always computed against LIVE values. Against the pending proposal they are filtered with
hysteresis, so normal percentile jitter does not create a new commit every day:
- a pending field is withdrawn (back to live) only when the fresh value is within min_change/2 of live;
- it is replaced when the direction flips or it moved by >= min_change relative to the pending value;
- otherwise the pending value is kept.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import FIELDS, ContainerRec, ContainerResources, FieldChange

SHRINK_GUARDS = {"oom_recent", "oom_unknown", "throttled", "throttle_unknown"}
_EPS = 1e-6


@dataclass
class PendingDecision:
    kind: str  # same | supersede | withdraw | conflict
    target: dict[str, ContainerResources] = field(default_factory=dict)
    changes: list[FieldChange] = field(default_factory=list)  # target vs LIVE (what the trailers record)
    edits: dict[str, dict[str, float]] = field(default_factory=dict)  # container -> field -> value (target vs HEAD)
    detail: str = ""


def _same(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) < _EPS


def expected_head(live: dict[str, ContainerResources],
                  pending: dict[tuple[str, str], float]) -> dict[str, ContainerResources]:
    out = {c: dict(cr.values) for c, cr in live.items()}
    for (c, f), value in pending.items():
        out.setdefault(c, {name: None for name in FIELDS})[f] = value
    return {c: ContainerResources(v) for c, v in out.items()}


def decide_pending(live: dict[str, ContainerResources], head: dict[str, ContainerResources],
                   pending: dict[tuple[str, str], float], recs: list[ContainerRec],
                   min_change: float) -> PendingDecision:
    recs_by = {r.container: r for r in recs}
    target_vals = {c: dict(cr.values) for c, cr in live.items()}
    basis: dict[tuple[str, str], str] = {}
    keys = set(pending) | {(r.container, ch.field) for r in recs for ch in r.changes}

    for c, f in sorted(keys):
        if c not in live:
            continue
        live_v, pend, rec = live[c].get(f), pending.get((c, f)), recs_by.get(c)
        new_ch = next((ch for ch in rec.changes if ch.field == f), None) if rec else None
        if new_ch is not None:
            cand, why = new_ch.new, new_ch.basis
        elif rec is not None and f in rec.proposed:
            cand, why = rec.proposed[f], "최신 계산값"
        else:
            reason = (rec.skipped.get(f) or rec.skipped.get("*")) if rec else None
            if pend is not None and live_v is not None and pend < live_v and reason in SHRINK_GUARDS:
                cand, why = live_v, f"축소 보류 ({reason})"  # a pending shrink is no longer safe
            else:
                cand, why = (pend if pend is not None else live_v), "이전 제안 유지"

        if pend is None:
            final = cand
        elif live_v is not None and live_v > 0 and abs(cand - live_v) / live_v < min_change / 2:
            final, why = live_v, "현재 값 유지 (변경 불필요)"
        elif live_v is not None and not _same(cand, live_v) and (cand > live_v) != (pend > live_v):
            final = cand  # direction flipped (e.g. a usage spike: shrink -> grow)
        elif abs(cand - pend) / pend >= min_change:
            final = cand
        else:
            final, why = pend, "이전 제안 유지"
        if final is None:
            continue
        target_vals[c][f] = final
        basis[(c, f)] = why

    target = {c: ContainerResources(v) for c, v in target_vals.items()}
    for c, cr in target.items():
        for res in ("cpu", "memory"):
            req, lim = cr.get(f"{res}.request"), cr.get(f"{res}.limit")
            if req is not None and lim is not None and req > lim + _EPS:
                return PendingDecision("conflict", detail=f"{c}: {res} request > limit after merging proposals")

    edits: dict[str, dict[str, float]] = {}
    for c, cr in target.items():
        for f in FIELDS:
            value = cr.get(f)
            if value is not None and not _same(value, head[c].get(f) if c in head else None):
                edits.setdefault(c, {})[f] = value
    if not edits:
        return PendingDecision("same", target=target)

    changes: list[FieldChange] = []
    for c, cr in target.items():
        for f in FIELDS:
            old, new = live[c].get(f), cr.get(f)
            if new is None or _same(old, new):
                continue
            direction = "set" if old is None else ("shrink" if new < old else "grow")
            changes.append(FieldChange(c, f, old, new, direction, basis.get((c, f), "이전 제안 유지")))
    kind = "supersede" if changes else "withdraw"
    return PendingDecision(kind, target=target, changes=changes, edits=edits)
