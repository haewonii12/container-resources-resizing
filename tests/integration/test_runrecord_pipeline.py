"""rightsizing writes the run record (runs/YYYY/MM/<KST date>.json) that the daily report reads."""

import json

import pytest

from rightsizing.config import ReportConfig
from rightsizing.main import EXIT_INFRA, EXIT_OK, EXIT_SECURITY
from tests.fakes import tool_call
from tests.integration.test_pipeline import APPLY, git, run

# the fixture runs at 2026-11-25T03:00Z = 2026-11-25 12:00 KST
RECORD = "runs/2026/11/2026-11-25.json"


@pytest.fixture
def renv(env):
    bare = env["tmp"] / "report.git"
    git(env["tmp"], "init", "--bare", "-b", "main", str(bare))
    seed = env["tmp"] / "report-seed"
    git(env["tmp"], "clone", str(bare), str(seed))
    (seed / "README.md").write_text("# report\n")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "init")
    git(seed, "push", "origin", "main")
    env["cfg"].report = ReportConfig(git_url=f"file://{bare}")
    env["report_bare"] = bare
    return env


def record(env):
    text = git(env["report_bare"], "show", f"main:{RECORD}")
    data = json.loads(text)
    return data, {w["workload"].split("/")[-1]: w for w in data["workloads"]}


def has_record(env):
    return RECORD in git(env["report_bare"], "ls-tree", "-r", "--name-only", "main")


def test_record_carries_decisions_and_proposals(renv):
    report, _, _ = run(renv)
    assert report.exit_code == EXIT_OK
    data, w = record(renv)
    assert data["record_version"] == 1 and data["date"] == "2026-11-25" and data["run_status"] == "ok"
    assert data["pushed"] is True and data["push_enabled"] is True
    web = w["web"]
    assert web["status"] == "committed" and web["llm_decision"] == "apply" and web["llm_notes"] == "재시작 없음, 적용"
    cpu = next(r for r in web["proposal"] if r["field"] == "cpu.request")
    assert cpu["changed"] and cpu["current"] == 2000 and cpu["proposed"] == 1100 and cpu["p95"] == 1000


def test_held_and_no_llm_decisions(renv):
    run(renv, llm_down=True)
    _, w = record(renv)
    assert w["web"]["status"] == "held:llm_unavailable" and w["web"]["llm_decision"] == "hold"
    assert w["web"]["proposal"]  # numbers are recorded even when held
    run(renv, no_llm=True, mcp_down=True)
    _, w = record(renv)
    assert w["web"]["status"] == "committed" and w["web"]["llm_decision"] is None
    assert w["web"]["llm_notes"] == "사용 안 함 (--no-llm)"


def test_pending_and_revert_suppression_in_record(renv):
    report, out, _ = run(renv)
    sha = out["web"].commit
    run(renv)  # nothing synced: both pending_same
    _, w = record(renv)
    assert w["api"]["pending"]["kind"] == "same" and w["api"]["pending"]["sha"]
    assert any(r["changed"] for r in w["api"]["proposal"])
    git(renv["seed"], "pull", "-q", "origin", "main")
    git(renv["seed"], "revert", "--no-edit", sha)
    git(renv["seed"], "push", "-q", "origin", "main")
    run(renv)
    _, w = record(renv)
    web = w["web"]
    assert web["status"] == "no_change" and web["llm_decision"] is None
    suppressed = [r for r in web["proposal"] if r["reason"] == "suppressed_by_revert"]
    assert suppressed and not any(r["changed"] for r in web["proposal"])


def test_push_disabled_marks_record_not_pushed(renv):
    renv["cfg"].git.push_enabled = False
    run(renv)
    data, w = record(renv)
    assert data["pushed"] is False and data["push_enabled"] is False and w["web"]["status"] == "committed"


def test_failure_record_on_early_return(renv):
    report, _, _ = run(renv, prom_down=True)
    assert report.exit_code == EXIT_INFRA
    data, _ = record(renv)
    assert data["run_status"] == "failed:prometheus" and data["exit_code"] == EXIT_INFRA


def test_no_record_for_dry_run_or_only(renv):
    run(renv, dry_run=True)
    run(renv, only=["development/Deployment/web"])
    assert not has_record(renv)


def test_record_push_failure_never_masks_security_exit(renv):
    renv["cfg"].report = ReportConfig(git_url=f"file://{renv['tmp']}/missing.git")
    report, out, _ = run(renv, script=[[tool_call("bash", {"command": "id"})], APPLY])
    assert out["web"].status == "held:security" and report.exit_code == EXIT_SECURITY


def test_record_push_failure_turns_clean_run_into_exit_2(renv):
    renv["cfg"].report = ReportConfig(git_url=f"file://{renv['tmp']}/missing.git")
    report, out, _ = run(renv)
    assert out["web"].status == "committed" and report.exit_code == EXIT_INFRA
