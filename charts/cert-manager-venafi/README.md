# cert-manager-venafi

Installs the **cert-manager Operator for Red Hat OpenShift**, injects the trusted
CA, provisions the TPP secrets, and creates a shared **Venafi TPP `ClusterIssuer`**
that tenants request certificates from.

A single `helm install` is **self-gating**: a post-install hook Job waits for the
operator to be fully installed (CSV `Succeeded`, operands `Available`, CRDs
`Established`, and the **validating webhook serving admission**) before the
`ClusterIssuer` is created. See the
[top-level README](https://github.com/ephico2real2/venafi-cert-manager-helm/blob/main/charts/README.md) for the full install + validation walkthrough.

- **Type:** application · **Scope:** cluster (operator + `ClusterIssuer`)
- **Requires:** OpenShift 4.x + OLM, `cluster-admin`, Helm 3 (Red Hat path)
- **Also runs on upstream cert-manager** — turn the operator/injection pieces off
  and let the CA-pull Job establish TPP trust from the endpoint. See
  [Using with upstream cert-manager](https://github.com/ephico2real2/venafi-cert-manager-helm/blob/main/charts/README.md#using-with-upstream-cert-manager).

---

<!-- Links to anything outside this directory are ABSOLUTE on purpose. This README ships INSIDE the
     packaged chart (helm pull --untar puts it at the chart root), where ../ and ../../ resolve to nothing —
     a relative link that works on GitHub is dead for everyone who pulled the chart. -->

## Install from the chart repository

```bash
helm repo add cert-manager-venafi https://ephico2real2.github.io/venafi-cert-manager-helm
helm repo update
helm search repo cert-manager-venafi
helm install cert-manager-venafi cert-manager-venafi/cert-manager-venafi -n default
```

Published by `.github/workflows/helm.yaml` on every change under `charts/`. Note chart-releaser publishes
only charts that **changed** since the last tag, so a chart that never changes never appears in the index.

To download and read the chart before installing it — or to move it into an air-gapped cluster — see
[`docs/HELM_DOWNLOAD_AND_INSTALL.md`](https://github.com/ephico2real2/venafi-cert-manager-helm/blob/main/docs/HELM_DOWNLOAD_AND_INSTALL.md).

## Quick start

```bash
# 1. Provide the TPP credentials (existing secret, or let the chart create it)
oc -n cert-manager create secret generic tpp-secret \
  --from-literal=username='<AD_SERVICE_ACCOUNT>' \
  --from-literal=password='<AD_SERVICE_ACCOUNT_PASSWORD>'

# 2. Install (timeout must exceed verifyJob.timeoutSeconds)
helm install cert-manager-venafi . --timeout 12m

# 3. Confirm
oc get csv -n cert-manager-operator
oc get clusterissuer robocorp-tpp-venafi-issuer-rnd -o wide   # READY=True
```

---

## Resources this chart manages

| Resource | Kind | Created when |
|----------|------|--------------|
| Operator + operand namespaces | `Namespace` | `namespaces.create=true` |
| Trusted-CA ConfigMap | `ConfigMap` | `trustedCA.enabled=true` |
| Operator install | `OperatorGroup` + `Subscription` | `operator.enabled=true` |
| TPP credentials | `Secret` (`data`, base64) | `tppCredentials.create=true` |
| TPP CA bundle | `Secret` (`stringData`, PEM) | `tppCABundle.create=true` |
| CA-pull hook | `Job` (+ ordinary `Role`/`RoleBinding`/`SA`) | `caBundleSecretRef.enabled` + `caPullJob.enabled` |
| Orphaned-CSV reclaim | `Job` (+ ordinary `Role`/`RoleBinding`/`SA`) | `operator.enabled` + `csvReclaim.enabled` |
| InstallPlan approver | `Job` (+ ordinary `Role`/`RoleBinding`/`SA`) | `operator.enabled` + `installPlanApprover.enabled` |
| Verify hook | `Job` (+ ordinary `ClusterRole`/`Binding`/`SA`) | `verifyJob.enabled=true` |
| Venafi issuer | `ClusterIssuer` | `clusterIssuer.enabled=true` |

### Ordering

Every Job's RBAC is an **ordinary resource**, not a hook — Helm never records hook resources in the release,
so hooked RBAC survives `helm uninstall` and is never reconciled on upgrade. Ordinary resources are also
applied before any hook runs, which is exactly what a hook Job needs from its own ServiceAccount.

Post-install hook order (by `helm.sh/hook-weight`):

**CSV reclaim `-6`** → **InstallPlan approver `-5`** → **CA-pull Job `-4`** → **Verify Job `5`** →
**ClusterIssuer `10`**

The reclaim runs first because an orphaned CSV makes the Subscription unresolvable, and OLM then stages no
InstallPlan at all — so an approver ahead of it would wait out its whole budget for something that cannot
arrive.

Under ArgoCD the weights are inert and `argocd.argoproj.io/sync-wave` does the ordering instead:

| wave | what |
|---|---|
| `-2` | Namespaces, CA-pull RBAC |
| `-1` | trusted-CA ConfigMap, CA-pull Job, `OperatorGroup`, reclaim RBAC |
| `0` | `Subscription`, reclaim Job, approver Job + their RBAC |
| `1` | verify RBAC, TPP secrets |
| `3` | verify Job |
| `4` | `ClusterIssuer` |

The approver shares the Subscription's wave deliberately: with `installPlanApproval: Manual` a Subscription
reports `Progressing` until its InstallPlan is approved, and ArgoCD's health check forgives that only when
`.status.installedCSV` is already set — an upgrade. On a first install an approver in any later wave waits on
the wave that is waiting on it.

Every Job also carries `argocd.argoproj.io/hook: Sync`. Without it ArgoCD treats a Helm-hook Job as an
ordinary resource, re-applies it every sync, and the **second** sync fails on the immutable `spec.template`.

---

## Parameters

### Namespaces

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `namespaces.create` | bool | `true` | Create the operator + operand namespaces. Set `false` if pre-created (e.g. GitOps). |
| `namespaces.operator` | string | `cert-manager-operator` | Namespace hosting the OLM operator. |
| `namespaces.operand` | string | `cert-manager` | Namespace hosting the operands + referenced secrets. |

| `namespaces.protectOnUninstall` | bool | `true` | Stamp `helm.sh/resource-policy: keep` on both Namespaces, so `helm uninstall` leaves them behind. ON by default because this chart CREATES them and other things end up living in them — deleting a namespace takes everything inside it. Measured on a lab cluster: the operand namespace held the root CA Secret of an unrelated LDAP TLS chain. Helm reads this from the STORED release manifest, so annotating a namespace by hand after the fact does nothing. |

### Trusted CA injection

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `trustedCA.enabled` | bool | `true` | Create the `inject-trusted-cabundle` ConfigMap and wire `TRUSTED_CA_CONFIGMAP_NAME` into the Subscription. |
| `trustedCA.configMapName` | string | `trusted-ca` | Name of the injected-CA ConfigMap. |

### Operator (OLM Subscription + OperatorGroup)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `operator.enabled` | bool | `true` | Install the operator (OperatorGroup + Subscription). |
| `operator.channel` | string | `stable-v1` | Subscription channel. |
| `operator.packageName` | string | `openshift-cert-manager-operator` | OLM package name. |
| `operator.source` | string | `redhat-operators` | CatalogSource name. |
| `operator.sourceNamespace` | string | `openshift-marketplace` | CatalogSource namespace. |
| `operator.installPlanApproval` | string | `Manual` | `Manual` pairs with `startingCSV` and the approver below: the pinned version installs unattended, anything else waits for a person. `Automatic` lets OLM install every InstallPlan the channel produces — on a rolling channel like `stable-v1` that means an unreviewed operator upgrade. |
| `operator.startingCSV` | string | `cert-manager-operator.v1.19.1` | The pinned version, and the approver's authority — it approves an InstallPlan only if it installs exactly this CSV. Note the CSV name does not begin with `packageName`. To move version, bump this and upgrade. |
| `operator.upgradeStrategy` | string | `Default` | OperatorGroup upgrade strategy. |

### TPP credentials secret (authentication)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `tppCredentials.create` | bool | `false` | `false` = reference an existing secret; `true` = render from base64 values. |
| `tppCredentials.secretName` | string | `tpp-secret` | Secret name (keys must be `username` / `password`). |
| `tppCredentials.usernameBase64` | string | `""` | Base64 username. Required when `create=true`. |
| `tppCredentials.passwordBase64` | string | `""` | Base64 password. Required when `create=true`. |

> Encode with `printf '%s' '<value>' | base64`. The secret is written as `data`
> (not `stringData`) because there is no secret-manager wiring.

### TPP CA bundle secret (certs)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `tppCABundle.create` | bool | `false` | Create the CA-bundle secret from a provided PEM. Mutually exclusive with the CA-pull Job. |
| `tppCABundle.secretName` | string | `robocorp-bundle-cert-tpp-ca` | CA-bundle secret name. |
| `tppCABundle.key` | string | `ca.crt` | Key holding the PEM. |
| `tppCABundle.caBundle` | string | `""` | PEM (intermediate + root). Required when `create=true`; use `--set-file`. |

### CA-pull Job

Gated by `clusterIssuer.venafi.tpp.caBundleSecretRef.enabled` **and**
`caPullJob.enabled`. Pulls the endpoint chain, strips the leaf, and writes the
CA-bundle secret before the issuer is created.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `caPullJob.enabled` | bool | `true` | CA source selector: `true` = pull from endpoint; `false` = use `tppCABundle.create`/out-of-band. |
| `caPullJob.image` | string | `registry.redhat.io/openshift4/ose-cli:latest` | Image with `openssl` + `oc`. |
| `caPullJob.imagePullPolicy` | string | `IfNotPresent` | Image pull policy. |
| `caPullJob.host` | string | `vta-qa.robocorpint.net` | Host to pull the chain from. |
| `caPullJob.port` | int | `443` | Port to pull from. |
| `caPullJob.backoffLimit` | int | `2` | Job retry budget. |
| `caPullJob.rbac.create` | bool | `true` | Create the secret-writer `Role`/`RoleBinding`/`SA`. |
| `caPullJob.resources` | object | `128Mi–512Mi` mem | Pod resources (512Mi limit avoids the `oc apply` OOMKill). |

### Orphaned-CSV reclaim

`helm uninstall` removes the Subscription but OLM leaves the CSV behind with no `ownerReferences`, so nothing
garbage-collects it. The next install cannot resolve — `ResolutionFailed: constraints not satisfiable:
@existing/...` — and **no InstallPlan is ever staged**, so the approver would wait out its whole budget for
something that cannot arrive. Under GitOps there is no human in the sync loop to run `oc delete
clusterserviceversions...`, so the Subscription would fail every sync forever.

Deletes only on all of: the Subscription reports `ResolutionFailed`; the CSV is in the operator namespace;
its name matches the package; it has no `ownerReferences`; no Subscription references it as `installedCSV`;
it is **not** an OLM copy (`olm.copiedFrom` empty — AllNamespaces mode copies the CSV into every namespace and
every copy looks unowned); and its phase is **settled** (`Succeeded` or `Failed`) — a CSV mid-install matches
every other test, and deleting it would tear out work in progress.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `csvReclaim.enabled` | bool | `true` | Run the reclaim hook (weight `-6`, before the approver). |
| `csvReclaim.image` | string | `registry.redhat.io/openshift4/ose-cli:latest` | Image providing `oc`. |
| `csvReclaim.imagePullPolicy` | string | `IfNotPresent` | |
| `csvReclaim.waitSeconds` | int | `120` | How long to wait for OLM to publish a verdict (`installedCSV` or `ResolutionFailed`) before deciding. Only entered when a candidate orphan actually exists — on a clean cluster the Job exits in seconds. |
| `csvReclaim.resources` | object | `64Mi–128Mi` mem | Pod resources. |

### InstallPlan approver

Approves **only** the InstallPlan that installs `operator.startingCSV`, so a `Manual`-approval Subscription
still installs unattended while a channel upgrade waits for a human. Without it, `Manual` blocks the first
install too — OLM stages an unapproved InstallPlan and waits.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `installPlanApprover.enabled` | bool | `true` | Run the approver hook (weight `-5`). Disable only if something else approves InstallPlans; with `installPlanApproval: Manual` and no approver, the first install hangs and the verify gate times out on stage 1 (which says so). |
| `installPlanApprover.image` | string | `registry.redhat.io/openshift4/ose-cli:latest` | Image providing `oc`. |
| `installPlanApprover.imagePullPolicy` | string | `IfNotPresent` | |
| `installPlanApprover.waitSeconds` | int | `600` | One budget for both waits — for OLM to stage a plan, and for the CSV to reach `Succeeded` afterwards. |
| `installPlanApprover.backoffLimit` | int | `0` | Every attempt polls the same state, so a retry re-waits from zero with no new information. |
| `installPlanApprover.ttlSecondsAfterFinished` | int | `3600` | |
| `installPlanApprover.resources` | object | `64Mi–128Mi` mem | Pod resources. |

### Verify Job

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `verifyJob.enabled` | bool | `true` | Gate the `ClusterIssuer` on operator readiness. `false` = plain issuer (manual phasing). |
| `verifyJob.image` | string | `registry.redhat.io/openshift4/ose-cli:latest` | Image with `oc`. |
| `verifyJob.imagePullPolicy` | string | `IfNotPresent` | Image pull policy. |
| `verifyJob.timeoutSeconds` | int | `600` | Wait budget; on exceed the release fails and no issuer is created. |
| `verifyJob.backoffLimit` | int | `1` | Job retry budget. |
| `verifyJob.rbac.create` | bool | `true` | Create the read-only `ClusterRole`/`Binding`/`SA`. |
| `verifyJob.resources` | object | `128Mi–512Mi` mem | Pod resources (the stage-5 webhook probe runs `oc create --dry-run=server`; 512Mi avoids an OOMKill). |
| `verifyJob.checkCredentialsSecret` | bool | `true` | Stage 6: confirm the pre-created TPP credentials Secret exists and carries `username`/`password` before the ClusterIssuer is created. Only applies when `tppCredentials.create=false`. Turn OFF when the Secret arrives later than this Job — an ExternalSecret or SealedSecret a controller reconciles, or a CSI-projected credential; the check would fail an install that is fine. Turning it off also drops the `resourceNames`-scoped `get secrets` rule from the verify ClusterRole. |

### ClusterIssuer

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `clusterIssuer.enabled` | bool | `true` | Create the `ClusterIssuer`. |
| `clusterIssuer.name` | string | `robocorp-tpp-venafi-issuer-rnd` | Issuer name (tenants reference this). |
| `clusterIssuer.venafi.zone` | string | `\VED\Policy\Testing\ARO` | TPP policy folder (zone). |
| `clusterIssuer.venafi.tpp.url` | string | `https://vta-qa.robocorpint.net/vedsdk` | TPP endpoint URL. |
| `clusterIssuer.venafi.tpp.caBundleSecretRef.enabled` | bool | `false` | Master switch for the per-issuer CA bundle **and** the CA-pull Job. Off = trust via injected CA. |

---

## Notes

- **installPlanApproval** is parameterized (`operator.installPlanApproval`). With
  `Manual`, approve the pending plan:
  `oc patch installplan <name> -n cert-manager-operator --type merge -p '{"spec":{"approved":true}}'`.
- **Hook lifecycle:** with `verifyJob.enabled=true` the `ClusterIssuer` is a hook.
  `helm uninstall` removes it, but disabling via
  `helm upgrade --set clusterIssuer.enabled=false` orphans it — delete manually.
- **Job images** default to the Red Hat `ose-cli` image; override
  `verifyJob.image` / `caPullJob.image` for a mirror or air-gapped registry.

---

## References

- Design reasoning & the Red-Hat-vs-upstream narrative: [`steps.md`](https://github.com/ephico2real2/venafi-cert-manager-helm/blob/main/steps.md)
- Walkthrough (install + validate): [top-level charts README](https://github.com/ephico2real2/venafi-cert-manager-helm/blob/main/charts/README.md)
- **Red Hat** — Installing the cert-manager Operator (CLI):
  <https://docs.redhat.com/en/documentation/openshift_container_platform/4.20/html/security_and_compliance/cert-manager-operator-for-red-hat-openshift#cert-manager-install-cli_cert-manager-operator-install>
- **Red Hat** — Injecting a custom CA certificate (trusted-CA injection, § 9.4.1
  under "Configuring the egress proxy"): same guide.
- **Upstream cert-manager** — Venafi issuer configuration:
  <https://cert-manager.io/v1.6-docs/configuration/venafi/>
