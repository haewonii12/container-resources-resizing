# container-resources-resizing

Kubernetes 컨테이너의 CPU/메모리 request·limit을 **실제 사용량 기준으로 제안하고, 사람이 승인해야만 반영되는** GitOps 도구입니다. 두 구성요소로 이뤄져 있습니다.

| 구성요소 | 실행 | 역할 |
|---|---|---|
| **rightsizing** (`python -m rightsizing`) | 매일 03:00 CronJob | Prometheus 7일 사용량을 기반으로 namespace별 정책에 따라 새 request/limit을 계산합니다. manifest repo에 **workload당 commit 1개**를 push하고, commit message에 근거를 남깁니다 |
| **gitops-mirror** (`python -m rightsizing.mirror`) | 매일 02:30 CronJob | 클러스터에 새로 생긴 Deployment를 manifest repo와 ArgoCD(수동 sync)에 등록하고, 사라진 것은 안전장치를 거쳐 정리합니다 |

```
 Prometheus ──▶ rightsizing ──commit/push──▶ Gitea manifest repo ◀──add/delete── gitops-mirror ◀── 클러스터 Deployment 목록
                     │                              │
            kubectl-ai MCP (read-only)       ArgoCD (수동 sync = 승인, git revert = 거절)
                                                    │
                                                    ▼
                                         inference / development workload
```

- 클러스터에 실제로 반영되는 것은 **사람이 ArgoCD에서 Sync할 때뿐**입니다. 자동 sync는 어디에도 없습니다.
- agent의 클러스터 권한은 읽기 전용입니다. 바뀌는 경로는 git commit 하나뿐입니다.
- LLM(OpenAI 호환, 선택)은 kubectl-ai로 상황을 살펴보고 **적용/보류와 메모만** 반환합니다. 값은 정책 코드가 계산합니다.

| 할 일 | 방법 |
|---|---|
| 승인 | ArgoCD에서 OutOfSync diff와 commit message를 확인하고 **Sync**합니다 |
| 거절 | `git revert <commit>` 후 push합니다. 같은 workload, 같은 필드, 같은 방향의 제안은 `suppress_days`(기본 14일) 동안 다시 나오지 않습니다 |
| 이력 확인 | `git log --grep 'Rightsizing-Workload: inference/'` |

## 디렉터리 구조
```
rightsizing/            agent 코드 (policy, prom, discovery, manifest 편집/렌더링 검증, gitops, LLM 검토 루프)
rightsizing/mirror/     GitOps 미러 (clean, scan, plan, run)
deploy/                 Kustomize 배포 manifest (CronJob, RBAC, NetworkPolicy, ConfigMap, alerts, mirror/)
deploy-legacy-sidecar/  k8s 1.28 이하용 overlay
examples/gitops/        manifest repo 구조 예시와 ArgoCD Application 예시
scripts/                알림 규칙 생성, 클러스터 검증, sidecar wrapper
tests/                  단위/통합 테스트 (fake Prometheus/LLM/kubectl-ai/k8s, 실제 git, helm, kustomize, promtool)
docs/                   kubectl-ai 검증 기록, 설계 문서(docs/design: 스펙과 합의된 계획)
```

## 빠른 시작
```bash
# 테스트
uv venv -p 3.12 .venv && uv pip install -p .venv/bin/python -e '.[test]'
.venv/bin/python -m pytest -q

# 이미지 (linux/amd64)
docker buildx build --platform linux/amd64 -t <registry>/rightsizing-agent:<tag> -f Dockerfile .
docker buildx build --platform linux/amd64 -t <registry>/rightsizing-kubectl-ai:v0.0.31 -f Dockerfile.kubectl-ai .

# 배포: placeholder를 채운 뒤 (아래 "설치" 참고)
kubectl -n rightsizing create secret generic rightsizing-secrets \
  --from-literal=gitea_token='<bot token>' --from-literal=llm_api_key='<key 또는 unused>'
kubectl apply -k deploy/
kubectl apply -f deploy/mirror/pod-init.yaml   # 기존 Deployment를 처음 한 번 등록합니다
```

