# Deep Interview Spec: GitOps Mirror (클러스터 → Gitea 앱 등록/정리) + rightsizing 재제안

## Metadata
- Interview ID: k8s-deploy-exporter-20261125
- Rounds: 5 (+ Round 0 topology; Round 3 asked 3 times for clarification)
- Final Ambiguity Score: 19%
- Type: brownfield
- Generated: 2026-11-25
- Threshold: 0.2
- Threshold Source: default
- Initial Context Summarized: no
- Status: PASSED, **pending approval**

## Clarity Breakdown
| Dimension | Score | Weight | Weighted |
|---|---|---|---|
| Goal Clarity | 0.88 | 0.35 | 0.308 |
| Constraint Clarity | 0.85 | 0.25 | 0.213 |
| Success Criteria | 0.70 | 0.25 | 0.175 |
| Context Clarity | 0.75 | 0.15 | 0.113 |
| **Total Clarity** | | | **0.808** |
| **Ambiguity** | | | **0.192** |

## Topology
| Component | Status | Description | Coverage |
|---|---|---|---|
| export | active | Read Deployments in the target namespaces and produce a clean `<ns>/<app>/deployment.yaml` with runtime fields removed | AC 1–4 |
| git-push | active | Add new apps and delete removed apps in the Gitea `resource` repo. **Never modifies existing apps** | AC 5–8 |
| argocd | active | Create Applications for new apps (manual sync, no automatic sync even the first time) and delete Applications for removed apps (non-cascade) | AC 9–12 |
| schedule | active | A one-off init pod plus a periodic run (CronJob) | AC 13–14 |
| (change) rightsizing re-proposal | active (scope added during the interview) | While a pending commit exists, recompute daily. If the values changed, replace the proposal with a new commit | AC 15–17 |

## Goal
For the target namespaces (`inference` and `development` by default), keep the **set of apps that exist** in the Gitea `resource` repo and in ArgoCD Applications in line with the Deployments that exist in the cluster.
- A Deployment that exists in the cluster but not in git: the mirror adds it to git and creates a manual-sync Application.
- An app that is in git but no longer exists in the cluster: the mirror deletes it from git, and deletes its Application without cascading.
- **The content of apps already in git (resources, images and so on) is not touched by the mirror.** Changes to content are owned by rightsizing proposals plus human approval.
- rightsizing no longer skips workloads that have a pending commit. It recomputes daily, and when the recommendation changed (for example a usage spike), it supersedes the pending proposal with a new commit.

## Constraints
- **Cluster is the source of truth for existence only.** The mirror judges only whether an app exists; it does not mirror content.
- Kinds: Deployment only. StatefulSet is a non-goal for now.
- Directory convention: `<namespace>/<deployment name>/deployment.yaml` (plain YAML), the same as the current `resource` repo.
- Manifest cleaning: remove `status`, `metadata.{uid, resourceVersion, generation, creationTimestamp, managedFields, selfLink, ownerReferences}`, the annotations `kubectl.kubernetes.io/last-applied-configuration` and `deployment.kubernetes.io/revision`, and ArgoCD tracking labels/annotations. Remove server-injected defaults where needed so that ArgoCD does not report OutOfSync.
- Owning an existing app: if an app directory is already in git, it is treated as existing regardless of who created it (for example sample-web), and the mirror does not touch its content. Deletion also applies to every app under the target namespace directories.
- **Deletion safeguards (all of them):**
  - Applications are always deleted **without cascading** (no finalizer), so cluster resources are never touched.
  - Delete only after the absence is **confirmed on 2 consecutive runs**. The first time, only mark it as a deletion candidate.
  - If listing Deployments in a namespace fails (API error, permissions), **stop all deletions** in that namespace.
- **No automatic sync at all.** The mirror only creates the Application. The first sync is done by a human in ArgoCD as well (from then on the app is a rightsizing target).
- **Dedicated SA `gitops-mirror`**: get/list on Deployments in the target namespaces, and get/list/create/delete on `applications.argoproj.io` in the `argocd` namespace. The rightsizing SA stays read-only as before.
- The Git token reuses the bot (`rightsizing-bot`, Write on the `resource` repo only), via a separate Secret or the same one.
- rightsizing change: while pending, compute against the **live values as the baseline**. If the result differs from the values of the pending commit, make a new commit. If it is the same, do nothing (no duplicate commit). The drift check takes "HEAD = the pending commit's values" into account.

## Non-Goals
- Mirroring the content of existing apps (syncing images or env changes back into git)
- StatefulSet, Service, ConfigMap and similar (Deployment only)
- Automatic ArgoCD Sync (including the first one)
- Changing workloads in the cluster directly

## Acceptance Criteria
**export**
- [ ] 1. A Deployment that exists only in the cluster produces `<ns>/<name>/deployment.yaml`. The file contains none of the runtime fields or tracking labels listed above.
- [ ] 2. The exported manifest passes `kubeconform -strict`, and rendering it gives the same spec as the live one (normalized comparison).
- [ ] 3. Namespaces outside the allowlist are never read.
- [ ] 4. Deployments managed by helm or owned by another resource (with ownerReferences) are skipped with a logged reason. Assumption: they have another source of truth.

