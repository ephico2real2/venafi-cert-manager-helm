# Ordering, races and the approval gate — plan

Scope: `charts/cert-manager-venafi`. Written before changing anything, with each premise validated on a live
cluster first. Two claims I started with turned out to be **wrong**; they are retracted below rather than
quietly dropped, because the fix they implied would have been real work in the wrong direction.

## 1. The logical flow, as it actually is

```
namespaces.yaml          Namespace x2                    ordinary
operator.yaml            OperatorGroup + Subscription    ordinary
tpp-credentials-secret   Secret                          ordinary
tpp-cabundle-secret      Secret                          ordinary
trusted-ca-configmap     ConfigMap                       ordinary
                              |
                              v            OLM: resolve -> InstallPlan -> CSV -> 3 operand Deployments
                              |                           -> cert-manager CRDs -> webhook serving
verify-rbac.yaml         SA + ClusterRole + CRB          HOOK weight 0
capull-job.yaml          SA + Role + RoleBinding         HOOK weight 1
capull-job.yaml          Job (pull TPP CA -> Secret)     HOOK weight 3
verify-job.yaml          Job (5-stage readiness gate)    HOOK weight 5
clusterissuer.yaml       ClusterIssuer                   HOOK weight 10   <- gated behind the gate
```

The verify Job's five stages are the right five, and stage 5 is a race the sibling charts never had to
handle:

1. `Subscription.status.installedCSV` appears
2. that CSV reaches `phase=Succeeded`
3. the operand Deployments become `Available`
4. `clusterissuers` and `certificates` CRDs report `Established`
5. the cert-manager **validating webhook actually serves admission**, probed with a
   `oc create --dry-run=server` that persists nothing

Stage 5 exists because a CRD being `Established` does not mean the webhook fronting it is answering yet —
commit `35ba846` found that the hard way. Nothing in this plan changes those five stages.

## 2. RETRACTED: "the ClusterIssuer hook cannot survive a first install"

My first reading was that Helm builds every manifest — hooks included — before running any hook, so a
`cert-manager.io/v1` ClusterIssuer at weight 10 could never work on a cluster without the CRDs, and the
chart's own comment claiming otherwise was wrong. **That is incorrect, and the chart is right.**

Validated with two sandbox charts on a live cluster:

| sandbox | result |
|---|---|
| CR of an unserved kind as a `post-install` hook at weight 10, **no** gate creating the CRD | **fails** — `unable to build kubernetes object for deleting hook … no matches for kind "ProbeIssuer"`. But the weight-5 gate Job **ran to completion first** (`probe-gate Complete 1/1`). |
| weight-5 hook **creates** the CRD and waits for `Established`; weight-10 hook is a CR of that kind | **succeeds** — `STATUS: deployed`, gate succeeded, CRD present, CR created |

So Helm resolves a hook's kinds **when it processes that hook**, not at operation start. A CRD that appears
during an earlier hook is picked up. The weight-10 gating works exactly as the chart's comment says.

Consequence for this plan: **no `crds/` vendoring here.** That was the fix for the sibling NCO chart because
its policy CRs are *ordinary resources*, and ordinary resources ARE all built up front — a different Helm
error (`unable to build kubernetes objects from release manifest`) and a different problem. Vendoring
cert-manager's CRDs into this chart would have been unnecessary work and a copy to go stale.

## 3. What IS wrong

### 3.1 `verifyJob.enabled=false` is a footgun, not an option

Measured: with the flag off the ClusterIssuer renders with **no hook annotations at all** — an ordinary
resource, built with the whole manifest up front. On a cluster without the cert-manager CRDs that fails the
install outright, which is what `values.yaml` currently documents as "on a fresh cluster install it once the
operator is Ready". That is the manual phasing to remove.

Fix: keep the gate mandatory for the ClusterIssuer. If someone genuinely wants no verify Job, the honest
options are to disable the ClusterIssuer too, or to accept a documented two-step — but the default must not
be a flag that quietly turns a working install into a failing one.

### 3.2 The approval shortcut — `installPlanApproval: Automatic`, no pin

The Subscription rides the **rolling** `stable-v1` channel with `Automatic` approval and no `startingCSV`.
Measured on the cluster: `channel=stable-v1 approval=Automatic startingCSV=<unset>
installedCSV=cert-manager-operator.v1.19.1 state=AtLatestKnown`, and the channel's `currentCSV` is
`v1.19.1` today. When v1.20 is published, OLM installs it with no review and no change in git.

Fix, matching both sibling charts:

```yaml
operator:
  installPlanApproval: Manual
  startingCSV: cert-manager-operator.v1.19.1
```

plus an approver Job that approves **only** the pinned CSV. Everything the sibling charts learned applies:

