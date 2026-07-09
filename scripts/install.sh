#!/usr/bin/env bash
# Fresh install of the cert-manager-venafi chart (Red Hat operator path).
# Self-gating: the verify Job waits for the operator + webhook before the
# ClusterIssuer is created, so a single install is enough.
#
# The credentials below are TEST defaults (tppCredentials.create=true). For a
# real install, either pre-create the tpp-secret and drop the --set flags, or
# override:  TPP_USERNAME=... TPP_PASSWORD=... ./scripts/install.sh
#
# Targets whatever cluster your `oc` is logged into (any OpenShift). Set
# USE_CRC=true to source a running CRC first.
#
# Usage:  ./scripts/install.sh
set -uo pipefail

# repo root = parent of this script's directory (location-independent)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"
oc_preflight

RELEASE="${RELEASE:-cert-manager-venafi}"
CHART="${CHART:-${REPO_ROOT}/charts/cert-manager-venafi}"
TIMEOUT="${TIMEOUT:-12m}"
LOG="${LOG:-/tmp/cert-manager-venafi-install.log}"
TPP_USERNAME="${TPP_USERNAME:-svc-aro}"
TPP_PASSWORD="${TPP_PASSWORD:-dummy-pass}"

echo "installing commit: $(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || echo '(not a git repo)')"
helm install "${RELEASE}" "${CHART}" \
  --set tppCredentials.create=true \
  --set tppCredentials.usernameBase64="$(printf '%s' "${TPP_USERNAME}" | base64)" \
  --set tppCredentials.passwordBase64="$(printf '%s' "${TPP_PASSWORD}" | base64)" \
  --timeout "${TIMEOUT}" > "${LOG}" 2>&1
echo "HELM_DONE exit=$?"
grep -E "STATUS:|Error" "${LOG}" | head -3
echo "(full log: ${LOG})"
