"""End-to-end pipeline with fakes: kubectl-ai MCP, Prometheus, OpenAI-compatible LLM, k8s API, and a real git remote."""

import io
import json
import logging
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from rightsizing import logs
from rightsizing.config import Config, SecretStore
from rightsizing.main import EXIT_INFRA, EXIT_OK, EXIT_SECURITY, Deps, RunOptions, Runner
from rightsizing.mcp_client import HttpMcpClient
from tests.fakes import FakeKubectlAI, FakeLLM, tool_call
from tests.kube_fixtures import FakeKube, application, deployment
from tests.test_prom import FakeProm, vec

TOKEN = "TOKEN-123456789"
RES = {"requests": {"cpu": "2", "memory": "8Gi"}, "limits": {"cpu": "4", "memory": "16Gi"}}


def manifest(name):
    return textwrap.dedent(f"""\
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: {name}
          namespace: development
        spec:
          replicas: 2
          template:
            spec:
              containers:
                - name: {name}
                  image: app:1
                  resources:
                    requests:
                      cpu: "2"
                      memory: 8Gi
                    limits:
                      cpu: "4"
                      memory: 16Gi
        """)


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=h", "-c", "user.email=h@h", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def env(tmp_path):
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", str(bare), str(seed))
    for name in ("web", "api"):
        (seed / f"apps/{name}").mkdir(parents=True)
        (seed / f"apps/{name}/deploy.yaml").write_text(manifest(name))
    git(seed, "add", ".")
    git(seed, "commit", "-m", "init")
    git(seed, "push", "origin", "main")
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "gitea_token").write_text(TOKEN)
    url = f"file://{bare}"
    cfg = Config.model_validate({"namespaces": ["development"], "git": {"url": url, "push_enabled": True},
                                 "secret_dir": str(secrets), "prometheus": {"use_recording_rules": False}})
    kube = FakeKube([deployment("web", resources=RES), deployment("api", app="api", resources=RES)],
                    [application("web", "apps/web", repo=url), application("api", "apps/api", repo=url)])
    return {"tmp": tmp_path, "bare": bare, "seed": seed, "cfg": cfg, "kube": kube, "runs": 0}


PROM_RULES = [
    ("quantile_over_time(0.95, (max by (namespace, workload, container) (rate(container_cpu", vec(web=1.0, api=1.0)),
    ("max_over_time((max by (namespace, workload, container) (rate(container_cpu", vec(web=1.5, api=1.5)),
    ("quantile_over_time(0.95, (max by (namespace, workload, container) (container_memory",
     vec(web=4 * 2 ** 30, api=4 * 2 ** 30)),
    ("max_over_time((max by (namespace, workload, container) (container_memory", vec(web=6 * 2 ** 30, api=6 * 2 ** 30)),
    ("count_over_time((max by (namespace, workload, container) (rate(container_cpu", vec(web=168, api=168)),
    ("container_cpu_cfs_throttled_periods_total", vec(web=0.0, api=0.0)),
    ("kube_pod_container_status_restarts_total", vec(web=0, api=0)),
    ("OOMKilled", vec()),
    ("kube_deployment_spec_replicas", vec(**{"": 2.0})),
    ("vector(1)", vec(**{"": 1})),
]

APPLY = [tool_call("final_decision", {"decision": "apply", "notes": "재시작 없음, 적용"})]


def run(env, script=None, llm_down=False, mcp_down=False, prom_down=False, now=None, rules=None, llm_box=None,
        **opts):
    env["runs"] += 1
    cfg = env["cfg"]
    prom_rules = [("", httpx.ConnectError("down"))] if prom_down else (rules or PROM_RULES)
    llm = FakeLLM(script if script is not None else [APPLY, APPLY], down=llm_down)
    if llm_box is not None:
        llm_box.append(llm)
    kai = FakeKubectlAI(fail_first=10 ** 6 if mcp_down else 0)
    cfg.timeouts.mcp_connect = 1
    deps = Deps(kube=env["kube"], prom=FakeProm(prom_rules).client(), llm=llm.client(),
                mcp=HttpMcpClient("http://127.0.0.1:9080/mcp", transport=kai.transport()),
                secrets=SecretStore(cfg.secret_dir), workdir=env["tmp"] / f"work{env['runs']}",
                now=now or datetime(2026, 11, 25, 3, 0, tzinfo=timezone.utc))
    report = Runner(cfg, deps, RunOptions(**opts)).run()
    return report, {o.workload.split("/")[-1]: o for o in report.outcomes}, kai


def remote_log(env):
    return git(env["bare"], "log", "--format=%H%x1f%B%x1e", "main").strip("\x1e\n").split("\x1e")


def test_dry_run_never_commits(env):
    report, out, _ = run(env, dry_run=True)
    assert report.exit_code == EXIT_OK
    assert out["web"].status == out["api"].status == "proposed"
    assert len(remote_log(env)) == 1 and "Rightsizing-Workload: development/Deployment/web" in report.proposals[0]


