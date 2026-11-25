# Plan: GitOps Mirror + rightsizing re-proposal

- Status: **PENDING APPROVAL**. v2.1 has Architect and Critic consensus (APPROVE_WITH_CHANGES, all changes applied). Implementation has not started. DV-A to DV-F need user sign-off.
- Spec: `.omc/specs/deep-interview-gitops-mirror.md` (AC 1–17 → M1–M17)
- Mode: RALPLAN-DR deliberate
- Code touch points:
  - `rightsizing/main.py:154-230`: `process()`; pending branch at L164-172; `if not changes` at L184; `expected` at L203-208
  - `rightsizing/gitops.py:142-219`: History; `reverts` at L169; `pending_state` at L199
  - `rightsizing/discovery.py:58` (`resolve_app`) and `:236` (`get_application`, argoproj.io v1alpha1)
  - `rightsizing/manifest/render.py:54-58` (YAML_SUFFIXES load) and `:212-234` (`validate_edit`)
  - `rightsizing/policy.py:129` (`min_change`)

## Spec deviations (need user acknowledgement)
| # | Deviation | Why |
|---|---|---|
| DV-A | An Application carrying `resources-finalizer.argocd.argoproj.io` is **not deleted**. The run ends with a safety exit (4) and an alert asking a human to delete it (spec M11 says "Application is deleted"). Alternative: grant `patch` so the mirror can strip the finalizer, then delete. | A plain delete would cascade and remove the workload's other resources. `patch` would also let a leaked token enable auto-sync. |
| DV-B | The mirror **refreshes content only for directories it added itself whose Application was never synced** (`status.history` empty). Spec M6 says "never touch existing content". | The export is a snapshot. Without a refresh, a human's first sync days later would roll back CI image or scale changes and restart pods. |
| DV-C | The mirror's Applications go into a **dedicated AppProject `gitops-mirror`**, guarded by a ValidatingAdmissionPolicy, instead of `default`. | `create` on Applications would otherwise allow an auto-sync app pointing anywhere. |
| DV-D | A **mass-delete guard** and a **never-deployed guard** can hold back deletes the spec would otherwise allow. | Recreated or emptied namespaces, and git-first apps that were never deployed. |
| DV-E | Image 0.2.0 also ships the earlier, unrelated commit-message fix (`P95 2m` formatting). | Stated here explicitly instead of bundled silently. |
| DV-F | An app the mirror added but a human **never synced** is never deleted automatically (`never_deployed`, which needs a successful sync in its history). It is reported in the summary, and a human removes it by hand. | Prevents deleting apps that never ran; the trade-off is that abandoned, never-synced apps need manual cleanup. |

## Requirements Summary
`python -m rightsizing.mirror` keeps the **set** of apps in sync across `admin-gitea/resource` and ArgoCD, for the target namespaces:
- **add:** a live Deployment with no app → cleaned manifest committed, plus a manual-sync Application in project `gitops-mirror`. The mirror never syncs.
- **delete:** an app whose Deployment has been missing across ≥2 consecutive successful runs spanning ≥12h, and which was previously seen live → its directory is removed and its Application is deleted (non-cascade, with a precondition).
- **reconcile:** ArgoCD state is reconciled every run, so a failure is self-healing.
- Existing content is never edited, except DV-B.

rightsizing, while a commit is pending, recomputes against live and supersedes only on a material change (`min_change` from the pending value, or a direction flip). If the new recommendation is "keep live", it **withdraws** the pending proposal with a commit that restores the live values. This is not a git revert, so no suppression is triggered.

## RALPLAN-DR Summary
### Principles
1. Existence-only mirror; content changes belong to rightsizing and humans (sole exception DV-B).
2. Deletes are slow, bounded and reversible: 2 consecutive confirmations over ≥12h, a mass-delete guard, a never-deployed guard, non-cascade with a precondition, and git history to restore from.
3. Least privilege enforced by the platform: a separate SA, a dedicated AppProject, and an admission policy. No workload writes.
4. Humans own every sync.
5. Self-healing and idempotent: every run recomputes the plan from cluster + repo + ArgoCD + state, so a partial failure is repaired on the next run.

### Decision Drivers
1. Never delete or roll back a healthy app.
2. Zero-touch onboarding of new Deployments.
3. No write-write conflict with rightsizing, and no daily commit churn.

