"""CLI: python -m rightsizing.report [--config PATH] [--workdir DIR] [--dry-run]"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .. import logs
from ..config import DEFAULT_CONFIG_PATH, SecretStore, load_config
from ..discovery import KubeClient
from ..gitops import GitError, clone, git_env, write_askpass
from ..prom import PromClient
from .nodes import Res, parse_res
from .run import EXIT_INFRA, run_report

log = logging.getLogger("rightsizing.report")


class ReportKubeClient(KubeClient):
    """Read-only lists for the report (daily-report SA: namespaced reads + cluster-wide nodes/pods/metrics)."""

    def list_deployments(self, namespace: str) -> list[dict]:
        return self._dicts(self.apps.list_namespaced_deployment(namespace).items, "Deployment")

    def list_nodes(self) -> list[dict]:
        return self._dicts(self.core.list_node().items, "Node")

    def list_pods(self) -> list[dict]:
        return self._dicts(self.core.list_pod_for_all_namespaces().items, "Pod")

    def node_usage(self) -> dict[str, Res]:
        items = self.custom.list_cluster_custom_object("metrics.k8s.io", "v1beta1", "nodes").get("items") or []
        return {i["metadata"]["name"]: parse_res(i.get("usage")) for i in items}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rightsizing.report",
                                description="Daily report: rightsizing proposals, node capacity, N-1 failover.")
    p.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    p.add_argument("--workdir", default=None)
    p.add_argument("--dry-run", action="store_true", help="print the report; no commit, no push")
    p.add_argument("--log-level", default="INFO")
    return p


def cli(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    now = datetime.now(timezone.utc)
    logs.setup(f"report-{now:%Y%m%dT%H%M%SZ}", args.log_level)
    if cfg.report is None:
        log.error("config has no `report` section")
        return EXIT_INFRA
    store = SecretStore(cfg.secret_dir)
    token = store.path("gitea_token")
    if token.exists():
        store.read("gitea_token")
    logs.REDACT.add(*store.known_values())
    workdir = Path(args.workdir or tempfile.mkdtemp(prefix="report-"))
    (workdir / "home").mkdir(parents=True, exist_ok=True)
    env = git_env(write_askpass(workdir / "bin"), cfg.git.username, token if token.exists() else None,
                  workdir / "home")
    try:
        repo = clone(cfg.report.git_url, cfg.report.branch, workdir / "repo", env, cfg.timeouts.git)
        result = run_report(cfg, ReportKubeClient(cfg), PromClient(cfg.prometheus.url, cfg.timeouts.prom), repo, now,
                            dry_run=args.dry_run)
    except GitError as exc:
        log.error("report repo failed", extra={"error": str(exc)[-300:]})
        return EXIT_INFRA
    if args.dry_run:
        print(result.markdown)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(cli())
