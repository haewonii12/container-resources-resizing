# Deep Interview Spec: K8s Pod Rightsizing Agent (kubectl-ai MCP → Gitea → ArgoCD 수동 sync)

## Metadata
- Interview ID: k8s-rightsizing-20261125
- Rounds: 9 (+ Round 0 topology)
- Final Ambiguity Score: 18%
- Type: brownfield
- Generated: 2026-11-25
- Threshold: 0.2
- Threshold Source: default
- Initial Context Summarized: no
- Status: PASSED — **pending approval**

## Clarity Breakdown
| Dimension | Score | Weight | Weighted |
|-----------|-------|--------|----------|
| Goal Clarity | 0.82 | 0.35 | 0.287 |
| Constraint Clarity | 0.80 | 0.25 | 0.200 |
| Success Criteria | 0.88 | 0.25 | 0.220 |
| Context Clarity | 0.75 | 0.15 | 0.113 |
| **Total Clarity** | | | **0.82** |
| **Ambiguity** | | | **0.18** |

## Topology
| Component | Status | Description | Coverage |
|-----------|--------|-------------|----------|
| collect | active | kubectl-ai(`--mcp-server`, read-only SA)로 대상 workload/resources/OOM·restart 이벤트 조회 + Prometheus 7일 사용량 조회 | AC 1–4 |
| recommend | active | OpenAI 호환 LLM agent loop가 namespace별 정책으로 새 request/limit 산정 | AC 5–9 |
| git-commit | active | manifest 파일 형식 자동 판별·수정, 앱별 1 commit을 Gitea에 push. commit message = 이력 | AC 10–14 |
| argocd-gate | active | 승인 = 사람이 ArgoCD에서 OutOfSync diff 확인 후 수동 sync. 거절 = `git revert` | AC 15–17 |

Round 0 변경: 원안의 "이력 기록"과 "승인 게이트"는 별도 컴포넌트가 아니라 각각 git commit message와 ArgoCD 수동 sync로 통합됨.

## Goal
`inference`, `development` namespace의 pod를 대상으로, Prometheus 7일 사용량 이력과 namespace별 정책에 따라 CPU/Memory request·limit 조정안을 만들고, 이를 Gitea manifest repo에 앱별 commit(근거 포함)으로 push하는 CronJob agent를 만든다. 클러스터 반영은 사람이 ArgoCD에서 수동 sync할 때만 일어난다.

## Constraints
- **대상 범위:** namespace `inference`, `development`만. GPU 리소스(`nvidia.com/gpu` 등)는 건드리지 않음.
- **클러스터 권한:** agent는 read-only ServiceAccount(두 namespace get/list + 이벤트/metrics 조회)만 가짐. 클러스터에 쓰기 불가 → 변경 경로는 Gitea commit 하나뿐 (권한으로 강제).
- **kubectl-ai:** 컨테이너 안에서 `kubectl-ai --mcp-server` (stdio)로 실행, agent가 MCP client로 연결. MCP 서버 모드는 kubectl/bash tool만 노출하며 자체 LLM은 필요 없음.
- **LLM:** OpenAI 호환 API(`/v1/chat/completions` + tools). `base_url`, `model`, API key는 config/secret. 모델은 tool calling을 지원해야 함.
- **사용량 근거:** Prometheus (container_cpu_usage_seconds_total, container_memory_working_set_bytes 등) 7일 window. kubectl-ai로는 이력을 못 얻으므로 Prometheus 조회 tool을 agent에 별도로 제공.
- **정책 (ConfigMap, 수정 가능):**

  | 항목 | inference (보수) | development (공격) |
  |---|---|---|
  | CPU request | P95 × 1.3 | P95 × 1.1 |
  | Mem request | P95 × 1.3 | P95 × 1.1 |
  | Mem limit | max × 1.5 | max × 1.2 |
  | CPU limit | 기존 값 유지 (옵션) | 기존 값 유지 (옵션) |
  | 1회 최대 축소폭 | 25% | 50% |

  공통: 변화량 10% 미만은 제안 안 함 / 최근 7일 OOMKilled 또는 의미 있는 throttling이 있으면 축소 금지 / 데이터 3일 미만인 pod는 skip.