설계 문서: [docs/design/spec-rightsizing.md](docs/design/spec-rightsizing.md), [docs/design/plan-rightsizing.md](docs/design/plan-rightsizing.md), [docs/design/spec-gitops-mirror.md](docs/design/spec-gitops-mirror.md), [docs/design/plan-gitops-mirror.md](docs/design/plan-gitops-mirror.md)

## 동작 방식

```
CronJob pod (ns rightsizing, SA rightsizing-reader: read-only)
 ├─ agent (UID 1000, secret 파일 마운트)
 │    discovery → Prometheus 7일 사용량 → 정책 계산(결정적) → LLM 검토(apply|hold만)
 │    → manifest 수정 → 렌더링으로 diff 검증 → commit → push
 └─ kubectl-ai MCP 네이티브 sidecar (UID 1001, secret 없음, localhost:9080)
```

- **값은 코드가 계산합니다.** LLM(OpenAI 호환 사내 엔드포인트)은 kubectl-ai의 `kubectl` tool(허용 목록 기반 구조화 인자)과 Prometheus 조회로 상황을 살펴본 뒤 **적용/보류와 메모만** 반환합니다. LLM이 응답하지 않거나, 시간이 초과되거나, 결정을 내리지 않으면 **보류**합니다.
- **bash tool은 LLM에 절대 넘기지 않습니다.** kubectl-ai는 명령을 shell로 실행하므로, agent가 argv를 직접 조립하고 인자마다 `shlex.quote`를 적용합니다. 자세한 내용은 `docs/kubectl-ai.md`에 있습니다.
- **렌더링 검증:** 수정 전후로 `helm template`/`kustomize build`를 돌려, 대상 컨테이너의 `resources` 외에 바뀐 것이 있으면 commit하지 않습니다.

### 정책 (ConfigMap에서 변경 가능)

| | inference (보수적) | development (공격적) |
|---|---|---|
| CPU/메모리 request | P95 × 1.3 | P95 × 1.1 |
| 메모리 limit | max × 1.5 | max × 1.2 |
| 1회 최대 축소 / 증가 | 25% / ×2 | 50% / ×2 |

두 namespace에 공통으로 적용되는 규칙:
- 변화가 10% 미만이면 무시합니다.
- 데이터가 3일치 미만이면 건너뜁니다.
- 축소하지 않는 경우: OOM이 있었거나 OOM 여부를 알 수 없으면 메모리를, throttling이 있었거나 throttling 여부를 알 수 없으면 CPU를 줄이지 않습니다.
- CPU limit은 유지하고, GPU는 건드리지 않습니다.
- 원래 없던 limit은 새로 추가하지 않습니다.
- QoS 등급을 유지하고, LimitRange를 준수합니다.

## 설치

1. **이미지를 빌드하고 push합니다.** 모든 바이너리는 checksum으로 고정되어 있습니다.
   ```bash
   docker build -t $REGISTRY/rightsizing-agent:0.1.0 -f Dockerfile .
   docker build -t $REGISTRY/rightsizing-kubectl-ai:v0.0.31 -f Dockerfile.kubectl-ai .
   ```
2. **Gitea bot 계정을 준비합니다.** `rightsizing-bot` 사용자에게 **manifest repo 하나에만** write 권한을 줍니다. 이 `deploy/` 디렉터리는 bot이 쓸 수 없는 다른 repo나 보호된 경로에 두세요.
3. **Secret을 만듭니다.** git에는 절대 넣지 않습니다.
   ```bash
   kubectl create namespace rightsizing
   kubectl -n rightsizing create secret generic rightsizing-secrets \
     --from-literal=gitea_token='<bot token>' --from-literal=llm_api_key='<llm key>'
   ```
