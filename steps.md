# cert-manager + Venafi (TPP) on OpenShift — design notes & runbook

This document is the **reasoning** behind the Helm charts in [`charts/`](charts/):
what each step does, *why* it's there, and how the **Red Hat cert-manager
Operator** shaped the design differently from the **upstream cert-manager** docs.
The charts are the implementation; this is the "why".

## Primary references

- Red Hat — cert-manager Operator for OpenShift (install + CA injection):
  <https://docs.redhat.com/en/documentation/openshift_container_platform/4.20/html/security_and_compliance/cert-manager-operator-for-red-hat-openshift#cert-manager-install-cli_cert-manager-operator-install>
- Upstream — cert-manager Venafi issuer configuration:
  <https://cert-manager.io/v1.6-docs/configuration/venafi/>

---

## The core idea: one chart, two cert-manager worlds

There are two ways to run cert-manager with Venafi, and **this chart supports
both**. On the **Red Hat Operator** it uses the operator's enterprise CA
injection; on **upstream cert-manager** (which has no such injection) it pulls the
TPP CA itself. The `caBundleSecretRef` switch is what makes that portability work.

| Concern | Upstream cert-manager (generic) | Red Hat Operator (enterprise) |
|---------|----------------------------------|--------------------------------|
| Install | Community chart / kubectl (install it yourself; set `operator.enabled=false`) | **OLM `Subscription`** from `redhat-operators` (`operator.enabled=true`) |
| Trusting a private CA | No cluster injection → **CA-pull Job fetches the TPP CA** and wires it via `caBundleSecretRef` | **Cluster-wide CA injection** via a labelled ConfigMap + `TRUSTED_CA_CONFIGMAP_NAME` |
| Where trust lives | On the `ClusterIssuer` (`caBundleSecretRef`) | **System trust store of the cert-manager controller** (cluster default) |
| Chart toggles | `operator.enabled=false`, `trustedCA.enabled=false`, `caBundleSecretRef.enabled=true` | operator + trustedCA on, `caBundleSecretRef.enabled=false` |

**Why `caBundleSecretRef` + the CA-pull Job exist (the portability insight).**
Trusting a private TPP CA differs sharply between the two worlds:

- **Red Hat Operator** — *enhanced to consume the cluster's own trust defaults*.
  You drop an *empty* ConfigMap labelled
  `config.openshift.io/inject-trusted-cabundle=true`; OpenShift's Cluster Network
  Operator fills it with the **cluster-wide trusted CA bundle** (the enterprise
  roots + proxy CAs the whole platform trusts), and the operator mounts it into
  the cert-manager controller's system trust at
  `/etc/pki/tls/certs/cert-manager-tls-ca-bundle.crt` via
  `TRUSTED_CA_CONFIGMAP_NAME`. Trust is a **cluster default**, so the per-issuer
  bundle is redundant → `caBundleSecretRef.enabled=false`.

- **Upstream cert-manager** — *no such injection exists*. `TRUSTED_CA_CONFIGMAP_NAME`
  is a Red-Hat-operator-only knob. So the chart makes TPP trust **self-contained**:
  set `caBundleSecretRef.enabled=true` and the **CA-pull Job fetches the CA chain
  directly from the Venafi TPP endpoint** (`openssl s_client`), writes it to a
  secret, and the `ClusterIssuer` references it via `caBundleSecretRef`. No
  dependency on any platform-specific CA machinery.

The upstream Venafi doc frames the same field: `caBundle` is *"...or empty to use
system root CAs."* Red Hat's injection puts the enterprise CA into those system
roots (so `caBundle` can be empty); upstream, the CA-pull Job supplies the bundle
explicitly. Same issuer contract, two trust sources — the chart does whichever the
platform calls for.

---

## Step 0 · Namespaces

Two namespaces, mirroring Red Hat's layout:

- `cert-manager-operator` — the OLM operator itself.
- `cert-manager` — the operands (controller, webhook, cainjector) **and** the
  `ClusterIssuer`'s referenced secrets. This is also cert-manager's default
  *cluster resource namespace*, where `ClusterIssuer` secrets must live.

The operator namespace opts into platform monitoring
(`openshift.io/cluster-monitoring: "true"`).

> Chart note: rendered as `Namespace` objects (Helm-manageable) rather than the
> OpenShift `Project` objects from the raw runbook — functionally equivalent, and
> the monitoring label is preserved.

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: cert-manager-operator
  labels:
    openshift.io/cluster-monitoring: "true"
---
apiVersion: v1
kind: Namespace
metadata:
  name: cert-manager
```

---

## Step 1 · Install the operator (OLM)

The enterprise path installs via OLM, not the community Helm chart. An
`OperatorGroup` scopes the operator; a `Subscription` subscribes it to a channel.

```yaml
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: cert-manager-operator
  namespace: cert-manager-operator
spec:
  upgradeStrategy: Default
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: openshift-cert-manager-operator
  namespace: cert-manager-operator
