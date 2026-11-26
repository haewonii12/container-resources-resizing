"""Run record: what one rightsizing run decided, per workload, pushed to the report repo.

The daily report reads it so its numbers, statuses and LLM decisions are exactly the ones rightsizing used
(one source of truth). Layout: runs/YYYY/MM/YYYY-MM-DD.json, dated in the report timezone (Asia/Seoul by default),
so the 03:00 KST run (18:00 UTC the previous day) lands on the KST day the report looks up.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config
from .gitops import GitError, PathGuardError, clone, git_env, write_askpass
from .models import Outcome

log = logging.getLogger("rightsizing.runrecord")

RECORD_VERSION = 1
RUNS_DIR = "runs"


def local_date(now: datetime, tz: str) -> date:
    return now.astimezone(ZoneInfo(tz)).date()


def record_path(day: date) -> str:
    return f"{RUNS_DIR}/{day:%Y}/{day:%m}/{day.isoformat()}.json"


def build_record(cfg: Config, *, run_id: str, started_at: datetime, finished_at: datetime, run_status: str,
                 exit_code: int, pushed: bool, outcomes: list[Outcome]) -> dict:
    """Pure: the JSON document for one run (dated in the report timezone)."""
    tz = cfg.report.timezone if cfg.report else "Asia/Seoul"
    return {
        "record_version": RECORD_VERSION,
        "date": local_date(started_at, tz).isoformat(),
        "run_id": run_id,
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": finished_at.isoformat(timespec="seconds"),
        "run_status": run_status,
        "exit_code": exit_code,
        "push_enabled": cfg.git.push_enabled,
        "pushed": pushed,
        "namespaces": list(cfg.namespaces),
        "workloads": [asdict(o) for o in sorted(outcomes, key=lambda o: o.workload)],
    }


def proposal_row(container, fname, current, proposed, changed, direction, basis, reason, usage) -> dict:
    """One field of a proposal as stored in the record (cpu in millicores, memory in bytes)."""
    res = fname.split(".")[0]
    u = getattr(usage, res, None) if usage is not None else None
    return {"container": container, "field": fname, "current": current, "proposed": proposed,
            "changed": changed, "direction": direction, "basis": basis, "reason": reason,
            "p95": u.p95 if u else None, "max": u.max if u else None}


def proposal_rows(containers: dict, recs: list, usage: dict) -> list[dict]:
    """Changed fields (after revert suppression) plus every other computed field with its skip reason.
    Shared by rightsizing (record) and the daily report (Deployments rightsizing did not judge)."""
    rows = []
    for rec in recs:
        changed = {ch.field: ch for ch in rec.changes}
        current = containers[rec.container]
        u = usage.get(rec.container)
        for fname in sorted(set(rec.proposed) | set(changed) | (set(rec.skipped) - {"*"})):
            ch = changed.get(fname)
            if ch:
                rows.append(proposal_row(rec.container, fname, ch.old, ch.new, True, ch.direction, ch.basis,
                                         None, u))
            else:
                rows.append(proposal_row(rec.container, fname, current.get(fname), rec.proposed.get(fname),
                                         False, None, None, rec.skipped.get(fname), u))
        if "*" in rec.skipped:
            rows.append(proposal_row(rec.container, "*", None, None, False, None, None, rec.skipped["*"], u))
    return rows


def dumps(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False, indent=1, sort_keys=True, default=str) + "\n"


def write_record(cfg: Config, record: dict, workdir: Path, token_file: Path | None) -> str | None:
    """Clone the report repo, write the record, commit (runs/ only) and push. Returns the commit sha or None
    when the file was already identical. Raises GitError on clone/push failures."""
    assert cfg.report is not None
    base = workdir / "report-record"
    env = git_env(write_askpass(base / "bin"), cfg.git.username, token_file, base / "home")
    (base / "home").mkdir(parents=True, exist_ok=True)
    repo = clone(cfg.report.git_url, cfg.report.branch, base / "repo", env, cfg.timeouts.git)
    rel = record_path(date.fromisoformat(record["date"]))
    target = repo.workdir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dumps(record), encoding="utf-8")
    try:
        sha = repo.commit([rel], f"run record {record['date']} ({record['run_id']}, {record['run_status']})",
                          cfg.git.author_name, cfg.git.author_email, allowed_prefix=RUNS_DIR)
    except PathGuardError as exc:  # cannot happen with record_path(), kept as a hard guard
        raise GitError(str(exc)) from exc
    if sha is not None:
        repo.push(cfg.report.branch)
    return sha