- **wave 0, the Subscription's own wave, and `argocd.argoproj.io/hook: Sync` — not `PostSync`.** A
  Manual-approval Subscription reports `Progressing` on a first install, because ArgoCD's health check for
  `operators.coreos.com/Subscription` forgives `InstallPlanPending/RequiresApproval` only when
  `.status.installedCSV` is already set. In any later wave the approver waits on the wave that is waiting on
  it; as a `PostSync` hook it waits for a sync that cannot complete.
- **a pending upgrade is reported, never silently ignored.** Refusing to approve a non-pinned plan is the
  point, but a refusal nobody logs looks identical to an approver that failed to notice.
- **an empty read is not "Automatic".** `oc get … 2>/dev/null || true` collapses *field absent*,
  *Subscription missing* and *read failed* into one empty string. Keep the exit status and branch on it.

### 3.3 Zero sync-waves: 0 of 16 documents

Measured. Under ArgoCD every document therefore lands in wave 0 — Subscription, secrets, namespaces and the
ClusterIssuer applied together, and deletes unordered. Absent is not neutral; ArgoCD reads it as wave 0.

Proposed ladder, and it is not arbitrary — each step is something the next step needs:

| wave | what | why here |
|---|---|---|
| -2 | Namespaces | everything else lives in them |
| -1 | OperatorGroup | must exist before the Subscription resolves |
| 0 | Subscription, **approver** + its RBAC | the approver shares the Subscription's wave, per 3.2 |
| 1 | Secrets, trusted-CA ConfigMap, verify RBAC, ca-pull RBAC | prerequisites of the Jobs above them |
| 2 | ca-pull Job | writes the CA Secret the issuer may reference |
| 3 | verify Job | the five-stage readiness gate |
| 4 | ClusterIssuer | needs the CRDs and the webhook the gate waited for |

Deletes run in reverse, which is the property that matters on uninstall: the ClusterIssuer goes before the
Subscription, so the operator is still alive while its own resource is removed.

### 3.4 The ClusterIssuer needs `SkipDryRunOnMissingResource=true` under ArgoCD

Helm hook ordering does not apply under ArgoCD; Argo dry-runs resources instead, and a dry-run against a
kind the cluster does not serve yet **fails the whole sync**, not just that document. These CRDs arrive
mid-sync from OLM. Per the ArgoCD docs the dry run still runs once the CRD is present, so this costs no
validation on any later sync.

### 3.5 Hook resources that should be ordinary

`verify-rbac.yaml` (SA + ClusterRole + ClusterRoleBinding) and the ca-pull RBAC are Helm **hooks**. Helm
*creates* hook resources rather than applying them and never records them in the release, so they survive
`helm uninstall`, are never reconciled on upgrade, and accumulate. The sibling NCO chart hit exactly this
with a namespace-as-a-hook. They should be ordinary resources at wave 1.

### 3.6 Swallowed reads in the verify Job

```
csv=$(oc get subscription "${SUBSCRIPTION}" … 2>/dev/null || true)
phase=$(oc get csv "${csv}" … 2>/dev/null || true)
count=$(oc get deploy -n "${OPERAND_NS}" -o name 2>/dev/null | wc -l)
```

A Forbidden, a NotFound and a genuinely-absent field are indistinguishable. The Job then spins its whole
budget and reports `TIMEOUT: no installedCSV`, which points at OLM when the cause was RBAC. Fix: keep the
exit status, branch, and name the likely cause with the command to check.

### 3.7 Short resource names

`oc get subscription`, `csv`, `deploy`, `crd` in the verify Job, and 7 `clusterissuer` uses across the
scripts and docs. A resource plural is not reserved: measured in the sibling chart, `oc get subscription`
bound to `subscriptions.messaging.knative.dev` and returned Forbidden. These Jobs set `KUBECACHEDIR` to a
fresh `emptyDir`, so the discovery cache is **cold on every run** and the binding is not stable between two
runs of the same Job. A misrouted *list* returns nothing rather than failing — which for
`oc get deploy -n cert-manager` would read as "no operand deployments yet" forever.

### 3.8 EVERY Job needs an ArgoCD hook annotation, not just the approver

Caught in review of this plan: an earlier draft specified `argocd.argoproj.io/hook: Sync` only for the
approver. The verify Job and the ca-pull Job need it too, and for a different reason than ordering.

A `helm.sh/hook` Job with **no** `argocd.argoproj.io/hook` is not a hook to ArgoCD at all — it is an ordinary
resource. Argo then applies it on every sync, and a Job's `spec.template` is **immutable**, so the second
sync fails with `field is immutable`. The Helm annotations do nothing here; Argo does not read them.