- **Manifest 형식:** 저장소 구성을 모르고 형식이 섞여 있을 수 있음 → pod → owner(Deployment/StatefulSet) → ArgoCD tracking label/annotation → Application → repo path 역추적 후 형식(plain YAML / Helm values / Kustomize) 자동 판별. 판별·수정 불가 시 해당 앱은 skip하고 로그에 남김.
- **Git:** 대상 branch(기본 main)에 직접 push. 앱(workload)당 1 commit. 같은 실행에서 여러 앱 → 여러 commit.
- **실행 형태:** 처음부터 K8s CronJob (namespace `rightsizing`, 기본 1일 1회). 이 CronJob 자체 manifest도 Gitea + ArgoCD로 배포.
- **Secret:** LLM API key, Gitea token은 사람이 `kubectl create secret`으로 수동 생성. git에 커밋 금지.
- **Placeholder:** Prometheus URL, Gitea repo URL/branch, LLM base_url/model, 이미지 registry, schedule은 ConfigMap/values placeholder로 두고 사용자가 채움.
- **전제조건 (agent 외부):** 대상 ArgoCD Application은 auto-sync가 꺼져 있어야 함. 켜져 있으면 승인 게이트가 무력화됨 → README에 명시.

## Non-Goals
- 클러스터에 직접 `kubectl apply/patch/set resources` 하는 경로
- ArgoCD sync 자동화 (승인은 항상 사람)
- HPA/VPA 설정, replica 수 조정, GPU 리소스 조정
- 별도 이력 DB/대시보드 (이력은 git log)
- 두 namespace 외 대상

## Acceptance Criteria
**collect**
- [ ] 1. CronJob pod 안에서 kubectl-ai MCP 서버가 stdio로 기동되고 agent가 `tools/list`로 tool 목록을 받는다.
- [ ] 2. read-only SA로 `inference`/`development`의 workload·컨테이너 resources를 조회할 수 있고, 쓰기(`kubectl patch` 등) 시도는 RBAC로 거부된다(테스트로 확인).
- [ ] 3. 컨테이너별 7일 CPU/Mem P95·max, OOMKilled 이력, throttling 비율을 Prometheus에서 얻는다.
- [ ] 4. 다른 namespace의 pod는 수집·제안 대상에 나타나지 않는다.

**recommend**
- [ ] 5. 고정 입력(사용량 fixture)에 대해 정책 계산 결과가 위 표와 일치한다 (단위 테스트, namespace별).
- [ ] 6. 변화량 <10%, 데이터 <3일, 최근 OOM 있는 축소, GPU 리소스는 제안되지 않는다 (각각 테스트).
- [ ] 7. 축소폭이 namespace 상한(25%/50%)을 넘지 않도록 clamp된다.
- [ ] 8. 최종 값은 LLM 출력이 아니라 정책 계산값 기준으로 검증되며, 정책 범위를 벗어난 LLM 제안은 거부된다.
- [ ] 9. `--dry-run`으로 실행하면 commit 없이 제안 목록만 출력한다.

**git-commit**
- [ ] 10. plain YAML / Helm values / Kustomize 각각의 fixture repo에서 올바른 필드만 수정한다 (나머지 diff 없음).
- [ ] 11. 형식 판별 실패 시 해당 앱은 skip되고 사유가 로그에 남으며 다른 앱 처리는 계속된다.
- [ ] 12. 앱별 1 commit이 생성되고 commit message가 아래 형식을 따른다:
  ```
  rightsizing(<ns>/<app>): <핵심 변경 요약>

  요약: <한글 한 줄>
  container: <name>
    cpu.request  <old> → <new>  (P95 <v>, ×<k>)
    mem.request  <old> → <new>  (P95 <v>, ×<k>)
    mem.limit    <old> → <new>  (max <v>, ×<k>)
  window: <from>..<to> (7d), OOM: <n>, throttle: <p>%
  policy: <ns policy name>
  예상 절감: CPU <x> cores, Memory <y> GiB (replicas <r> 반영)
  LLM 판단: <정책 외 고려사항/주의점>
  거절하려면: git revert <this commit>

  Rightsizing-Id: <date>-<ns>-<app>
  Rightsizing-Run: <cronjob run id>
  ```
- [ ] 13. Gitea token은 Secret에서만 읽고 로그/commit에 노출되지 않는다.
- [ ] 14. 변경할 것이 없으면 commit을 만들지 않는다.

**argocd-gate**
- [ ] 15. push 후 해당 ArgoCD Application이 OutOfSync로 표시되고, 사람이 sync하기 전에는 클러스터 리소스 값이 바뀌지 않는다.
- [ ] 16. revert된 `Rightsizing-Id`(동일 ns/app/방향) 제안은 다음 실행부터 설정된 기간(기본 14일) 동안 재제안되지 않는다.
- [ ] 17. 아직 sync/revert되지 않은 이전 rightsizing commit이 있는 앱에는 새 commit을 쌓지 않는다(중복 방지).

