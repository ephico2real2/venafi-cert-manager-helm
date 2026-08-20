# Download the charts, then install from the copy on disk

Every command here was run against the published repository on 2026-08-20 with `helm v3.14.0+g3fc9f4b` and
`oc 4.13.6`. Where a command fails, the failure is shown rather than described — two of them fail *on
purpose*, and one fails for a reason that is not obvious from the flag's name.

One exception, stated so the rest can be trusted: the `skopeo copy` in §7 was verified only on its source
side, because there is no internal registry here to copy into.

**Why download first rather than `helm install` straight from the repo?** Three real reasons: you want to
read the templates — particularly the three Jobs, which delete and patch things — before they touch a
cluster; the cluster has no route to `github.io` and the chart must travel on a laptop; or you need the exact
bytes of one version pinned in your own artefact store. If none of those apply, install from the repo
directly — `charts/cert-manager-venafi/README.md` covers that.

---

## 1. Add the repository and see what it offers

```sh
helm repo add cert-manager-venafi https://ephico2real2.github.io/venafi-cert-manager-helm
helm repo update
helm search repo cert-manager-venafi --versions
```

```
NAME                                          CHART VERSION  APP VERSION  DESCRIPTION
cert-manager-venafi/cert-manager-venafi       0.3.2          stable-v1    Installs the cert-manager Operator …
cert-manager-venafi/cert-manager-venafi       0.3.1          stable-v1    Installs the cert-manager Operator …
cert-manager-venafi/venafi-certificate-sample 0.1.0          1.0.0        Tenant-facing sample: requests a TLS …
```

**Two charts, and they are for different people.** `cert-manager-venafi` is the platform chart — it installs
the operator and the shared `ClusterIssuer`, and you install it once per cluster.
`venafi-certificate-sample` is what a tenant installs in their own namespace to request a certificate
against that issuer; it never touches TPP credentials.

**`APP VERSION` here is a channel, not a build.** `stable-v1` is the OLM subscription channel; the operator
version actually installed is pinned by `operator.startingCSV` in values, currently
`cert-manager-operator.v1.19.1`. That is deliberate — see §6.

`helm repo update` is not optional. The local cache is a snapshot, and skipping the update means `helm pull`
happily fetches whatever was current last week.

> **If `helm search` shows a version older than the repo's `index.yaml`, wait rather than debug.** GitHub
> Pages republishes asynchronously — the branch can carry 0.3.2 while the served index still says 0.3.1 for a
> few minutes. Two related traps cost real time while writing this: the urls in `index.yaml` are **relative**
> under `packages_with_index`, so `curl`-ing one directly returns `000` and looks like a broken repository;
> and a CDN copy can be stale. `helm repo add` + `helm pull` resolves both correctly and is the only honest
> test.

---

## 2. Download it

```sh
# The packaged chart, as published
helm pull cert-manager-venafi/cert-manager-venafi

# → cert-manager-venafi-0.3.2.tgz
```

```sh
# A specific version, not merely the newest, into a directory of your choosing
mkdir -p ./pinned
helm pull cert-manager-venafi/cert-manager-venafi --version 0.3.1 -d ./pinned
```

**`-d` does not create the directory, and the error does not say so.** Without the `mkdir` above:

```
Error: open pinned/cert-manager-venafi-0.3.1.tgz2971398898: no such file or directory
```

That temp-file suffix makes it read like a corrupt download. It is not — the target directory simply does
not exist. Create it first.

```sh
# Download AND unpack in one step — usually the one you want
helm pull cert-manager-venafi/cert-manager-venafi --untar --untardir ./src

# → ./src/cert-manager-venafi/{Chart.yaml,values.yaml,README.md,templates/}
```

`--untar` leaves no `.tgz` behind. If you need both the archive (to archive or to move) and the tree (to
read), pull twice, or `tar -xzf` the archive yourself.

---

## 3. Read it before you run it

```sh
tar -tzf cert-manager-venafi-0.3.2.tgz | head
```