4. **placeholder를 채웁니다.** `grep -rn PLACEHOLDER deploy/`로 찾을 수 있습니다.
   - `deploy/configmap.yaml`: Prometheus URL, LLM base_url과 model, Gitea repo URL
   - `deploy/kustomization.yaml`: 이미지 registry, 실행 주기(schedule)
   - `deploy/networkpolicy.yaml`: apiserver, Gitea, LLM, chart repo의 CIDR, Prometheus namespace
5. **ArgoCD로 배포합니다.** `deploy/`를 가리키는 Application을 **auto-sync 없이** 만들고 수동으로 sync합니다.
6. **알림 규칙을 설치합니다.**
   - prometheus-operator를 쓰면 `kubectl apply -f deploy/alerts/prometheusrule.yaml`을 실행합니다. `release` label을 맞추세요.
   - prometheus-operator가 없으면 `deploy/alerts/rules.yaml`을 Prometheus `rule_files`에 추가합니다.
   - recording rule이 7일치 데이터를 모으기 전까지 agent는 자동으로 인라인 쿼리를 씁니다.
7. **클러스터에서 검증합니다.** `scripts/verify-cluster.sh`로 RBAC, sidecar 격리, dry-run, Job 완료를 확인합니다.

### 전제조건
- **조정 대상 ArgoCD Application은 auto-sync가 꺼져 있어야 합니다.** 켜져 있으면 해당 workload는 `skipped:auto_sync_enabled`로 건너뜁니다.
- 기본 `deploy/`는 Kubernetes 1.29 이상이 필요합니다(네이티브 sidecar). **1.28 이하**에서는 `deploy-legacy-sidecar/` overlay를 쓰세요. 이 overlay에서는 agent가 종료할 때 공유 emptyDir에 `/shared/done`을 쓰고, `scripts/sidecar-wrapper.sh`가 그 파일을 보고 kubectl-ai를 끝내서 Job이 정상 완료됩니다.
- kube-state-metrics와 cAdvisor 메트릭이 있어야 합니다. 없으면 상태를 "unknown"으로 보고 값을 줄이지 않습니다.

## Coverage gate (DV7) — push를 켜기 전에 반드시 거치는 단계

`git.push_enabled: false`가 기본값입니다. 이 상태에서는 commit을 로컬에만 만들고 로그에 출력하며 push는 하지 않습니다. 최소 3일 동안 실행한 뒤 Job 로그의 `summary` 줄을 확인하세요.

```json
{"msg": "summary", "total": 40, "counts": {"committed": 9, "skipped": 25, "held": 2, "no_change": 4},
 "reasons": {"skipped:helm_ambiguous": 6, "skipped:drift": 3, ...}, "coverage": 0.275, ...}
```

skip 사유를 보고 필요한 곳에 `manual_mappings`를 추가하세요. 적용 범위(coverage)에 동의하면 `push_enabled: true`로 바꿉니다. 권장 기준은 development에서 50% 이상입니다.

## 결과 상태와 skip 사유

| 상태 | 의미 |
|---|---|
| `committed` / `proposed` | commit을 만들었습니다. `proposed`는 dry-run일 때입니다 |
| `held:llm_hold` / `held:llm_unavailable` / `held:llm_no_decision` | LLM이 보류했거나, 응답 실패 또는 시간 초과, 결정 없음으로 보류했습니다 |
| `held:security` | 금지된 tool 호출(bash, `--server` 등)로 보류했습니다. **Job이 exit 3으로 끝나고 알림이 갑니다** |
| `superseded` / `withdrawn` / `no_change:pending_same` / `no_change:pending_endorsed` | 승인 대기 중인 제안을 교체했거나 철회했거나 그대로 유지한 경우입니다. 대기 기간이 오래되면 `stale_pending`이 붙습니다. 아래 "GitOps mirror" 섹션의 rightsizing 부분을 참고하세요 |
| `skipped:drift` | git(렌더링 결과)과 live workload template이 다릅니다. 수동 변경이나 webhook 때문일 수 있습니다 |
| `skipped:helm_no_match` / `helm_ambiguous` / `helm_inline_values` | Helm values에서 수정할 블록을 하나로 특정하지 못했습니다. `manual_mappings`로 지정하세요 |
| `skipped:render_mismatch` / `render_failed` / `render_nondeterministic` | 렌더링 검증을 통과하지 못했습니다 |
| `skipped:auto_sync_enabled` / `multi_source` / `target_revision_mismatch` / `foreign_repo` / `unsupported_ignore_differences` / `unsupported_source` | 지원하지 않는 Application입니다(DV5) |
| `no_change` | 바꿀 것이 없습니다. detail에 이유가 나옵니다: `below_min_change`, `insufficient_data`, `oom_recent`, `hpa_utilization`, `suppressed_by_revert` 등 |

