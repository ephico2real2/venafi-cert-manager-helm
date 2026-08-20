#!/usr/bin/env python3
"""Assert this chart's install ordering is DECLARED, not accidental.

The RULES are lifted from openshift-rbac-automation/working-sessions/scripts/check-ordering.py, which earned
each of them on a live cluster. The CODE is deliberately much smaller: that chart discovers multiple charts
and overlays by glob and has an orphan sweeper to order; this repo has one chart, one values file, and no
sweeper. Copying its 434 lines would be carrying machinery for problems this chart does not have.

The rules kept, and what each one cost to learn:

  1. EVERY DOCUMENT DECLARES A SYNC-WAVE. Absent is not neutral — ArgoCD reads a missing wave as 0, which is
     the Subscription's own wave. Checked in the TEMPLATE SOURCE as well as the render, because a render only
     ever sees the templates the current values switch on.

  2. THE APPROVER SHARES THE SUBSCRIPTION'S WAVE, and is not a PostSync hook. installPlanApproval is Manual,
     so on a FIRST install the Subscription reports Progressing until its InstallPlan is approved — ArgoCD's
     built-in health check forgives InstallPlanPending/RequiresApproval only when .status.installedCSV is
     already set, i.e. an upgrade. In any later wave the approver waits on the wave that is waiting on it.

  3. THE CSV RECLAIM RUNS BEFORE THE APPROVER. An orphaned CSV makes the Subscription unresolvable, and no
     InstallPlan is ever staged — so an approver that runs first waits out its whole budget for something
     that cannot arrive. Measured in the sibling chart: 03:26:53 to 03:30:29, recovering only by luck.

  4. THE READINESS GATE SITS BETWEEN THE SUBSCRIPTION AND THE CLUSTERISSUER. The issuer is a cert-manager.io
     kind; the gate is what waits for those CRDs and for the webhook to serve admission.

  5. EVERY JOB CARRIES AN ARGOCD HOOK ANNOTATION. ArgoCD does not read helm.sh/hook at all, so a Helm-hook
     Job without one is an ordinary resource to Argo — re-applied every sync, and a Job's spec.template is
     immutable, so the SECOND sync fails with "field is immutable".

  6. EVERY JOB'S OWN RBAC ARRIVES NO LATER THAN THE JOB. A Job cannot authenticate with a ServiceAccount
     that lands in a later wave.

  7. NO TWO HOOKS SHARE A WEIGHT. Helm falls back to name order, so their sequence becomes accidental.

  8. EVERY SELECTOR MUST MATCH SOMETHING. A check whose selector quietly matches nothing passes forever while
     testing nothing, which is worse than no check.
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

WAVE = "argocd.argoproj.io/sync-wave"
WEIGHT = "helm.sh/hook-weight"
HOOK = "helm.sh/hook"
ARGO_HOOK = "argocd.argoproj.io/hook"

# Matched on the resource NAME suffix, which is release-name independent because every one of these is
# named {{ .Release.Name }}-<component>.
APPROVER = "installplan-approver"
RECLAIM = "csv-reclaim"
VERIFY = "verify"

# Rendered combinations to check. Two, not a glob: this chart has one values file, and the ca-pull path is
# reached by a flag rather than an overlay.
COMBINATIONS = [
    ("defaults", []),
    ("ca-pull enabled", ["--set", "clusterIssuer.venafi.tpp.caBundleSecretRef.enabled=true"]),
]


def render(extra):
    proc = subprocess.run(["helm", "template", "ordering-probe", CHART] + extra,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit("::error::helm template failed\n%s" % proc.stderr)
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def source_waves():
    """RULE 1, on the template SOURCE — covers templates no values combination switches on."""
    errors, seen = [], 0
    for path in sorted(glob.glob(os.path.join(CHART, "templates", "*.y*ml"))):
        if os.path.basename(path).startswith("_"):
            continue
        text = open(path).read()
        # Split textually: these files contain Go actions and are not parseable as YAML until rendered.
        for i, chunk in enumerate(re.split(r"(?m)^---\s*$", text)):
            if not re.search(r"(?m)^kind:\s*\S", chunk):
                continue
            seen += 1
            # An annotation LINE, not a substring — a commented-out annotation must not satisfy this.
            if not re.search(r"(?m)^\s*%s\s*:" % re.escape(WAVE), chunk):
                kind = re.search(r"(?m)^kind:\s*(\S+)", chunk).group(1)
                errors.append("%s (document %d, kind %s) declares no %s"
                              % (os.path.relpath(path, REPO), i + 1, kind, WAVE))
    if not seen:
        errors.append("no manifest documents found under templates/ — this check covers nothing")
    return errors, seen


def check(label, docs):
    errors = []
    waves, hooks = {}, {}
    unwaved = []
    for d in docs:
        kind, name = d["kind"], d["metadata"].get("name", "<unnamed>")
        ann = d["metadata"].get("annotations") or {}
        w = ann.get(WAVE)
        if w is None:
            unwaved.append("%s/%s" % (kind, name))
            continue
        # KEYED BY (kind, name), NOT NAME. A Job and its ServiceAccount/Role/RoleBinding all share one
        # name here — `<release>-installplan-approver` is four different objects — so keying by name alone
        # kept whichever rendered first and made the Job invisible to the selectors below. Rule 8 caught it
        # by refusing to pass when a selector matched nothing, which is exactly what that rule is for.
        waves[(kind, name)] = (kind, int(w), ann)
        if HOOK in ann:
            if WEIGHT not in ann:
                errors.append("%s: %s/%s is a Helm hook with no %s" % (label, kind, name, WEIGHT))
            else:
                hooks.setdefault(int(ann[WEIGHT]), []).append("%s/%s" % (kind, name))
            # RULE 5
            if ARGO_HOOK not in ann and kind == "Job":
                errors.append("%s: Job/%s is a Helm hook with no %s. ArgoCD does not read %s, so it treats "
                              "this as an ordinary resource, re-applies it every sync, and the SECOND sync "
                              "fails on the immutable spec.template." % (label, name, ARGO_HOOK, HOOK))
            elif ann.get(ARGO_HOOK) == "PostSync" and APPROVER in name:
                errors.append("%s: the approver is an ArgoCD PostSync hook, which runs only after the whole "
                              "sync succeeds and every resource is Healthy — unreachable while the "
                              "Subscription is Progressing for want of this very approval." % label)

    if unwaved:
        errors.append("%s: %d resource(s) carry no %s, which ArgoCD reads as wave 0 — the Subscription's "
                      "wave: %s" % (label, len(unwaved), WAVE, ", ".join(unwaved)))

    def find(suffix, kind=None):
        return {n: v for (k, n), v in waves.items() if suffix in n and (kind is None or k == kind)}

    sub = find("", "Subscription")
    approver = find(APPROVER, "Job")
    reclaim = find(RECLAIM, "Job")
    verify = find(VERIFY, "Job")
    issuer = find("", "ClusterIssuer")

    # RULE 8 — every selector must match something, or the assertions below are dead code.
    for what, got in (("Subscription", sub), ("approver Job", approver), ("reclaim Job", reclaim),
                      ("verify Job", verify), ("ClusterIssuer", issuer)):
        if not got:
            errors.append("%s: no %s rendered — the ordering assertions that depend on it cannot run, so "
                          "this is failing rather than passing vacuously" % (label, what))

    if sub and approver:
        sub_wave = list(sub.values())[0][1]
        for n, (_, w, _) in approver.items():
            if w != sub_wave:  # RULE 2
                errors.append("%s: approver %s is at wave %d but the Subscription is at wave %d. A "
                              "Manual-approval Subscription reports Progressing until this Job runs, so "
                              "ArgoCD would never reach wave %d." % (label, n, w, sub_wave, w))

    if approver and reclaim:  # RULE 3
        a_weight = min(int(v[2][WEIGHT]) for v in approver.values() if WEIGHT in v[2])
        r_weight = min(int(v[2][WEIGHT]) for v in reclaim.values() if WEIGHT in v[2])
        if r_weight >= a_weight:
            errors.append("%s: the CSV reclaim is at %s=%d, not before the approver (%d). An orphaned CSV "
                          "makes the Subscription unresolvable and NO InstallPlan is ever staged, so the "
                          "approver would wait out its whole budget for something that cannot arrive."
                          % (label, WEIGHT, r_weight, a_weight))

    if verify and issuer:  # RULE 4
        v_wave = max(v[1] for v in verify.values())
        for n, (_, w, _) in issuer.items():
            if w <= v_wave:
                errors.append("%s: ClusterIssuer %s is at wave %d, not after the readiness gate (wave %d). "
                              "It is a cert-manager.io kind, and the gate is what waits for those CRDs and "
                              "for the webhook to serve admission." % (label, n, w, v_wave))

    # RULE 6 — each Job's RBAC must not arrive later than the Job.
    for comp in (APPROVER, RECLAIM, VERIFY):
        # Unpack (kind, name) — an earlier version destructured this dict as `n, v`, which made `comp in n`
        # a test against a TUPLE. It matched nothing, `continue` skipped the rule, and the check reported
        # success while asserting nothing. Caught by negative-testing the rule rather than trusting it.
        jobs = {n: v for (k, n), v in waves.items() if comp in n and k == "Job"}
        if not jobs:
            continue
        j_wave = min(v[1] for v in jobs.values())
        for (kind, n), (_, w, _) in waves.items():
            if comp in n and kind in ("ServiceAccount", "Role", "RoleBinding", "ClusterRole",
                                      "ClusterRoleBinding") and w > j_wave:
                errors.append("%s: %s/%s is at wave %d, AFTER the %s Job (wave %d) — a Job cannot "
                              "authenticate with a ServiceAccount that does not exist yet."
                              % (label, kind, n, w, comp, j_wave))

    # RULE 7
    for weight, holders in sorted(hooks.items()):
        if len(holders) > 1:
            errors.append("%s: hooks share %s=%d, so Helm falls back to name order and their sequence is "
                          "accidental: %s" % (label, WEIGHT, weight, ", ".join(holders)))
    return errors


def main():
    errors, seen = source_waves()
    print("  template sources: %d document(s), every one declaring a wave" % seen)
    for label, extra in COMBINATIONS:
        docs = render(extra)
        errs = check(label, docs)
        print("  %-18s %d resources, %d problem(s)" % (label, len(docs), len(errs)))
        errors += errs

    if errors:
        print()
        for e in errors:
            print("::error::%s" % e)
        print("\nFAILED: %d ordering problem(s)" % len(errors))
        return 1
    print("\nOK: every document declares a wave; the reclaim runs before the approver; the approver shares "
          "the Subscription's wave and is not PostSync; the gate precedes the ClusterIssuer; every Job "
          "carries an ArgoCD hook annotation and its own RBAC no later than itself; no two hooks share a "
          "weight.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
