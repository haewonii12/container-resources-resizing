"""Configuration model (ConfigMap YAML) with bounds validation.

Secrets are never part of the config file: they are read from mounted files.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from ruamel.yaml import YAML

from . import DEFAULT_NAMESPACES, namespace_error

DEFAULT_CONFIG_PATH = "/etc/rightsizing/config.yaml"
DEFAULT_SECRET_DIR = "/var/run/secrets/rightsizing"


def normalize_repo(url: str) -> str:
    """Comparable form of a git URL (scheme, credentials, case, trailing slash and .git removed)."""
    url = url.strip().lower()
    url = re.sub(r"^[a-z+]+://", "", url)
    url = re.sub(r"^[^@/]+@", "", url)
    url = url.replace(":", "/", 1) if re.match(r"^[^/]+:[^0-9]", url) else url
    return url.removesuffix("/").removesuffix(".git")


def _check_namespaces(value: list[str], field: str) -> list[str]:
    if not value:
        raise ValueError(f"{field}: at least one namespace is required")
    errors = [e for e in (namespace_error(ns) for ns in value) if e]
    if errors:
        raise ValueError(f"{field}: " + "; ".join(errors))
    if len(set(value)) != len(value):
        raise ValueError(f"{field}: duplicate namespaces")
    return value


class NamespacePolicy(BaseModel):
    request_multiplier: float = Field(gt=0, le=5, description="CPU/memory request = P95 x this")
    mem_limit_multiplier: float = Field(gt=0, le=5, description="memory limit = max x this")
    max_shrink: float = Field(gt=0, lt=1, description="max fractional decrease per run")
    max_increase: float | None = Field(default=2.0, gt=1, description="max growth multiplier; null = unbounded")
    label: str = ""


class Timeouts(BaseModel):
    prom: float = Field(default=30, gt=0)
    llm_turn: float = Field(default=60, gt=0)
    llm_total: float = Field(default=300, gt=0)
    git: float = Field(default=120, gt=0)
    render: float = Field(default=120, gt=0)
    mcp_connect: float = Field(default=60, gt=0)
    run_budget: float = Field(default=5400, gt=0)

    def reserve(self, with_llm: bool = True, with_record: bool = False) -> float:
        """Worst case for one more workload plus the final push (push + two rebase/push retries),
        plus the run-record clone + push to the report repo when one is configured."""
        workload = 4 * self.render + (self.llm_total + self.llm_turn if with_llm else 0) + 12 * self.prom
        return workload + 6 * self.git + (2 * self.git if with_record else 0)


class PrometheusConfig(BaseModel):
    url: str = "http://PROMETHEUS_URL_PLACEHOLDER:9090"
    use_recording_rules: bool = True


class LLMConfig(BaseModel):
    base_url: str = "http://LLM_BASE_URL_PLACEHOLDER/v1"
    model: str = "MODEL_PLACEHOLDER"
    max_turns: int = Field(default=8, ge=1, le=30)


class GitConfig(BaseModel):
    url: str = "https://GITEA_HOST_PLACEHOLDER/org/manifests.git"
    branch: str = "main"
    username: str = "rightsizing-bot"
    author_name: str = "rightsizing-bot"
    author_email: str = "rightsizing-bot@example.invalid"
    push_enabled: bool = False
    head_is_branch: bool = True


class MCPConfig(BaseModel):
    url: str = "http://127.0.0.1:9080/mcp"


class ArgoCDConfig(BaseModel):
    namespace: str = "argocd"
    tracking_method: Literal["annotation", "label", "annotation+label"] = "annotation"
    instance_label: str = "app.kubernetes.io/instance"
    kustomize_build_options: list[str] = Field(default_factory=list)


class ReportConfig(BaseModel):
    """Daily report + rightsizing run record, both pushed to a separate repo (never the manifest repo)."""
    git_url: str = "https://GITEA_HOST_PLACEHOLDER/org/report.git"
    branch: str = "main"
    node_role_prefix: str = "node-role.kubernetes.io/"
    runs_enabled: bool = True  # rightsizing writes runs/YYYY/MM/<date>.json (independent of git.push_enabled)
    wait_for_record_minutes: int = Field(default=10, ge=0, le=60)
    timezone: str = "Asia/Seoul"

    @field_validator("timezone")
    @classmethod
    def _tz_exists(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value


class MirrorConfig(BaseModel):
    namespaces: list[str] = Field(default_factory=lambda: list(DEFAULT_NAMESPACES))
    argocd_project: str = "gitops-mirror"
    destination_server: str = "https://kubernetes.default.svc"
    app_name_template: str = "{name}"
    min_confirmations: int = Field(default=2, ge=2)
    min_missing_interval_hours: float = Field(default=12, ge=1)
    max_deletes_per_run: int = Field(default=3, ge=0)
    max_delete_ratio: float = Field(default=0.3, gt=0, le=1)
    min_apps_for_ratio: int = Field(default=5, ge=1)
    skip_helm: bool = True
    managed_label_key: str = "rightsizing/managed-by"
    managed_label_value: str = "gitops-mirror"
    k8s_timeout: float = Field(default=30, gt=0)
    # opt-in, per namespace (subsets of `namespaces`): the mirror starts the first sync of its never-synced apps
    # (pinned to the SHA it pushed, only when git == live) / commits live drift of synced apps into git
    auto_first_sync_namespaces: list[str] = Field(default_factory=list)
    adopt_drift_namespaces: list[str] = Field(default_factory=list)
    adopt_ignore_annotation_prefixes: list[str] = Field(default_factory=list)  # written by other controllers

    @field_validator("namespaces")
    @classmethod
    def _mirror_ns_allowed(cls, value: list[str]) -> list[str]:
        return _check_namespaces(value, "mirror.namespaces")


class ManualMapping(BaseModel):
    workload: str = Field(description="<namespace>/<Kind>/<name>")
    format: Literal["plain", "helm", "kustomize"] | None = None
    values_file: str | None = None
    values_key: str | None = Field(default=None, description="dotted path to the resources block, per container")
    container: str | None = None


class Config(BaseModel):
    namespaces: list[str] = Field(default_factory=lambda: list(DEFAULT_NAMESPACES))
    policies: dict[str, NamespacePolicy] = Field(default_factory=lambda: {
        "inference": NamespacePolicy(request_multiplier=1.3, mem_limit_multiplier=1.5, max_shrink=0.25,
                                     label="inference (conservative)"),
        "development": NamespacePolicy(request_multiplier=1.1, mem_limit_multiplier=1.2, max_shrink=0.50,
                                       label="development (aggressive)"),
    })
    window_days: int = Field(default=7, ge=1, le=60)
    min_days: int = Field(default=3, ge=0)  # 0 = no minimum (testing only: recommendations from sparse data)
    min_change: float = Field(default=0.10, ge=0, lt=1)
    throttle_threshold: float = Field(default=0.05, ge=0, le=1)
    suppress_days: int = Field(default=14, ge=0)
    cpu_limit_mode: Literal["keep", "adjust"] = "keep"
    prometheus: PrometheusConfig = Field(default_factory=PrometheusConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    git: GitConfig = Field(default_factory=GitConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    argocd: ArgoCDConfig = Field(default_factory=ArgoCDConfig)
    timeouts: Timeouts = Field(default_factory=Timeouts)
    manual_mappings: list[ManualMapping] = Field(default_factory=list)
    mirror: MirrorConfig = Field(default_factory=MirrorConfig)
    report: ReportConfig | None = None
    secret_dir: str = DEFAULT_SECRET_DIR

    @field_validator("namespaces")
    @classmethod
    def _namespaces_allowed(cls, value: list[str]) -> list[str]:
        return _check_namespaces(value, "namespaces")

    @model_validator(mode="after")
    def _cross_checks(self) -> "Config":
        if self.min_days > self.window_days:
            raise ValueError(f"min_days ({self.min_days}) must be <= window_days ({self.window_days})")
        if self.report is not None and normalize_repo(self.report.git_url) == normalize_repo(self.git.url):
            raise ValueError("report.git_url must not be the manifest repo (git.url)")
        if self.timeouts.run_budget <= self.timeouts.reserve(with_record=self.records_enabled):
            raise ValueError(f"timeouts.run_budget ({self.timeouts.run_budget}) must exceed the per-workload+push "
                             f"reserve ({self.timeouts.reserve():.0f}s); otherwise every workload is skipped:budget")
        missing = [ns for ns in self.namespaces if ns not in self.policies]
        if missing:
            raise ValueError(f"no policy defined for namespaces: {missing}")
        if "namespaces" not in self.mirror.model_fields_set:  # unset: the mirror covers the same namespaces
            self.mirror.namespaces = list(self.namespaces)
        outside = sorted(set(self.mirror.namespaces) - set(self.namespaces))
        if outside:  # the mirror registers apps for rightsizing: it must not reach beyond `namespaces`
            raise ValueError(f"mirror.namespaces must be a subset of namespaces: {outside}")
        for fname in ("auto_first_sync_namespaces", "adopt_drift_namespaces"):
            unknown = sorted(set(getattr(self.mirror, fname)) - set(self.mirror.namespaces))
            if unknown:
                raise ValueError(f"mirror.{fname} must be a subset of mirror.namespaces: {unknown}")
        return self

    @property
    def records_enabled(self) -> bool:
        return self.report is not None and self.report.runs_enabled

    def policy_for(self, namespace: str) -> NamespacePolicy:
        return self.policies[namespace]

    def mapping_for(self, workload_key: str) -> list[ManualMapping]:
        return [m for m in self.manual_mappings if m.workload == workload_key]


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
    yaml = YAML(typ="safe")
    with open(path, encoding="utf-8") as handle:
        data = yaml.load(handle) or {}
    return Config.model_validate(data)


class SecretStore:
    """Reads secrets from mounted files; remembers values so logs can redact them."""

    def __init__(self, secret_dir: str | Path):
        self.dir = Path(secret_dir)
        self._loaded: dict[str, str] = {}

    def path(self, name: str) -> Path:
        return self.dir / name

    def read(self, name: str) -> str:
        if name not in self._loaded:
            value = self.path(name).read_text(encoding="utf-8").strip()
            if not value:
                raise ValueError(f"secret file {self.path(name)} is empty")
            self._loaded[name] = value
        return self._loaded[name]

    def known_values(self) -> list[str]:
        return [v for v in self._loaded.values() if v]