spec:
  channel: stable-v1
  installPlanApproval: Automatic     # parameterized in the chart
  name: openshift-cert-manager-operator
  source: redhat-operators
  sourceNamespace: openshift-marketplace
```

**Why `installPlanApproval` is a parameter.** `Automatic` lets OLM apply
InstallPlans as they appear (hands-off upgrades within the channel). Regulated
environments want `Manual` so an admin approves each InstallPlan — a genuine
policy choice, so the chart exposes it as `operator.installPlanApproval`.

Verify:

```bash
oc get subscription -n cert-manager-operator
oc get csv -n cert-manager-operator          # PHASE = Succeeded
oc get pods -n cert-manager                   # operands Running
```

---

## Step 2 · Inject the trusted CA (the enterprise enhancement)

This is the step that most distinguishes the Red Hat operator from upstream, and
it is **confirmed by the Red Hat docs** (§ "Injecting a custom CA certificate",
which sits under *"Configuring the egress proxy"* — i.e. it exists for clusters
with a cluster-wide proxy / private enterprise CAs).

```yaml
# An EMPTY config map — OpenShift populates ca-bundle.crt for us.
apiVersion: v1
kind: ConfigMap
metadata:
  name: trusted-ca
  namespace: cert-manager
  labels:
    config.openshift.io/inject-trusted-cabundle: "true"
```

Then the operator is told to consume it. The Red Hat doc does this by patching the
Subscription; the chart bakes the same `config.env` into the Subscription from
Step 1:

```yaml
spec:
  config:
    env:
      - name: TRUSTED_CA_CONFIGMAP_NAME
        value: trusted-ca
```

What happens under the hood (verified from the Red Hat docs' own verification
steps): OpenShift injects the cluster-wide bundle into `ca-bundle.crt`, and the
operator mounts it into the controller as a volume at
`/etc/pki/tls/certs/cert-manager-tls-ca-bundle.crt`. From that point cert-manager
trusts every CA the cluster trusts — **system-wide, no per-issuer config**.

> Design consequence: because the TPP CA is now in the system trust, the Venafi
> issuer's `caBundle` can be left empty. That's why Step 4's `caBundleSecretRef`
> is **off by default** on the Red Hat path.
>
> This whole step is **Red-Hat-operator-only**. On upstream cert-manager there is
> no `TRUSTED_CA_CONFIGMAP_NAME` injection — skip it (`trustedCA.enabled=false`)
> and use Step 3b's CA-pull Job instead (`caBundleSecretRef.enabled=true`).

---

## Step 3 · TPP secrets

The issuer needs credentials, and *optionally* a CA bundle.

### 3a · Credentials (required)

cert-manager's Venafi issuer authenticates to TPP with a secret. Our environment
uses `username` / `password` keys.

```bash
oc -n cert-manager create secret generic tpp-secret \
  --from-literal=username='<AD_SERVICE_ACCOUNT>' \
  --from-literal=password='<AD_SERVICE_ACCOUNT_PASSWORD>'
```

> **Upstream note.** cert-manager marks username/password as **deprecated for TPP
> ≥ 19.2**, recommending access-token auth (`--from-literal=access-token=…`)
> instead. We keep username/password to match the target TPP; switching to a token
> is a secret-key change only.
>
> Chart note: the chart writes this secret as **`data` (base64)**, not
> `stringData`, and can either create it (`tppCredentials.create=true`) or
> reference a pre-created one (default) — because there's no secret manager wired
> in and credentials are usually delivered out-of-band.

### 3b · CA bundle (the upstream path, or when cluster trust doesn't cover TPP)

Needed in two cases: you're on **upstream cert-manager** (no Step 2 injection), or
you're on the Red Hat operator but the **TPP CA isn't in the cluster trust**. In
both, the chart pulls the CA **directly from the Venafi TPP endpoint** and
references it via `caBundleSecretRef` — making TPP trust self-contained. The
runbook builds it by pulling the endpoint chain and keeping the intermediate +
root:

```bash
openssl s_client -connect vta-qa.robocorpint.net:443 -servername vta-qa.robocorpint.net \
  -showcerts < /dev/null 2>/dev/null \
  | sed -n '/-----BEGIN CERTIFICATE-----/,/-----END CERTIFICATE-----/p' > full-chain.pem
awk '/-----BEGIN CERTIFICATE-----/ {n++} n >= 2 {print}' full-chain.pem > robocorp-bundle-cert.pem
oc -n cert-manager create secret generic robocorp-bundle-cert-tpp-ca \
  --from-file=ca.crt=robocorp-bundle-cert.pem
```

`n >= 2` skips the leaf/server cert. The chart automates this exact pipeline with
an optional **CA-pull Job** (gated by the same `caBundleSecretRef.enabled` switch).

---

## Step 4 · The Venafi TPP ClusterIssuer

One shared, cluster-scoped issuer. Tenants reference it and never touch TPP
credentials. `caBundleSecretRef` is omitted by default (system trust covers it).

```yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: robocorp-tpp-venafi-issuer-rnd
spec:
  venafi:
    zone: \VED\Policy\Testing\ARO
    tpp:
      url: https://vta-qa.robocorpint.net/vedsdk
      # caBundleSecretRef omitted → trust via the injected cluster CA (Step 2).
      # Enable it only if the TPP CA is outside the cluster trust bundle.
      credentialsRef:
        name: tpp-secret
