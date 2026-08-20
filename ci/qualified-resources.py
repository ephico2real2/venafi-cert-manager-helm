#!/usr/bin/env python3
"""Assert every `oc` call in this chart names its resource IN FULL.

COPIED from group-sync-operator-helm-chart/ci/qualified-resources.py and retargeted, rather than rewritten —
the rule and its failure modes are identical, only the kinds differ.

WHY — a defect measured on a live cluster in the sibling openshift-rbac-automation chart, whose scripts have
the same call shapes as these:

    Error from server (Forbidden): subscriptions.messaging.knative.dev "…" is forbidden:
    … cannot get resource "subscriptions" in API group "messaging.knative.dev"

`oc get subscription` had bound to the WRONG API GROUP. A resource plural is not reserved: Knative Eventing
serves `subscriptions`, OLM serves `subscriptions`, and which one a short name resolves to is decided by API
discovery — so it changes as CRDs are installed and removed. These scripts run in containers with HOME=/tmp
and therefore a COLD discovery cache on every run, so the binding is not stable even between two runs of the
same Job.

THE FAILURE MODE IS WHY THIS IS A CI GATE AND NOT A STYLE PREFERENCE. A misrouted request returns Forbidden,
which reads as missing RBAC on the resource you MEANT. Worse, a misrouted LIST returns NOTHING rather than
failing — and this chart has two decisions driven by a list:

    relabel-provenance.sh   `oc get groupsyncs…` decides which CRs are LIVE, and `oc get groups…` decides
                            which Groups carry a stale provenance value. An empty list from a misrouted read
                            means "no live CR" and "no groups to fix" — a silent no-op that reports success.
    operator-wait / approver  read the Subscription and its InstallPlans to decide whether to wait or approve.

THIS CHART IS MORE EXPOSED THAN EITHER SIBLING: its Jobs set KUBECACHEDIR to a fresh emptyDir, so the
discovery cache is COLD on every run and a short name's binding is not stable even between two runs of the
same Job.

    relabel-provenance.sh   -> not present here
    verify-job.yaml         `oc get deploy -n cert-manager` decided whether the operands were up. A
                            misrouted list returns NOTHING, which reads as "not ready yet" forever.
    installplan-approver    reads the Subscription and its InstallPlans to decide whether to approve.
    csv-reclaim             LISTS CSVs to decide what to DELETE — the highest-consequence list in the chart.

WHAT IS CHECKED, and deliberately not:

  checked      the resource ARGUMENT of an `oc` verb, in the rendered chart (so a script that only exists as
              a ConfigMap value is covered exactly like a file), in every files/*.sh source script, and in
              NOTES.txt plus the values files — because an operator copy-pastes those.
  not checked  `oc get "$var"` forms, which hold `-o name` output that is already fully qualified; comment
              lines, so prose can still discuss `oc get csv`; and kubectl, which this chart does not use.

There is deliberately NO allowlist for "clusters where the short name is fine". A cluster can add a colliding
CRD tomorrow, so the rule is unconditional.
"""

import glob
import os
import re
import subprocess
import sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHART = os.path.join(REPO, "charts", "cert-manager-venafi")

# Every spelling `oc` accepts for the kinds this chart touches, including the documented short aliases,
# because all of them route through the same discovery.
MUST_QUALIFY = {
    "subscription": "subscriptions.operators.coreos.com",
    "subscriptions": "subscriptions.operators.coreos.com",
    "sub": "subscriptions.operators.coreos.com",
    "subs": "subscriptions.operators.coreos.com",
    "installplan": "installplans.operators.coreos.com",
    "installplans": "installplans.operators.coreos.com",
    "ip": "installplans.operators.coreos.com",
    "csv": "clusterserviceversions.operators.coreos.com",
    "csvs": "clusterserviceversions.operators.coreos.com",
    "clusterserviceversion": "clusterserviceversions.operators.coreos.com",
    "clusterserviceversions": "clusterserviceversions.operators.coreos.com",
    "operatorgroup": "operatorgroups.operators.coreos.com",
    "operatorgroups": "operatorgroups.operators.coreos.com",
    "og": "operatorgroups.operators.coreos.com",
    # This chart's kinds. `deploy` is the one that bit hardest here: a misrouted LIST returns nothing
    # rather than failing, so `oc get deploy -n cert-manager` would read as "no operands yet" forever.
    "deploy": "deployments.apps",
    "deployments": "deployments.apps",
    "crd": "customresourcedefinitions.apiextensions.k8s.io",
    "crds": "customresourcedefinitions.apiextensions.k8s.io",
    "clusterissuer": "clusterissuers.cert-manager.io",
    "clusterissuers": "clusterissuers.cert-manager.io",
    "certificate": "certificates.cert-manager.io",
    "certificates": "certificates.cert-manager.io",
    "certmanager": "certmanagers.operator.openshift.io",
    "certmanagers": "certmanagers.operator.openshift.io",
}