Exit code는 세 가지입니다.
- `0`: 정상
- `2`: 인프라 문제(Prometheus, MCP sidecar, git clone/push)
- `3`: 보안 이벤트(다른 workload의 commit은 push된 뒤 실패로 끝납니다)

## GitOps mirror (클러스터 → Gitea/ArgoCD 앱 등록·정리)

`python -m rightsizing.mirror`가 매일 02:30에 실행됩니다(`deploy/mirror/cronjob.yaml`, rightsizing보다 먼저). 대상 namespace에서 **앱이 존재하는지만** 확인해 Gitea `resource` repo와 ArgoCD를 맞춥니다.

| 상황 | 미러가 하는 일 |
|---|---|
| 클러스터에 새 Deployment가 있는데 git에 `<ns>/<name>/`이 없음 | 정리한 manifest를 `<ns>/<name>/deployment.yaml`로 commit하고(`Mirror-Action: add`), **수동 sync** Application을 만듭니다(AppProject `gitops-mirror`) |
| git에 있는 앱의 Deployment가 클러스터에서 사라짐 | 안전장치를 통과하면 디렉터리를 삭제하는 commit을 올리고(`Mirror-Action: delete`), Application을 **non-cascade**로 지웁니다 |
| 이미 git에 있는 앱 | **내용을 건드리지 않습니다.** 예외는 미러가 만들었고 아직 한 번도 Sync되지 않은 앱 하나뿐입니다. 이 앱은 클러스터 변경을 따라 갱신하지만, 사람이 git을 직접 고쳤다면 갱신하지 않습니다 |
| helm으로 관리되거나 ownerReferences가 있거나 다른 ArgoCD 앱이 이미 관리하는 Deployment | 건너뜁니다(`helm_managed` / `owned` / `already_tracked`) |

**Sync는 첫 번째를 포함해 모두 사람이 합니다.** 미러가 등록한 앱은 ArgoCD에 OutOfSync로 표시됩니다. ArgoCD에서 diff를 확인하고 첫 Sync를 하면 추적 label이 붙고, 그다음부터 rightsizing의 대상이 됩니다. 첫 Sync는 내용이 같아서 label만 붙고 pod는 재시작되지 않습니다.

### 삭제 안전장치
- 앱이 **연속 2회 이상** 성공한 실행에서 사라진 상태로 확인되고, 처음 발견한 뒤 **12시간 이상** 지나야 지웁니다. 수동 init pod로 바로 다시 돌려도 삭제가 앞당겨지지 않습니다.
- namespace 목록 조회가 실패하면 그 namespace의 카운트를 처음부터 다시 셉니다(exit 2).
- **대량 삭제 방지:** 한 번에 지울 앱이 3개를 넘거나, 비율이 30%를 넘거나(앱이 5개 이상인 namespace만 적용), namespace가 통째로 비어 있으면 삭제를 멈추고 exit 4로 끝냅니다(DV-D).
- **Sync된 적이 없거나 실제로 떠 있던 적이 없는 앱은 지우지 않습니다**(`never_deployed`, DV-F). 이런 앱은 사람이 정리합니다.
- **resources finalizer가 붙은 Application은 지우지 않습니다.** 지우면 cascade로 workload까지 삭제됩니다. 이 경우 exit 4와 알림으로 알리니 사람이 지우세요(DV-A).
- 삭제할 때는 uid/resourceVersion precondition을 붙이고 `propagationPolicy=Orphan`을 씁니다. 클러스터 리소스는 건드리지 않습니다.
- 앱 디렉터리를 지우면 그 안의 **모든 파일**이 함께 지워집니다(예: sample-web의 Service 정의).
- 상태는 repo의 `.mirror/state.yaml` 하나에 남습니다(`seen` / `missing` / `exports`). 이 파일은 값이 바뀔 때만 commit됩니다.