### Viable Options
**A. Python module in the same image (favored).**
- Reuses the tested git, config and logging code and the existing fakes.
- Cons: release coupling (a rightsizing hotfix redeploys a delete-capable tool). Mitigation: separate SA, AppProject, CronJob and NetworkPolicy, and deletes are gated by tests.

**B. Shell script.** Rejected: the 2-pass state, the guards and the preconditions are untestable.

**C. ApplicationSet git-directory generator + exporter.**
- Steelman: "no cascade" becomes a controller property (`preserveResourcesOnDeletion`).
- Rejected for v1:
  - it still needs the exporter and the state
  - deletion becomes "the directory disappeared", applied immediately with no 2-pass guard at the ArgoCD level
  - it requires a cluster-wide ApplicationSet config
- Follow-up F2.

### State store (sub-decision)
**A single root file `.mirror/state.yaml`, written in at most one commit per run, is favored.** It holds `seen` (a Deployment that has ever been live), `missing` (first_missing_run, first_missing_at, consecutive_ok_runs) and `last_ok_list` per namespace.
- It never writes into app directories (Principle 1), causes no ArgoCD refresh noise on app paths, and stays auditable in git.
- Rejected:
  - per-app marker files: they write into existing directories
  - Application annotations: need `patch`
  - a ConfigMap: separate from git, and needs extra RBAC

## Implementation Steps
### Step 1 — Config `MirrorConfig` (`rightsizing/config.py`)
- `namespaces`: ⊆ allowlist
- `argocd_project`: `gitops-mirror`
- `destination_server`
- `app_name_template`: `{name}`
- `min_confirmations`: ≥2
- `min_missing_interval_hours`: default 12
- `max_deletes_per_run`: default 3
- `max_delete_ratio`: default 0.3 per namespace
- `skip_helm`: true
- `managed_label`: `rightsizing/managed-by=gitops-mirror`
- `timeouts.k8s`: default 30s per request
- Pydantic bounds, the same as the existing config.

### Step 2 — Export/clean `rightsizing/mirror/clean.py` (M1, M2, M4)
- Keep `apiVersion`, `kind`, `metadata.{name, namespace, labels, annotations}` and `spec`. Drop:
  - `status` and `metadata.{uid, resourceVersion, generation, creationTimestamp, managedFields, selfLink, ownerReferences, finalizers}`
  - the annotations `kubectl.kubernetes.io/last-applied-configuration`, `deployment.kubernetes.io/revision`, `argocd.argoproj.io/*`
  - `spec.template.metadata.creationTimestamp`
  - **only top-level** `metadata.labels["app.kubernetes.io/instance"]`, and only when it equals the target app name. `spec.selector` and template labels are never touched.
- Drop `spec.replicas` when an HPA targets the Deployment, so the first sync can't override the HPA.
- `skip_reason`:
  - `owned`: has ownerReferences
  - `helm_managed`: label `app.kubernetes.io/managed-by=Helm` or annotation `meta.helm.sh/release-name`
  - `already_tracked`: `discovery.resolve_app()` → an Application exists **and** lists this resource in `status.resources`
- Test: `kubeconform -strict` on the output, and `clean(live).spec == live.spec` (minus replicas when HPA).

### Step 3 — Repo scan `rightsizing/mirror/scan.py`
- For each `<ns>/` directory, each subdirectory `<d>/` is classified as:
  - **app**: it holds YAML files (`YAML_SUFFIXES`, multi-doc via `load_safe_all`) containing exactly one `Deployment` with `metadata.name == <d>` → app `<ns>/<d>`
  - **foreign** (never deleted, logged `foreign_layout`): anything else, i.e. kustomization or Chart, a different name, parse errors, or more than one Deployment
- Also treat any `spec.source.path` of an existing Application that points into the repo as present, so duplicate directories are never created.

### Step 4 — Planner `rightsizing/mirror/plan.py` (pure; M5–M8, M14)
Inputs: `cluster[ns] | ListError`, `repo_apps`, `foreign`, `apps` (managed Applications keyed by path, plus all Applications by path), `state`, `now`, and the run id.