OC_CALL = re.compile(r"\boc\s+(?:get|patch|delete|wait|describe|label|annotate)\s+"
                     r"(?:-{1,2}[\w-]+(?:[= ]\S+)?\s+)*"
                     r"([A-Za-z][\w.,-]*)")


def offenders(text, where):
    found = []
    for line_no, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        for m in OC_CALL.finditer(line):
            arg = m.group(1)
            if arg.startswith("$"):
                continue
            for part in arg.split(","):
                if part in MUST_QUALIFY:
                    found.append((where, line_no, part, MUST_QUALIFY[part], line.strip()[:100]))
    return found


def render():
    """Render with defaults; this chart needs no --set to render offline."""
    proc = subprocess.run(
        ["helm", "template", "qualified-probe", CHART],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit("::error::helm template failed\n%s" % proc.stderr)
    return proc.stdout


def main():
    errors, scripts_seen, files_seen = [], 0, 0

    # 1. the scripts as they actually run. Unlike the sibling charts, this one puts them INLINE in each
    #    Job's container command rather than in a ConfigMap, so that is where to look — and checking the
    #    RENDER means a values change that alters a command is covered too.
    for doc in yaml.safe_load_all(render()):
        if not doc or doc.get("kind") not in ("Job", "CronJob"):
            continue
        spec = doc["spec"]["jobTemplate"]["spec"] if doc["kind"] == "CronJob" else doc["spec"]
        for c in spec["template"]["spec"].get("containers", []):
            for part in (c.get("command") or []) + (c.get("args") or []):
                if "oc " not in part:
                    continue
                scripts_seen += 1
                errors += offenders(part, "%s/%s" % (doc["metadata"]["name"], c["name"]))

    # 2. the repo's helper scripts, which an operator runs by hand from a runbook
    for path in sorted(glob.glob(os.path.join(REPO, "scripts", "*.sh"))):
        files_seen += 1
        errors += offenders(open(path).read(), os.path.relpath(path, REPO))

    # 3. NOTES.txt and the values files — an operator copy-pastes these, so an ambiguous command is a trap
    for path in [os.path.join(CHART, "templates", "NOTES.txt")] + \
                sorted(glob.glob(os.path.join(CHART, "*values*.y*ml"))):
        if os.path.exists(path):
            errors += offenders(open(path).read(), os.path.relpath(path, REPO))

    # EVERY SELECTOR MUST MATCH SOMETHING. A check that silently inspects nothing passes forever.
    if not scripts_seen:
        errors.append(("(render)", 0, "-", "-", "no Job container command containing `oc ` rendered — "
                                                "either the scripts moved out of the Jobs or the chart "
                                                "stopped shipping them"))
    if not files_seen:
        errors.append(("(scripts)", 0, "-", "-", "no scripts/*.sh found — this check stopped covering the "
                                                 "repo's helper scripts"))

    if errors:
        for where, line_no, short, full, ctx in errors:
            print("::error::%s:%s uses the ambiguous resource name '%s' — write '%s'. A plural is not "
                  "reserved, so `%s` can bind to another API group; measured on a live cluster, "
                  "`oc get subscription` resolved to subscriptions.messaging.knative.dev and returned "
                  "Forbidden. A misrouted LIST returns nothing rather than failing. Context: %s"
                  % (where, line_no, short, full, short, ctx))
        print("\nFAILED: %d ambiguous resource name(s)" % len(errors))
        return 1
    print("OK: %d rendered script(s) and %d source script(s) name every resource in full."
          % (scripts_seen, files_seen))
    return 0


if __name__ == "__main__":
    sys.exit(main())
