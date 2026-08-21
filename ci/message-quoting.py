#!/usr/bin/env python3
"""Assert no shell message string closes its own quote early.

WHY — a defect that reached a live cluster and was reported by the operator:

    /bin/bash: line 123: }{.currentCSV}{n}{end}': Read-only file system
    [approver] installed cert-manager-operator.v1.20.0 is older than the pin v1.19.1

A `fail "..."` message embedded a jsonpath containing plain `"` instead of `\\"`. In a double-quoted bash
string that CLOSES the string, so the following ` -> ` was bare and bash read `>` as a REDIRECT. Two
consequences, and the second is the dangerous one:

  1. bash tried to create a file named `}{.currentCSV}{n}{end}'` — visible only because the container's
     root filesystem is read-only.
  2. THE `fail` NEVER RAN. Execution continued past it into the next branch, which reported v1.20.0 as
     "older than" v1.19.1 — a wrong verdict produced by a quoting bug.

`bash -n` cannot catch this: `fail "..." > file` is syntactically valid shell. Nor did running the script
locally — on a writable filesystem the redirect SUCCEEDS silently and creates a junk file in the working
directory, so the run looks like a pass. It was only the read-only container that surfaced it.

THE CHECK: for every `log "` / `fail "` in the rendered scripts, scan forward for the first UNESCAPED
closing quote. If anything other than whitespace follows it on that line, the message closed early and
whatever follows is being interpreted as shell.
"""

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
OPENERS = re.compile(r"\b(log|fail)\s+\"")


def offenders(script, where):
    bad = []
    for m in OPENERS.finditer(script):
        i = m.end()                       # first char inside the string
        while i < len(script):
            c = script[i]
            if c == "\\":
                i += 2
                continue
            if c == '"':
                break
            i += 1
        else:
            bad.append((where, "unterminated message string"))
            continue
        # what follows the closing quote, up to end of line
        eol = script.find("\n", i)
        rest = script[i + 1: eol if eol != -1 else len(script)]
        # Trailing shell after a message is often LEGITIMATE — `;;` ends a case branch, `\\` continues a
        # line, `&&`/`||` chain. A first version of this check flagged all of it and produced 30 false
        # positives on a correct chart, which would have trained everyone to ignore it.
        #
        # The defect is narrower: a REDIRECT. That is what turns a message into a file-write and stops the
        # `fail` from exiting. `>&2` and `>/dev/null` are the deliberate ones.
        r = rest.strip()
        if not r:
            continue
        probe = r.replace(">&2", "").replace(">/dev/null", "").replace("2>&1", "")
        if ">" in probe or "<" in probe:
            line = script[:i].count("\n") + 1
            bad.append((where, "line %d: message closes its quote early and a REDIRECT follows — bash would "
                               "write a file instead of printing, and the command would not exit: >>>%s<<<"
                        % (line, r[:70])))
    return bad


def main():
    combos = ([], ["--set", "clusterIssuer.venafi.tpp.caBundleSecretRef.enabled=true"])
    bad, seen = [], 0
    for extra in combos:
        out = subprocess.run(["helm", "template", "quote-probe", CHART] + extra,
                             capture_output=True, text=True)
        if out.returncode != 0:
            sys.exit("::error::helm template failed\n%s" % out.stderr)
        for d in yaml.safe_load_all(out.stdout):
            if not d or d.get("kind") != "Job":
                continue
            for c in d["spec"]["template"]["spec"]["containers"]:
                for part in (c.get("command") or []):
                    if "\n" not in part or "log " not in part:
                        continue
                    seen += 1
                    bad += offenders(part, "%s/%s" % (d["metadata"]["name"], c["name"]))

    # EVERY SELECTOR MUST MATCH SOMETHING: zero scripts inspected would pass forever.
    if seen == 0:
        bad.append(("(render)", "no inline Job script with log/fail found — this check covers nothing"))

    for where, msg in bad:
        print("::error::%s: %s" % (where, msg))
    print("inspected %d script(s), %d problem(s)" % (seen, len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
