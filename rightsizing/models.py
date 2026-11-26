"""Shared data types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from . import quantity

RESOURCES = ("cpu", "memory")
FIELDS = ("cpu.request", "cpu.limit", "memory.request", "memory.limit")
Direction = Literal["shrink", "grow", "set"]


def split_field(name: str) -> tuple[str, str]:
    resource, kind = name.split(".")
    return resource, kind


@dataclass(frozen=True)
class WorkloadRef:
    namespace: str
    kind: str  # Deployment | StatefulSet
    name: str

    @property
    def key(self) -> str:
        return f"{self.namespace}/{self.kind}/{self.name}"


@dataclass
class ContainerResources:
    """Normalized values: cpu in millicores, memory in bytes, None = unset."""

    values: dict[str, float | None] = field(default_factory=lambda: {f: None for f in FIELDS})

    @classmethod
    def from_k8s(cls, resources: dict | None) -> "ContainerResources":
        resources = resources or {}
        out = {f: None for f in FIELDS}
        for section, kind in (("requests", "request"), ("limits", "limit")):
            for res in RESOURCES:
                raw = (resources.get(section) or {}).get(res)
                if raw is not None:
                    out[f"{res}.{kind}"] = quantity.parse(res, raw)
        return cls(out)

    def get(self, name: str) -> float | None:
        return self.values.get(name)

    def same_as(self, other: "ContainerResources", fields: tuple[str, ...] = FIELDS) -> bool:
        for f in fields:
            a, b = self.values.get(f), other.values.get(f)
            if (a is None) != (b is None):
                return False
            if a is not None and abs(a - b) > 1e-6:
                return False
        return True

    def is_guaranteed(self) -> bool:
        v = self.values
        if any(v[f] is None for f in FIELDS):
            return False
        return abs(v["cpu.request"] - v["cpu.limit"]) < 1e-6 and abs(v["memory.request"] - v["memory.limit"]) < 1e-6


@dataclass
class Usage:
    p95: float | None = None
    max: float | None = None


@dataclass
class ContainerUsage:
    cpu: Usage = field(default_factory=Usage)
    memory: Usage = field(default_factory=Usage)
    coverage_hours: float | None = None
    oom: bool | None = None  # None = unknown
    restarts: float | None = None
    throttle_ratio: float | None = None  # None = unknown


@dataclass
class LimitRangeBounds:
    min: dict[str, float] = field(default_factory=dict)  # resource -> value
    max: dict[str, float] = field(default_factory=dict)
    max_ratio: dict[str, float] = field(default_factory=dict)


@dataclass
class FieldChange:
    container: str
    field: str
    old: float | None
    new: float
    direction: Direction
    basis: str

    @property
    def resource(self) -> str:
        return split_field(self.field)[0]


@dataclass
class ContainerRec:
    container: str
    changes: list[FieldChange] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)  # field (or "*") -> reason
    notes: list[str] = field(default_factory=list)
    proposed: dict[str, float] = field(default_factory=dict)  # rounded target even when dropped by min_change


@dataclass
class Outcome:
    workload: str
    status: str  # committed | held:<r> | skipped:<r> | proposed (dry-run) | no_change
    detail: str = ""
    commit: str | None = None
    llm_decision: str | None = None  # apply | hold | None (LLM not consulted)
    llm_notes: str = ""
    proposal: list[dict] = field(default_factory=list)  # per field: container, field, current, proposed, ...
    pending: dict | None = None  # {sha, age_days, kind}: a proposal still waiting for a human sync
