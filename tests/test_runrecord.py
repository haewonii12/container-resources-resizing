import json
import subprocess
from datetime import datetime, timezone

import pytest

from rightsizing.config import Config
from rightsizing.gitops import GitError
from rightsizing.models import Outcome
from rightsizing.runrecord import RECORD_VERSION, build_record, local_date, record_path, write_record


def sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def report_remote(tmp_path):
    bare = tmp_path / "report.git"
    sh(tmp_path, "init", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    sh(tmp_path, "clone", str(bare), str(seed))
    (seed / "README.md").write_text("# report\n")
    sh(seed, "add", ".")
    sh(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init")
    sh(seed, "push", "origin", "main")
    return bare


def cfg_with_report(url="file:///nonexistent/report.git", **git):
    return Config.model_validate({"git": {"url": "http://gitea/admin/resource.git", **git},
                                  "report": {"git_url": url}})


def outcomes():
    return [Outcome("development/Deployment/web", "held:llm_hold", "주간 배치", llm_decision="hold",
                    llm_notes="배치 때 급증", proposal=[{"container": "app", "field": "memory.request",
                                                     "current": 2.0, "proposed": 1.0}]),
            Outcome("development/Deployment/api", "skipped:no_argocd_app")]


def test_date_is_report_timezone_not_utc():
    # 03:00 KST on 2026-11-26 is 18:00 UTC on 2026-11-25
    now = datetime(2026, 11, 25, 18, 0, tzinfo=timezone.utc)
    assert local_date(now, "Asia/Seoul").isoformat() == "2026-11-26"
    rec = build_record(cfg_with_report(), run_id="r1", started_at=now, finished_at=now, run_status="ok",
                       exit_code=0, pushed=False, outcomes=outcomes())
    assert rec["date"] == "2026-11-26"
    assert record_path(local_date(now, "Asia/Seoul")) == "runs/2026/11/2026-11-26.json"


def test_record_fields():
    now = datetime(2026, 11, 25, 18, 0, tzinfo=timezone.utc)
    rec = build_record(cfg_with_report(), run_id="r1", started_at=now, finished_at=now, run_status="ok",
                       exit_code=0, pushed=False, outcomes=outcomes())
    assert rec["record_version"] == RECORD_VERSION == 1
    assert rec["push_enabled"] is False and rec["pushed"] is False and rec["exit_code"] == 0
    assert rec["push_failed"] is False
    by = {w["workload"]: w for w in rec["workloads"]}
    held = by["development/Deployment/web"]
    assert held["llm_decision"] == "hold" and held["llm_notes"] == "배치 때 급증"
    assert held["proposal"][0]["proposed"] == 1.0
    assert by["development/Deployment/api"]["llm_decision"] is None
    json.dumps(rec)  # serializable


def test_write_record_commits_and_pushes_once(tmp_path, report_remote):
    cfg = cfg_with_report(f"file://{report_remote}")
    now = datetime(2026, 11, 25, 18, 0, tzinfo=timezone.utc)
    rec = build_record(cfg, run_id="r1", started_at=now, finished_at=now, run_status="ok", exit_code=0,
                       pushed=False, outcomes=outcomes())
    sha = write_record(cfg, rec, tmp_path / "w1", None)
    assert sha is not None
    data = json.loads(sh(tmp_path, "--git-dir", str(report_remote), "show", "main:runs/2026/11/2026-11-26.json"))
    assert data["run_id"] == "r1"
    # same content again: no new commit, nothing pushed
    head = sh(tmp_path, "--git-dir", str(report_remote), "rev-parse", "main")
    assert write_record(cfg, rec, tmp_path / "w2", None) is None
    assert sh(tmp_path, "--git-dir", str(report_remote), "rev-parse", "main") == head
    # only runs/ is touched
    files = sh(tmp_path, "--git-dir", str(report_remote), "show", "--name-only", "--format=", sha.strip()).split()
    assert files == ["runs/2026/11/2026-11-26.json"]


def test_write_record_clone_failure_raises(tmp_path):
    cfg = cfg_with_report(f"file://{tmp_path}/missing.git")
    now = datetime(2026, 11, 25, 18, 0, tzinfo=timezone.utc)
    rec = build_record(cfg, run_id="r1", started_at=now, finished_at=now, run_status="ok", exit_code=0,
                       pushed=False, outcomes=[])
    with pytest.raises(GitError):
        write_record(cfg, rec, tmp_path / "w", None)