```

Verify:

```bash
oc get clusterissuer robocorp-tpp-venafi-issuer-rnd -o wide
# READY=True, Reason="Venafi issuer started"
```

> Chart note: on a fresh cluster the `cert-manager.io` CRDs don't exist until the
> operator finishes installing, so the chart creates the `ClusterIssuer` behind a
> **post-install verify Job** that waits for the operator to be ready. A single
> `helm install` is therefore self-gating — no manual two-phase apply.

---

## Step 5 · Validate — a tenant Certificate

The proof that the whole chain works. A tenant in `daa-aro-rnd` requests a cert by
creating a `Certificate` that references the shared `ClusterIssuer` — the only
object they manage.

```yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: test-dojo-portal-rnd
  namespace: daa-aro-rnd
spec:
  secretName: test-dojo-portal-rnd-tls
  duration: 2160h        # 90 days
  renewBefore: 720h      # renew 30 days early
  issuerRef:
    name: robocorp-tpp-venafi-issuer-rnd
    kind: ClusterIssuer
  commonName: test-dojo-portal-rnd.robocorpint.net
  dnsNames:
    - test-dojo-portal-rnd.robocorpint.net
    - test-dojo-portal.rnd.robocorpint.net
  privateKey:
    algorithm: RSA
    size: 2048
```

Verify issuance:

```bash
oc -n daa-aro-rnd get certificate test-dojo-portal-rnd     # READY=True
oc -n daa-aro-rnd describe certificate test-dojo-portal-rnd # Requested → Issuing → Issued
oc -n daa-aro-rnd get secret test-dojo-portal-rnd-tls       # tls.crt / tls.key / ca.crt

oc -n daa-aro-rnd get secret test-dojo-portal-rnd-tls \
  -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -noout -text
```

This is shipped as its own chart — [`venafi-certificate-sample`](charts/venafi-certificate-sample) —
because a tenant's surface is exactly one namespaced object, separate from the
admin-owned operator + issuer.

---

## Running on upstream cert-manager (not the Red Hat operator)

The chart is portable. If cert-manager is already installed from the community
distribution (not the OLM operator), turn off the Red-Hat-specific pieces and let
the CA-pull Job establish TPP trust from the endpoint:

```bash
helm install cert-manager-venafi charts/cert-manager-venafi \
  --set operator.enabled=false \
  --set trustedCA.enabled=false \
  --set verifyJob.enabled=false \
  --set clusterIssuer.venafi.tpp.caBundleSecretRef.enabled=true
```

- `operator.enabled=false` — you manage cert-manager yourself (no OLM Subscription).
- `trustedCA.enabled=false` — there is no `TRUSTED_CA_CONFIGMAP_NAME` injection upstream.
- `verifyJob.enabled=false` — nothing to wait for (no OLM CSV); the `ClusterIssuer`
  becomes a plain resource.
- `caBundleSecretRef.enabled=true` — the **CA-pull Job fetches the TPP CA from the
  endpoint** and the issuer trusts it. This is the reason the switch exists.

> The CA-pull Job uses the `oc` CLI + `openssl`; override `caPullJob.image` to a
> `kubectl`+`openssl` image on non-OpenShift clusters.

---

## Summary of design decisions

| Decision | Rationale |
|----------|-----------|
| OLM `Subscription`, not community chart | Red Hat lifecycle, supported upgrades, FIPS. |
| `installPlanApproval` parameterized | Automatic vs Manual is a real governance choice. |
| Trusted-CA injection via labelled ConfigMap | Red Hat enterprise enhancement — reuse **cluster trust defaults** (Red Hat path). |
| `caBundleSecretRef` + CA-pull Job | **Portability** — lets the chart run on **upstream cert-manager**, pulling the TPP CA from the endpoint since there's no operator CA injection. |
| `caBundleSecretRef` off by default | On the Red Hat path the injected CA is already in system trust (upstream `caBundle` "empty = system roots"). |
| Credentials as base64 `data`, create-or-existing | No secret manager; deliver out-of-band. |
| Verify Job gates the `ClusterIssuer` | CRDs appear only after the operator installs. |
| Separate tenant sample chart | Tenant manages one namespaced `Certificate`, nothing more. |

---

## References

- Red Hat — cert-manager Operator for OpenShift (install via CLI):
  <https://docs.redhat.com/en/documentation/openshift_container_platform/4.20/html/security_and_compliance/cert-manager-operator-for-red-hat-openshift#cert-manager-install-cli_cert-manager-operator-install>
- Red Hat — Injecting a custom CA certificate (under "Configuring the egress
  proxy"): same guide, § 9.4.1.
- Upstream cert-manager — Venafi (VaaS + TPP) issuer configuration:
  <https://cert-manager.io/v1.6-docs/configuration/venafi/>