## Assumptions Exposed & Resolved
| Assumption | Challenge | Resolution |
|---|---|---|
| 이력을 별도로 저장해야 한다 | Round 0 | git commit message로 통합 |
| 승인 게이트는 agent 안에 있어야 한다 | Round 0 | ArgoCD 수동 sync가 승인 |
| kubectl-ai로 사용량을 판단할 수 있다 | `kubectl top`은 스냅샷뿐 | Prometheus 7일 이력 기반 |
| 승인 안 한 변경이 main에 있어도 괜찮다 | 다른 sync에 섞여 배포될 위험 | 앱별 commit + 거절=`git revert`, agent가 revert 인식 |
| kubectl-ai MCP에 기존 kubeconfig 사용 | agent가 GitOps를 우회할 수 있음 | read-only SA로 권한 강제 |
| 하나의 정책으로 충분 | inference vs development 위험도 다름 | namespace별 정책 |
| 최소 버전은 로컬 CLI | Simplifier 제안 | 사용자 선택: 처음부터 CronJob |
| 기존 harness의 Anthropic SDK 사용 | 클러스터에서 사내 LLM 사용 | OpenAI 호환 API |
| manifest 형식이 하나다 | 사용자도 모름 | 역추적 + 형식 자동 판별, 실패 시 skip |
| (미확인) 최종 값 결정 주체 | 인터뷰에서 직접 다루지 않음 | 제안: 정책 계산은 결정적 코드, LLM은 예외 판단·설명. plan 단계에서 확정 (AC 8) |

## Technical Context
- `harness/main.py`: Anthropic SDK 기반 수동 agentic loop(tool_use → tool_result 반복). 루프 구조는 재사용하되 OpenAI 호환 클라이언트로 교체 필요.
- `harness/mcp_server.py`: 의존성 없는 stdio JSON-RPC MCP 서버 예시(서버 측). 이번에는 agent가 kubectl-ai MCP의 **client**가 되어야 함 → MCP client 구현(또는 `mcp` 패키지) 필요.
- `.mcp.json`: Claude Code용 harness-tools 등록. 새 agent와는 별개.
- 로컬 Mac에는 kubectl/kubectl-ai 미설치. 개발·테스트는 fixture 기반 + 클러스터 검증은 배포 후.
- 외부: 기존 Gitea ↔ ArgoCD 연동 존재(세부 repo 구조 미확인).

## Ontology (Key Entities)
| Entity | Type | Fields | Relationships |
|---|---|---|---|
| Pod / Container | external | ns, name, resources | owned by Workload |
| Workload | core | kind, name, ns, replicas | has Containers; tracked by ArgoApp |
| ResourceSpec | core | cpu/mem request/limit | belongs to Container |
| PrometheusMetric | supporting | P95, max, OOM, throttle, window | measures Container |
| NamespacePolicy | core | multipliers, max shrink, thresholds | applies to Workload by ns |
| Recommendation | core | old/new values, rationale, savings | produced from Metric+Policy |
| ManifestRepo | external | url, branch, path | sourced by ArgoApp |
| ManifestFormat | supporting | yaml/helm/kustomize | determines edit strategy |
| Commit | core | message, Rightsizing-Id/Run | carries Recommendation |
| RevertCommit | core | reverted Rightsizing-Id | suppresses Recommendation |
| ArgoApp | external | name, path, syncPolicy(manual) | gate for Commit → cluster |
| ServiceAccount | supporting | read-only RBAC | used by kubectl-ai MCP |
| CronJob | supporting | schedule, ns rightsizing | runs agent |
| LLMEndpoint | external | base_url, model | drives agent loop |

## Ontology Convergence
| Round | Entities | New | Stable | Stability |
|---|---|---|---|---|
| 1 | 8 | 8 | - | - |
| 2 | 9 | 1 | 8 | 89% |
| 3 | 10 | 1 | 9 | 90% |
| 4 | 11 | 1 | 10 | 91% |
| 5 | 12 | 1 | 11 | 92% |
| 6 | 13 | 1 | 12 | 92% |
| 7 | 14 | 1 | 13 | 93% |
| 8–9 | 14 | 0 | 14 | 100% |

## Interview Transcript
<details>
<summary>Q&A (Round 0 + 9 rounds)</summary>

- **R0** 토폴로지 → 이력은 git commit message로, 승인은 사람이 ArgoCD 보고 수동 sync.
- **R1** 산정 근거 → Prometheus 이력 기반. (63%→55%)
- **R2** manifest 형식 → 잘 모르겠음/섞여있음 → 자동 판별. (→51%)
- **R3** 거절 처리 → 앱별 commit + revert=거절. (→43.5%)
- **R4** [Contrarian] 클러스터 권한 → read-only SA. (→38%)
- **R5** 산정 정책 → namespace별 차등. (→29.5%)
- **R6** [Simplifier] 실행 형태 → 처음부터 CronJob. (→26%)
- **R7** 배포 구성 → 권장안 + LLM은 사내 엔드포인트. (→24.5%)
- **R8** LLM API → OpenAI 호환. (→20.5%)
- **R9** commit 형식 → 기본 형식 + 한글 요약, LLM 판단 근거, 예상 절감량, 롤백 안내. (→18%)
</details>
