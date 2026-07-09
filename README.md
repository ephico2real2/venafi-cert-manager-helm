# venafi-cert-manager-helm

Helm charts to run **cert-manager with a Venafi Trust Protection Platform (TPP)
`ClusterIssuer`** — on the **Red Hat cert-manager Operator** *or* **upstream
cert-manager** — plus a sample that validates the whole path end-to-end.

| Chart | What it does | Who runs it |
|-------|--------------|-------------|
| 📦 [`charts/cert-manager-venafi`](charts/cert-manager-venafi) | Installs the operator, injects the trusted CA (or pulls the TPP CA), provisions the TPP secrets, and creates the shared Venafi `ClusterIssuer`. | Platform / cluster admin |
| ✅ [`charts/venafi-certificate-sample`](charts/venafi-certificate-sample) | A tenant `Certificate` that requests a cert from the shared issuer — proves the integration works. | App tenant / validation |

## Quick start

```bash
# 1. TPP credentials (or set tppCredentials.create=true)
oc -n cert-manager create secret generic tpp-secret \
  --from-literal=username='<AD_SERVICE_ACCOUNT>' \
  --from-literal=password='<AD_SERVICE_ACCOUNT_PASSWORD>'

# 2. Install the operator + issuer (self-gating; timeout covers the operator install)
helm install cert-manager-venafi charts/cert-manager-venafi --timeout 12m
oc get clusterissuer -o wide          # READY=True

# 3. Validate by issuing a certificate
helm install my-cert charts/venafi-certificate-sample -n daa-aro-rnd --create-namespace
oc -n daa-aro-rnd get certificate     # READY=True
```

## Two trust models, one chart

- **Red Hat Operator** — uses the operator's cluster-wide **trusted-CA injection**
  (`caBundleSecretRef` off; trust comes from the cluster default).
- **Upstream cert-manager** — no operator injection, so the built-in **CA-pull Job
  fetches the TPP CA from the endpoint** and wires it to the issuer
  (`caBundleSecretRef.enabled=true`). See
  [Using with upstream cert-manager](charts/README.md#using-with-upstream-cert-manager).

## Documentation

| Doc | Purpose |
|-----|---------|
| [charts/README.md](charts/README.md) | Full install + validation walkthrough, configuration reference. |
| [steps.md](steps.md) | Design reasoning + how the Red Hat operator shaped the approach vs upstream cert-manager. |
| [charts/cert-manager-venafi/README.md](charts/cert-manager-venafi/README.md) | Integration chart — complete parameters. |
| [charts/venafi-certificate-sample/README.md](charts/venafi-certificate-sample/README.md) | Sample chart — complete parameters. |

## References

- Red Hat — cert-manager Operator for OpenShift:
  <https://docs.redhat.com/en/documentation/openshift_container_platform/4.20/html/security_and_compliance/cert-manager-operator-for-red-hat-openshift#cert-manager-install-cli_cert-manager-operator-install>
- Upstream cert-manager — Venafi issuer:
  <https://cert-manager.io/v1.6-docs/configuration/venafi/>