### 권한과 격리
- SA `gitops-mirror`의 권한은 다음뿐입니다. update/patch와 Secret 접근 권한은 없습니다.
  - 대상 namespace: Deployment와 HPA의 get/list
  - argocd: Application의 get/list/create/delete
- 전용 AppProject `gitops-mirror`가 sourceRepos는 이 repo만, destination은 두 namespace만, 리소스는 Deployment만 허용합니다(DV-C).
- ValidatingAdmissionPolicy가 미러 SA가 만드는 Application을 검사합니다. project가 다르거나, auto-sync가 켜져 있거나, `operation`이나 finalizer가 있거나, multi-source인 Application은 거부합니다.
- NetworkPolicy는 ingress를 모두 막고, egress는 DNS, apiserver, Gitea만 허용합니다.

### 실행과 종료 코드
```bash
kubectl apply -f deploy/mirror/pod-init.yaml          # 처음 한 번 수동 실행
kubectl -n rightsizing logs -f gitops-mirror-init      # 마지막 줄 "summary": added / deleted_dirs / apps / halts / skipped
kubectl -n rightsizing delete pod gitops-mirror-init   # 다시 실행하기 전에 지웁니다
python -m rightsizing.mirror --config ... --dry-run    # 계획만 출력합니다 (commit도, ArgoCD 변경도 없음)
```
| exit | 의미 | 알림 |
|---|---|---|
| 0 | 정상 | — |
| 2 | 인프라 문제: 목록 조회, git, ArgoCD API 오류. 다음 실행에서 자동으로 다시 맞춥니다 | `MirrorJobFailed` |
| 4 | 안전 정지: 대량 삭제 방지, finalizer가 붙은 앱, Application 이름 충돌 | `MirrorJobFailed` |

배포하기 전에 `deploy/mirror/appproject.yaml`의 `MANIFEST_REPO_URL_PLACEHOLDER`를 configmap의 `git.url`과 같은 값으로 바꾸세요.

### 잘못 지워졌을 때 복구
1. Gitea에서 해당 `Mirror-Action: delete` commit을 `git revert`합니다. 그러면 디렉터리가 돌아옵니다.
2. Deployment를 다시 만들거나, ArgoCD Application을 다시 만들어 Sync합니다. Deployment가 없는 상태로 두면 미러가 다시 "missing"으로 세기 시작합니다.

### rightsizing: 승인 대기 중인 제안 처리
승인을 기다리는(pending) 제안이 있어도 rightsizing은 매일 다시 계산합니다.
- 값이 **의미 있게** 바뀌면 새 commit으로 제안을 교체합니다(`Rightsizing-Supersedes`). 기준은 기존 제안 대비 `min_change` 이상의 변화이거나, 축소와 증가 방향이 뒤집힌 경우입니다. 예를 들어 사용량이 갑자기 늘어 축소 대신 증가가 필요해진 경우입니다.
- 작은 변동이면 그대로 둡니다(`no_change:pending_same`). 이때는 LLM도 호출하지 않습니다.
- 다시 계산한 값이 현재 값과 거의 같으면 제안을 **철회**합니다. 철회는 현재 값으로 되돌리는 commit(`Rightsizing-Withdraw`)이고, git revert가 아니므로 같은 방향의 제안 억제(suppression)가 걸리지 않습니다.
- 사람이 교체나 철회 commit을 revert하면, 그 결정을 존중해서 14일 동안 다시 손대지 않습니다(`no_change:pending_endorsed`).