**git-push**
- [ ] 5. Additions for a new app go into a single commit with trailers (`Mirror-Action: add`, `Mirror-Workload: ns/Deployment/name`).
- [ ] 6. The files of an app already in git are byte-for-byte unchanged before and after a mirror run.
- [ ] 7. After the 2-consecutive-run confirmation, the app directory is deleted in a single commit (`Mirror-Action: delete`). After only one detection there is no deletion, and the candidate state is recorded.
- [ ] 8. If listing fails, deletion is skipped for that namespace and the run exits non-zero.

**argocd**
- [ ] 9. The Application for a new app is created with repoURL/targetRevision/path matching the convention, `syncPolicy` without `automated`, and the label `rightsizing/managed-by: gitops-mirror`.
- [ ] 10. The mirror never issues a sync (no `operation` field patch).
- [ ] 11. When an app is deleted, its Application is deleted without cascading, and the Deployment and other resources in the cluster stay as they are (verified in a test).
- [ ] 12. RBAC: the `gitops-mirror` SA has no write permission on Deployments (`kubectl auth can-i`).

**schedule**
- [ ] 13. A one-off `mirror-init` pod manifest and a periodic CronJob (default every day at 02:30, before rightsizing at 03:00) are provided.
- [ ] 14. Idempotent: running twice in a row with no change produces no commit and no Application change.

**rightsizing re-proposal**
- [ ] 15. When a pending commit exists and the recomputation result differs, a new commit is created (`skipped:pending` removed). The new commit's `Rightsizing-Change` uses the live values as old.
- [ ] 16. When the recomputation result equals the pending values, there is no commit (outcome `no_change:pending_same`).
- [ ] 17. Superseding a pending proposal does not trip the drift check (HEAD = pending values, live = original values).

## Assumptions Exposed & Resolved
| Assumption | Challenge | Resolution |
|---|---|---|
| After export, git becomes the source of truth | The user wants the cluster to be the source of truth | Truth only for **existence**: add/delete only, content not touched |
| The mirror reverts pending rightsizing proposals back to cluster values | The mirror would wipe out proposals | The mirror does not touch content → no conflict |
| rightsizing skips pending apps | Usage may spike tomorrow and require an increase | Recompute daily and supersede with a new commit |
| Delete immediately once gone | A transient API error or redeploy could delete a healthy app | non-cascade, 2 consecutive confirmations, stop on listing failure |
| The first sync after registration may be automatic | The user wants a human for the first sync too | Fully manual; the app becomes a rightsizing target after the first sync |
| The rightsizing SA can do this too | The read-only principle | Separate `gitops-mirror` SA |

## Technical Context
- Reuse: `rightsizing/gitops.py` (clone with askpass, commit, push with rebase retry, path guard), the trailer pattern in `commit_msg.py`, `discovery.KubeClient`, and `deploy/` (image and kustomize structure).
- Repo layout: `admin-gitea/resource` already has `development/sample-web`, `inference/sample-api` and a README.
- ArgoCD v2.12.3, label tracking. In this cluster `kubectl get app` resolves to Rancher, so the code must always use `argoproj.io` explicitly.
- Places in rightsizing that need changes:
  - in `main.process()`: the `PENDING` → skip branch, and the drift check order
  - in `gitops.pending_state()`: its usage
- Where to keep the deletion candidate state (2 consecutive confirmations): a marker file in git (`<ns>/<app>/.mirror-missing`, with a timestamp) or an annotation on the Application. To be decided in the plan.

## Ontology (Key Entities)
| Entity | Type | Fields | Relationships |
|---|---|---|---|
| Mirror | core | schedule, namespaces | adds/deletes AppDir, Application |
| ClusterDeployment | external | ns, name, spec, owner | source of existence |
| AppDir | core | `<ns>/<app>/deployment.yaml` | in ResourceRepo |
| ResourceRepo | external | Gitea admin-gitea/resource, main | holds AppDir |
| Application | external | name, path, syncPolicy(manual), label managed-by | points to AppDir |
| DeletionCandidate | supporting | first_seen_missing | gates deletion (2 consecutive confirmations) |
| MirrorSA | supporting | RBAC | used by Mirror |
| RightsizingCommit | core | trailers, pending | superseded by new commit when the recommendation changes |
| Rightsizing | core | daily | proposes on AppDir content |

## Ontology Convergence
| Round | Entities | New | Stable | Stability |
|---|---|---|---|---|
| 1 | 6 | 6 | - | - |
| 2 | 7 | 1 (RightsizingCommit) | 6 | 86% |
| 3 | 8 | 1 (Rightsizing) | 7 | 88% |
| 4 | 9 | 1 (DeletionCandidate, MirrorSA merged) | 8 | 89% |
| 5 | 9 | 0 | 9 | 100% |

## Interview Transcript
<details><summary>Q&A</summary>

- R0 topology: export, git push, ArgoCD registration, run mode → Looks right
- R1 source of truth: "The cluster should be the source of truth; a deployment that is not in the cluster should be deleted from git and argocd"
- R2 conflict rule: skip pending apps (later superseded by the R3 clarification)
- R3 (×3): "Shouldn't a pending commit be overwritten? Usage might spike tomorrow." / "What is a mirror?" / "The mirror only adds new ones to git and removes ones deleted from the cluster"
- R4 [Contrarian] deletion safety: apply all safeguards
- R5 first sync: a human does the first sync too
</details>