So all three Jobs get:

```yaml
argocd.argoproj.io/hook: Sync                                    # runs inside the sync, at its wave
argocd.argoproj.io/hook-delete-policy: BeforeHookCreation,HookSucceeded
```

`BeforeHookCreation` is what makes the immutability moot — the previous run is deleted before the next is
created. `HookSucceeded` keeps a failed run's pod log for reading, which for the verify Job is the whole
point of having it.

And deliberately NOT `Replace=true` or `SkipDryRunOnMissingResource=true` on the Jobs: both are valid
options that do nothing for a `batch/v1` Job. The kind is built in, so there is no missing resource to skip
a dry run for; and `BeforeHookCreation` already removes the previous object, so `kubectl replace` never
applies. The sibling group-sync chart carries that pair on its Jobs and it is cargo — it is not copied here.

`SkipDryRunOnMissingResource=true` DOES belong on the ClusterIssuer, per 3.4 — that one is a CRD-backed kind
which genuinely may not be served yet.

### 3.9 `[ count -ge 3 ]`

A magic number that matches today's three deployments (`cert-manager`, `cert-manager-cainjector`,
`cert-manager-webhook`, all 1/1). A version shipping four would satisfy it after three; one shipping two
would hang until timeout. Derive the expectation from the CSV's own `.spec.install.spec.deployments` instead,
so the chart asks the operator what it installs.

### 3.10 No `required` on the identifiers the verify Job depends on

`required` appears only in the two secret templates. `NAMESPACE`, `SUBSCRIPTION` and `OPERAND_NS` are passed
to the verify Job from values with no guard, and a misspelled values key **renders as an empty string and
does not fail**. An empty identifier makes every query match nothing, so the Job burns its whole budget and
reports `TIMEOUT: no installedCSV` — pointing at OLM for what is a typo.

This is not hypothetical. The sibling NCO chart shipped exactly this: a readiness Job read
`.Values.subscription.name`, which does not exist (the key is `packageName`), and the run reported
`no .* CSV reached Succeeded` after five minutes. Five minutes and a wrong diagnosis for a typo.

Fix: `required` on every identifier baked into a script, so the same mistake is a render-time error naming
the key instead of a runtime timeout blaming something else.

### 3.11 The ca-pull Job's deadline is hardcoded, the verify Job's is derived

`capull-job.yaml` has `activeDeadlineSeconds: 300` as a literal, while `verify-job.yaml` correctly uses
`add .Values.verifyJob.timeoutSeconds 120`. If anyone raises the CA-pull budget in values, the kubelet still
kills the pod at 300s — part-way, with no message of its own, which is the least debuggable way for a Job to
fail. Derive it the same way the verify Job does.

## 4. Not in scope

- The `venafi-certificate-sample` chart.
- `scripts/*.sh` beyond qualifying resource names — they are operator-run helpers, not part of the release.
- The ClusterIssuer currently reporting `False / ErrorSetup` on the cluster. **Confirmed acceptable by the
  operator** — there is no live TPP issuer backing this lab release, so `ErrorSetup` is the expected state
  and not a symptom of anything in this plan. Recorded here so a future reader does not mistake it for a
  regression introduced by these changes, and so the verification in section 5 does not treat it as a
  failure to fix.

## 5. How each change gets validated

| change | validation |
|---|---|
| approval gate | render with the pin; run the approver script against the live Subscription; synthetic unapproved InstallPlan to exercise the patch path; confirm a non-pinned plan is reported and refused |
| wave ladder | a `check-ordering.py` equivalent asserting every document declares a wave, the approver shares the Subscription's wave and is not `PostSync`, the gate precedes the ClusterIssuer, and each Job's RBAC is no later than the Job — each rule negative-tested by breaking the chart |
| qualified names | a `qualified-resources.py` equivalent over the render and the scripts, negative-tested |
| verify Job hardening | run the extracted script against the live cluster; force a Forbidden by running as a stripped SA and confirm it fails loudly instead of timing out |
| no regression | `helm lint`, render every values combination, and a real `helm upgrade` on the cluster with the release's ClusterIssuer and operand state compared before/after |

There is no CI in this repo at all, so the two check scripts above are also the first CI it gets.

Process rules carried from today, which are about how the validation is done rather than what is validated:

- **Run the shipped artifact, not a paraphrase.** Extract the script from the *rendered* ConfigMap or Job and
  run that. A hand-retyped approximation tests the retyping.
- **Negative-test every new check.** A check that cannot fail is worth nothing; break the chart, confirm the
  error names the line and the fix, restore.
