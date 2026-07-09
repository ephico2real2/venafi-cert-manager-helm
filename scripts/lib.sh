#!/usr/bin/env bash
# Shared preflight for the helper scripts.
#
# By default these target whatever cluster your `oc` is currently logged into —
# ANY OpenShift cluster, not just CRC. Log in first with `oc login <api> -u <user>`.
#
# Opt-ins (env vars):
#   USE_CRC=true   source a running CRC's oc environment before anything else
#   FORCE=true     skip the destructive-action confirmation (teardown, CI use)

# Ensure `oc` is available and pointed at a logged-in cluster; print the target.
oc_preflight() {
  if [ "${USE_CRC:-false}" = "true" ]; then
    if command -v crc >/dev/null 2>&1; then
      eval "$(crc oc-env)"
    else
      echo "ERROR: USE_CRC=true but the 'crc' CLI was not found in PATH." >&2
      exit 1
    fi
  fi

  if ! command -v oc >/dev/null 2>&1; then
    echo "ERROR: the 'oc' CLI was not found in PATH." >&2
    echo "  Install the OpenShift CLI, or set USE_CRC=true to use a running CRC." >&2
    exit 1
  fi

  if ! oc whoami >/dev/null 2>&1; then
    echo "ERROR: not logged into an OpenShift cluster." >&2
    echo "  Log in first, e.g.:  oc login <api-url> -u <user>" >&2
    echo "  (or set USE_CRC=true to target a running CRC cluster)" >&2
    exit 1
  fi

  echo "target: $(oc whoami) @ $(oc whoami --show-server 2>/dev/null)"
}

# Guard a destructive action: show the target cluster and require confirmation
# unless FORCE=true. Refuses to run non-interactively without FORCE.
confirm_destructive() {
  local what="${1:-This action}"
  echo "${what} on:"
  echo "  $(oc whoami --show-server 2>/dev/null)"
  if [ "${FORCE:-false}" = "true" ]; then
    echo "  FORCE=true → proceeding without prompt"
    return 0
  fi
  if [ ! -t 0 ]; then
    echo "ERROR: refusing to run destructively in a non-interactive shell." >&2
    echo "  Re-run with FORCE=true to override." >&2
    exit 1
  fi
  printf "  Type 'yes' to continue: "
  read -r ans
  [ "${ans}" = "yes" ] || { echo "aborted."; exit 1; }
}