| Condition | Action |
|---|---|
| live, no app dir, not foreign/tracked | `add_dir` (+ `seen`) |
| live, app dir exists, mirror-owned Application never synced, manifest differs from cleaned live | `refresh_dir` (DV-B) |
| app dir exists, no managed or other Application for the path | `create_app` (self-heal) |
| managed Application whose path is no longer in the repo | `delete_app` (self-heal) |
| app dir, not live, ns listed OK | `missing[app].consecutive_ok_runs += 1` (create on first) |
| app dir, not live, `seen` false or Application `status.history` empty | `skip: never_deployed` (never delete) |
| `consecutive_ok_runs ≥ min_confirmations` **and** `now - first_missing_at ≥ min_missing_interval` | `delete_dir` + `delete_app` |
| app is live again | clear `missing` |
| ns list error | no `missing` updates for that ns, **reset its counters**, exit 2 |

- **Mass-delete guard:** if deletes in a ns exceed `max_deletes_per_run`, or `deletes / apps_in_ns > max_delete_ratio`, or the ns lists **zero** Deployments while the repo has ≥1 app there, the planner emits no deletes for that ns, records `halt:mass_delete`, and exits 4.

### Step 5 — Git apply `rightsizing/mirror/run.py`
- One commit per `add_dir`, `refresh_dir` and `delete_dir`, with trailers `Mirror-Action` and `Mirror-Workload`, path-guarded to `<ns>/<name>`. Deleting a directory removes all of its files; this is noted in the README for pre-existing directories such as sample-web, which also holds a Service.
- One state commit (`Mirror-Action: state`) **only if** `.mirror/state.yaml` changed.
- One push with rebase retry (`gitops.py push`). An add/add conflict exits 2, and the next run replans from scratch.

### Step 6 — ArgoCD apply `rightsizing/mirror/argocd.py` (after the push; M9–M11)
- **create:**
  - `argoproj.io/v1alpha1`, project `gitops-mirror`
  - single `source{repoURL, targetRevision, path}`
  - no `syncPolicy.automated`, no finalizers
  - managed label
  - existing name → if the spec matches, no-op; else `app_name_conflict`, exit 4
- **delete:**
  - Only managed, single-source Applications whose path matches. Multi-source ones are skipped.
  - Do a get, then: if the finalizer is present → `needs_manual_delete`, exit 4 (DV-A). Otherwise delete with `preconditions{uid, resourceVersion}`, closing the race where a human adds a finalizer, and `propagationPolicy=Orphan` (harmless).
- Never writes `operation`.

### Step 7 — rightsizing supersede/withdraw (`main.py`, `gitops.py`, `commit_msg.py`) (M15–M17)
When `pending_state == PENDING`:
1. `expected_head = live ⊕ pending.changes`. If the rendered HEAD ≠ `expected_head` for any container → `skipped:drift` (a manual git edit).
2. Compute recommendations against live, as today, across **all** containers.
3. `target = live ⊕ new changes`.
4. **Material check, before the LLM:**
   - Supersede only if some field's `|target − pending| / pending ≥ min_change`, or its direction flips, or a pending field is now unchanged.
   - Otherwise `no_change:pending_same`.
5. If the new recommendation is all-live, this is a **withdraw**: edit HEAD back to live and commit with the trailer `Rightsizing-Withdraw: <pending sha>` and no `Change` lines. `History` treats it as the latest commit for the workload with no pending changes, so `pending_state` → APPLIED.
6. Otherwise, a **supersede**:
   - The edits are every field where `target ≠ HEAD`, across containers.
   - `expected` for `validate_edit` = `target` for every container touched.
   - `Rightsizing-Change` old = **live**.
   - Add a trailer `Rightsizing-Supersedes: <pending sha>`.
   - The LLM payload includes the pending sha and its values.
- The `if not changes` early return (`main.py:184`) is moved after this logic only for the pending path.
- Suppression and revert detection are unchanged. Withdraw and supersede are not reverts.

### Step 8 — Deploy `rightsizing/deploy/mirror/` (M12, M13)
- SA `gitops-mirror`.
- RBAC:
  - target namespaces: deployments get/list, hpa list
  - `argocd`: applications get/list/create/delete
  - no update/patch, no secrets