## 원래 스펙과 달라진 점 (합의된 계획 기준)

| # | 내용 |
|---|---|
| DV1 | SA에 `argocd` ns의 Application 읽기 권한과 대상 ns의 HPA, LimitRange, events, pods/log, metrics 읽기 권한을 추가했습니다(모두 읽기 전용) |
| DV2 | CPU/메모리 사용률 기준 HPA가 걸린 리소스는 건너뜁니다 |
| DV3 | 렌더링을 위해 Helm chart repo로 나가는 egress가 필요합니다 |
| DV4 | 이력, pending, revert 억제를 workload(`ns/Kind/name`) 단위로 관리합니다 |
| DV5 | source가 하나이고 `targetRevision`이 branch와 같은 Application만 지원합니다 |
| DV6 | kubectl-ai를 stdio가 아니라 네이티브 sidecar의 streamable-HTTP(localhost)로 연결합니다 |
| DV7 | push는 기본으로 꺼져 있고, coverage gate를 거친 뒤 켭니다 |
| DV-A (미러) | resources finalizer가 붙은 Application은 미러가 지우지 않습니다. 디렉터리와 카운트를 그대로 두고 매 실행마다 exit 4와 알림으로 알리니, 사람이 지워야 합니다 |
| DV-B (미러) | 미러가 만들었고 아직 한 번도 Sync되지 않은 앱만 클러스터 변경을 따라 갱신합니다. 사람이 git을 직접 고쳤다면 갱신하지 않습니다 |
| DV-C (미러) | 미러가 만드는 Application은 전용 AppProject `gitops-mirror`에만 둡니다. ValidatingAdmissionPolicy로 강제합니다 |
| DV-D (미러) | 대량 삭제 방지 장치(개수, 비율, 빈 namespace) 때문에 정상적인 삭제도 보류될 수 있습니다(exit 4) |
| DV-E | 0.2.x 이미지에는 미러 기능과 함께 commit message의 CPU 표시 수정(`P95 2m`)도 들어 있습니다 |
| DV-F (미러) | 한 번도 Sync되지 않았거나 실제로 떠 있던 적이 없는 앱은 Deployment가 사라져도 자동으로 지우지 않습니다(`never_deployed`). 사람이 정리합니다 |

## 알려진 한계
- 사람이 메시지를 직접 고친 revert는 감지하지 못합니다. `This reverts commit <sha>` 줄이 있어야 합니다.
- rightsizing commit이 아직 sync되지 않은 상태에서 누군가 다른 이유로 sync하면, 그 commit도 함께 배포됩니다. workload당 미승인 commit은 최대 1개로 제한됩니다.
- kubectl-ai의 MCP HTTP 서버는 인증 없이 모든 인터페이스에서 대기합니다. NetworkPolicy로 ingress를 막아 두었고, NetworkPolicy가 강제되는 CNI가 필요합니다.
- 실제 클러스터에서 kubectl-ai의 출력 기록과 E2E 검증은 아직 하지 않았습니다(클러스터 접근이 없었습니다). `scripts/verify-cluster.sh`로 확인하세요.
- 이미지는 colima에서 linux/amd64로 빌드하고 스모크 테스트를 거쳐 `docker.io/haewon123`에 push했습니다(agent 0.2.3, kubectl-ai v0.0.31).

## 로컬 개발

```bash
uv venv -p 3.12 .venv && uv pip install -p .venv/bin/python -e '.[test]'
.venv/bin/python -m pytest -q          # helm, kustomize, promtool, kubeconform이 있으면 관련 테스트도 함께 실행됩니다
.venv/bin/python scripts/gen_alerts.py  # 알림 규칙을 다시 생성합니다

# kubeconfig로 로컬 dry-run (kubectl-ai는 따로 띄웁니다)
kubectl-ai --mcp-server --mcp-server-mode streamable-http --http-port 9080 &
.venv/bin/python -m rightsizing --config ./local-config.yaml --dry-run --only development/Deployment/web
```
