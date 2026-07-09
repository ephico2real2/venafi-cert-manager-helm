# cert-manager-venafi

Installs the **cert-manager Operator for Red Hat OpenShift**, injects the trusted
CA, provisions the TPP secrets, and creates a shared **Venafi TPP `ClusterIssuer`**
that tenants request certificates from.

A single `helm install` is **self-gating**: a post-install hook Job waits for the
operator to be fully installed (CSV `Succeeded`, operands `Available`, CRDs
`Established`) before the `ClusterIssuer` is created. See the
[top-level README](../README.md) for the full install + validation walkthrough.

- **Type:** application · **Scope:** cluster (operator + `ClusterIssuer`)
- **Requires:** OpenShift 4.x + OLM, `cluster-admin`, Helm 3 (Red Hat path)
- **Also runs on upstream cert-manager** — turn the operator/injection pieces off
  and let the CA-pull Job establish TPP trust from the endpoint. See
  [Using with upstream cert-manager](../README.md#using-with-upstream-cert-manager).

---

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
| CA-pull hook | `Job` + `Role`/`RoleBinding`/`SA` | `caBundleSecretRef.enabled` + `caPullJob.enabled` |
| Verify hook | `Job` + `ClusterRole`/`Binding`/`SA` | `verifyJob.enabled=true` |
| Venafi issuer | `ClusterIssuer` | `clusterIssuer.enabled=true` |

Post-install hook order (by weight): verify RBAC `0` → CA-pull RBAC `1` →
**CA-pull Job `3`** → **Verify Job `5`** → **ClusterIssuer `10`**.

---

## Parameters

### Namespaces

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `namespaces.create` | bool | `true` | Create the operator + operand namespaces. Set `false` if pre-created (e.g. GitOps). |
| `namespaces.operator` | string | `cert-manager-operator` | Namespace hosting the OLM operator. |
| `namespaces.operand` | string | `cert-manager` | Namespace hosting the operands + referenced secrets. |

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
| `operator.installPlanApproval` | string | `Automatic` | `Automatic` (hands-off) or `Manual` (admin approves each InstallPlan). |
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

### Verify Job

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `verifyJob.enabled` | bool | `true` | Gate the `ClusterIssuer` on operator readiness. `false` = plain issuer (manual phasing). |
| `verifyJob.image` | string | `registry.redhat.io/openshift4/ose-cli:latest` | Image with `oc`. |
| `verifyJob.imagePullPolicy` | string | `IfNotPresent` | Image pull policy. |
| `verifyJob.timeoutSeconds` | int | `600` | Wait budget; on exceed the release fails and no issuer is created. |
| `verifyJob.backoffLimit` | int | `1` | Job retry budget. |
| `verifyJob.rbac.create` | bool | `true` | Create the read-only `ClusterRole`/`Binding`/`SA`. |
| `verifyJob.resources` | object | `64Mi–128Mi` mem | Pod resources. |

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

- Design reasoning & the Red-Hat-vs-upstream narrative: [`steps.md`](../../steps.md)
- Walkthrough (install + validate): [top-level charts README](../README.md)
- **Red Hat** — Installing the cert-manager Operator (CLI):
  <https://docs.redhat.com/en/documentation/openshift_container_platform/4.20/html/security_and_compliance/cert-manager-operator-for-red-hat-openshift#cert-manager-install-cli_cert-manager-operator-install>
- **Red Hat** — Injecting a custom CA certificate (trusted-CA injection, § 9.4.1
  under "Configuring the egress proxy"): same guide.
- **Upstream cert-manager** — Venafi issuer configuration:
  <https://cert-manager.io/v1.6-docs/configuration/venafi/>
