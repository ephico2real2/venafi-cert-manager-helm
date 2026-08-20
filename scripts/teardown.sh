#!/usr/bin/env bash
# Tear the cert-manager-venafi release + operator down to a clean cluster.
# Removes the helm release, the ClusterIssuer, the OLM CSV, the verify hook's
# cluster-scoped RBAC, the two namespaces, and the cert-manager CRDs — then
# waits until the namespaces are fully gone and prints a 0/0 clean check.
#
# Targets whatever cluster your `oc` is logged into (any OpenShift). Set
# USE_CRC=true to source a running CRC first; FORCE=true to skip the prompt.
#
# Usage:  ./scripts/teardown.sh
# Override defaults via env, e.g.  RELEASE=my-rel OPERAND_NS=cert-manager ./scripts/teardown.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

RELEASE="${RELEASE:-cert-manager-venafi}"
OPERATOR_NS="${OPERATOR_NS:-cert-manager-operator}"
OPERAND_NS="${OPERAND_NS:-cert-manager}"

oc_preflight
confirm_destructive "Delete cert-manager (release '${RELEASE}', CSV, verify RBAC, namespaces ${OPERAND_NS}/${OPERATOR_NS}, and cert-manager CRDs)"

echo "=== teardown ==="
helm uninstall "${RELEASE}" 2>&1 | tail -1
oc delete clusterissuers.cert-manager.io --all 2>/dev/null
oc delete clusterserviceversions.operators.coreos.com -n "${OPERATOR_NS}" --all 2>/dev/null
oc delete clusterrole "${RELEASE}-verify" 2>/dev/null
oc delete clusterrolebinding "${RELEASE}-verify" 2>/dev/null
oc delete ns "${OPERAND_NS}" "${OPERATOR_NS}" --wait=true --timeout=120s 2>&1 | tail -2
oc get customresourcedefinitions.apiextensions.k8s.io -o name 2>/dev/null | grep 'cert-manager.io' | xargs -r oc delete --timeout=60s >/dev/null 2>&1

# wait for the namespaces to finish terminating
for _ in $(seq 1 30); do
  [ "$(oc get ns "${OPERAND_NS}" "${OPERATOR_NS}" --no-headers 2>/dev/null | wc -l | tr -d ' ')" -eq 0 ] && break
  sleep 5
done

echo "clean → namespaces: $(oc get ns "${OPERAND_NS}" "${OPERATOR_NS}" --no-headers 2>/dev/null | wc -l | tr -d ' '), CRDs: $(oc get customresourcedefinitions.apiextensions.k8s.io 2>/dev/null | grep -c cert-manager.io)"
