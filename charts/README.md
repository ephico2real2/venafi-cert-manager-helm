# cert-manager + Venafi (TPP) on OpenShift — Helm charts

Turn the manual runbook into two repeatable Helm releases: one that
installs and wires the **cert-manager operator + Venafi TPP `ClusterIssuer`**,
and one that **validates** the whole path by requesting a real certificate.

| Chart | What it does | Who runs it |
|-------|--------------|-------------|
| 📦 [`cert-manager-venafi`](./cert-manager-venafi) | Installs the cert-manager operator, injects the trusted CA, provisions the TPP secrets, and creates the shared Venafi `ClusterIssuer`. | Platform / cluster admin |
| ✅ [`venafi-certificate-sample`](./venafi-certificate-sample) | A tenant `Certificate` that requests a cert from the shared `ClusterIssuer` — used to **prove the integration works**. | App tenant / validation |

Two charts because the operator + issuer are cluster-scoped, admin-owned
infrastructure, while a tenant only ever manages a namespaced `Certificate`.

---

## Contents

- [Prerequisites](#prerequisites)
- [How the install works (the gate)](#how-the-install-works-the-gate)
- [Part 1 — Install the integration chart](#part-1--install-the-integration-chart)
  - [Step 1 · TPP credentials secret](#step-1--tpp-credentials-secret)
  - [Step 2 · TPP CA bundle (optional)](#step-2--tpp-ca-bundle-optional)
  - [Step 3 · Install](#step-3--install)
  - [Step 4 · Verify the operator + issuer](#step-4--verify-the-operator--issuer)
  - [`installPlanApproval` (Automatic / Manual)](#installplanapproval-automatic--manual)
- [Part 2 — Validate with the sample chart](#part-2--validate-with-the-sample-chart)
- [Using with upstream cert-manager](#using-with-upstream-cert-manager)
- [Configuration reference](#configuration-reference)
- [Container images used by the Jobs](#container-images-used-by-the-jobs)
- [Uninstall](#uninstall)
- [Appendix](#appendix)

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| OpenShift 4.x cluster | The `Subscription`/`OperatorGroup` need OLM (any OpenShift/OKD/CRC). |
| `cluster-admin` | Installs a cluster-scoped operator + `ClusterIssuer`. |
| `helm` 3 and `oc` | Client tools on your machine. |
| Catalog reachable | The `redhat-operators` catalog source must serve `openshift-cert-manager-operator`. |
| Job image pullable | The Jobs use `registry.redhat.io/openshift4/ose-cli` — pulls on entitled clusters, or override it (see [images](#container-images-used-by-the-jobs)). |

> All `helm`/`oc` commands below are written to run from the **repo root**.

---

## How the install works (the gate)

The `ClusterIssuer` depends on the `cert-manager.io` CRDs, which don't exist
until the operator finishes installing. Helm applies all normal resources in one
pass with **no readiness barrier**, so the chart uses **post-install hooks** to
enforce the order — a single `helm install` is self-gating, no manual phasing.

```text
helm install cert-manager-venafi
      │
      ├─ (main resources)  Namespaces · trusted-CA ConfigMap · OperatorGroup
      │                    Subscription · TPP secrets
      │
      └─ (post-install hooks, run in weight order)
           weight 0   Verify RBAC
           weight 1   CA-pull RBAC   ┐ only when the CA bundle is enabled
           weight 3   CA-pull Job  ──┘ pulls the CA chain → writes the secret
           weight 5   Verify Job   ─── waits: CSV Succeeded · pods Available · CRDs Established
           weight 10  ClusterIssuer ── created ONLY after the Verify Job passes
```

If the Verify Job exceeds `verifyJob.timeoutSeconds` (default 600s) it exits
non-zero, **the release fails, and the ClusterIssuer is never created**.

---

## Part 1 — Install the integration chart

Path: [`charts/cert-manager-venafi`](./cert-manager-venafi)

### Step 1 · TPP credentials secret

The Venafi issuer authenticates to TPP with a secret whose keys are exactly
`username` and `password`. There's no secret-manager integration, so pick one:

**Mode A — bring your own secret (default).** Pre-create it, and the chart just
references `tppCredentials.secretName`:

```bash
oc -n cert-manager create secret generic tpp-secret \
  --from-literal=username='<AD_SERVICE_ACCOUNT>' \
  --from-literal=password='<AD_SERVICE_ACCOUNT_PASSWORD>'
```

...or as a manifest (values are base64, because the Secret uses `data`):

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: tpp-secret
  namespace: cert-manager
type: Opaque
data:
  username: <base64 of the AD service account>
  password: <base64 of the password>
```

**Mode B — let the chart create it** from pre-encoded base64 values
(`tppCredentials.create=true`):

```bash
--set tppCredentials.create=true \
--set tppCredentials.usernameBase64="$(printf '%s' 'svc-aro'  | base64)" \
--set tppCredentials.passwordBase64="$(printf '%s' 'S3cr3t!' | base64)"
```

> `create=true` requires **both** base64 values or the chart fails fast.

### Step 2 · TPP CA bundle (optional)

The issuer must trust the TPP endpoint's TLS cert. How you supply that trust is
the main difference between running on the **Red Hat operator** and on **upstream
cert-manager** — and it's why `caBundleSecretRef` exists:

- **Red Hat operator (default) — rely on the trusted-CA injection.** If the TPP CA
  is in the cluster-wide trust bundle (`trustedCA`), no per-issuer bundle is needed
  and the `ClusterIssuer` omits `caBundleSecretRef`. Nothing to do here.
- **Upstream cert-manager — pull the CA from the TPP endpoint.** Upstream has no
  `TRUSTED_CA_CONFIGMAP_NAME` injection, so set `caBundleSecretRef.enabled=true`:
  that one switch adds `caBundleSecretRef` to the issuer **and** runs the CA-pull
  Job (weight 3) that fetches the chain **directly from the TPP endpoint** and
  writes the secret before the issuer. (Also use this on the Red Hat operator when
  the TPP CA is outside the cluster trust.) See
  [Using with upstream cert-manager](#using-with-upstream-cert-manager).

  ```bash
  --set clusterIssuer.venafi.tpp.caBundleSecretRef.enabled=true
  ```

  ```bash
  --set clusterIssuer.venafi.tpp.caBundleSecretRef.enabled=true
  ```

  Choosing the source (only when the bundle is enabled):

  | You want | Set |
  |----------|-----|
  | Pull the CA from the endpoint (default) | `caPullJob.enabled=true` (+ `caPullJob.host/port`) |
  | Supply a local PEM | `caPullJob.enabled=false` `tppCABundle.create=true` `--set-file tppCABundle.caBundle=bundle.pem` |
  | Reference an out-of-band secret | `caPullJob.enabled=false` `tppCABundle.create=false` (pre-create it) |

> The chart **fails fast** if the CA-pull Job and `tppCABundle.create` are both
> active — they'd fight over the same secret. Pick one source.

### Step 3 · Install

```bash
helm install cert-manager-venafi charts/cert-manager-venafi --timeout 12m
```

> The `--timeout` must exceed `verifyJob.timeoutSeconds` (default 600s) so Helm
> waits for the gate instead of aborting mid-verification.

### Step 4 · Verify the operator + issuer

```bash
# Operator installed and running
oc get csv -n cert-manager-operator          # PHASE = Succeeded
oc get pods -n cert-manager                  # controller / webhook / cainjector Running

# Shared issuer is Ready
oc get clusterissuer robocorp-tpp-venafi-issuer-rnd -o wide
# Expect: READY=True, Reason="Venafi issuer started"
```

### `installPlanApproval` (Automatic / Manual)

The OLM `Subscription`'s approval mode is a value:

```bash
# Automatic (default) — hands-off upgrades within the channel
--set operator.installPlanApproval=Automatic

# Manual — an admin approves each InstallPlan (change-controlled envs)
--set operator.installPlanApproval=Manual
```

With `Manual`, approve the pending plan:

```bash
oc get installplan -n cert-manager-operator
oc patch installplan <name> -n cert-manager-operator \
  --type merge -p '{"spec":{"approved":true}}'
```

---

## Part 2 — Validate with the sample chart

Path: [`charts/venafi-certificate-sample`](./venafi-certificate-sample)

Once Part 1 shows the `ClusterIssuer` as `Ready=True`, prove the end-to-end path
by having a tenant request a real certificate. This chart creates **only** a
namespaced `Certificate` that references the shared issuer.

### Step 1 · Request the certificate

```bash
# namespace.create=true if the tenant namespace doesn't exist yet
helm install my-cert charts/venafi-certificate-sample \
  -n daa-aro-rnd --create-namespace
```

### Step 2 · Confirm issuance

```bash
# The Certificate should go Ready=True
oc -n daa-aro-rnd get certificate test-dojo-portal-rnd
# READY=True, with NOT BEFORE / NOT AFTER populated

# Watch the issuance flow (Requested → Issuing → Issued)
oc -n daa-aro-rnd describe certificate test-dojo-portal-rnd

# The resulting TLS secret holds tls.crt, tls.key, ca.crt
oc -n daa-aro-rnd get secret test-dojo-portal-rnd-tls

# Inspect the issued cert — confirm Issuer + CN/SANs
oc -n daa-aro-rnd get secret test-dojo-portal-rnd-tls \
  -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -noout -text
```

A `Ready=True` certificate with a populated TLS secret means the operator, the
CA trust, the TPP credentials, and the `ClusterIssuer` are all wired correctly.

> **Point the sample at your issuer/DNS** by overriding values, e.g.
> `--set issuerRef.name=<your-issuer>` and `--set certificate.commonName=<fqdn>`.

---

## Using with upstream cert-manager

The chart is **not Red-Hat-only**. It also runs against **upstream cert-manager**
(the community distribution). Upstream has no operator-level CA injection
(`TRUSTED_CA_CONFIGMAP_NAME`), so the **CA-pull Job** is what establishes TPP
trust — it fetches the CA **directly from the Venafi TPP endpoint** and wires it to
the issuer via `caBundleSecretRef`. That portability is the reason the switch
exists.

Install cert-manager yourself first, then:

```bash
helm install cert-manager-venafi charts/cert-manager-venafi \
  --set operator.enabled=false \
  --set trustedCA.enabled=false \
  --set verifyJob.enabled=false \
  --set clusterIssuer.venafi.tpp.caBundleSecretRef.enabled=true
```

| Toggle | Why |
|--------|-----|
| `operator.enabled=false` | You manage cert-manager (no OLM Subscription). |
| `trustedCA.enabled=false` | No `TRUSTED_CA_CONFIGMAP_NAME` injection upstream. |
| `verifyJob.enabled=false` | No OLM CSV to wait for; `ClusterIssuer` becomes a plain resource. |
| `caBundleSecretRef.enabled=true` | CA-pull Job fetches the TPP CA from the endpoint. |

> The CA-pull Job uses `oc` + `openssl`. On non-OpenShift clusters, override
> `caPullJob.image` with a `kubectl`+`openssl` image.

---

## Configuration reference

### `cert-manager-venafi` (key values)

| Value | Default | Purpose |
|-------|---------|---------|
| `namespaces.create` | `true` | Create the `cert-manager-operator` / `cert-manager` namespaces. |
| `trustedCA.enabled` | `true` | Create the inject-trusted-cabundle ConfigMap + wire it into the operator. |
| `operator.channel` | `stable-v1` | Subscription channel. |
| `operator.installPlanApproval` | `Automatic` | `Automatic` or `Manual`. |
| `tppCredentials.create` | `false` | `false` = existing secret; `true` = create from base64 values. |
| `tppCredentials.secretName` | `tpp-secret` | Name of the credentials secret. |
| `tppCredentials.usernameBase64` / `passwordBase64` | `""` | Base64 values, required when `create=true`. |
| `clusterIssuer.enabled` | `true` | Create the `ClusterIssuer`. |
| `clusterIssuer.name` | `robocorp-tpp-venafi-issuer-rnd` | Issuer name (tenants reference this). |
| `clusterIssuer.venafi.zone` | `\VED\Policy\Testing\ARO` | TPP policy folder. |
| `clusterIssuer.venafi.tpp.url` | `https://vta-qa.robocorpint.net/vedsdk` | TPP endpoint. |
| `clusterIssuer.venafi.tpp.caBundleSecretRef.enabled` | `false` | Master switch for the per-issuer CA bundle **and** the CA-pull Job. |
| `caPullJob.enabled` | `true` | CA source selector (pull vs PEM/out-of-band). |
| `caPullJob.host` / `port` | `vta-qa.robocorpint.net` / `443` | Endpoint the CA-pull Job pulls from. |
| `verifyJob.enabled` | `true` | Gate the issuer on operator readiness. `false` = manual phasing. |
| `verifyJob.timeoutSeconds` | `600` | Wait budget for the gate. |

### `venafi-certificate-sample` (key values)

| Value | Default | Purpose |
|-------|---------|---------|
| `namespace.name` | `daa-aro-rnd` | Tenant namespace. |
| `namespace.create` | `false` | Create the namespace (or use `--create-namespace`). |
| `issuerRef.name` | `robocorp-tpp-venafi-issuer-rnd` | Must match the issuer from Part 1. |
| `certificate.commonName` | `test-dojo-portal-rnd.robocorpint.net` | Cert CN. |
| `certificate.dnsNames` | *(two SANs)* | SAN list. |
| `certificate.duration` / `renewBefore` | `2160h` / `720h` | 90-day cert, renew 30 days early. |

Full, commented values live in each chart's `values.yaml`.

---

## Container images used by the Jobs

Both hook Jobs need the OpenShift CLI (the CA-pull Job also needs `openssl`,
which the same image provides). Yes — images are set in `values.yaml`:

| Job | Value | Default |
|-----|-------|---------|
| Verify Job | `verifyJob.image` | `registry.redhat.io/openshift4/ose-cli:latest` |
| CA-pull Job | `caPullJob.image` | `registry.redhat.io/openshift4/ose-cli:latest` |

Both default to `imagePullPolicy: IfNotPresent`. The Red Hat `ose-cli` image
pulls on any entitled OpenShift/CRC cluster via the global pull secret. Override
for a mirror or air-gapped registry:

```bash
--set verifyJob.image=<registry>/ose-cli:<tag> \
--set caPullJob.image=<registry>/ose-cli:<tag>
```

> The image must provide `oc` (Verify Job) and `oc` + `openssl` (CA-pull Job).

---

## Uninstall

```bash
helm uninstall my-cert -n daa-aro-rnd            # tenant certificate
helm uninstall cert-manager-venafi               # operator + issuer
```

> **Hook lifecycle caveat.** With `verifyJob.enabled=true` the `ClusterIssuer` is
> a Helm hook. `helm uninstall` **does** remove it, but disabling it via
> `helm upgrade --set clusterIssuer.enabled=false` orphans it — delete it
> explicitly (`oc delete clusterissuer <name>`) in that case. Uninstalling the
> operator release leaves the OLM CSV/CRDs; remove those manually if desired.

---

## Appendix

### Building the TPP CA bundle by hand (the runbook step)

The CA-pull Job runs this exact pipeline in-cluster; here it is for reference:

```bash
openssl s_client -connect vta-qa.robocorpint.net:443 -servername vta-qa.robocorpint.net \
  -showcerts < /dev/null 2>/dev/null \
  | sed -n '/-----BEGIN CERTIFICATE-----/,/-----END CERTIFICATE-----/p' > full-chain.pem
awk '/-----BEGIN CERTIFICATE-----/ {n++} n >= 2 {print}' full-chain.pem > robocorp-bundle-cert.pem
openssl crl2pkcs7 -nocrl -certfile robocorp-bundle-cert.pem | openssl pkcs7 -print_certs -noout
```

`n >= 2` skips the leaf/server cert and keeps only the intermediate + root CA.

### Corrections applied vs `steps.md`

- `Subscription.spec.config.env` was a single map in `steps.md`; the OLM
  `SubscriptionConfig` schema requires a **list** of env vars — the chart emits
  the correct list form.
- OpenShift `Project` objects render as `Namespace` (Helm-manageable, equivalent)
  while preserving the `openshift.io/cluster-monitoring` label on the operator
  namespace.
- The per-issuer `caBundleSecretRef` is **optional and off by default** — the
  operator's trusted-CA injection is expected to cover the TPP CA.

---

## References

The design reasoning — and how the **Red Hat operator** shaped it differently from
**upstream cert-manager** — is written up in [`steps.md`](../steps.md).

- **Red Hat** — Installing the cert-manager Operator for Red Hat OpenShift (CLI):
  <https://docs.redhat.com/en/documentation/openshift_container_platform/4.20/html/security_and_compliance/cert-manager-operator-for-red-hat-openshift#cert-manager-install-cli_cert-manager-operator-install>
- **Red Hat** — Injecting a custom CA certificate for the cert-manager Operator
  (the trusted-CA injection strategy; under "Configuring the egress proxy"): same
  guide, § 9.4.1.
- **Upstream cert-manager** — Venafi (VaaS + TPP) issuer configuration:
  <https://cert-manager.io/v1.6-docs/configuration/venafi/>