```
cert-manager-venafi/Chart.yaml
cert-manager-venafi/values.yaml
cert-manager-venafi/templates/NOTES.txt
cert-manager-venafi/templates/_helpers.tpl
cert-manager-venafi/templates/capull-job.yaml
cert-manager-venafi/templates/clusterissuer.yaml
cert-manager-venafi/templates/csv-reclaim-job.yaml
cert-manager-venafi/templates/installplan-approver-job.yaml
…
```

18 entries, 33647 bytes.

**The three files worth reading first, because they act on the cluster rather than just declaring state:**

| file | what it does that you should know about |
|---|---|
| `csv-reclaim-job.yaml` | **deletes** a `ClusterServiceVersion` — but only one that is unowned, unreferenced, not an OLM copy, and in a settled phase, and only when the Subscription reports `ResolutionFailed`. Six conditions, each commented with why. |
| `installplan-approver-job.yaml` | **patches** `.spec.approved` on an InstallPlan — only the one installing `operator.startingCSV`. Any other version is left for a human. |
| `verify-job.yaml` | read-only. Six staged checks; it gates the `ClusterIssuer` and creates nothing. |

None of the three touches the `Subscription` itself, which is why a `helm upgrade` cannot fight them.

---

## 4. Render it, and lint the copy you actually have

```sh
helm lint ./src/cert-manager-venafi
helm template mine ./src/cert-manager-venafi | grep -c '^kind:'
```

```
1 chart(s) linted, 0 chart(s) failed
18
```

Linting the *downloaded* tree rather than the repo means you are checking the bytes you will install. A
tarball that renders 18 documents and lints clean is a real chart, not a truncated download.

---

## 5. Two renders that fail on purpose

Both of these are guards. If you hit one, the chart is telling you something rather than breaking.

```sh
helm template mine ./src/cert-manager-venafi --set verifyJob.enabled=false
```

```
Error: … clusterIssuer.enabled=true requires verifyJob.enabled=true. …
```

Without the gate the `ClusterIssuer` renders as an **ordinary** resource, and Helm builds every ordinary
resource before applying anything — so on a cluster that does not yet serve `cert-manager.io/v1` the install
fails with an obscure `no matches for kind "ClusterIssuer"`. Either leave the gate on, or set
`clusterIssuer.enabled=false` and apply the issuer separately.

```sh
helm template mine ./src/cert-manager-venafi --set operator.startingCSV=null
```

```
Error: … installPlanApprover is enabled but operator.startingCSV is empty. …
```

The pin **is** the approver's authority. With nothing to match it would either approve every InstallPlan the
channel produces — defeating `installPlanApproval: Manual` — or approve none and hang the install.

---

## 6. Install from the downloaded copy

The chart expects the TPP credentials Secret to **already exist**; `tppCredentials.create` is `false` by
default, and that is the safer default — a Secret Helm owns can be pruned by Helm.

```sh
oc create namespace cert-manager --dry-run=client -o yaml | oc apply -f -

oc -n cert-manager create secret generic tpp-secret \
  --from-literal=username='<AD_SERVICE_ACCOUNT>' \
  --from-literal=password='<AD_SERVICE_ACCOUNT_PASSWORD>'
```

The key names matter: cert-manager's Venafi issuer reads `username` and `password`. A Secret with other names
fails at runtime exactly like a missing one — which is why the verify gate's stage 6 checks the keys, not just
existence.

```sh
helm upgrade --install cert-manager-venafi ./src/cert-manager-venafi \
  -n default --wait --timeout 15m
```

Dry-run it first if you want to see what would change without touching anything:

```sh
helm upgrade --install --dry-run cert-manager-venafi ./src/cert-manager-venafi -n default
```

```
NAME: cert-manager-venafi
NAMESPACE: default
STATUS: pending-upgrade
REVISION: 8
```

**What happens during those minutes,** in hook-weight order, so a wait does not look like a hang:

