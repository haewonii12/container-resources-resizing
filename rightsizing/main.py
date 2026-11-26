"""Orchestrator: discovery -> metrics -> policy -> LLM review -> edit -> render-validate -> commit -> push.

Exit codes: 0 ok | 2 infrastructure failure | 3 security event (other workloads' commits are still pushed).
"""

from __future__ import annotations

import argparse
import logging
import secrets as pysecrets
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import commit_msg, logs, runrecord
from .config import DEFAULT_CONFIG_PATH, Config, SecretStore, load_config
from .discovery import Candidate, KubeAPI, discover
from .gitops import PENDING, WEBHOOK_MUTATED, GitError, GitRepo, History, PathGuardError, clone, git_env, \
    pending_state, write_askpass
from .manifest import SkipEdit
from .manifest.detect import detect_format
from .manifest.editors import edit_helm, edit_kustomize, edit_plain
from .manifest.render import Renderer, check_drift, container_resources, nondeterministic_mask, validate_edit
from .mcp_client import HttpMcpClient, McpError
from .models import FIELDS, ContainerRec, ContainerResources, FieldChange, Outcome, split_field
from .pending import decide_pending, expected_head
from .policy import PolicyParams, recommend_container
from .prom import PromClient, collect_usage, workload_stats
from .review_loop import Reviewer

log = logging.getLogger("rightsizing.main")

EXIT_OK, EXIT_INFRA, EXIT_SECURITY = 0, 2, 3


@dataclass
class Deps:
    kube: KubeAPI
    prom: PromClient
    llm: object | None  # openai.OpenAI-compatible
    mcp: object | None  # HttpMcpClient-compatible
    secrets: SecretStore
    workdir: Path
    renderer: Renderer | None = None
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class RunOptions:
    dry_run: bool = False
    no_llm: bool = False
    only: list[str] = field(default_factory=list)


@dataclass
class RunReport:
    exit_code: int
    outcomes: list[Outcome]
    pushed: int = 0
    proposals: list[str] = field(default_factory=list)
    run_status: str = "ok"  # ok | failed:<prometheus|mcp|git_clone>
    push_failed: bool = False

    def counts(self) -> Counter:
        return Counter(o.status.split(":")[0] for o in self.outcomes)


