"""CLI: python -m rightsizing.mirror [--config PATH] [--workdir DIR] [--dry-run]"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .. import logs
from ..config import DEFAULT_CONFIG_PATH, Config, SecretStore, load_config
from ..gitops import GitError, clone, git_env, write_askpass
from ..runrecord import MIRROR_DIR, write_record
from .run import EXIT_INFRA, KubeMirrorClient, MirrorReport, merge_mirror_records, mirror_record, run_mirror

log = logging.getLogger("rightsizing.mirror")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rightsizing.mirror",
                                description="GitOps mirror: add new Deployments to the manifest repo + ArgoCD, "
                                            "remove deleted ones (guarded). Never syncs, except the opt-in "
                                            "SHA-pinned first sync (mirror.auto_first_sync_namespaces).")
    p.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    p.add_argument("--workdir", default=None)
    p.add_argument("--dry-run", action="store_true", help="print the plan only; no commits, no ArgoCD changes")
    p.add_argument("--log-level", default="INFO")
    return p


def cli(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    now = datetime.now(timezone.utc)
    logs.setup(f"mirror-{now:%Y%m%dT%H%M%SZ}", args.log_level)
    store = SecretStore(cfg.secret_dir)
    if store.path("gitea_token").exists():
        store.read("gitea_token")
    logs.REDACT.add(*store.known_values())
    workdir = Path(args.workdir or tempfile.mkdtemp(prefix="mirror-"))
    (workdir / "home").mkdir(parents=True, exist_ok=True)
    token = store.path("gitea_token")
    env = git_env(write_askpass(workdir / "bin"), cfg.git.username, token if token.exists() else None,
                  workdir / "home")
    try:
        repo = clone(cfg.git.url, cfg.git.branch, workdir / "repo", env, cfg.timeouts.git)
    except GitError as exc:
        log.error("git clone failed", extra={"error": str(exc)})
        return EXIT_INFRA
    try:
        report = run_mirror(cfg, KubeMirrorClient(cfg), repo, now, dry_run=args.dry_run)
    except Exception:  # anything unexpected is an infrastructure failure, never a silent success
        log.exception("mirror run failed")
        return EXIT_INFRA
    log.info("summary", extra=report.summary())
    if args.dry_run:
        p = report.plan
        out = {"add": [f"{a[0]}/{a[1]}" for a in p.add_dir], "refresh": [f"{a[0]}/{a[1]}" for a in p.refresh_dir],
               "delete": [f"{a[0]}/{a[1]}" for a in p.delete_dir], "create_app": [f"{a[0]}/{a[1]}" for a in p.create_app],
               "delete_app": p.delete_app, "halts": p.halts, "manual": p.manual,
               "skipped": p.skipped}
        if p.adopt_dir or p.first_sync or p.sync_skipped:
            out.update(adopt={f"{a[0]}/{a[1]}": a[3] for a in p.adopt_dir},
                       first_sync={f"{a[0]}/{a[1]}": a[2] for a in p.first_sync}, sync_skipped=p.sync_skipped)
        print(json.dumps(out, ensure_ascii=False, indent=1))
    elif cfg.report is not None:
        write_mirror_record(cfg, report, now, workdir, token if token.exists() else None)
    return report.exit_code


def write_mirror_record(cfg: Config, report: MirrorReport, now: datetime, workdir: Path, token: Path | None) -> None:
    """Non-fatal: a missing record only shows a banner in the daily report; the exit code is unchanged."""
    try:
        write_record(cfg, mirror_record(cfg, report, now), workdir, token, prefix=MIRROR_DIR,
                     merge=merge_mirror_records)
    except Exception as exc:
        log.warning("mirror record not written", extra={"error": f"{type(exc).__name__}: {str(exc)[-200:]}"})


if __name__ == "__main__":
    sys.exit(cli())
