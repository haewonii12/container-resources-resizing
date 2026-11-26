import pytest
from pydantic import ValidationError

from rightsizing import quantity as q
from rightsizing.config import Config, NamespacePolicy, SecretStore


@pytest.mark.parametrize("raw,expected", [("1", 1000), ("1000m", 1000), ("0.5", 500), ("250m", 250), (2, 2000),
                                          ("1.5", 1500)])
def test_parse_cpu(raw, expected):
    assert q.parse_cpu(raw) == expected


@pytest.mark.parametrize("raw,expected", [("1Gi", 1024 ** 3), ("1024Mi", 1024 ** 3), ("500M", 5e8),
                                          ("128974848", 128974848), ("1G", 1e9), ("512Ki", 512 * 1024)])
def test_parse_mem(raw, expected):
    assert q.parse_mem(raw) == expected


def test_format_and_round():
    assert q.format_cpu(1400) == "1400m" and q.format_cpu(2000) == "2"
    assert q.format_mem(5 * q.GI) == "5Gi" and q.format_mem(5120 * q.MI + 16 * q.MI) == "5136Mi"
    assert q.round_cpu_up(1401) == 1410 and q.round_cpu_up(1400) == 1400
    assert q.round_mem_up(q.MI) == 16 * q.MI and q.round_mem_up(32 * q.MI) == 32 * q.MI


def test_human_cpu_small_values():
    assert q.human("cpu", 2) == "2m" and q.human("cpu", 1080) == "1.08" and q.human("cpu", 2000) == "2"


def test_invalid_quantity():
    with pytest.raises(q.QuantityError):
        q.parse_cpu("1 core")


def test_config_defaults_valid():
    cfg = Config()
    assert cfg.policies["inference"].request_multiplier == 1.3
    assert cfg.policies["development"].max_shrink == 0.5
    assert cfg.git.push_enabled is False


@pytest.mark.parametrize("override", [
    {"namespaces": ["inference", "kube-system"]},
    {"namespaces": []},
    {"min_days": 10, "window_days": 7},
    {"policies": {"inference": {"request_multiplier": 0, "mem_limit_multiplier": 1.5, "max_shrink": 0.25},
                  "development": {"request_multiplier": 1.1, "mem_limit_multiplier": 1.2, "max_shrink": 0.5}}},
    {"policies": {"inference": {"request_multiplier": 6, "mem_limit_multiplier": 1.5, "max_shrink": 0.25},
                  "development": {"request_multiplier": 1.1, "mem_limit_multiplier": 1.2, "max_shrink": 0.5}}},
    {"policies": {"inference": {"request_multiplier": 1.3, "mem_limit_multiplier": 1.5, "max_shrink": 1.0},
                  "development": {"request_multiplier": 1.1, "mem_limit_multiplier": 1.2, "max_shrink": 0.5}}},
    {"namespaces": ["inference"], "policies": {}},
    {"timeouts": {"run_budget": 600}},
    {"namespaces": ["argocd"], "policies": {"argocd": {"request_multiplier": 1.1, "mem_limit_multiplier": 1.2, "max_shrink": 0.5}}},
    {"namespaces": ["rightsizing"], "policies": {"rightsizing": {"request_multiplier": 1.1, "mem_limit_multiplier": 1.2, "max_shrink": 0.5}}},
    {"namespaces": ["Bad_NS"], "policies": {"Bad_NS": {"request_multiplier": 1.1, "mem_limit_multiplier": 1.2, "max_shrink": 0.5}}},
    {"namespaces": ['x"} or vector(1) #'], "policies": {}},
    {"namespaces": ["inference", "inference"]},
    {"namespaces": ["inference"], "mirror": {"namespaces": ["development"]}},
])
def test_config_rejects(override):
    with pytest.raises(ValidationError):
        Config.model_validate(override)


def test_policy_bounds():
    with pytest.raises(ValidationError):
        NamespacePolicy(request_multiplier=1.1, mem_limit_multiplier=1.2, max_shrink=0)


def test_secret_store_reads_files(tmp_path):
    (tmp_path / "gitea_token").write_text("s3cr3t\n")
    store = SecretStore(tmp_path)
    assert store.read("gitea_token") == "s3cr3t"
    assert store.known_values() == ["s3cr3t"]
    with pytest.raises(FileNotFoundError):
        store.read("llm_api_key")


def test_config_accepts_any_namespace_with_a_policy():
    pol = {"request_multiplier": 1.3, "mem_limit_multiplier": 1.5, "max_shrink": 0.25}
    cfg = Config.model_validate({"namespaces": ["default", "dify"], "policies": {"default": pol, "dify": pol},
                                 "mirror": {"namespaces": ["default"]}})
    assert cfg.namespaces == ["default", "dify"] and cfg.mirror.namespaces == ["default"]
    assert cfg.policy_for("dify").max_shrink == 0.25