class Runner:
    def __init__(self, cfg: Config, deps: Deps, opts: RunOptions):
        self.cfg, self.deps, self.opts = cfg, deps, opts
        self.run_id = f"{deps.now:%Y%m%dT%H%M%SZ}-{pysecrets.token_hex(3)}"
        self.security_events: list[str] = []
        self.proposals: list[str] = []
        self.reviewer: Reviewer | None = None
        self.repo: GitRepo | None = None
        self.history: History | None = None
        self._ctx: dict = {}  # per-workload facts for the run record (proposal, pending, LLM decision)

    # ------------------------------------------------------------------ run
    def run(self) -> RunReport:
        report = self._run()
        if self.cfg.records_enabled and not self.opts.dry_run and not self.opts.only:
            self._push_record(report)
        return report

    def _push_record(self, report: RunReport) -> None:
        """Write the run record to the report repo; a failure never hides a security exit (3)."""
        deps = self.deps
        record = runrecord.build_record(
            self.cfg, run_id=self.run_id, started_at=deps.now, finished_at=datetime.now(timezone.utc),
            run_status=report.run_status, exit_code=report.exit_code, pushed=report.pushed > 0,
            outcomes=report.outcomes)
        record["push_failed"] = report.push_failed
        token = deps.secrets.path("gitea_token")
        try:
            sha = runrecord.write_record(self.cfg, record, deps.workdir, token if token.exists() else None)
            log.info("run record", extra={"date": record["date"], "commit": sha})
        except Exception as exc:  # the record is a by-product: log, and fail the Job only if it was clean
            log.error("run_record_push_failed", extra={"error": str(exc)[-300:]})
            if report.exit_code == EXIT_OK:
                report.exit_code = EXIT_INFRA

    def _run(self) -> RunReport:
        cfg, deps = self.cfg, self.deps
        start = time.monotonic()
        outcomes: list[Outcome] = []

        if deps.prom.vector("vector(1)") is None:
            log.error("prometheus unreachable")
            return RunReport(EXIT_INFRA, outcomes, run_status="failed:prometheus")
        if not self.opts.no_llm:
            try:
                deps.mcp.connect(deadline_seconds=cfg.timeouts.mcp_connect)
                log.info("mcp connected", extra={"tools": sorted(t.get("name") for t in deps.mcp.list_tools())})
            except McpError as exc:
                log.error("mcp sidecar unreachable", extra={"error": str(exc)})
                return RunReport(EXIT_INFRA, outcomes, run_status="failed:mcp")
            self.reviewer = Reviewer(deps.llm, cfg.llm.model, deps.mcp,
                                     lambda ref, metric, days, q: workload_stats(deps.prom, ref, metric, days, q),
                                     cfg.llm.max_turns,
                                     cfg.timeouts.llm_total, frozenset(cfg.namespaces))

        if cfg.min_days == 0:
            log.warning("min_days=0: recommendations may be based on very little data (testing only)")
        candidates, skipped = discover(deps.kube, cfg)
        if self.opts.only:
            candidates = [c for c in candidates if c.ref.key in self.opts.only]
            skipped = [s for s in skipped if s.ref.key in self.opts.only]
        outcomes += [Outcome(s.ref.key, f"skipped:{s.reason}", s.detail) for s in skipped]

        try:
            token_file = deps.secrets.path("gitea_token")
            env = git_env(write_askpass(deps.workdir / "bin"), cfg.git.username,
                          token_file if token_file.exists() else None, deps.workdir / "home")
            (deps.workdir / "home").mkdir(parents=True, exist_ok=True)
            self.repo = clone(cfg.git.url, cfg.git.branch, deps.workdir / "repo", env, cfg.timeouts.git)
            self.history = History.scan(self.repo)
        except GitError as exc:
            log.error("git clone failed", extra={"error": str(exc)})
            return RunReport(EXIT_INFRA, outcomes, run_status="failed:git_clone")

        renderer = deps.renderer or Renderer(timeout=cfg.timeouts.render, kube_version=deps.kube.version(),
                                             api_versions=deps.kube.api_versions(),
                                             kustomize_build_options=cfg.argocd.kustomize_build_options)
        self._renderer = renderer
        for cand in candidates:
            reserve = cfg.timeouts.reserve(not self.opts.no_llm, cfg.records_enabled)
            if time.monotonic() - start + reserve > cfg.timeouts.run_budget:
                outcomes.append(Outcome(cand.ref.key, "skipped:budget"))
                continue
            self._ctx = {}
            try:
                outcome = self.process(cand, renderer)
            except SkipEdit as exc:
                outcome = Outcome(cand.ref.key, f"skipped:{exc.reason}", exc.detail)
            except PathGuardError as exc:
                self.repo.reset_worktree()
                outcome = Outcome(cand.ref.key, "skipped:path_guard", str(exc))
            except Exception as exc:  # one workload must never break the run
                log.exception("workload failed", extra={"workload": cand.ref.key})
                self.repo.reset_worktree()
                outcome = Outcome(cand.ref.key, "skipped:error", type(exc).__name__)
            self._annotate(outcome)
            log.info("workload", extra={"workload": outcome.workload, "outcome": outcome.status,
                                        "detail": outcome.detail, "commit": outcome.commit})
            outcomes.append(outcome)

        exit_code = EXIT_SECURITY if self.security_events else EXIT_OK
        pushed, push_failed = 0, False
        committed = [o for o in outcomes if o.status in ("committed", "superseded", "withdrawn")]
        if committed and not self.opts.dry_run and cfg.git.push_enabled:
            try:
                self.repo.push(cfg.git.branch)
                pushed = len(committed)
            except GitError as exc:
                log.error("git push failed", extra={"error": str(exc)})
                push_failed = True
                exit_code = EXIT_SECURITY if self.security_events else EXIT_INFRA
        report = RunReport(exit_code, outcomes, pushed, self.proposals, push_failed=push_failed)
        self._summary(report, len(candidates) + len(skipped))
        return report

    # ------------------------------------------------------------------ one workload
    def process(self, cand: Candidate, renderer: Renderer) -> Outcome:
        cfg, repo, ref = self.cfg, self.repo, cand.ref
        src, key = cand.source, (ref.kind, ref.namespace, ref.name)
        mappings = cfg.mapping_for(ref.key)
        fmt = detect_format(src, repo.workdir, mappings)

        before = renderer.render(fmt, repo.workdir, src)
        mask = nondeterministic_mask(before, renderer.render(fmt, repo.workdir, src))
        head = {c: container_resources(before, key, c) for c in cand.containers}
        latest = self.history.latest_unreverted(ref.key)
        pending_commit, pending_vals = None, {}
        if latest:
            state = pending_state(latest, head, cand.containers, cand.argo_status)
            if state == PENDING:
                if self.history.endorsed_pending(ref.key, cfg.suppress_days, self.deps.now.timestamp()):
                    self._ctx["pending"] = self._pending_info(latest, "endorsed")
                    self._ctx["proposal"] = [{"container": ch.container, "field": ch.field, "current": ch.old,
                                              "proposed": ch.new, "changed": True, "direction": ch.direction,
                                              "basis": "승인 대기 중인 제안", "reason": None, "p95": None,
                                              "max": None} for ch in latest.changes]
                    return Outcome(ref.key, "no_change", f"pending_endorsed {latest.sha[:10]}")
                pending_vals = {(ch.container, ch.field): ch.new for ch in latest.changes}
                want = expected_head(cand.containers, pending_vals)
                drifted = sorted(c for c in head if not head[c].same_as(want[c]))
                if drifted:
                    return Outcome(ref.key, "skipped:drift", f"HEAD differs from pending proposal: {drifted}")
                pending_commit = latest
            elif state == WEBHOOK_MUTATED:
                return Outcome(ref.key, "skipped:drift", "live differs while ArgoCD reports Synced (webhook?)")
        if pending_commit is None:
            for name, live in cand.containers.items():
                check_drift(before, key, name, live)

        cpu_limited = {c: r.get("cpu.limit") is not None for c, r in cand.containers.items()}
        usage, replicas, source = collect_usage(self.deps.prom, ref, cpu_limited, cfg.window_days,
                                                cfg.prometheus.use_recording_rules)
        policy = cfg.policy_for(ref.namespace)
        params = PolicyParams(cfg.min_days, cfg.min_change, cfg.throttle_threshold, cfg.cpu_limit_mode)
        recs = [recommend_container(c, cand.containers[c], usage[c], policy, params, cand.limitrange,
                                    cand.blocked_resources) for c in cand.containers]
        self._apply_suppression(ref.key, recs)
        self._ctx["proposal"] = runrecord.proposal_rows(cand.containers, recs, usage)
        window = f"{(self.deps.now - timedelta(days=cfg.window_days)):%Y-%m-%d}..{self.deps.now:%Y-%m-%d} " \
                 f"({cfg.window_days}d)"
        if pending_commit is not None:
            return self._process_pending(cand, fmt, before, mask, head, recs, usage, replicas, window, source,
                                         pending_commit, pending_vals, policy, mappings)
        changes = [ch for r in recs for ch in r.changes]
        if not changes:
            reasons = sorted({v for r in recs for v in r.skipped.values()})
            return Outcome(ref.key, "no_change", ",".join(reasons))
        note, held = self._review(ref, self._payload(cand, recs, usage, replicas, window, source))
        if held:
            return held

        edits, expected = {}, {}
        for rec in recs:
            if rec.changes:
                edits[rec.container] = {ch.field: ch.new for ch in rec.changes}
                final = dict(cand.containers[rec.container].values)
                final.update(edits[rec.container])
                expected[rec.container] = ContainerResources(final)
        info = self._commit_info(ref, changes, window, usage, policy, replicas, note)
        return self._edit_and_commit(cand, fmt, before, mask, head, mappings, edits, expected,
                                     commit_msg.render(info), commit_msg.title(info))

    def _process_pending(self, cand: Candidate, fmt, before, mask, head, recs, usage, replicas, window, source,
                         pending_commit, pending_vals, policy, mappings) -> Outcome:
        """A previous proposal is still waiting for a human sync: keep it, supersede it, or withdraw it."""
        ref = cand.ref
        d = decide_pending(cand.containers, head, pending_vals, recs, self.cfg.min_change)
        self._ctx["pending"] = self._pending_info(pending_commit, d.kind)
        basis = {(ch.container, ch.field): ch.basis for ch in d.changes}
        rows = []
        for c, target in d.target.items():  # target vs LIVE: what stays proposed after this run
            for f in FIELDS:
                old, new = cand.containers[c].get(f) if c in cand.containers else None, target.get(f)
                if new is None or (old is not None and abs(new - old) < 1e-6):
                    continue
                direction = "set" if old is None else ("shrink" if new < old else "grow")
                rows.append(runrecord.proposal_row(c, f, old, new, True, direction,
                                      basis.get((c, f), "승인 대기 중인 제안"), None, usage.get(c)))
        self._ctx["proposal"] = rows
        if d.kind == "same":
            age = (self.deps.now.timestamp() - pending_commit.time) / 86400
            stale = " stale_pending" if age > self.cfg.suppress_days else ""
            return Outcome(ref.key, "no_change", f"pending_same {pending_commit.sha[:10]} age={age:.1f}d{stale}")
        if d.kind == "conflict":
            return Outcome(ref.key, "skipped:pending_conflict", d.detail)
        expected = {c: d.target[c] for c in d.edits}
        if d.kind == "withdraw":
            restored = [FieldChange(c, f, pending_vals[(c, f)], cand.containers[c].get(f), "set", "현재 값")
                        for (c, f) in sorted(pending_vals) if cand.containers[c].get(f) is not None]
            message = commit_msg.render_withdraw(ref, f"{self.deps.now:%Y%m%d}", self.run_id, pending_commit.sha,
                                                 restored, "최신 사용량 기준으로 변경이 더 이상 필요하지 않음")
            return self._edit_and_commit(cand, fmt, before, mask, head, mappings, d.edits, expected, message,
                                         f"withdraw {pending_commit.sha[:10]}", status="withdrawn")
        payload = self._payload(cand, recs, usage, replicas, window, source)
        payload["pending_proposal"] = {"sha": pending_commit.sha,
                                       "values": {f"{c}.{f}": v for (c, f), v in pending_vals.items()}}
        payload["recommendation"] = [{"container": ch.container, "field": ch.field, "old": ch.old, "new": ch.new,
                                      "direction": ch.direction, "basis": ch.basis} for ch in d.changes]
        note, held = self._review(ref, payload)
        if held:
            return held
        info = self._commit_info(ref, d.changes, window, usage, policy, replicas, note, pending_commit.sha)
        return self._edit_and_commit(cand, fmt, before, mask, head, mappings, d.edits, expected,
                                     commit_msg.render(info), commit_msg.title(info), status="superseded")

    def _review(self, ref, payload: dict) -> tuple[str, Outcome | None]:
        """LLM review (or the explicit --no-llm note). Returns (note, held outcome or None)."""
        if self.opts.no_llm:
            self._ctx.update(decision=None, notes="사용 안 함 (--no-llm)")
            return "사용 안 함 (--no-llm)", None
        review = self.reviewer.review(ref, payload)
        if review.security_events:
            self.security_events += [f"{ref.key}: {e}" for e in review.security_events]
        self._ctx.update(decision="apply" if review.decision == "apply" else "hold", notes=review.notes)
        if review.decision != "apply":
            return review.notes, Outcome(ref.key, f"held:{review.reason}", review.notes)
        return review.notes, None

    # ------------------------------------------------------------------ run record facts
    def _annotate(self, outcome: Outcome) -> None:
        """Attach what this workload's code path decided (one source of truth for the daily report)."""
        ctx = self._ctx
        outcome.proposal = ctx.get("proposal", [])
        outcome.pending = ctx.get("pending")
        if "decision" in ctx and outcome.status.split(":")[0] in ("committed", "superseded", "held", "proposed"):
            outcome.llm_decision, outcome.llm_notes = ctx["decision"], ctx.get("notes", "")

    def _pending_info(self, commit, kind: str) -> dict:
        return {"sha": commit.sha, "age_days": round((self.deps.now.timestamp() - commit.time) / 86400, 1),
                "kind": kind}


    def _commit_info(self, ref, changes, window, usage, policy, replicas, note,
                     supersedes: str | None = None) -> commit_msg.CommitInfo:
        return commit_msg.CommitInfo(
            workload=ref, date=f"{self.deps.now:%Y%m%d}", run_id=self.run_id, changes=changes, window=window,
            oom={c: (None if u.oom is None else int(u.oom)) for c, u in usage.items()},
            throttle={c: u.throttle_ratio for c, u in usage.items()}, policy_label=policy.label or ref.namespace,
            avg_replicas=replicas, llm_note=note, supersedes=supersedes)

    def _edit_and_commit(self, cand: Candidate, fmt, before, mask, head, mappings, edits, expected, message, title,
                         status: str = "committed") -> Outcome:
        cfg, repo, ref, src = self.cfg, self.repo, cand.ref, cand.source
        key = (ref.kind, ref.namespace, ref.name)
        files: list[str] = []
        try:
            for container, values in edits.items():
                if fmt == "plain":
                    touched = edit_plain(repo.workdir, src, ref.kind, ref.name, ref.namespace, container, values)
                elif fmt == "helm":
                    touched = edit_helm(repo.workdir, src, container, head[container], values, mappings)
                else:
                    touched = edit_kustomize(repo.workdir, src, ref.kind, ref.name, container, values)
                files += [f for f in touched if f not in files]
            validate_edit(before, self._renderer.render(fmt, repo.workdir, src), mask, key, expected)
        except SkipEdit:
            repo.reset_worktree()
            raise
        if self.opts.dry_run:
            repo.reset_worktree()
            self.proposals.append(message)
            return Outcome(ref.key, "proposed", title)
        sha = repo.commit(files, message, cfg.git.author_name, cfg.git.author_email, allowed_prefix=src.path)
        if sha is None:
            return Outcome(ref.key, "no_change", "edit produced no diff")
        self.proposals.append(message)
        return Outcome(ref.key, status, title, commit=sha)

    def _apply_suppression(self, workload: str, recs: list[ContainerRec]) -> None:
        for rec in recs:
            blocked = {split_field(ch.field)[0] for ch in rec.changes
                       if self.history.is_suppressed(workload, rec.container, ch.field, ch.direction,
                                                     self.cfg.suppress_days, self.deps.now.timestamp())}
            if blocked:  # drop the whole resource so pairs/invariants stay consistent
                for ch in [c for c in rec.changes if split_field(c.field)[0] in blocked]:
                    rec.changes.remove(ch)
                    rec.skipped[ch.field] = "suppressed_by_revert"
                    rec.proposed.pop(ch.field, None)  # pending path must keep the pending value, not this one

    @staticmethod
    def _payload(cand: Candidate, recs, usage, replicas, window, source) -> dict:
        return {
            "workload": cand.ref.key, "argocd_application": cand.source.app_name, "window": window,
            "metrics_source": source, "avg_replicas": replicas, "hpa_blocked": sorted(cand.blocked_resources),
            "containers": {c: {"current": cand.containers[c].values,
                               "usage": {"cpu_p95_m": u.cpu.p95, "cpu_max_m": u.cpu.max, "mem_p95_b": u.memory.p95,
                                         "mem_max_b": u.memory.max, "coverage_h": u.coverage_hours,
                                         "oom": u.oom, "restarts": u.restarts, "throttle": u.throttle_ratio}}
                           for c, u in usage.items()},
            "recommendation": [{"container": ch.container, "field": ch.field, "old": ch.old, "new": ch.new,
                                "direction": ch.direction, "basis": ch.basis} for r in recs for ch in r.changes],
            "skipped_fields": {r.container: r.skipped for r in recs if r.skipped},
        }

    def _summary(self, report: RunReport, total: int) -> None:
        counts = report.counts()
        reasons = Counter(o.status for o in report.outcomes if o.status.startswith(("skipped:", "held:")))
        acted = counts.get("committed", 0) + counts.get("held", 0) + counts.get("proposed", 0)
        stale = sum(1 for o in report.outcomes if "stale_pending" in o.detail)
        log.info("summary", extra={"total": total, "counts": dict(counts), "reasons": dict(reasons),
                                   "coverage": round(acted / total, 3) if total else None, "stale_pending": stale,
                                   "pushed": report.pushed, "security_events": self.security_events,
                                   "exit_code": report.exit_code})


