#!/usr/bin/env bash
# Verify the cert-manager-venafi install: stream the verify Job's 5-step gate
# (if it's still running), then print the final release / operator / operand /
# ClusterIssuer state.
#
# Targets whatever cluster your `oc` is logged into (any OpenShift). Set
# USE_CRC=true to source a running CRC first.
#
# Usage:  ./scripts/verify.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"
oc_preflight

RELEASE="${RELEASE:-cert-manager-venafi}"
OPERATOR_NS="${OPERATOR_NS:-cert-manager-operator}"
OPERAND_NS="${OPERAND_NS:-cert-manager}"
ISSUER="${ISSUER:-robocorp-tpp-venafi-issuer-rnd}"
LOG="${LOG:-/tmp/cert-manager-venafi-install.log}"

# --- stream the verify Job gate while it runs (skips if already finished) ----
echo "=== verify Job (5-step gate) ==="
last=""
for _ in $(seq 1 34); do
  pod=$(oc get pod -n "${OPERATOR_NS}" -l job-name="${RELEASE}-verify-operator" -o name 2>/dev/null | head -1)
  [ -z "${pod}" ] && { echo "  (verify pod not present — already completed & cleaned up)"; break; }
  cur=$(oc logs "${pod}" -n "${OPERATOR_NS}" 2>/dev/null | grep -E "^\[verify" | tail -1)
  [ "${cur}" != "${last}" ] && [ -n "${cur}" ] && { echo "${cur}"; last="${cur}"; }
  ph=$(oc get "${pod}" -n "${OPERATOR_NS}" -o jsonpath='{.status.phase}' 2>/dev/null)
  case "${ph}" in Succeeded|Failed) echo ">>> verify Job: ${ph}"; break;; esac
  sleep 7
done

# --- final state ------------------------------------------------------------
echo "=== install result ==="
[ -f "${LOG}" ] && grep -E "STATUS:|Error" "${LOG}" | head -3 || echo "  (no install log at ${LOG})"

echo "=== state ==="
helm list -A 2>/dev/null | grep "${RELEASE}" | awk '{print "  release:",$1,$8}'
oc get csv -n "${OPERATOR_NS}" 2>/dev/null | grep "cert-manager " | awk '{print "  operator:",$NF}'
oc get pods -n "${OPERAND_NS}" --no-headers 2>/dev/null | awk '{print "  pod:",$1,$3}'
oc get clusterissuer "${ISSUER}" --no-headers 2>/dev/null | awk '{print "  issuer:",$1,"READY="$2}'
oc get clusterissuer "${ISSUER}" -o jsonpath='  reason: {.status.conditions[0].reason}{"\n"}' 2>/dev/null