- `appproject.yaml` (`gitops-mirror`):
  - `sourceRepos` = the resource repo
  - destinations `inference` and `development` on the in-cluster server
  - `namespaceResourceWhitelist` `apps/Deployment` only
  - no cluster resources
- `validatingadmissionpolicy.yaml` (k8s 1.32 GA) plus a binding. Applications created by `system:serviceaccount:rightsizing:gitops-mirror` must have `spec.project == gitops-mirror`, no `syncPolicy.automated`, no `operation` and no finalizers.
- `cronjob.yaml`:
  - `30 2 * * *` Asia/Seoul, Forbid, `backoffLimit: 0`
  - `activeDeadlineSeconds: 900`, `startingDeadlineSeconds: 3600`, TTL 86400
  - `command: ["python", "-m", "rightsizing.mirror"]`
  - only the `gitea_token` secret item, read-only rootfs, emptyDirs
  - label `app.kubernetes.io/name: gitops-mirror`
- `pod-init.yaml`: the same spec, one-off, sharing the same 12h interval rule, so a manual run can never confirm a delete early.
- `networkpolicy.yaml`: ingress deny-all for `gitops-mirror`.
- Alerts (via `scripts/gen_alerts.py`): `MirrorJobFailed` (exits 2 and 4), `MirrorJobNotRun`.
- `rightsizing/mirror/__main__.py`.
- Everything is added to `deploy/kustomization.yaml`.

### Step 9 — Docs + image
- README "GitOps mirror": the flow, the guards, exit codes 0/2/4, and recovery.
- Recovery: if the mirror deleted a directory wrongly, `git revert` the delete commit **and** re-create the Deployment, or the next run marks it missing again.
- Bump to 0.2.0 (DV-E), build amd64, push to `haewon123`, update the kustomization.

## Acceptance Criteria (mapping)
| ID | Verified by |
|---|---|
| M1, M2 | `tests/mirror/test_clean.py`: live-shaped fixture incl. managedFields/HPA; `kubeconform -strict`; spec equality |
| M3 | `test_run.py::test_only_allowlisted_ns_read` (FakeKube call recording) |
| M4 | `test_clean.py::test_skip_reasons` (owned, helm, already_tracked via `status.resources`) |
| M5, M6 | `test_run.py` with a bare repo: one commit per add with trailers; existing files byte-identical; refresh only for never-synced mirror-owned apps |
| M7 | `test_plan.py`: no delete after 1 run; no delete when 2 runs fall within 12h; a list error between runs resets counters; delete after ≥2 OK runs ≥12h; `never_deployed` skip; mass-delete halt (count, ratio, empty-ns) |
| M8 | `test_plan.py::test_list_error_halts` + exit 2 |
| M9–M11 | `test_argocd.py` (FakeArgo): create spec/project/no automated/no operation; finalizer → `needs_manual_delete`, exit 4; delete sends preconditions; multi-source skipped; Argo failure on run 1 healed on run 2 |
| M12 | `test_deploy.py::test_mirror_rbac` + VAP render + `kubectl auth can-i` in `verify-cluster.sh` |
| M13 | `test_deploy.py::test_mirror_cronjob_and_init_pod` |
| M14 | `test_run.py::test_idempotent_rerun` |
| M15–M17 | `tests/test_supersede.py`: material change → supersede (old=live, Supersedes trailer); jitter → `pending_same` with no LLM call; all-live → withdraw (no suppression); manually edited HEAD → drift; multi-container revert-to-live passes `validate_edit` |

## Risks and Mitigations
| Risk | Mitigation |
|---|---|
| Healthy apps deleted (API glitch, recreated or emptied ns) | list error halts and resets; 2 OK runs ≥12h; mass-delete guard incl. empty-ns; `never_deployed`; git revert restore path |
| Cascade delete | finalizer → refuse + exit 4; uid/resourceVersion precondition; VAP forbids the mirror adding finalizers |
| Leaked token creates an auto-sync app pointing anywhere | dedicated AppProject + VAP on SA-created Applications; no patch/update |
| The first sync rolls back CI changes or HPA scale | DV-B refresh for never-synced apps; drop replicas under HPA |
| Daily supersede churn | material threshold vs pending; check before the LLM |
| ArgoCD half fails after push | reconcile `create_app`/`delete_app` every run |
| Mirror vs rightsizing push race | disjoint paths; rebase retry; conflict exits 2 and replans |

