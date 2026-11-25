import subprocess
import time

import pytest

from rightsizing import gitops
from rightsizing.commit_msg import CommitInfo, render
from rightsizing.gitops import (APPLIED, PENDING, SUPERSEDED, WEBHOOK_MUTATED, History, PathGuardError,
                                clone, git_env, pending_state, write_askpass)
from rightsizing.models import ContainerResources, FieldChange, WorkloadRef
from rightsizing.quantity import GI

WL = WorkloadRef("development", "Deployment", "web")


def sh(cwd, *args, env=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout


@pytest.fixture
def remote(tmp_path):
    bare = tmp_path / "remote.git"
    sh(tmp_path, "init", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    sh(tmp_path, "clone", str(bare), str(seed))
    (seed / "apps/web").mkdir(parents=True)
    (seed / "apps/web/deploy.yaml").write_text("kind: Deployment\n")
    (seed / "apps/other").mkdir(parents=True)
    (seed / "apps/other/x.yaml").write_text("x: 1\n")
    sh(seed, "add", ".")
    sh(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init")
    sh(seed, "push", "origin", "main")
    return bare, seed


@pytest.fixture
def repo(tmp_path, remote):
    bare, _ = remote
    home = tmp_path / "home"
    home.mkdir()
    env = git_env(write_askpass(tmp_path / "bin"), "bot", None, home)
    return clone(f"file://{bare}", "main", tmp_path / "work", env)


def msg(changes, run="r1"):
    return render(CommitInfo(WL, "20261125", run, changes, "w", {}, {}, "development", 1.0, ""))


def test_clone_is_full_history_partial(repo):
    assert repo.run("rev-parse", "--is-shallow-repository").stdout.strip() == "false"
    assert repo.run("config", "remote.origin.partialclonefilter", check=False).stdout.strip() in ("blob:none", "")


def test_token_never_in_argv_or_env(tmp_path, monkeypatch):
    token_file = tmp_path / "gitea_token"
    token_file.write_text("SUPERSECRET")
    env = git_env(write_askpass(tmp_path / "bin"), "bot", token_file, tmp_path)
    seen = []
    real_run = subprocess.run

    def spy(argv, **kw):
        seen.append((argv, kw.get("env") or {}))
        return real_run(argv, **kw)

    monkeypatch.setattr(gitops.subprocess, "run", spy)
    with pytest.raises(gitops.GitError):
        clone("https://127.0.0.1:1/org/repo.git", "main", tmp_path / "w", env, timeout=20)
    for argv, e in seen:
        assert "SUPERSECRET" not in " ".join(argv)
        assert all("SUPERSECRET" not in str(v) for v in e.values())
    # askpass hands out the token only when git asks for a password
    out = real_run([env["GIT_ASKPASS"], "Password for 'https://bot@gitea':"], env=env, capture_output=True,
                   text=True).stdout
    assert out == "SUPERSECRET"


def test_path_guard(repo):
    (repo.workdir / "apps/other/x.yaml").write_text("x: 2\n")
    with pytest.raises(PathGuardError):
        repo.commit(["apps/other/x.yaml"], "m", "a", "a@a", "apps/web")
    with pytest.raises(PathGuardError):
        repo.commit(["apps/web/../other/x.yaml"], "m", "a", "a@a", "apps/web")


def test_noop_commit_returns_none(repo):
    assert repo.commit(["apps/web/deploy.yaml"], "m", "a", "a@a", "apps/web") is None


def test_commit_and_push(repo, remote):
    bare, _ = remote
    (repo.workdir / "apps/web/deploy.yaml").write_text("kind: Deployment\n# changed\n")
    sha = repo.commit(["apps/web/deploy.yaml"], msg([FieldChange("web", "cpu.request", 2000, 1000, "shrink", "b")]),
                      "bot", "bot@x", "apps/web")
    assert sha and repo.unpushed_count("main") == 1
    repo.push("main")
    assert sh(bare, "rev-parse", "main").strip() == sha


def test_push_rebase_retry(repo, remote, tmp_path):
    bare, seed = remote
    (seed / "apps/other/x.yaml").write_text("x: 3\n")
    sh(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-am", "concurrent")
    sh(seed, "push", "origin", "main")
    (repo.workdir / "apps/web/deploy.yaml").write_text("kind: Deployment\n# mine\n")
    repo.commit(["apps/web/deploy.yaml"], "mine", "bot", "bot@x", "apps/web")
    repo.push("main")
    log = sh(bare, "log", "--format=%s", "main").split("\n")
    assert log[0] == "mine" and "concurrent" in log


def _commit(repo, changes, content):
    (repo.workdir / "apps/web/deploy.yaml").write_text(content)
    return repo.commit(["apps/web/deploy.yaml"], msg(changes), "bot", "bot@x", "apps/web")


def test_revert_suppression_same_direction_only(repo):
    sha = _commit(repo, [FieldChange("web", "cpu.request", 2000, 1000, "shrink", "b"),
                         FieldChange("web", "memory.request", 1 * GI, 2 * GI, "grow", "b")], "a: 1\n")
    repo.run("-c", "user.name=h", "-c", "user.email=h@h", "revert", "--no-edit", sha)
    hist = History.scan(repo)
    k = WL.key
    assert hist.is_suppressed(k, "web", "cpu.request", "shrink", days=14)
    assert not hist.is_suppressed(k, "web", "cpu.request", "grow", days=14)
    assert hist.is_suppressed(k, "web", "memory.request", "grow", days=14)  # mixed commit: each field tracked
    assert not hist.is_suppressed(k, "web", "memory.request", "shrink", days=14)
    assert not hist.is_suppressed(k, "web", "cpu.request", "shrink", days=14, now=time.time() + 15 * 86400)
    assert hist.latest_unreverted(k) is None


def test_latest_unreverted(repo):
    _commit(repo, [FieldChange("web", "cpu.request", 2000, 1000, "shrink", "b")], "a: 1\n")
    sha2 = _commit(repo, [FieldChange("web", "cpu.request", 1000, 800, "shrink", "b")], "a: 2\n")
    hist = History.scan(repo)
    assert hist.latest_unreverted(WL.key).sha == sha2


def _cr(cpu):
    return {"web": ContainerResources({"cpu.request": cpu, "cpu.limit": None, "memory.request": None,
                                       "memory.limit": None})}


def test_pending_states(repo):
    _commit(repo, [FieldChange("web", "cpu.request", 2000, 1000, "shrink", "b")], "a: 1\n")
    c = History.scan(repo).latest_unreverted(WL.key)
    assert pending_state(c, head=_cr(1000.0), live=_cr(2000.0), argo_status="OutOfSync") == PENDING
    assert pending_state(c, head=_cr(1000.0), live=_cr(2000.0), argo_status=None) == PENDING
    assert pending_state(c, head=_cr(1000.0), live=_cr(1000.0), argo_status="Synced") == APPLIED
    assert pending_state(c, head=_cr(1500.0), live=_cr(2000.0), argo_status="OutOfSync") == SUPERSEDED
    assert pending_state(c, head=_cr(1000.0), live=_cr(1200.0), argo_status="Synced") == WEBHOOK_MUTATED


def test_reset_worktree_discards_edits_everywhere(repo):
    (repo.workdir / "apps/web/deploy.yaml").write_text("broken\n")
    (repo.workdir / "apps/web/new.yaml").write_text("x\n")
    (repo.workdir / "apps/other/x.yaml").write_text("shared edit outside the app path\n")
    repo.reset_worktree()
    assert (repo.workdir / "apps/web/deploy.yaml").read_text() == "kind: Deployment\n"
    assert (repo.workdir / "apps/other/x.yaml").read_text() == "x: 1\n"
    assert not (repo.workdir / "apps/web/new.yaml").exists()
