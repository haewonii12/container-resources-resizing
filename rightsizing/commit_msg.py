"""Commit message rendering and trailer parsing (the commit message is the history record)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import quantity
from .models import FieldChange, WorkloadRef, split_field

_LABEL = {"cpu.request": "cpu.request", "cpu.limit": "cpu.limit", "memory.request": "mem.request",
          "memory.limit": "mem.limit"}
_KO = {"cpu.request": "CPU request", "cpu.limit": "CPU limit", "memory.request": "메모리 request",
       "memory.limit": "메모리 limit"}
_DIR_KO = {"shrink": "축소", "grow": "증가", "set": "신규 설정"}
TRAILER_RE = re.compile(r"^Rightsizing-(Id|Workload|Change|Run|Supersedes|Withdraw): (.+)$", re.M)
CHANGE_RE = re.compile(r"^(?P<container>[^.]+)\.(?P<field>(cpu|memory)\.(request|limit))="
                       r"(?P<old>[^,]*)->(?P<new>[^,]+),(?P<direction>shrink|grow|set)$")
REVERT_RE = re.compile(r"^This reverts commit ([0-9a-f]{7,40})", re.M)
_FORGERY_RE = re.compile(r"(?i)(rightsizing-(id|workload|change|run|supersedes|withdraw)\s*:|this reverts commit)")


def sanitize_note(text: str, limit: int = 500) -> str:
    """LLM notes are untrusted: one line, no trailer-like or revert-like text (they must not forge history)."""
    one_line = re.sub(r"\s+", " ", text or "").strip()
    one_line = _FORGERY_RE.sub("[removed]", one_line)
    return one_line[:limit]


def trailer_block(message: str) -> str:
    """The last paragraph of the message: the only place trailers are read from."""
    paragraphs = [p for p in re.split(r"\n\s*\n", message.strip()) if p.strip()]
    return paragraphs[-1] if paragraphs else ""


@dataclass
class CommitInfo:
    workload: WorkloadRef
    date: str  # YYYYMMDD
    run_id: str
    changes: list[FieldChange]
    window: str  # "2026-11-18..2026-11-25 (7d)"
    oom: dict[str, float | None]  # container -> restarts/oom count (None = unknown)
    throttle: dict[str, float | None]  # container -> ratio
    policy_label: str
    avg_replicas: float | None
    llm_note: str
    supersedes: str | None = None  # sha of the pending proposal this commit replaces


@dataclass
class ParsedChange:
    container: str
    field: str
    old: float | None
    new: float
    direction: str


@dataclass
class ParsedTrailers:
    id: str | None = None
    workload: str | None = None
    run: str | None = None
    changes: list[ParsedChange] = field(default_factory=list)
    supersedes: str | None = None
    withdraw: str | None = None

    @property
    def kind(self) -> str:
        if self.withdraw:
            return "withdraw"
        return "supersede" if self.supersedes else "propose"


def _q(fname: str, value: float | None) -> str:
    return "unset" if value is None else quantity.format_quantity(split_field(fname)[0], value)


def _pct(change: FieldChange) -> str:
    if change.old in (None, 0):
        return ""
    return f"{abs(change.new - change.old) / change.old:.0%} "


def title(info: CommitInfo) -> str:
    parts = []
    for c in info.changes:
        parts.append(f"{c.container} {_LABEL[c.field]} {_q(c.field, c.old)}→{_q(c.field, c.new)}")
    text = ", ".join(parts)
    if len(text) > 120:
        text = f"{len(info.changes)} resource fields in {len({c.container for c in info.changes})} container(s)"
    return f"rightsizing({info.workload.namespace}/{info.workload.name}): {text}"


def summary_ko(info: CommitInfo) -> str:
    by_container: dict[str, list[str]] = {}
    for c in info.changes:
        by_container.setdefault(c.container, []).append(f"{_KO[c.field]} {_pct(c)}{_DIR_KO[c.direction]}")
    body = "; ".join(f"{name} 컨테이너 {', '.join(items)}" for name, items in by_container.items())
    return f"{body} (최근 사용량 P95/max 기준)"


def requests_savings(changes, replicas: float) -> tuple[float, float]:
    """(cpu millicores, memory bytes) freed by request changes x replicas. Items are FieldChange-like
    (.field/.old/.new) or run-record proposal rows ({"field", "current", "proposed"})."""
    keys = {"old": "current", "new": "proposed", "field": "field"}

    def get(c, k):
        return c[keys[k]] if isinstance(c, dict) else getattr(c, k)
    cpu = sum(get(c, "old") - get(c, "new") for c in changes
              if get(c, "field") == "cpu.request" and get(c, "old") is not None) * replicas
    mem = sum(get(c, "old") - get(c, "new") for c in changes
              if get(c, "field") == "memory.request" and get(c, "old") is not None) * replicas
    return cpu, mem


def savings(info: CommitInfo) -> str:
    reps = info.avg_replicas if info.avg_replicas is not None else 1.0
    cpu, mem = requests_savings(info.changes, reps)
    note = f"평균 replicas {reps:.1f}" if info.avg_replicas is not None else "replicas 정보 없음, 1로 계산"
    return f"CPU {cpu / 1000:.1f} cores, Memory {mem / quantity.GI:.1f} GiB (requests 기준, {note})"


def render(info: CommitInfo) -> str:
    lines = [title(info), "", f"요약: {summary_ko(info)}"]
    containers: dict[str, list[FieldChange]] = {}
    for c in info.changes:
        containers.setdefault(c.container, []).append(c)
    for name, items in containers.items():
        lines.append(f"container: {name}")
        for c in items:
            lines.append(f"  {_LABEL[c.field]:<12}{_q(c.field, c.old):>8} → {_q(c.field, c.new):<8} ({c.basis})")
        oom = info.oom.get(name)
        thr = info.throttle.get(name)
        lines.append(f"  OOM: {'unknown' if oom is None else int(oom)}, throttle: "
                     f"{'unknown' if thr is None else f'{thr:.1%}'}")
    lines.append(f"window: {info.window}")
    lines.append(f"policy: {info.policy_label}")
    lines.append(f"예상 절감: {savings(info)}")
    if info.supersedes:
        lines.append(f"대체: 승인 대기 중이던 제안 {info.supersedes[:10]}을 최신 사용량 기준으로 교체합니다 "
                     "(변경 전 값은 현재 클러스터 값 기준)")
    lines.append(f"LLM 판단: {sanitize_note(info.llm_note) or '특이사항 없음'}")
    lines.append("거절하려면: git revert <this commit>  (revert 후 같은 방향 제안은 일정 기간 다시 나오지 않습니다)")
    lines.append("")
    w = info.workload
    lines.append(f"Rightsizing-Id: {info.date}-{w.namespace}-{w.kind.lower()}-{w.name}")
    lines.append(f"Rightsizing-Workload: {w.key}")
    for c in info.changes:
        old = "" if c.old is None else _q(c.field, c.old)
        lines.append(f"Rightsizing-Change: {c.container}.{c.field}={old}->{_q(c.field, c.new)},{c.direction}")
    if info.supersedes:
        lines.append(f"Rightsizing-Supersedes: {info.supersedes}")
    lines.append(f"Rightsizing-Run: {info.run_id}")
    return "\n".join(lines) + "\n"


def render_withdraw(workload: WorkloadRef, date: str, run_id: str, pending_sha: str,
                    restored: list[FieldChange], reason: str) -> str:
    """Withdraw a pending proposal by restoring the live values (not a git revert: no suppression)."""
    lines = [f"rightsizing({workload.namespace}/{workload.name}): withdraw pending proposal {pending_sha[:10]}", "",
             f"요약: 승인 대기 중이던 제안을 철회하고 현재 클러스터 값으로 되돌립니다. 사유: {reason}"]
    for c in restored:
        lines.append(f"  {c.container} {_LABEL[c.field]:<12}{_q(c.field, c.old):>8} → {_q(c.field, c.new):<8} (현재 값 유지)")
    lines.append("거절하려면: git revert <this commit>  (이전 제안을 다시 살리고, 봇은 이를 사람의 결정으로 존중합니다)")
    lines.append("")
    lines.append(f"Rightsizing-Id: {date}-{workload.namespace}-{workload.kind.lower()}-{workload.name}")
    lines.append(f"Rightsizing-Workload: {workload.key}")
    lines.append(f"Rightsizing-Withdraw: {pending_sha}")
    lines.append(f"Rightsizing-Run: {run_id}")
    return "\n".join(lines) + "\n"


def parse_trailers(message: str) -> ParsedTrailers:
    out = ParsedTrailers()
    block = trailer_block(message)
    if not all(TRAILER_RE.match(line) for line in block.splitlines() if line.strip()):
        return out  # a real trailer block contains nothing but Rightsizing-* lines
    for key, value in TRAILER_RE.findall(block):
        value = value.strip()
        if key == "Id":
            out.id = value
        elif key == "Workload":
            out.workload = value
        elif key == "Run":
            out.run = value
        elif key == "Supersedes":
            out.supersedes = value
        elif key == "Withdraw":
            out.withdraw = value
        elif key == "Change":
            m = CHANGE_RE.match(value)
            if not m:
                continue
            resource = m["field"].split(".")[0]
            out.changes.append(ParsedChange(
                container=m["container"], field=m["field"],
                old=quantity.parse(resource, m["old"]) if m["old"] else None,
                new=quantity.parse(resource, m["new"]), direction=m["direction"]))
    return out


def reverted_sha(message: str) -> str | None:
    """git / Gitea revert commits: 'Revert "..."' subject plus a line starting 'This reverts commit <sha>'."""
    if parse_trailers(message).workload:
        return None  # a rightsizing commit is never itself a revert
    m = REVERT_RE.search(message)
    return m.group(1) if m else None
