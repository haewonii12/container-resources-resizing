"""Git operations: clone, history scan (revert suppression / pending), guarded commits, push with retry.

The Gitea token is never placed in argv, URLs, or logs: git asks GIT_ASKPASS, which reads the mounted file.
"""

from __future__ import annotations

import os
import posixpath
import stat
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import commit_msg
from .models import ContainerResources


class GitError(RuntimeError):
    pass


class PathGuardError(GitError):
    pass


ASKPASS = """#!/bin/sh
case "$1" in
  Username*) printf '%s\\n' "$RS_GIT_USERNAME" ;;
  *) cat "$RS_GIT_TOKEN_FILE" ;;
esac
"""


def write_askpass(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "git-askpass.sh"
    path.write_text(ASKPASS, encoding="utf-8")
    path.chmod(stat.S_IRWXU)
    return path


def git_env(askpass: Path, username: str, token_file: Path | None, home: Path) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "LANG": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ASKPASS": str(askpass),
        "RS_GIT_USERNAME": username,
    }
    if token_file is not None:
        env["RS_GIT_TOKEN_FILE"] = str(token_file)
    return env


@dataclass
class GitRepo:
    workdir: Path
    env: dict[str, str]
    timeout: float = 120
    identity: tuple[str, ...] = ()  # set by commit(); reused by push() when it has to rebase

    def run(self, *args: str, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
        try:
            proc = subprocess.run(["git", *args], cwd=self.workdir, env=self.env, input=input_text,
                                  capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {args[0]} timed out") from exc
        if check and proc.returncode != 0:
            raise GitError(f"git {args[0]} failed: {proc.stderr.strip()[-400:]}")
        return proc

    # ------------------------------------------------------------------ commits
    def commit(self, paths: list[str], message: str, author_name: str, author_email: str,
               allowed_prefix: str) -> str | None:
        prefix = posixpath.normpath(allowed_prefix).strip("/")
        for rel in paths:
            norm = posixpath.normpath(rel)
            if norm.startswith("..") or os.path.isabs(norm):
                raise PathGuardError(f"path escapes repo: {rel}")
            if prefix not in ("", ".") and not (norm == prefix or norm.startswith(prefix + "/")):
                raise PathGuardError(f"refusing to stage {rel}: outside application path {prefix}")
        present = [p for p in paths if (self.workdir / p).exists()]
        if present:  # paths removed with `git rm` are already staged and would not match a pathspec
            self.run("add", "-A", "--", *present)
        if self.run("diff", "--cached", "--quiet", check=False).returncode == 0:
            return None
        self.identity = ("-c", f"user.name={author_name}", "-c", f"user.email={author_email}")
        self.run(*self.identity, "commit", "--no-verify", "-F", "-", input_text=message)
        return self.head()

    def head(self) -> str:
        return self.run("rev-parse", "HEAD").stdout.strip()

    def reset_worktree(self) -> None:
        """Discard ALL uncommitted edits (earlier workloads are already committed, so this is safe).

        Repo-wide on purpose: an editor may touch files outside the app path before the path guard rejects it."""
        self.run("reset", "--hard", "-q", "HEAD")
        self.run("clean", "-fdq")

    def unpushed_count(self, branch: str) -> int:
        out = self.run("rev-list", "--count", f"origin/{branch}..HEAD", check=False)
        return int(out.stdout.strip() or 0) if out.returncode == 0 else 0

    def push(self, branch: str, retries: int = 2) -> None:
        for attempt in range(retries + 1):
            proc = self.run("push", "origin", f"HEAD:refs/heads/{branch}", check=False)
            if proc.returncode == 0:
                return
            rejected = "rejected" in proc.stderr or "non-fast-forward" in proc.stderr or "fetch first" in proc.stderr
            if not rejected or attempt == retries:
                raise GitError(f"git push failed: {proc.stderr.strip()[-400:]}")
            # the rebase rewrites our commits, so it needs the committer identity used by commit()
            rebase = self.run(*self.identity, "pull", "--rebase", "origin", branch, check=False)
            if rebase.returncode != 0:
                self.run("rebase", "--abort", check=False)
                raise GitError(f"rebase onto origin/{branch} failed: {rebase.stderr.strip()[-400:]}")


def clone(url: str, branch: str, dest: Path, env: dict[str, str], timeout: float = 120) -> GitRepo:
    """Full-history partial clone (commits/trees complete, blobs on demand)."""
    proc = subprocess.run(["git", "clone", "--filter=blob:none", "--branch", branch, "--single-branch", url,
                           str(dest)], env=env, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise GitError(f"git clone failed: {proc.stderr.strip()[-400:]}")
    return GitRepo(dest, env, timeout)


# ---------------------------------------------------------------------- history


@dataclass
class RSCommit:
    sha: str
    time: int
    workload: str
    rs_id: str | None
    changes: list[commit_msg.ParsedChange]
    kind: str = "propose"  # propose | supersede | withdraw


@dataclass
class History:
    commits: list[RSCommit] = field(default_factory=list)  # newest first
    reverts: dict[str, int] = field(default_factory=dict)  # reverted sha -> revert commit time

    @classmethod
    def scan(cls, repo: GitRepo) -> "History":
        out = repo.run("log", "--format=%H%x1f%ct%x1f%B%x1e").stdout
        hist = cls()
        for record in out.split("\x1e"):
            record = record.strip("\n")
            if not record:
                continue
            sha, ts, body = record.split("\x1f", 2)
            reverted = commit_msg.reverted_sha(body)
            if reverted:
                hist.reverts[reverted] = int(ts)
            trailers = commit_msg.parse_trailers(body)
            if trailers.workload and not reverted:
                hist.commits.append(RSCommit(sha, int(ts), trailers.workload, trailers.id, trailers.changes,
                                             trailers.kind))
        return hist

    def _revert_time(self, sha: str) -> int | None:
        for short, ts in self.reverts.items():
            if sha.startswith(short) or short.startswith(sha):
                return ts
        return None

    def is_reverted(self, commit: RSCommit) -> bool:
        return self._revert_time(commit.sha) is not None

    def is_suppressed(self, workload: str, container: str, fname: str, direction: str, days: int,
                      now: float | None = None) -> bool:
        now = time.time() if now is None else now
        for c in self.commits:
            if c.workload != workload:
                continue
            ts = self._revert_time(c.sha)
            if ts is None or now - ts > days * 86400:
                continue
            for ch in c.changes:
                if ch.container == container and ch.field == fname and ch.direction == direction:
                    return True
        return False

    def endorsed_pending(self, workload: str, days: int, now: float | None = None) -> bool:
        """True when the newest bot commit for the workload is a supersede/withdraw that a human reverted
        within `days`: the restored pending proposal is then treated as the human's decision."""
        now = time.time() if now is None else now
        newest = next((c for c in self.commits if c.workload == workload), None)
        if newest is None or newest.kind == "propose":
            return False
        ts = self._revert_time(newest.sha)
        return ts is not None and now - ts <= days * 86400

    def latest_unreverted(self, workload: str) -> RSCommit | None:
        for c in self.commits:
            if c.workload == workload and not self.is_reverted(c):
                return c
        return None


PENDING = "pending"
APPLIED = "applied"
SUPERSEDED = "superseded"
WEBHOOK_MUTATED = "webhook_mutated"


def pending_state(commit: RSCommit, head: dict[str, ContainerResources], live: dict[str, ContainerResources],
                  argo_status: str | None) -> str:
    """Classify the latest unreverted rightsizing commit for a workload (all comparisons normalized).

    pending only if HEAD still carries the commit's values AND live differs AND ArgoCD reports OutOfSync.
    """
    def matches(resources: dict[str, ContainerResources]) -> bool:
        for ch in commit.changes:
            cur = resources.get(ch.container)
            value = cur.get(ch.field) if cur else None
            if value is None or abs(value - ch.new) > 1e-6:
                return False
        return True

    if not matches(head):
        return SUPERSEDED
    if matches(live):
        return APPLIED
    if argo_status == "Synced":
        return WEBHOOK_MUTATED
    return PENDING
