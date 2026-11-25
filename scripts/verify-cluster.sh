#!/usr/bin/env bash
# Plan Step 12: cluster verification. Run from a machine with cluster-admin-ish kubectl access AFTER
# `kubectl apply -k deploy/` (or an ArgoCD sync) and after creating the rightsizing-secrets Secret.
# Read-only except: creates short-lived Jobs in the `rightsizing` namespace (deleted at the end).
set -euo pipefail

NS=rightsizing
SA="system:serviceaccount:${NS}:rightsizing-reader"
PASS=0; FAIL=0
ok()   { echo "  PASS  $*"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL  $*"; FAIL=$((FAIL+1)); }
step() { echo; echo "== $*"; }

job_from_cronjob() {  # $1 job name, $2 python expression that edits the pod spec `p`
  kubectl -n "$NS" get cronjob rightsizing -o json | python3 -c '
import json, sys
cj = json.load(sys.stdin); name, edit = sys.argv[1], sys.argv[2]
job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "namespace": "'"$NS"'",
       "labels": {"rightsizing/verify": "true"}}, "spec": cj["spec"]["jobTemplate"]["spec"]}
p = job["spec"]["template"]["spec"]; exec(edit); print(json.dumps(job))' "$1" "$2" | kubectl apply -f - >/dev/null
}

wait_job() {  # $1 job name, $2 timeout seconds -> prints Complete|Failed|Timeout
  local end=$((SECONDS+$2)) s
  while (( SECONDS < end )); do
    s=$(kubectl -n "$NS" get job "$1" -o jsonpath='{range .status.conditions[*]}{.type}={.status} {end}' 2>/dev/null || true)
    [[ "$s" == *"Complete=True"* ]] && { echo Complete; return; }
    [[ "$s" == *"Failed=True"* ]] && { echo Failed; return; }
    sleep 5
  done
  echo Timeout
}

cleanup() { kubectl -n "$NS" delete job -l rightsizing/verify=true --ignore-not-found >/dev/null 2>&1 || true; }
trap cleanup EXIT

step "12.1 RBAC is read-only"
for ns in inference development; do
  for verb in create update patch delete; do
    for res in deployments statefulsets pods; do
      if [[ "$(kubectl auth can-i "$verb" "$res" -n "$ns" --as="$SA" 2>/dev/null)" == "no" ]]; then :; else bad "$verb $res in $ns allowed"; fi
    done
  done
  [[ "$(kubectl auth can-i get deployments -n "$ns" --as="$SA")" == "yes" ]] && ok "get deployments in $ns" || bad "cannot read $ns"
done
[[ "$(kubectl auth can-i get secrets -n "$NS" --as="$SA")" == "no" ]] && ok "cannot read secrets" || bad "can read secrets"
[[ "$(kubectl auth can-i patch applications.argoproj.io -n argocd --as="$SA")" == "no" ]] && ok "cannot patch Applications" || bad "can patch Applications"
[[ "$(kubectl auth can-i list applications.argoproj.io -n argocd --as="$SA")" == "yes" ]] && ok "can list Applications" || bad "cannot list Applications"
(( FAIL == 0 )) && ok "no write verbs for the SA in target namespaces"

step "12.2 sidecar isolation (kubectl-ai container cannot see the agent's secrets or processes)"
job_from_cronjob rs-verify-iso 'p["containers"][0]["command"] = ["python", "-c", "import time; time.sleep(240)"]; p["containers"][0]["args"] = []'
kubectl -n "$NS" wait --for=condition=Ready pod -l job-name=rs-verify-iso --timeout=180s >/dev/null
POD=$(kubectl -n "$NS" get pod -l job-name=rs-verify-iso -o jsonpath='{.items[0].metadata.name}')
if kubectl -n "$NS" exec "$POD" -c kubectl-ai -- sh -c 'ls /var/run/secrets/rightsizing' >/dev/null 2>&1; then
  bad "secret dir visible in sidecar"; else ok "secret dir not mounted in sidecar"; fi
if kubectl -n "$NS" exec "$POD" -c kubectl-ai -- sh -c 'cat /proc/*/environ 2>/dev/null | tr "\0" "\n" | grep -qiE "gitea|llm_api"'; then
  bad "agent env visible from sidecar"; else ok "agent environment not visible from sidecar"; fi
if kubectl -n "$NS" exec "$POD" -c kubectl-ai -- sh -c 'kubectl auth can-i patch deployments -n development' 2>/dev/null | grep -q yes; then
  bad "sidecar kubectl can patch"; else ok "sidecar kubectl is read-only"; fi
kubectl -n "$NS" delete job rs-verify-iso --wait=false >/dev/null

step "12.3-12.4 dry-run Job: MCP tools listed, no commit, Job completes (native sidecar terminates)"
job_from_cronjob rs-verify-dry 'p["containers"][0]["args"] = p["containers"][0]["args"] + ["--dry-run"]'
RESULT=$(wait_job rs-verify-dry 1200)
LOGS=$(kubectl -n "$NS" logs job/rs-verify-dry -c agent 2>/dev/null || true)
[[ "$RESULT" == "Complete" ]] && ok "dry-run Job Complete" || bad "dry-run Job $RESULT (sidecar hang? check exit code)"
echo "$LOGS" | grep -q '"msg": "mcp connected"' && echo "$LOGS" | grep '"mcp connected"' | grep -q '"kubectl"' \
  && ok "MCP tools/list includes kubectl (S1)" || bad "no 'mcp connected' log with kubectl tool"
echo "$LOGS" | grep '"msg": "summary"' | tail -1 | python3 -c '
import json, sys
s = json.loads(sys.stdin.read() or "{}")
print("  INFO  summary:", json.dumps({k: s.get(k) for k in ("total", "counts", "reasons", "coverage", "exit_code")}, ensure_ascii=False))' || true

step "Manual checks (plan Step 12.5-12.7) - do these by hand"
cat <<'EOT'
  [ ] Coverage gate (DV7): run the dry-run daily for >=3 days, agree on coverage, then set git.push_enabled: true
  [ ] Real run on ONE development workload:  --only development/Deployment/<name>
      -> commit with Rightsizing-* trailers exists in Gitea
      -> ArgoCD app shows OutOfSync and live resources are unchanged
      -> manual Sync -> live resources updated
      -> next run: no new commit for that workload (pending cleared, no diff)
  [ ] git revert that commit -> next run does not re-propose the same direction (suppressed_by_revert)
  [ ] Repeat for one inference workload
EOT

echo; echo "Result: ${PASS} passed, ${FAIL} failed"
(( FAIL == 0 ))
