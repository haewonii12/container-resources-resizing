"""Build the daily report from the cluster, Prometheus and the rightsizing run record, and push it.

Exit codes: 0 ok | 2 infrastructure failure (Prometheus, apiserver, report repo).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol

from ..config import Config
from ..gitops import GitRepo
from ..models import ContainerResources, WorkloadRef
from ..prom import PromClient, collect_usage
from ..runrecord import RECORD_VERSION, local_date, record_path
from .changes import build_table, compute_proposal, needs_compute, record_entries, workload_key
from .nodes import Res, node_rows, simulate_failover
from .render import DailyReport, render_json, render_markdown

log = logging.getLogger("rightsizing.report")

EXIT_OK, EXIT_INFRA = 0, 2


class ReportKube(Protocol):
    def list_deployments(self, namespace: str) -> list[dict]: ...
    def list_hpas(self, namespace: str) -> list[dict]: ...
    def list_limitranges(self, namespace: str) -> list[dict]: ...
    def list_nodes(self) -> list[dict]: ...
    def list_pods(self) -> list[dict]: ...
    def node_usage(self) -> dict[str, Res]: ...


@dataclass
class ReportResult:
    exit_code: int
    markdown: str = ""
    commit: str | None = None


def report_paths(day: date) -> tuple[str, str]:
    base = f"reports/{day:%Y}/{day:%m}/{day.isoformat()}"
    return f"{base}.md", f"{base}.json"


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def find_record(repo: GitRepo, branch: str, today: date, wait_minutes: int,
                sleep: Callable[[float], None]) -> tuple[dict | None, list[str]]:
    """Today's run record (waiting for it up to wait_minutes), else yesterday's with a banner."""
    path = repo.workdir / record_path(today)
    for attempt in range(wait_minutes + 1):
        if path.exists():
            break
        if attempt < wait_minutes:
            sleep(60)
            repo.run("pull", "--ff-only", "-q", "origin", branch, check=False)
    banners: list[str] = []
    record = _load(path) if path.exists() else None
    if record is None:
        yesterday = repo.workdir / record_path(today - timedelta(days=1))
        record = _load(yesterday) if yesterday.exists() else None
        banners.append("오늘 rightsizing 실행 기록이 없습니다" + (" — 어제 기록을 사용합니다" if record else ""))
    if record is not None and record.get("record_version") != RECORD_VERSION:
        banners.append(f"알 수 없는 실행 기록 형식(record_version={record.get('record_version')}) — 기록을 쓰지 않습니다")
        record = None
    if record is not None and str(record.get("run_status", "")).startswith("failed"):
        banners.append(f"rightsizing 실행 실패 ({record['run_status']}, exit {record.get('exit_code')})")
    return record, banners


def _computed(cfg: Config, kube: ReportKube, prom: PromClient, deps_by_ns: dict[str, list[dict]],
              entries: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for ns, deps in deps_by_ns.items():
        todo = [d for d in deps if needs_compute(entries.get(workload_key(d)))]
        if not todo:
            continue
        hpas, lrs = kube.list_hpas(ns), kube.list_limitranges(ns)
        for dep in todo:
            meta = dep["metadata"]
            ref = WorkloadRef(ns, "Deployment", meta["name"])
            try:
                containers = {c["name"]: ContainerResources.from_k8s(c.get("resources")) for c in
                              ((dep.get("spec") or {}).get("template") or {}).get("spec", {}).get("containers") or []}
                cpu_limited = {c: r.get("cpu.limit") is not None for c, r in containers.items()}
                usage, _, _ = collect_usage(prom, ref, cpu_limited, cfg.window_days,
                                            cfg.prometheus.use_recording_rules)
                out[workload_key(dep)] = compute_proposal(cfg, dep, usage, hpas, lrs)
            except Exception as exc:  # one Deployment must not break the report
                log.warning("usage lookup failed", extra={"workload": ref.key, "error": type(exc).__name__})
    return out


def build_report(cfg: Config, kube: ReportKube, prom: PromClient, record: dict | None, banners: list[str],
                 today: date) -> DailyReport:
    prefix = cfg.report.node_role_prefix
    deps_by_ns = {ns: [d for d in kube.list_deployments(ns) if d.get("kind", "Deployment") == "Deployment"]
                  for ns in cfg.namespaces}
    nodes, pods = kube.list_nodes(), kube.list_pods()
    try:
        usage = kube.node_usage()
        metrics = True
    except Exception as exc:  # metrics-server missing: show n/a, keep requests/allocatable/N-1
        log.warning("node metrics unavailable", extra={"error": type(exc).__name__})
        usage, metrics = None, False
    entries = record_entries(record)
    table = build_table([d for deps in deps_by_ns.values() for d in deps], record,
                        _computed(cfg, kube, prom, deps_by_ns, entries))
    header = {k: v for k, v in (record or {}).items() if k != "workloads"} or None
    return DailyReport(date=today.isoformat(), generated_at=f"{today.isoformat()} ({cfg.report.timezone})",
                       namespaces=list(cfg.namespaces), record=header, banners=banners, table=table,
                       nodes=node_rows(nodes, pods, usage, prefix), failover=simulate_failover(nodes, pods, prefix),
                       metrics_available=metrics)


def run_report(cfg: Config, kube: ReportKube, prom: PromClient, repo: GitRepo, now: datetime,
               dry_run: bool = False, sleep: Callable[[float], None] = time.sleep) -> ReportResult:
    assert cfg.report is not None
    if prom.vector("vector(1)") is None:
        log.error("prometheus unreachable")
        return ReportResult(EXIT_INFRA)
    today = local_date(now, cfg.report.timezone)
    record, banners = find_record(repo, cfg.report.branch, today, 0 if dry_run else cfg.report.wait_for_record_minutes,
                                  sleep)
    try:
        report = build_report(cfg, kube, prom, record, banners, today)
    except Exception as exc:  # apiserver list failures etc.
        log.error("cluster read failed", extra={"error": f"{type(exc).__name__}: {str(exc)[-200:]}"})
        return ReportResult(EXIT_INFRA)
    md = render_markdown(report)
    if dry_run:
        return ReportResult(EXIT_OK, md)
    md_path, json_path = report_paths(today)
    for rel, text in ((md_path, md), (json_path, render_json(report)), ("README.md", md)):
        target = repo.workdir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    sha = repo.commit([md_path, json_path, "README.md"], f"daily report {today.isoformat()}",
                      cfg.git.author_name, cfg.git.author_email, allowed_prefix="")
    if sha is not None:
        repo.push(cfg.report.branch)
    log.info("report", extra={"date": today.isoformat(), "commit": sha, "rows": len(report.table.rows),
                              "banners": banners})
    return ReportResult(EXIT_OK, md, sha)