```
-6  csv-reclaim            clears a CSV orphaned by a previous uninstall, or exits in seconds
-5  installplan-approver   approves ONLY cert-manager-operator.v1.19.1, then waits for Succeeded
-4  ca-pull                (only if caBundleSecretRef.enabled) fetches the TPP CA into a Secret
 5  verify-operator        six staged checks; its log is the one to read if anything stalls
10  ClusterIssuer          created only after the gate passes
```

If it stalls, the gate names the cause. Stage 1 timing out usually means the InstallPlan was never
approved — check `oc logs -n cert-manager-operator job/cert-manager-venafi-installplan-approver`.

**To move operator version, do not approve an InstallPlan on the cluster.** Bump `operator.startingCSV` and
upgrade. Approving by hand works once and is then reverted, because the pin is what the approver enforces.

---

## 7. Air-gapped: move the chart and the image separately

A `.tgz` is not enough — the chart references an image, and the operator itself comes from a catalog.

```sh
# WHICH images does this chart pull? Ask the chart rather than grepping values.yaml — an empty
# `tag` means "use appVersion", so grepping the tag alone answers "" and you would mirror nothing.
helm template mine ./src/cert-manager-venafi \
  --set clusterIssuer.venafi.tpp.caBundleSecretRef.enabled=true \
  | grep -E '^\s+image:' | sed 's/.*image: //' | tr -d '"' | sort -u
```

```
registry.redhat.io/openshift4/ose-cli:latest
```

**One image, used by all three Jobs** — they only need `oc`. Note what is *not* in that list: the
cert-manager operator and its three operands are installed by **OLM** from the `redhat-operators`
CatalogSource, not by this chart. A disconnected cluster therefore needs a mirrored catalog as well, which is
outside this chart's scope — see the OpenShift disconnected-install documentation for `oc-mirror`.

```sh
# On a connected machine
skopeo copy \
  docker://registry.redhat.io/openshift4/ose-cli:latest \
  docker://internal-registry.example.net/openshift4/ose-cli:latest
```

`:latest` is a moving tag. For a reproducible air-gap, pin the digest instead:

```sh
skopeo inspect docker://registry.redhat.io/openshift4/ose-cli:latest | jq -r .Digest
```

then set the three Job images to that digest at install time:

```sh
helm upgrade --install cert-manager-venafi ./src/cert-manager-venafi -n default \
  --set verifyJob.image=internal-registry.example.net/openshift4/ose-cli@sha256:… \
  --set installPlanApprover.image=internal-registry.example.net/openshift4/ose-cli@sha256:… \
  --set csvReclaim.image=internal-registry.example.net/openshift4/ose-cli@sha256:… \
  --set caPullJob.image=internal-registry.example.net/openshift4/ose-cli@sha256:…
```

Pinning the digest fixes the **content**, so it survives any retagging on either side.

---

## 8. The tenant chart

Same repository, and a tenant needs nothing but the issuer's name:

```sh
helm pull cert-manager-venafi/venafi-certificate-sample --untar --untardir ./src
helm install my-cert ./src/venafi-certificate-sample -n my-app --create-namespace
```

It creates a `Certificate` and nothing else. Credentials, the issuer and the operator all stay with the
platform chart.

---

## Quick reference

```sh
helm repo add cert-manager-venafi https://ephico2real2.github.io/venafi-cert-manager-helm
helm repo update
helm search repo cert-manager-venafi --versions        # both charts, all versions

mkdir -p ./src                                        # -d/--untardir do NOT create it
helm pull cert-manager-venafi/cert-manager-venafi --untar --untardir ./src
helm pull cert-manager-venafi/cert-manager-venafi --version 0.3.1 -d ./pinned

helm lint  ./src/cert-manager-venafi
helm template mine ./src/cert-manager-venafi | grep -c '^kind:'      # 18

oc -n cert-manager create secret generic tpp-secret \
  --from-literal=username='<user>' --from-literal=password='<pass>'

helm upgrade --install cert-manager-venafi ./src/cert-manager-venafi -n default --wait --timeout 15m
oc logs -n cert-manager-operator job/cert-manager-venafi-verify-operator   # if it stalls
```