def test_commit_push_pending_and_revert_suppression(env):
    report, out, _ = run(env)
    assert report.exit_code == EXIT_OK and report.pushed == 2
    assert out["web"].status == "committed" and out["api"].status == "committed"
    commits = remote_log(env)
    assert len(commits) == 3
    web_commit = next(c for c in commits if "Rightsizing-Workload: development/Deployment/web" in c)
    sha = web_commit.split("\x1f")[0].strip()
    body = web_commit.split("\x1f")[1]
    assert body.startswith("rightsizing(development/web): web cpu.request 2→1100m")
    assert "Rightsizing-Change: web.cpu.request=2->1100m,shrink" in body and "LLM 판단: 재시작 없음, 적용" in body
    assert "예상 절감:" in body and "거절하려면: git revert" in body

    # nobody synced: live still old -> same recommendation as pending, no new commits (idempotent rerun)
    report, out, _ = run(env)
    assert out["web"].status == out["api"].status == "no_change"
    assert out["web"].detail.startswith("pending_same") and out["api"].detail.startswith("pending_same")
    assert len(remote_log(env)) == 3

    # human rejects web via git revert
    git(env["seed"], "pull", "-q", "origin", "main")
    git(env["seed"], "revert", "--no-edit", sha)
    git(env["seed"], "push", "-q", "origin", "main")
    report, out, _ = run(env)
    assert out["web"].status == "no_change" and "suppressed_by_revert" in out["web"].detail
    assert out["api"].status == "no_change" and out["api"].detail.startswith("pending_same")
    assert len(remote_log(env)) == 4


def test_synced_commit_is_applied_and_not_pending(env):
    run(env)
    for w in env["kube"].workloads:  # simulate a manual ArgoCD sync: live now equals the committed values
        w["spec"]["template"]["spec"]["containers"][0]["resources"] = {
            "requests": {"cpu": "1100m", "memory": "4512Mi"}, "limits": {"cpu": "4", "memory": "8Gi"}}
    report, out, _ = run(env)
    assert out["web"].status == "no_change" and out["api"].status == "no_change"
    assert report.exit_code == EXIT_OK


def test_security_event_holds_workload_but_pushes_others(env):
    script = [[tool_call("bash", {"command": "cat /proc/1/environ"})], APPLY]
    report, out, kai = run(env, script=script)
    assert report.exit_code == EXIT_SECURITY
    assert out["web"].status == "held:security" and out["api"].status == "committed" and report.pushed == 1
    assert kai.commands == []


def test_push_disabled_commits_locally_only(env):
    env["cfg"].git.push_enabled = False
    report, out, _ = run(env)
    assert out["web"].status == "committed" and report.pushed == 0 and len(remote_log(env)) == 1


def test_llm_down_holds_everything(env):
    report, out, _ = run(env, llm_down=True)
    assert report.exit_code == EXIT_OK and out["web"].status == "held:llm_unavailable"
    assert len(remote_log(env)) == 1


def test_no_llm_flag_commits_with_note(env):
    report, out, _ = run(env, no_llm=True, mcp_down=True)
    assert out["web"].status == "committed"
    assert "LLM 판단: 사용 안 함 (--no-llm)" in remote_log(env)[0]


def test_infra_failures_exit_2(env):
    assert run(env, mcp_down=True)[0].exit_code == EXIT_INFRA
    assert run(env, prom_down=True)[0].exit_code == EXIT_INFRA
    env["cfg"].git.url = "file:///nonexistent/repo.git"
    assert run(env)[0].exit_code == EXIT_INFRA


def test_run_budget(env):
    env["cfg"].timeouts.run_budget = 1e-9  # bypasses validation on purpose (attribute assignment)
    _, out, _ = run(env)
    assert out["web"].status == "skipped:budget"


def test_logs_redact_secrets(env):
    stream = io.StringIO()
    logs.setup("r", stream=stream)
    logs.REDACT.add(TOKEN)
    logging.getLogger("rightsizing.test").info(f"oops {TOKEN} Bearer abcdefghijkl token=hunter22",
                                                extra={"url": "https://bot:pw123@gitea/x.git"})
    run(env)
    text = stream.getvalue()
    assert TOKEN not in text and "abcdefghijkl" not in text and "hunter22" not in text and "pw123" not in text
    summary = [json.loads(line) for line in text.splitlines() if '"msg": "summary"' in line]
    assert summary and summary[-1]["counts"] == {"committed": 2} and summary[-1]["coverage"] == 1.0


def test_cli_help_lists_flags():
    out = subprocess.run([sys.executable, "-m", "rightsizing", "--help"], capture_output=True, text=True,
                         cwd=Path(__file__).parents[2]).stdout
    assert "--dry-run" in out and "--no-llm" in out and "--only" in out


def test_stale_pending_flagged(env):
    from datetime import timedelta

    run(env)
    _, out, _ = run(env, now=datetime.now(timezone.utc) + timedelta(days=1))
    assert out["web"].detail.startswith("pending_same") and "stale_pending" not in out["web"].detail
    report, out, _ = run(env, now=datetime.now(timezone.utc) + timedelta(days=20))
    assert out["web"].detail.startswith("pending_same") and "stale_pending" in out["web"].detail


def test_dry_run_leaves_worktree_clean(env):
    report, out, _ = run(env, dry_run=True)
    work = env["tmp"] / f"work{env['runs']}" / "repo"
    assert git(work, "status", "--porcelain") == ""