# ---------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rightsizing", description=__doc__.splitlines()[0])
    p.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="config YAML (ConfigMap)")
    p.add_argument("--dry-run", action="store_true", help="compute and print proposals; never commit or push")
    p.add_argument("--no-llm", action="store_true", help="skip the LLM review (commits note it explicitly)")
    p.add_argument("--only", action="append", default=[], metavar="NS/KIND/NAME", help="limit to workload(s)")
    p.add_argument("--workdir", default=None, help="scratch dir (default: a temp dir)")
    p.add_argument("--log-level", default="INFO")
    return p


def cli(argv: list[str] | None = None) -> int:
    try:
        return _cli(argv)
    finally:
        _signal_done()


def _signal_done() -> None:
    """Legacy (<1.29) plain-sidecar mode: tell the kubectl-ai wrapper to exit so the Job can complete."""
    import os

    done = os.environ.get("RS_DONE_FILE")
    if done:
        try:
            Path(done).touch()
        except OSError:
            pass


def _cli(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    store = SecretStore(cfg.secret_dir)
    workdir = Path(args.workdir or tempfile.mkdtemp(prefix="rightsizing-"))
    llm = mcp = None
    if not args.no_llm:
        import openai

        llm = openai.OpenAI(base_url=cfg.llm.base_url, api_key=store.read("llm_api_key"),
                            timeout=cfg.timeouts.llm_turn, max_retries=1)
        mcp = HttpMcpClient(cfg.mcp.url, timeout=cfg.timeouts.llm_turn)
    if store.path("gitea_token").exists():
        store.read("gitea_token")
    logs.REDACT.add(*store.known_values())
    from .discovery import KubeClient

    deps = Deps(kube=KubeClient(cfg), prom=PromClient(cfg.prometheus.url, cfg.timeouts.prom), llm=llm, mcp=mcp,
                secrets=store, workdir=workdir)
    runner = Runner(cfg, deps, RunOptions(args.dry_run, args.no_llm, args.only))
    logs.setup(runner.run_id, args.log_level)
    report = runner.run()
    for message in report.proposals if args.dry_run or not cfg.git.push_enabled else []:
        print("-" * 72 + "\n" + logs.REDACT(message))
    return report.exit_code


if __name__ == "__main__":
    sys.exit(cli())
