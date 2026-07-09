# scripts

Convenience scripts for exercising the charts against a cluster.

They target **whatever OpenShift cluster your `oc` is logged into** — not just
CRC. Log in first (`oc login <api-url> -u <user>`), or set `USE_CRC=true` to
source a running CRC's environment. A shared [`lib.sh`](lib.sh) does the
preflight (checks `oc` is present + logged in) and prints the target cluster.

| Script | What it does |
|--------|--------------|
| [`teardown.sh`](teardown.sh) | Removes the release, ClusterIssuer, CSV, verify RBAC, namespaces, and cert-manager CRDs → clean 0/0. **Prompts for confirmation** (destructive). |
| [`install.sh`](install.sh) | Fresh install of `charts/cert-manager-venafi` (self-gating). Uses **test** TPP credentials by default. |
| [`verify.sh`](verify.sh) | Streams the verify Job's 5-step gate, then prints release / operator / operand / ClusterIssuer state. |

## Usage

```bash
# 1. Point oc at your cluster
oc login https://api.my-cluster.example.com:6443 -u admin
#    ...or, for a running CRC:  export USE_CRC=true

./scripts/teardown.sh          # clean slate (confirms first; FORCE=true to skip)
./scripts/install.sh           # fresh install (waits for the gate)
./scripts/verify.sh            # confirm the result
```

## Overridable env vars

Defaults match the chart's defaults; override any as needed:

```bash
# Cluster targeting (all scripts)
USE_CRC=true                        # source a running CRC before oc (default: use current oc login)
FORCE=true                          # teardown.sh — skip the destructive confirmation (CI/non-interactive)

# Names
RELEASE=cert-manager-venafi         # helm release name
OPERATOR_NS=cert-manager-operator   # operator namespace
OPERAND_NS=cert-manager             # operand namespace
ISSUER=robocorp-tpp-venafi-issuer-rnd

# install.sh
CHART=charts/cert-manager-venafi
TIMEOUT=12m                         # must exceed verifyJob.timeoutSeconds
TPP_USERNAME=... TPP_PASSWORD=...   # real creds instead of the test defaults
LOG=/tmp/cert-manager-venafi-install.log
```

> `install.sh` sets `tppCredentials.create=true` with **test** credentials
> (`svc-aro` / `dummy-pass`). For a real install, pre-create the `tpp-secret` and
> remove the `--set tppCredentials.*` flags, or pass `TPP_USERNAME`/`TPP_PASSWORD`.