- **Enumerate which changed paths did NOT execute.** On the group-sync change, the upgrade looked like full
  coverage until the paths were counted: 12 of 47 changed lines were in test scripts that only run under
  `helm test`, and the relabel write path had executed zero times because it found nothing to fix. Both were
  then exercised deliberately. State the gap rather than letting a green upgrade imply coverage.
- **Compare UIDs, not counts.** A delete-and-recreate leaves counts identical. This is how a sweeper bug
  survived its own verification: 42 objects before, 42 after.
- **`oc auth can-i` as the actual ServiceAccount**, not as yourself. And pass arguments explicitly — `oc auth
  can-i ${probe}` in zsh sends the whole string as one argument and reports a false failure.
- **A cancelled `helm --wait` is not a failed install.** It records the revision as
  `failed: context canceled` while the Kubernetes work runs to completion, so re-run the upgrade to make the
  release record reality rather than assuming something broke.

## 6. Lessons from today's two charts, audited against this one

The point of this section is that it is not a wish list — each row was checked against the actual templates,
and three of the hardest-won lessons are **already implemented here**. Those are left alone.

| lesson | came from | status in this chart |
|---|---|---|
| ONE shared deadline for all stages, not one per stage | NCO readiness wait, where three loops could each take the full budget and reach 3x the documented number | **already correct** — one `deadline=$(( now + TIMEOUT ))`, used by all five stages |
| `activeDeadlineSeconds` above the script's own budget, so the script's message wins over a silent kubelet kill | NCO readiness wait | **already correct** in verify (`timeoutSeconds + 120`); **to fix** in ca-pull, which hardcodes 300 (3.11) |
| Staged progress logging, so silence is not read as a hang | the orphan sweeper, whose 74 silent seconds were read as "it only lists, never deletes" | **already correct** — `1/5` … `5/5` with per-stage detail |
| Approver shares the Subscription's wave, `hook: Sync` not `PostSync` | NCO, where wave 1 + PostSync deadlocked a first ArgoCD sync | **to apply** (3.2) |
| `SkipDryRunOnMissingResource=true` on a CR whose CRD arrives mid-sync | NCO policy CRs | **to apply** on the ClusterIssuer (3.4) |
| Every document declares a sync-wave; absent is not neutral | NCO check-ordering.py | **to apply** — 0 of 16 today (3.3) |
| Every Job carries an ArgoCD hook annotation, or Argo treats it as an ordinary resource and the second sync fails on the immutable `spec.template` | reviewing this plan | **to apply** to all three Jobs (3.8) |
| Hook resources are *created*, never recorded, so they survive uninstall and never reconcile | NCO namespace-as-a-hook | **to apply** — verify RBAC and ca-pull RBAC (3.5) |
| An empty read is not a negative answer; keep the exit status and branch | NCO approver reporting a successful no-op over a Forbidden | **to apply** (3.6) |
| Forbidden-as-not-found is the recurring killer | the 0.19.1 sweeper deleted all 42 RoleBindings it managed because `! oc get role` is true for Forbidden as well as absent | **to apply** — same shape in the verify Job's reads (3.6) |
| `required` on identifiers baked into scripts | NCO reading a values key that does not exist and blaming OLM | **to apply** (3.10) |
| Name every resource in full; a plural is not reserved | `oc get subscription` binding to `subscriptions.messaging.knative.dev` | **to apply** — and this chart is more exposed, because `KUBECACHEDIR` is a fresh `emptyDir` so discovery is cold every run (3.7) |
| A misrouted **list** returns nothing rather than failing | group-sync relabel, whose decisions are list-driven | **to apply** — `oc get deploy -n cert-manager` would read as "no operands yet" forever (3.7) |
| Derive expectations from the source of truth, not a magic number | NCO deriving the operand deployment from the CSV | **to apply** — `[ count -ge 3 ]` (3.9) |
| No arbitrary cap on work that must finish; pace and retry instead | the sweeper refusing 429 deletions | **not applicable** — nothing here deletes in bulk |
| Counters in a `printf \| while read` pipeline are lost to the subshell | the sweeper reporting success while every delete failed | **not applicable** — checked; no accumulating counter runs in a pipeline here |
| `$( )` strips trailing newlines, so `wc -l` counts separators and `while read` drops the last line | the sweeper printing 4 live CRs where 5 existed | **not applicable** — the one `wc -l` is on a live pipeline, not a captured string |
| `crds/` is the only Helm mechanism early enough for an *ordinary* CR | NCO first install | **not applicable, and validated as such** — see section 2. The CR here is a hook behind a gate, and Helm re-discovers per hook. |
| Under ArgoCD, `crds/` is rendered by `--include-crds`, so set `skipCrds: true` | group-sync vendored CRD | **not applicable** — no `crds/` here, which is precisely why not vendoring is the better answer |