## Pre-mortem
1. **Every development app was deleted from git.**
   - Cause: the namespace was recreated empty.
   - Mitigation: the empty-ns guard and the ratio guard → halt, exit 4.
   - Detection: the `MirrorJobFailed` alert, and the visible `missing` entries in the state commit the day before.
2. **The first sync restarted pods and reverted an image.**
   - Cause: a stale snapshot.
   - Mitigation: DV-B refresh while never synced, and the spec-equality test.
   - Detection: the ArgoCD diff shown before the human sync, and the `refresh_dir` commits.
3. **A supersede overwrote a manual git edit.**
   - Mitigation: the `expected_head` check → `skipped:drift`.
   - Detection: the `skipped:drift` count in the rightsizing summary, plus the coverage review.

## Expanded Test Plan
- **Unit:** clean, scan, the planner table (including guards and time), Application builder/delete, supersede/withdraw/material check, trailers.
- **Integration (FakeKube + FakeArgo + bare repo):**
  1. add
  2. remove the Deployment → missing
  3. under 12h → no delete
  4. ≥12h → delete
  - Plus: Argo fails then heals; a list error resets; mass-delete halt; rerun idempotent.
  - rightsizing: pending + spike → supersede; pending + jitter → no commit; pending + flat usage → withdraw.
- **E2E (cluster):**
  1. `kubectl create deploy demo-x -n development`
  2. Run mirror-init → dir + Application (OutOfSync, project `gitops-mirror`)
  3. Human sync → label present, pods not restarted
  4. rightsizing targets it
  5. Delete the Deployment; on runs ≥12h apart, the second one deletes, and the Application is gone without touching other resources
  - Also: the VAP rejects an automated app created as the SA.
- **Observability:** summary `{added, refreshed, created_apps, deleted_apps, missing, halted, skipped{reason}}`; exit codes 0/2/4; alerts `MirrorJobFailed` and `MirrorJobNotRun`.

## Follow-ups
- F1: StatefulSet.
- F2: ApplicationSet with `preserveResourcesOnDeletion`.
- F3: notifications for adds and deletes.
- F4: a separate image or release train for the mirror, if coupling becomes a problem.


## Amendments v2.1 (binding; override earlier text where they conflict)
1. **Trailers (blocker, Architect N1 / Critic D):**
   - Extend `TRAILER_RE` (`commit_msg.py:16`), `ParsedTrailers` and `_FORGERY_RE` with `Supersedes` and `Withdraw`.
   - A Withdraw commit carries `Rightsizing-Workload`, `Rightsizing-Withdraw` and `Rightsizing-Run`, and no `Change` lines.
   - Round-trip tests check that `History.scan` picks a supersede or withdraw commit as `latest_unreverted`.
2. **Human revert endorsement (Architect N2):**
   - If the latest bot commit for a workload (supersede or withdraw) was reverted within `suppress_days`, the restored pending commit is treated as human-endorsed: `no_change:pending_endorsed`, with no further supersede or withdraw for that window.
   - On the pending path, a suppressed field keeps its pending value instead of reverting to live.
3. **Hysteresis (Architect N3):**
   - Withdraw a pending field only when the raw recommendation is within `min_change/2` of live, or its direction flips.
   - Supersede only when `|target − pending| / pending ≥ min_change`.
4. **DV-B refresh safety (Architect N4 / Critic B):**
   - Refresh only when **all** of these hold:
     - the Application is mirror-owned
     - `status.history` is empty **and** `status.operationState` is absent
     - `git log -1 -- <path>` has `Mirror-Action: add|refresh`
     - the hash of the HEAD file equals the hash of the last export recorded in `state.yaml`
   - Otherwise skip with `human_edited`. Test included.
5. **State file (Architect N5):**
   - It holds per-run **counters** (run number per namespace, `consecutive_ok_runs`, export hashes) and no per-run timestamps.
   - `first_missing_at` is set once when an app goes missing, so the state commit happens only when something actually changes.
6. **`delete_app` self-heal (Architect N5):**
   - Requires the path to be **absent on disk**. A foreign layout or a parse error never counts as absent.
   - These deletes count toward the mass-delete guard.
