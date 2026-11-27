import json
import subprocess
from datetime import datetime, timezone

import httpx
import pytest

from rightsizing.config import Config
from rightsizing.gitops import clone, git_env, write_askpass
from rightsizing.report.nodes import Res
from rightsizing.report.run import EXIT_INFRA, EXIT_OK, run_report
from tests.integration.test_pipeline import PROM_RULES
from tests.kube_fixtures import deployment
from tests.test_prom import FakeProm

NOW = datetime(2026, 11, 25, 20, 0, tzinfo=timezone.utc)  # 2026-11-26 05:00 KST
RES = {"requests": {"cpu": "2", "memory": "8Gi"}, "limits": {"cpu": "4", "memory": "16Gi"}}
GI = 2 ** 30


def sh(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


class Kube:
    def __init__(self, fail_nodes=False, metrics=True):
        self.fail_nodes, self.metrics = fail_nodes, metrics

    def list_deployments(self, ns):
        return [deployment("web", resources=RES), deployment("api", app="api", resources=RES)]

    def list_hpas(self, ns):
        return []

    def list_limitranges(self, ns):
        return []

    def list_nodes(self):
        if self.fail_nodes:
            raise RuntimeError("apiserver down")
        return [{"metadata": {"name": "n1", "labels": {"node-role.kubernetes.io/control-plane": ""}},
                 "spec": {}, "status": {"allocatable": {"cpu": "20", "memory": "119Gi", "pods": "110"}}}]

    def list_pods(self):
        return [{"metadata": {"name": "web-1", "namespace": "development"},
                 "spec": {"nodeName": "n1", "containers": [{"resources": {"requests": {"cpu": "2", "memory": "8Gi"}}}]},
                 "status": {"phase": "Running"}}]

    def node_usage(self):
        if not self.metrics:
            raise RuntimeError("no metrics api")
        return {"n1": Res(3000, 40 * GI, 0)}


@pytest.fixture
def rep(tmp_path):
    bare = tmp_path / "report.git"
    sh(tmp_path, "init", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    sh(tmp_path, "clone", str(bare), str(seed))
    (seed / "README.md").write_text("# report\n")
    sh(seed, "add", ".")
    sh(seed, "commit", "-m", "init")
    sh(seed, "push", "origin", "main")
    cfg = Config.model_validate({"namespaces": ["development"], "prometheus": {"use_recording_rules": False},
                                 "git": {"url": "http://gitea/a/resource.git"},
                                 "report": {"git_url": f"file://{bare}", "wait_for_record_minutes": 2}})
    return {"tmp": tmp_path, "bare": bare, "seed": seed, "cfg": cfg, "n": 0}


def clone_repo(r):
    r["n"] += 1
    home = r["tmp"] / f"home{r['n']}"
    home.mkdir()
    env = git_env(write_askpass(r["tmp"] / f"bin{r['n']}"), "bot", None, home)
    return clone(f"file://{r['bare']}", "main", r["tmp"] / f"work{r['n']}", env)


def push_record(r, day="2026-11-26", status="ok", **web):
    sh(r["seed"], "pull", "-q", "origin", "main")
    rec = {"record_version": 1, "date": day, "run_id": "r9", "run_status": status, "exit_code": 0,
           "pushed": False, "push_enabled": False,
           "workloads": [{"workload": "development/Deployment/web", "status": "held:llm_hold", "detail": "",
                          "commit": None, "llm_decision": "hold", "llm_notes": "주간 배치", "pending": None,
                          "proposal": [{"container": "web", "field": "cpu.request", "current": 2000, "proposed": 1100,
                                        "changed": True, "direction": "shrink", "basis": "b", "reason": None,
                                        "p95": 1000, "max": 1500}], **web},
                         {"workload": "development/Deployment/api", "status": "skipped:no_argocd_app",
                          "detail": "", "commit": None, "llm_decision": None, "llm_notes": "", "pending": None,
                          "proposal": []}]}
    path = r["seed"] / f"runs/{day[:4]}/{day[5:7]}/{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec))
    sh(r["seed"], "add", ".")
    sh(r["seed"], "commit", "-m", f"record {day}")
    sh(r["seed"], "push", "-q", "origin", "main")


def show(r, rel):
    return sh(r["tmp"], "--git-dir", str(r["bare"]), "show", f"main:{rel}")


def run(r, kube=None, prom_rules=None, **kw):
    sleeps = []
    res = run_report(r["cfg"], kube or Kube(), FakeProm(prom_rules or PROM_RULES).client(), clone_repo(r), NOW,
                     sleep=sleeps.append, **kw)
    return res, sleeps


def test_writes_dated_files_and_readme(rep):
    push_record(rep)
    res, sleeps = run(rep)
    assert res.exit_code == EXIT_OK and res.commit and sleeps == []
    md = show(rep, "reports/2026/11/2026-11-26.md")
    assert show(rep, "README.md") == md
    assert json.loads(show(rep, "reports/2026/11/2026-11-26.json"))["date"] == "2026-11-26"
    assert "| development | web | web | cpu.req | 2 | 1100m | -45% | 1 | 보류 (llm_hold) | hold: 주간 배치 |" in md
    assert "| development | api | api | cpu.req | 2 | 1100m" in md and "제안 가능 (Sync 전)" in md  # computed
    assert "❌ 단일 노드: 장애 시 전체 중단" in md and "run `r9`" in md


def test_same_day_rerun_without_changes_makes_no_commit(rep):
    push_record(rep)
    run(rep)
    head = sh(rep["tmp"], "--git-dir", str(rep["bare"]), "rev-parse", "main")
    res, _ = run(rep)
    assert res.exit_code == EXIT_OK and res.commit is None
    assert sh(rep["tmp"], "--git-dir", str(rep["bare"]), "rev-parse", "main") == head


def test_waits_then_falls_back_to_yesterday_with_banner(rep):
    push_record(rep, day="2026-11-25")
    res, sleeps = run(rep)
    assert sleeps == [60, 60]  # wait_for_record_minutes=2
    assert "> ⚠️ 오늘 rightsizing 실행 기록이 없습니다 — 어제 기록을 사용합니다" in res.markdown


def test_missing_and_failed_records(rep):
    res, _ = run(rep)
    assert "오늘 rightsizing 실행 기록이 없습니다" in res.markdown and "실행 기록 없음" in res.markdown
    push_record(rep, status="failed:prometheus")
    res, _ = run(rep)
    assert "rightsizing 실행 실패 (failed:prometheus" in res.markdown


def test_metrics_missing_is_not_fatal(rep):
    push_record(rep)
    res, _ = run(rep, kube=Kube(metrics=False))
    assert res.exit_code == EXIT_OK and "실사용량은 n/a" in res.markdown


def test_infra_failures_exit_2(rep):
    assert run(rep, prom_rules=[("", httpx.ConnectError("down"))])[0].exit_code == EXIT_INFRA
    assert run(rep, kube=Kube(fail_nodes=True))[0].exit_code == EXIT_INFRA


def test_cli_exits_2_when_report_repo_unreachable(tmp_path, monkeypatch):
    from rightsizing.report import __main__ as cli_mod

    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"git: {{url: http://gitea/a/resource.git}}\nsecret_dir: {tmp_path}\n"
                   f"report: {{git_url: 'file://{tmp_path}/missing.git'}}\n")
    monkeypatch.setattr(cli_mod, "ReportKubeClient", lambda cfg: Kube())
    assert cli_mod.cli(["--config", str(cfg), "--workdir", str(tmp_path / "w")]) == EXIT_INFRA


def test_dry_run_does_not_write(rep):
    push_record(rep)
    res, sleeps = run(rep, dry_run=True)
    assert res.exit_code == EXIT_OK and res.commit is None and "# Daily report 2026-11-26" in res.markdown
    assert "reports/" not in sh(rep["tmp"], "--git-dir", str(rep["bare"]), "ls-tree", "-r", "--name-only", "main")


def test_loads_todays_mirror_record(rep):
    push_record(rep)
    res, _ = run(rep)
    assert "> ⚠️ mirror 기록 없음" in res.markdown
    sh(rep["seed"], "pull", "-q", "origin", "main")
    path = rep["seed"] / "mirror/2026/11/2026-11-26.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"record_version": 1, "date": "2026-11-26", "exit_code": 0, "created": [],
                                "first_synced": ["development/web"], "adopted": [], "skipped": [],
                                "deleted": [], "recreate_failed": []}))
    sh(rep["seed"], "add", ".")
    sh(rep["seed"], "commit", "-m", "mirror record")
    sh(rep["seed"], "push", "-q", "origin", "main")
    res, _ = run(rep)
    assert "mirror 기록 없음" not in res.markdown and "| development/web |" in res.markdown
    assert json.loads(show(rep, "reports/2026/11/2026-11-26.json"))["mirror"]["first_synced"] == ["development/web"]
