# venafi-certificate-sample

## Install from the chart repository

```bash
helm repo add cert-manager-venafi https://ephico2real2.github.io/venafi-cert-manager-helm
helm repo update
helm install my-cert cert-manager-venafi/venafi-certificate-sample \
  -n <your-namespace> --create-namespace
```

The issuer this chart requests from is installed cluster-wide by the `cert-manager-venafi` chart in the same
repository — a tenant only manages the `Certificate` here, never the TPP credentials.


A tenant-facing sample that **validates** the Venafi integration end-to-end: it
creates a namespaced `Certificate` that requests a real cert from the shared
`ClusterIssuer` installed by the [`cert-manager-venafi`](../cert-manager-venafi)
chart. This is the only object a tenant manages — cert-manager handles issuance
and renewal.

- **Type:** application · **Scope:** namespaced (a single `Certificate`)
- **Prerequisite:** the shared `ClusterIssuer` must exist and be `Ready=True`

---

## Quick start

```bash
# Request the certificate (creates the namespace if it doesn't exist)
helm install my-cert . -n daa-aro-rnd --create-namespace

# Confirm issuance
oc -n daa-aro-rnd get certificate test-dojo-portal-rnd     # READY=True
oc -n daa-aro-rnd get secret test-dojo-portal-rnd-tls      # tls.crt / tls.key / ca.crt
```

Inspect the issued certificate:

```bash
oc -n daa-aro-rnd get secret test-dojo-portal-rnd-tls \
  -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -noout -text
# Confirm: Issuer is the TPP CA, Subject CN + SANs match the values below
```

A `Ready=True` certificate with a populated TLS secret proves the operator, CA
trust, TPP credentials, and `ClusterIssuer` are all wired correctly.

---

## Resources this chart manages

| Resource | Kind | Created when |
|----------|------|--------------|
| Tenant namespace | `Namespace` | `namespace.create=true` |
| Certificate request | `Certificate` (`cert-manager.io/v1`) | always |

---

## Parameters

### Namespace

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `namespace.create` | bool | `false` | Create the tenant namespace (or use `helm --create-namespace`). |
| `namespace.name` | string | `daa-aro-rnd` | Namespace the `Certificate` and its TLS secret live in. |

### Issuer reference

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `issuerRef.name` | string | `robocorp-tpp-venafi-issuer-rnd` | Must match `clusterIssuer.name` from the integration chart. |
| `issuerRef.kind` | string | `ClusterIssuer` | Issuer kind (`ClusterIssuer` for the shared issuer). |

### Certificate

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `certificate.name` | string | `test-dojo-portal-rnd` | Name of the `Certificate` resource. |
| `certificate.secretName` | string | `test-dojo-portal-rnd-tls` | Secret cert-manager writes `tls.crt`/`tls.key`/`ca.crt` into. |
| `certificate.duration` | string | `2160h` | Certificate lifetime (90 days). |
| `certificate.renewBefore` | string | `720h` | Renew this long before expiry (30 days). |
| `certificate.commonName` | string | `test-dojo-portal-rnd.robocorpint.net` | Certificate CN. |
| `certificate.dnsNames` | list | *(two SANs)* | Subject Alternative Names. |
| `certificate.privateKey.algorithm` | string | `RSA` | Private-key algorithm. |
| `certificate.privateKey.size` | int | `2048` | Private-key size. |

---

## Customizing for your own certificate

```bash
helm install my-cert . -n <your-namespace> --create-namespace \
  --set issuerRef.name=<your-cluster-issuer> \
  --set certificate.commonName=<fqdn> \
  --set 'certificate.dnsNames={<fqdn>,<alt-fqdn>}'
```

> `issuerRef.name` must match the `ClusterIssuer` created by the
> `cert-manager-venafi` chart, or issuance stays pending.

---

## References

- Design reasoning & full walkthrough: [`steps.md`](../../steps.md) ·
  [top-level charts README](../README.md)
- Integration chart (the `ClusterIssuer` this references):
  [`cert-manager-venafi`](../cert-manager-venafi)
- **Upstream cert-manager** — Certificate resources & Venafi issuer:
  <https://cert-manager.io/v1.6-docs/configuration/venafi/>
- **Red Hat** — cert-manager Operator for OpenShift:
  <https://docs.redhat.com/en/documentation/openshift_container_platform/4.20/html/security_and_compliance/cert-manager-operator-for-red-hat-openshift#cert-manager-install-cli_cert-manager-operator-install>