7. **Ratio guard (Critic A):** the ratio applies only when the namespace has ≥ `min_apps_for_ratio` apps (default 5) **and** deletes ≥ 2. The count guard and the empty-namespace guard always apply.
8. **`seen` (Critic C):**
   - Every live, non-skipped Deployment is marked `seen` on each successful list, which includes pre-existing apps such as sample-web.
   - `never_deployed` = `seen` false **or** the Application has no successful sync in its history. This is DV-F.
9. **Mirror NetworkPolicy (Critic #10):**
   - Ingress: deny all.
   - Egress: allow only DNS, kube-apiserver (443/6443) and Gitea (`namespaceSelector gitea`, port 3000).
   - The cluster uses Calico, so these rules are enforced.
10. **VAP details (Architect):**
    - `matchConstraints`: `argoproj.io/applications` CREATE.
    - `matchConditions`: `request.userInfo.username == 'system:serviceaccount:rightsizing:gitops-mirror'`.
    - CEL checks guarded with `has()`:
      - `object.spec.project == 'gitops-mirror'`
      - no `syncPolicy.automated`
      - no `operation`
      - no finalizers
    - `failurePolicy: Fail`. VAP is GA since k8s 1.30, and this cluster runs 1.32.13.
    - An e2e check confirms that an automated app created as the SA is rejected.

## ADR
- **Decision:** a Python `rightsizing.mirror` module in the shared image. It acts on existence only and keeps its state in `.mirror/state.yaml`.
  - It runs before rightsizing, every day at 02:30.
  - Deletes are guarded: 2 consecutive OK runs ≥12h apart, a mass-delete guard, a never-deployed guard, non-cascade deletes with preconditions, and the finalizer case escalates to a human.
  - Its Applications use the dedicated AppProject `gitops-mirror`, enforced by a VAP.
  - rightsizing gains supersede and withdraw, with hysteresis and human-revert endorsement.
- **Drivers:** never delete or roll back a healthy app; onboard new Deployments without manual steps; no write conflicts or churn with rightsizing.
- **Alternatives considered:**
  - B, a shell script: untestable guards.
  - C, ApplicationSet: immediate, controller-side delete semantics and a cluster-wide config change. Deferred as F2.
  - Marker files or annotations or a ConfigMap for state: they write into app directories, need extra RBAC, or sit outside git.
- **Why chosen:** it reuses tested git, config and logging code. Every safety rule can be unit-tested and integration-tested with the existing fakes. The platform (RBAC, AppProject, VAP) enforces least privilege on top of the code.
- **Consequences:**
  - The mirror and rightsizing release together. Each mirror run adds a few commits (adds, deletes, and the state file).
  - Some deletes need a human: finalizer apps (DV-A) and never-synced apps (DV-F).
  - Pre-existing directories such as sample-web lose every file in them, including the Service manifest, when their app is deleted.
- **Follow-ups:**
  - F1: StatefulSet.
  - F2: ApplicationSet.
  - F3: notifications.
  - F4: a separate release train.


## Changelog
- v2.1: amendments 1–10 (Architect N1–N5, Critic A–D, #10); DV-F added; ADR added.
- v2:
  - ArgoCD reconcile/self-heal (Critic 1)
  - mass-delete and empty-ns guards (Critic 2)
  - DV-A for the finalizer and other deviations, listed for sign-off (Critic 3)
  - supersede: material threshold, withdraw, all-container `expected`, check before the LLM, pending sha to the LLM (Critic 4, Architect D2/D4)
  - AppProject + VAP (Critic 5)
  - scan rules and foreign layout (Critic 6, Architect D8)
  - AC mapping fixes (Critic 7)
  - never-deployed guard (Architect D1)
  - consecutive and ≥12h with reset on error, via the root state file (Architect D3, marker relocation)
  - delete with preconditions, explicit v1alpha1, multi-source skipped (Architect D5)
  - HPA replicas dropped (Architect D6)
  - clean scope (Architect D7, Critic 8)
  - the `min_confirmations` setting now actually counts (Critic 9)
  - mirror NetworkPolicy and alerts (Critic 10, 11)
  - DV-E stated (Critic 12)
  - `__main__`, timeouts, exit codes (Critic 13)
  - recovery documented (Critic 14)
  - line refs corrected (Critic 15)
  - DV-B refresh (Architect tension)
