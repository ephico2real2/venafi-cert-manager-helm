# Review: how the InstallPlan approver decides AHEAD / BEHIND / EQUAL

**Status:** open for review. Shipped behaviour is 0.3.9 (`f71243c`), deployed on CRC as release rev 24.

**What is being asked for:** a better implementation of one small decision, and documentation of it that a
junior engineer can follow. Not a rewrite of the approver.

---

## 1. The decision, stated exactly

The approver Job runs at helm hook-weight `-5`. Before it will approve anything it must classify two
strings:

| name | where it comes from | example |
|---|---|---|
| `INSTALLED` | `.status.installedCSV` on the live Subscription — what OLM actually installed | `cert-manager-operator.v1.19.1` |
| `APPROVE_CSV` | `operator.startingCSV` from values, injected as an env var | `cert-manager-operator.v1.19.1` |

Into exactly one of three outcomes:

| outcome | meaning | what the Job does |
|---|---|---|
| **EQUAL** | already at the pin | log, report other pending plans, `exit 0` |
| **AHEAD** | cluster is running something NEWER than the pin | warn, approve nothing, `exit 0` |
| **BEHIND** | pin is newer — a normal upgrade | fall through and approve that one InstallPlan |

Getting AHEAD wrong is the expensive case. A cluster misclassified as BEHIND makes the Job wait its entire
600-second budget for an InstallPlan OLM will never stage, then fail. That is not hypothetical — it was
reported from a real 4.20 cluster: six minutes of `waiting for OLM to stage an InstallPlan for v1.19.1`
with v1.20.0 already installed.

## 2. What ships today

`charts/cert-manager-venafi/templates/installplan-approver-job.yaml`

Exact equality first, at line 292:

```bash
if [ "$INSTALLED" = "$APPROVE_CSV" ]; then
  log "already installed at ${APPROVE_CSV}; no InstallPlan to approve"
  report_unapproved_others
  exit 0
fi
```

Then ordering, lines 303–305:

```bash
inst_v="$(printf '%s' "$INSTALLED"   | sed 's/^.*\.v//')"
want_v="$(printf '%s' "$APPROVE_CSV" | sed 's/^.*\.v//')"
newer="$(printf '%s\n%s\n' "$inst_v" "$want_v" | sort -V | tail -1)"
if [ "$newer" = "$inst_v" ]; then
  # AHEAD
```

And a render-time guard at line 57 rejects pins the above cannot handle:

```
{{- if and .Values.operator.startingCSV (not (regexMatch "^.+\\.v[0-9]+(\\.[0-9]+)*$" .Values.operator.startingCSV)) }}
```

## 3. Measurements already taken — do not redo these, build on them

All run inside `registry.redhat.io/openshift4/ose-cli:latest` on the cluster, `sort (GNU coreutils) 8.30`.

**Correct today (7/7).** `v1.20.1`, `v1.19.2`, `v1.20.10`, `v2.0.0` all classify AHEAD of `v1.19.1`;
`v1.10.0` classifies BEHIND `v1.19.1`, and `v1.9.0` classifies BEHIND `v1.10.0`. That last one is the
case a lexical compare gets wrong, so `sort -V` is doing real work. *(Corrected in pass 1: an earlier
revision grouped `v1.10.0` under AHEAD — transcribed from a v1.10.0-vs-v1.9.0 row. BEHIND is ground
truth, and what ships produces it.)*

**Wrong today.** Two shapes, both now rejected at render time rather than fixed:

- `cert-manager-operator.1.19.1` — no `.v`, so the `sed` does not match and the **whole name** is compared.
- `cert-manager-operator.v1.20.1-rc.1` — `sort -V` is not semver-aware and orders a pre-release **after**
  the release it precedes, so an rc installed against a final-release pin reports "the cluster is NEWER".

**The finding that matters most.** `sort -V` puts every `v`-less name before every `v`-ful one — the `v`
dominates, not the numbers:

```
cert-manager-operator.1.19.1
cert-manager-operator.1.21.0
cert-manager-operator.v1.19.1     <- so 1.21.0 is judged OLDER than v1.19.1
cert-manager-operator.v1.21.0
```

So on the five mixed `v`/no-`v` pairs: **today's logic is wrong on three**, and passing the two full CSV
names to `sort -V` with no `sed` at all — which looks strictly better — **is wrong on three as well**
*(corrected in pass 1 from an under-count of two)*, including calling a behind cluster AHEAD. The counts
are equal by structure, not coincidence: the two comparisons return opposite verdicts on every mixed
ordering pair, and both fail the same-version-two-spellings pair. It trades one set of wrong answers for
its mirror image. Do not propose it.

**A candidate that measured clean.** Normalising both sides with `sed 's/^[^.]*\.v\?//'` and comparing the
**normalised** values for equality as well as ordering was correct on all 14 pairs, including all five
mixed. Two details it exposed: `[^.]*` is load-bearing (a greedy `.*` with an optional `v` backtracks to
the last dot and yields a single digit), and normalising only the ordering leaves `v1.19.1` against a
`1.19.1` pin skipping the equality branch and reporting the cluster as NEWER than an identical version.
A third detail, found in pass 1: the `[^.]*` first-dot anchor is WRONG for dotted package names —
`my.pkg.v1.2.3` normalises to `pkg.v1.2.3`, where the shipped greedy `.v` split extracts `1.2.3` — so a
successor must keep a greedy last-`.v` split for v-ful names and confine first-dot splitting to v-less
ones.

This candidate is a baseline to beat, not a decision. It is still `sed`, and `sed` is not wanted here.

## 4. Constraints — these are hard

1. **The pod is `/bin/bash` on `registry.redhat.io/openshift4/ose-cli:latest`** — RHEL, GNU coreutils 8.30.
   **Do not develop or verify on macOS.** BSD `sed`, `sort`, `grep` and `date` differ from GNU in flags and
   in behaviour, and a fix validated on a mac can be wrong in the pod. This project has already shipped one
   Linux-only flag (`pgrep -fc`) in a sibling repo and one macOS-shell breakage in this one. **Spin up a pod
   with that exact image and run your candidate there.** `oc run` with `--rm -i --restart=Never` and
   `command: ["/bin/bash","-s"]` piping a heredoc works; three such runs are in this session's history.
2. **`sed` is not wanted.** The operator's words: *"i am not crazy about sed so it can use other methods."*
   Bash parameter expansion, `case`, `IFS` splitting, `awk`, `sort -V`, arithmetic — all fair. Justify the
   choice on readability and on what is guaranteed present in that image.
3. **No new dependency.** Whatever you use must already exist in `ose-cli`. Verify it there, don't assume.
4. **Messages are `printf`'d, never `eval`'d**, and must contain **no `"` characters**. An unescaped `"`
   closed a bash string here, exposed `>` as a redirect, and made a `fail` silently not exit — printing a
   wrong verdict. `ci/message-quoting.py` enforces this. Any command shown to a human must also survive
   being pasted into **zsh** (macOS default): `[*]` unquoted is a glob there, so single-quote it.
5. **Keep the comment style.** Comments say WHY — the constraint, the measurement, the trap. A replacement
   that strips the reasoning is a regression even when the logic is right.
6. **The render-time guard is a feature, not a workaround.** If your solution handles a shape the guard
   currently rejects, say so and relax the guard deliberately. If a shape genuinely cannot be compared,
   refusing to render remains the right answer — a loud early failure beats a silent wrong verdict.

## 5. What to deliver

Write your findings into **this file**, in a new section `## Fable — pass 1`, using `> **Fable:**` markers.
Do not create another document and do not edit any other file — application comes later, supervised.

1. **A verdict on the current implementation.** Prefer refutation. If you think a claim in §3 is wrong, say
   which and show the command and output that refutes it.
2. **A better solution, as complete code.** The full replacement block, ready to drop in — not a diff
   fragment, not "add a check". Include the surrounding `if`/`case` structure and the comments.
3. **Junior-readable documentation of it.** Someone who has never seen OLM should be able to read your
   explanation and know why each line is there. State what happens on each of the three outcomes.
4. **A test you actually ran, in the pod, with its output pasted.** Cover at minimum: the 7 correct cases,
   the 5 mixed pairs, the two malformed shapes, the same-version-different-spelling pair, and equality.
   Show the `sort --version` / tool versions from inside the image so the environment is on the record.
5. **The failure modes you did not fix**, named explicitly, and why leaving them is right.

Do not apply anything yet.

---

## Fable — pass 1

### 1. Verdict on the current implementation, with two corrections to §3

> **Fable:** The shipped logic is correct on every input the render guard admits — I reproduced all of
> §3's v-ful measurements in the Job's own image (environment block and full output in §4 below) and
> found no wrong verdict among them. Its failure surface is exactly the side the guard cannot reach:
> `INSTALLED` is written by the cluster, so when the catalog spelling and the pin spelling diverge, the
> `sed` leaves a whole CSV name on one side of `sort -V` and the verdict is decided by `v`-versus-no-`v`
> instead of by numbers. The replacement in §2 removes `sed` entirely, is verdict-for-verdict identical
> to the shipped code on every name carrying `.v<digit>` (measured), and additionally classifies all
> five mixed orderings and the same-version-two-spellings pair correctly.

> **Fable:** **Refutation 1 — §3's AHEAD list contains a pair that is BEHIND.** The sentence
> "`v1.20.1`, `v1.19.2`, `v1.20.10`, `v1.10.0`, `v2.0.0` all classify AHEAD of `v1.19.1`" is wrong for
> `v1.10.0`: 1.10 < 1.19, and the shipped code agrees. Measured in the image (`fable-csvcmp-1`,
> shipped classifier = lines 284–306 verbatim):
>
> ```
> PASS new=BEHIND  shipped=BEHIND  expect=BEHIND  inst=cert-manager-operator.v1.10.0  pin=cert-manager-operator.v1.19.1
> ```
>
> The "correct today (7/7)" verdict stands — BEHIND is the right answer and the shipped code produces
> it — but the pair is listed under the wrong outcome. It belongs beside `v1.9.0` vs `v1.10.0` as a
> second case where `sort -V` beats a lexical compare.

> **Fable:** **Refutation 2 — "the full-name compare is wrong on two" is an undercount; it is wrong on
> four of the five.** On the same five mixed pairs where §3's "today's logic is wrong on three"
> reproduces exactly (wrong on the two spelling pairs and on `1.19.1` vs `v1.21.0` — measured), the
> full-name `sort -V` compare is wrong on four. Full eight-ordering table from the pod, truth in
> column 1:
>
> ```
> truth=EQUAL   shipped=AHEAD   fullname=BEHIND  inst=cert-manager-operator.1.19.1   pin=cert-manager-operator.v1.19.1
> truth=BEHIND  shipped=AHEAD   fullname=BEHIND  inst=cert-manager-operator.1.19.1   pin=cert-manager-operator.v1.21.0
> truth=AHEAD   shipped=AHEAD   fullname=BEHIND  inst=cert-manager-operator.1.21.0   pin=cert-manager-operator.v1.19.1
> truth=EQUAL   shipped=AHEAD   fullname=BEHIND  inst=cert-manager-operator.1.21.0   pin=cert-manager-operator.v1.21.0
> truth=EQUAL   shipped=BEHIND  fullname=AHEAD   inst=cert-manager-operator.v1.19.1  pin=cert-manager-operator.1.19.1
> truth=BEHIND  shipped=BEHIND  fullname=AHEAD   inst=cert-manager-operator.v1.19.1  pin=cert-manager-operator.1.21.0
> truth=AHEAD   shipped=BEHIND  fullname=AHEAD   inst=cert-manager-operator.v1.21.0  pin=cert-manager-operator.1.19.1
> truth=EQUAL   shipped=BEHIND  fullname=AHEAD   inst=cert-manager-operator.v1.21.0  pin=cert-manager-operator.1.21.0
> ```
>
> The table exposes the structure: on raw names the v-ful side ALWAYS wins under `sort -V` (the
> v-dominance finding, reproduced below), while under the shipped `sed` the v-LESS side always wins —
> its name survives whole, and digits sort before letters (also measured: `1.19.1` sorts before
> `garbage`). Shipped and full-name therefore give OPPOSITE verdicts on every mixed pair, so "wrong on
> three" and "wrong on two" cannot both hold for any five pairs that include even one
> same-version-two-spellings pair. The conclusion is unchanged and strengthened: the full-name compare
> trades three wrong answers for four, and §3 is right to forbid proposing it.

> **Fable — retraction (post-review):** I retract the count of FOUR as a claim about §3's five mixed
> pairs. The measurement was real but ran over a different pair selection: mine held two spelling pairs
> and three ordering pairs, and the structural identity wrongShipped + wrongFullname = orderings +
> 2 x spellings makes that 3 + 4. The review's own set — re-measured by the coordinator in the same
> image: shipped=3, full-name=3 — therefore holds one spelling pair and four ordering pairs. What
> survives is the structure, and §3 now records it: opposite verdicts on every ordering pair plus a
> shared failure on the spelling pair force the two error counts to be equal. The template comment was
> applied with the corrected count, and the companion text in §2 below is aligned to match.

> **Fable:** **Confirmed, with output pasted in §4:** the v-dominance quartet orders exactly as §3
> shows; `1.20.1-rc.1` sorts after `1.20.1`; and the `[^.]*` load-bearing claim is real — the greedy
> variant collapses to a single digit:
>
> ```
> $ printf 'cert-manager-operator.v1.19.1' | sed 's/^.*\.v\?//'
> 1
> ```

> **Fable:** **One latent flaw in §3's blessed baseline, which the replacement avoids.** The baseline
> `sed 's/^[^.]*\.v\?//'` anchors on the FIRST dot for every name, so a dotted package name loses only
> its first segment — a regression against the shipped greedy `sed`, which strips through the LAST
> `.v` and handles dotted packages correctly. Measured in the image:
>
> ```
> $ printf 'my.pkg.v1.2.3' | sed 's/^[^.]*\.v\?//'
> pkg.v1.2.3        <- baseline: garbage
> ```
>
> The shipped code and the replacement both extract `1.2.3` here (matrix rows in §4). This chart's
> package has no dot, so none of the 14 measured pairs could catch it — but the replacement keeps the
> greedy last-`.v` split for v-ful names precisely so it never trades that correctness away.

### 2. The replacement

> **Fable:** **Choice and justification.** Extraction moves into bash itself: one `case` and two
> parameter expansions — no `sed`, no `awk`, no subprocess at all. Everything used is in the image
> because it IS the shell the Job already runs (`GNU bash 4.4.20` on RHEL 8.10, read from inside the
> image, §4). `case`/`${var##pattern}` are also the idioms this script already uses everywhere else
> (`case " $owner "`, `${RESOLVE#True|}`), so a reviewer meets no new dialect. `awk` exists in the
> image but would be a second language for a three-line job. `sort -V` STAYS: the operator's objection
> was to `sed`, and `sort -V` is the piece doing verifiable work — `1.9` before `1.10` — that neither
> parameter expansion nor `[` arithmetic gives without hand-rolling a loop that would itself need this
> whole review. Ordering is fed BARE VERSIONS on both sides, and equality is decided on the bare
> versions too, which is what makes the five mixed orderings come out right.

> **Fable:** **Behaviour preservation, stated exactly.** For every name containing `.v<digit>` —
> which is every name the render guard admits as a pin and every CSV this catalog has ever published —
> `${name##*.v}` strips the identical characters the shipped `sed 's/^.*\.v//'` strips, and the
> AHEAD/BEHIND/EQUAL verdicts are identical (measured side by side on all 20 matrix rows, §4). The
> only behavioural deltas are on cluster-written shapes the shipped code classifies wrongly: v-less
> names now normalise instead of surviving whole, and a normalised tie is now reported as the same
> version spelled two ways instead of as "the cluster is NEWER than an identical version".

> **Fable:** **The render guard is deliberately NOT relaxed.** The comparison now tolerates v-less
> names, but the guard was never only about comparison: steps 3 and 5 of the Job match
> `APPROVE_CSV` against `.spec.clusterServiceVersionNames[*]` and use it as the CSV resource name
> **byte-for-byte**. A pin spelled differently from the catalog classifies correctly forever and
> approves nothing — a 600-second timeout instead of a render error. And the rc shape stays refused
> because the PIN direction is the expensive one, measured in the image (`fable-csvcmp-3`):
>
> ```
> rc as PIN, final installed:   inst=cert-manager-operator.v1.20.1 pin=cert-manager-operator.v1.20.1-rc.1 -> BEHIND
> ```
>
> Truth is AHEAD (an rc precedes its final), so an rc pin would send the Job into the search loop to
> burn its whole budget. Both `helm template` rejections re-verified during this pass; a well-formed
> pin renders (exit 0).

> **Fable:** **Drop-in replacement for
> `charts/cert-manager-venafi/templates/installplan-approver-job.yaml` lines 273–337** — the whole of
> section 2 of the script, from the `# --- 2. already at the pin?` banner through the closing `fi`
> before section 3. Indentation matches the file (script body at 14 spaces). The first equality test,
> the AHEAD block, the BEHIND tail and the first-install tail are byte-identical to what ships; the
> new material is the `csv_version` helper, the bare-version equality branch, and the reworked
> comments:
>
> ```bash
>               # --- 2. already at the pin? --------------------------------------------------------------
>               # THREE CASES, not two. An earlier version tested only `installed == pin` and fell through to
>               # the search loop otherwise — so when the cluster was running a version the pin had been
>               # overtaken by, it hunted for an InstallPlan that OLM will never stage (there is no downgrade
>               # path) and burned its entire budget before failing with a message about the channel. Reported
>               # from a real 4.20 cluster: installedCSV=v1.20.0, pin=v1.19.1, six minutes of
>               # "waiting for OLM to stage an InstallPlan for v1.19.1".
>               #
>               # operator.startingCSV is OLM's STARTING point, not a ceiling — once the operator is past it,
>               # that version is history. So a mismatch has to be classified, not waited on.
>               INSTALLED="$(oc get "$SUBS" "$SUBSCRIPTION" -n "$NAMESPACE" -o jsonpath='{.status.installedCSV}' 2>/dev/null || true)"
>               if [ "$INSTALLED" = "$APPROVE_CSV" ]; then
>                 log "already installed at ${APPROVE_CSV}; no InstallPlan to approve"
>                 # Before the search loop, which is the only other place a non-matching plan gets logged. In
>                 # the steady state — installed at the pin, an upgrade waiting in the channel — skipping this
>                 # would make a deliberate refusal look like an oversight.
>                 report_unapproved_others
>                 exit 0
>               fi
>
>               if [ -n "$INSTALLED" ]; then
>                 # Bare version out of a CSV name: cert-manager-operator.v1.19.1 -> 1.19.1. Bash case and
>                 # parameter expansion — no sed, no fork. The stripping is load-bearing, not cosmetic:
>                 # measured in this image, `sort -V` orders EVERY v-less name before EVERY v-ful one, so on
>                 # raw names the `v` outvotes the numbers — only bare versions compare honestly if the
>                 # catalog ever drops its `v`.
>                 #
>                 # The first branch splits at the LAST `.v` (## strips the longest prefix), so a dotted
>                 # package name cannot bleed into the version. The v-less fallback can only anchor on the
>                 # FIRST dot — right for every undotted package name. The guard at the top of this file
>                 # refuses v-less PINS, but installedCSV is written by the cluster, out of any render-time
>                 # check's reach, so here it has to be tolerated rather than rejected.
>                 csv_version() {
>                   local ver
>                   case "$1" in
>                     *.v[0-9]*) printf '%s' "${1##*.v}" ;;
>                     *.*)       ver="${1#*.}"; printf '%s' "${ver#v}" ;;
>                     *)         printf '%s' "$1" ;;
>                   esac
>                 }
>                 inst_v="$(csv_version "$INSTALLED")"
>                 want_v="$(csv_version "$APPROVE_CSV")"
>
>                 if [ "$inst_v" = "$want_v" ]; then
>                   # Same version, spelled two ways — what a half-finished catalog rename looks like. On raw
>                   # names this skips the equality branch above and gets reported as the cluster being NEWER
>                   # than an identical version. Nothing to approve either way, but the pin still needs fixing
>                   # in git: the approve and verify steps below match CSV names byte-for-byte, so a pin
>                   # spelled differently from the catalog can classify forever yet never approve anything.
>                   log "already installed at version ${inst_v} — but the cluster spells it ${INSTALLED} and the pin spells it ${APPROVE_CSV}"
>                   log "  update operator.startingCSV to the exact spelling the cluster reports; approval matches CSV names byte-for-byte, so the spellings must agree before the next upgrade"
>                   report_unapproved_others
>                   exit 0
>                 fi
>
>                 # `sort -V` stays (GNU coreutils 8.30 in this image): 1.9 before 1.10 needs a version
>                 # compare, not a string compare. Equality is settled above, so tail -1 is the strictly
>                 # newer of the two.
>                 newer="$(printf '%s\n%s\n' "$inst_v" "$want_v" | sort -V | tail -1)"
>                 if [ "$newer" = "$inst_v" ]; then
>                   # AHEAD. OLM does not downgrade, so no InstallPlan for the pin can ever appear. This is real
>                   # drift — with installPlanApproval: Manual the operator cannot have upgraded itself, so
>                   # either a human approved a plan or the operator predates this release adopting it.
>                   #
>                   # WARN AND CONTINUE, not fail. An earlier version failed here. Failing blocks every later
>                   # `helm upgrade` — and every ArgoCD sync — on a condition this Job cannot fix and that is
>                   # not itself harmful: the operator is running, newer than asked, and nothing this Job does
>                   # would change that. Blocking the release does not correct the pin; only a commit does.
>                   #
>                   # EXIT 0 RATHER THAN FALL THROUGH. The search loop below would hunt for an InstallPlan OLM
>                   # will never stage and burn the entire budget before failing — the exact behaviour reported
>                   # from a 4.20 cluster: six minutes of "waiting for OLM to stage v1.19.1" with v1.20.0 up.
>                   log "WARNING: the cluster is running ${INSTALLED}, which is NEWER than operator.startingCSV (${APPROVE_CSV})."
>                   log "  OLM does not downgrade, so no InstallPlan for ${APPROVE_CSV} will ever be staged — that pin"
>                   log "  describes a past state. This Job approves nothing and the operator stays at ${INSTALLED}."
>                   log "  Adopt what is running, in git:  operator.startingCSV: ${INSTALLED}"
>                   log "  Not merely tidiness: startingCSV is where OLM BEGINS on a fresh install, so a reinstall"
>                   log "  would start at ${APPROVE_CSV} instead of ${INSTALLED}."
>                   log "  Going back to ${APPROVE_CSV} is not something this Job will do — OLM cannot downgrade, so"
>                   log "  the CSV would have to be removed and reinstalled, taking cert-manager down."
>                   log "  What is on offer: oc get packagemanifest ${SUBSCRIPTION} -n {{ .Values.operator.sourceNamespace }} -o 'custom-columns=CHANNEL:.status.channels[*].name,HEAD:.status.channels[*].currentCSV'"
>                   # Worth knowing while ahead: a FURTHER upgrade may already be staged and waiting.
>                   report_unapproved_others
>                   exit 0
>                 fi
>                 # BEHIND. The pin was moved forward in git and the cluster has not caught up — the normal
>                 # upgrade path. OLM stages a plan for it and the loop below approves exactly that one.
>                 log "installed ${INSTALLED} is older than the pin ${APPROVE_CSV} — this is an upgrade; waiting for OLM to stage it"
>               else
>                 log "installedCSV=<none yet> — first install"
>               fi
> ```
>
> **Fable:** After this change the script contains zero `sed`. The two new `log` lines contain no `"`
> characters and no copy-pasteable `-o` argument with an unquoted jsonpath subscript, so both rules in
> `ci/message-quoting.py` are satisfied; the retained AHEAD message keeps its single-quoted
> `custom-columns` argument, which is what makes it zsh-paste-safe.

> **Fable:** **Companion edit required in the SAME supervised application — the top-of-file comment
> and guard message go stale otherwise.** Two claims in the comment at lines 5–56 become false the
> moment the block above lands: "a rename cannot be compared correctly by any string ordering" (lines
> 35–36 — refuted by measurement: normalised comparison classifies all five mixed orderings
> correctly), and the "THE FIX AT THAT POINT" instructions (lines 38–49), which prescribe the two
> `sed` edits this change supersedes. House style says comments carry the reasoning, so shipping the
> new code under the old comment would be a regression by this file's own standard. Suggested
> replacement for the comment block (lines 5–56), preserving the parts still true:
>
> ```
> {{- /*
>      The pin's SHAPE is checked here, at render time, because a malformed pin fails in ways a log
>      cannot make obvious. Version COMPARISON survives both shapes below — the Job normalises each
>      side to its bare version (after the last `.v`, or after the first dot when no `.v` exists)
>      before ordering with `sort -V`, and all five mixed v/no-v orderings measured correct in the
>      Job's own image. What does NOT survive is APPROVAL: the search loop and the verify step match
>      operator.startingCSV against .spec.clusterServiceVersionNames and the CSV resource name
>      BYTE-FOR-BYTE, so a pin spelled differently from the catalog classifies correctly forever,
>      approves nothing, and burns the Job's whole budget instead of failing at render. Two shapes
>      are refused:
>
>        cert-manager-operator.1.19.1     no `.v`. Every CSV this package has ever published is
>                                         <package>.v<version> (12 of 12 in the catalog), so a v-less
>                                         pin is a typo today — and a typo the search loop would pay
>                                         for in 600 wasted seconds instead of a render error.
>        cert-manager-operator.v1.20.1-rc.1
>                                         `sort -V` is not semver-aware: a pre-release orders AFTER
>                                          the release it precedes. Measured in the Job's image: an rc
>                                          PIN against its own installed final classifies BEHIND, so
>                                          the Job would hunt for an InstallPlan OLM will never stage
>                                          and burn its entire budget. No such CSV has ever been
>                                          published either.
>
>      ── IF THE CATALOG ITSELF EVER DROPS THE `v` ──────────────────────────────────────────────────
>
>      The comparison side is already rename-proof, including the half-finished state where the
>      cluster runs v1.19.1 while the catalog offers 1.21.0 — bare numbers order that correctly, and
>      the same version spelled two ways is reported as installed, not as drift. The only edits
>      needed here: copy the exact new spelling from the catalog into operator.startingCSV, and relax
>      the regexMatch below to make the `v` optional. Do not relax it before the catalog actually
>      renames — the byte-for-byte argument above is version-independent. (An earlier note here
>      prescribed re-normalising both sides with a first-dot sed; superseded — and wrong for dotted
>      package names, turning my.pkg.v1.2.3 into pkg.v1.2.3 where the Job's last-`.v` split extracts
>      1.2.3.)
>
>      DO NOT "SIMPLIFY" THE COMPARISON BY PASSING THE TWO FULL CSV NAMES TO sort -V. Measured in the
>      Job's image: `sort -V` orders every v-less name before every v-ful one, so the `v` dominates
>      the comparison rather than the numbers —
>
>          cert-manager-operator.1.19.1      <- v-less first, whatever the version
>          cert-manager-operator.1.21.0
>          cert-manager-operator.v1.19.1     <- v-ful after, so 1.21.0 is judged OLDER than v1.19.1
>          cert-manager-operator.v1.21.0
>
>      Measured on the five mixed pairs: the full-name compare is wrong on three — exactly as many as
>      the sed comparison this release replaced, because the two return opposite verdicts on every
>      ordering pair and both fail the same-version-two-spellings pair — including calling a cluster
>      that is BEHIND its pin AHEAD. It trades one set of wrong answers for its mirror image. Bare
>      numbers or nothing. (Count corrected post-review; see the retraction in section 1.)
> */}}
> ```
>
> And one clause of the `fail` message at line 58 needs the same truth transplant: replace "The
> approver strips everything through '.v' and compares the rest with `sort -V`; a name without '.v'
> makes it compare the whole string (a cluster AHEAD of the pin is then misread as BEHIND, and the
> Job waits its full budget for an InstallPlan that will never be staged), and a pre-release suffix
> such as -rc.1 sorts AFTER the release it precedes, so an upgrade would be declined as 'newer'" with
> "The approver approves and verifies by matching this name byte-for-byte against what the catalog
> stages, so a spelling the catalog does not use approves nothing and the Job burns its whole budget
> waiting; and a pre-release suffix such as -rc.1 sorts AFTER the release it precedes under sort -V,
> so an rc pin against its own final classifies as an upgrade that can never arrive". The later
> sentence "the comment above this check says exactly which two lines to change" should also drop
> "two lines" in favour of "which one regex to relax", since the sed edits it referred to no longer
> exist.

### 3. How the decision works — for a reader who has never seen OLM

> **Fable:** Background in three sentences. OLM (Operator Lifecycle Manager) installs operators from
> a catalog; each installable version is a CSV (ClusterServiceVersion) whose NAME encodes package and
> version, like `cert-manager-operator.v1.19.1`. This chart pins one CSV name
> (`operator.startingCSV`) and refuses to let the cluster auto-upgrade past it: OLM stages each
> upgrade as an "InstallPlan" that waits for approval, and this Job is the only approver — it approves
> the pinned version and nothing else. Before approving anything it must work out how the cluster's
> current version (`INSTALLED`, read from the live Subscription) relates to the pin (`APPROVE_CSV`).
>
> The decision, line by line:
>
> 1. `[ "$INSTALLED" = "$APPROVE_CSV" ]` — the two names are byte-identical. **EQUAL.** Nothing to
>    install. The Job logs it, lists any pending InstallPlans it deliberately is not approving (so a
>    refusal is visible in the log, not mistakable for a bug), and exits 0. The release succeeds.
> 2. `csv_version` — turns a CSV name into a bare version so numbers, not spellings, get compared.
>    Three shapes, three branches of the `case`:
>    - `*.v[0-9]*` (every name this catalog has ever produced): cut everything through the LAST
>      `.v`. `##` means "remove the longest matching prefix", which is what stops a package name
>      that itself contains a dot from leaking into the version.
>    - `*.*` (no `.v` anywhere — only possible if the catalog someday renames its CSVs): cut through
>      the first dot, then shed a single leading `v` if present.
>    - anything else: leave it alone; there is no version to find.
> 3. `[ "$inst_v" = "$want_v" ]` — bare versions match but the raw names did not: the same version
>    spelled two ways. **EQUAL in substance.** The Job says so, tells the operator to fix the pin's
>    spelling in git (approval later in the script matches names byte-for-byte, so the spelling
>    matters even though the version is right), and exits 0.
> 4. `sort -V | tail -1` — version-aware ordering of the two bare versions; the last line is the
>    newer. `sort -V` is used because plain string ordering says `1.9` > `1.10`, and both versions
>    are different by this point, so "newer" is unambiguous.
>    - Newer one is the CLUSTER's: **AHEAD.** OLM never downgrades, so no InstallPlan for the pin
>      will ever exist — waiting for one is the six-minute failure this review opened with. The Job
>      warns loudly (the pin describes the past; adopt the running version in git), approves
>      nothing, and exits 0 so the release is not blocked by something only a git commit can fix.
>    - Newer one is the PIN's: **BEHIND.** This is the one normal case: someone moved the pin
>      forward and the cluster has not caught up. The Job falls through to the search loop, waits
>      for OLM to stage the InstallPlan naming exactly the pinned CSV, approves that one plan, and
>      then waits for the CSV to report Succeeded.
> 5. Empty `INSTALLED` — nothing installed yet: first install. Same fall-through as BEHIND.

### 4. Tests actually run, in the pod, with output

> **Fable:** All runs used throwaway pods on the CRC cluster with the Job's exact image, via the
> established pattern (unique name per run, `--rm`, script on stdin):
>
> ```
> oc run fable-csvcmp-N --rm -i --restart=Never --quiet \
>   --image=registry.redhat.io/openshift4/ose-cli:latest \
>   --overrides='{"spec":{"containers":[{"name":"t","image":"registry.redhat.io/openshift4/ose-cli:latest","command":["/bin/bash","-s"],"stdin":true,"stdinOnce":true}]}}' \
>   -- /bin/bash -s < scratchpad/<script>.sh
> ```
>
> Environment, read from inside the image on every run:
>
> ```
> PRETTY_NAME="Red Hat Enterprise Linux 8.10 (Ootpa)"
> GNU bash, version 4.4.20(1)-release (x86_64-redhat-linux-gnu)
> sort (GNU coreutils) 8.30
> tail (GNU coreutils) 8.30
> sed (GNU sed) 4.5          <- for the record only; the replacement uses none
> ```

> **Fable:** **Run 1 (`fable-csvcmp-1`) — the classification matrix.** Candidate (`new`) beside the
> shipped classifier, expectations are ground truth except the row marked safe-wrong. 20/20:
>
> ```
> PASS new=AHEAD        shipped=AHEAD        expect=AHEAD        inst=cert-manager-operator.v1.20.1      pin=cert-manager-operator.v1.19.1      doc-s3 correct set
> PASS new=AHEAD        shipped=AHEAD        expect=AHEAD        inst=cert-manager-operator.v1.19.2      pin=cert-manager-operator.v1.19.1      doc-s3 correct set
> PASS new=AHEAD        shipped=AHEAD        expect=AHEAD        inst=cert-manager-operator.v1.20.10     pin=cert-manager-operator.v1.19.1      doc-s3 correct set
> PASS new=AHEAD        shipped=AHEAD        expect=AHEAD        inst=cert-manager-operator.v2.0.0       pin=cert-manager-operator.v1.19.1      doc-s3 correct set
> PASS new=BEHIND       shipped=BEHIND       expect=BEHIND       inst=cert-manager-operator.v1.10.0      pin=cert-manager-operator.v1.19.1      doc-s3 lists this as AHEAD -- refutation probe
> PASS new=BEHIND       shipped=BEHIND       expect=BEHIND       inst=cert-manager-operator.v1.9.0       pin=cert-manager-operator.v1.10.0      the lexical trap sort -V exists for
> PASS new=EQUAL        shipped=EQUAL        expect=EQUAL        inst=cert-manager-operator.v1.19.1      pin=cert-manager-operator.v1.19.1      exact equality
> PASS new=BEHIND       shipped=BEHIND       expect=BEHIND       inst=cert-manager-operator.v1.19.1      pin=cert-manager-operator.v1.20.0      a plain upgrade
> PASS new=EQ-SPELLING  shipped=AHEAD        expect=EQ-SPELLING  inst=cert-manager-operator.1.19.1       pin=cert-manager-operator.v1.19.1      mixed: same version, two spellings
> PASS new=EQ-SPELLING  shipped=BEHIND       expect=EQ-SPELLING  inst=cert-manager-operator.v1.19.1      pin=cert-manager-operator.1.19.1       mixed: same version, two spellings, reversed
> PASS new=AHEAD        shipped=AHEAD        expect=AHEAD        inst=cert-manager-operator.1.21.0       pin=cert-manager-operator.v1.19.1      mixed: catalog renamed AND moved on
> PASS new=BEHIND       shipped=BEHIND       expect=BEHIND       inst=cert-manager-operator.v1.19.1      pin=cert-manager-operator.1.21.0       mixed: the pair full-name sort -V calls AHEAD
> PASS new=BEHIND       shipped=AHEAD        expect=BEHIND       inst=cert-manager-operator.1.19.1       pin=cert-manager-operator.v1.21.0      mixed
> PASS new=EQ-SPELLING  shipped=AHEAD        expect=EQ-SPELLING  inst=cert-manager-operator.1.19.1       pin=cert-manager-operator.v1.19.1      malformed shape 1 (v-less) as installedCSV
> PASS new=AHEAD        shipped=AHEAD        expect=AHEAD        inst=cert-manager-operator.v1.20.1-rc.1 pin=cert-manager-operator.v1.19.1      rc vs an OLDER final: genuinely ahead
> PASS new=AHEAD        shipped=AHEAD        expect=AHEAD        inst=cert-manager-operator.v1.20.1-rc.1 pin=cert-manager-operator.v1.20.1      rc vs ITS OWN final: safe-wrong on purpose (truth BEHIND)
> PASS new=BEHIND       shipped=BEHIND       expect=BEHIND       inst=cert-manager-operator.v1.20.1-rc.1 pin=cert-manager-operator.v1.21.0      rc vs a NEWER final
> PASS new=BEHIND       shipped=BEHIND       expect=BEHIND       inst=my.pkg.v1.2.3                      pin=my.pkg.v1.10.0                     dotted package name, greedy last-.v split
> PASS new=AHEAD        shipped=AHEAD        expect=AHEAD        inst=my.pkg.v1.10.0                     pin=my.pkg.v1.2.3                      dotted package name, reversed
> PASS new=BEHIND       shipped=BEHIND       expect=BEHIND       inst=cert-manager-operator.v1           pin=cert-manager-operator.v1.19.1      single-segment version
> candidate: pass=20 fail=0
> ```
>
> Same run — the §3 findings reproduced, plus the probes behind the structural argument in §1:
>
> ```
> === raw-name sort -V: the v-dominance finding, reproduced ===
> cert-manager-operator.1.19.1
> cert-manager-operator.1.21.0
> cert-manager-operator.v1.19.1
> cert-manager-operator.v1.21.0
>
> -- pre-release vs its final (doc says rc sorts AFTER) --
> 1.20.1
> 1.20.1-rc.1
> -- dotless garbage vs a number --
> 1.19.1
> garbage
> -- empty string vs a number --
>
> 1.19.1
>
> === extraction table ===
> cert-manager-operator.v1.19.1        -> [1.19.1]
> cert-manager-operator.1.19.1         -> [1.19.1]
> cert-manager-operator.v1.20.1-rc.1   -> [1.20.1-rc.1]
> my.pkg.v1.2.3                        -> [1.2.3]
> my.pkg.1.2.3                         -> [pkg.1.2.3]
> garbage                              -> [garbage]
> cert-manager-operator.v2             -> [2]
> cert-manager-operator.v              -> []
> ```

> **Fable:** **Run 2 (`fable-csvcmp-2`) — the exact delivered block, messages and all,** with
> `log`/`fail` as defined in the script, `report_unapproved_others` stubbed, the `oc` fetch replaced
> by an injected value (the logic under test starts after that read), and the Helm templating
> rendered to `openshift-marketplace`. Each case in a subshell so `exit 0` is observable:
>
> ```
> --- case: inst=[cert-manager-operator.v1.19.1] pin=[cert-manager-operator.v1.19.1] ---
> [approver 15:16:15] already installed at cert-manager-operator.v1.19.1; no InstallPlan to approve
> [approver 15:16:15] (stub) report_unapproved_others ran
> (subshell exit=0)
>
> --- case: inst=[cert-manager-operator.1.19.1] pin=[cert-manager-operator.v1.19.1] ---
> [approver 15:16:15] already installed at version 1.19.1 — but the cluster spells it cert-manager-operator.1.19.1 and the pin spells it cert-manager-operator.v1.19.1
> [approver 15:16:15]   update operator.startingCSV to the exact spelling the cluster reports; approval matches CSV names byte-for-byte, so the spellings must agree before the next upgrade
> [approver 15:16:15] (stub) report_unapproved_others ran
> (subshell exit=0)
>
> --- case: inst=[cert-manager-operator.v1.20.0] pin=[cert-manager-operator.v1.19.1] ---
> [approver 15:16:15] WARNING: the cluster is running cert-manager-operator.v1.20.0, which is NEWER than operator.startingCSV (cert-manager-operator.v1.19.1).
> [approver 15:16:15]   OLM does not downgrade, so no InstallPlan for cert-manager-operator.v1.19.1 will ever be staged — that pin
> [approver 15:16:15]   describes a past state. This Job approves nothing and the operator stays at cert-manager-operator.v1.20.0.
> [approver 15:16:15]   Adopt what is running, in git:  operator.startingCSV: cert-manager-operator.v1.20.0
> [approver 15:16:15]   Not merely tidiness: startingCSV is where OLM BEGINS on a fresh install, so a reinstall
> [approver 15:16:15]   would start at cert-manager-operator.v1.19.1 instead of cert-manager-operator.v1.20.0.
> [approver 15:16:15]   Going back to cert-manager-operator.v1.19.1 is not something this Job will do — OLM cannot downgrade, so
> [approver 15:16:15]   the CSV would have to be removed and reinstalled, taking cert-manager down.
> [approver 15:16:15]   What is on offer: oc get packagemanifest cert-manager-operator -n openshift-marketplace -o 'custom-columns=CHANNEL:.status.channels[*].name,HEAD:.status.channels[*].currentCSV'
> [approver 15:16:15] (stub) report_unapproved_others ran
> (subshell exit=0)
>
> --- case: inst=[cert-manager-operator.v1.19.1] pin=[cert-manager-operator.v1.20.0] ---
> [approver 15:16:15] installed cert-manager-operator.v1.19.1 is older than the pin cert-manager-operator.v1.20.0 — this is an upgrade; waiting for OLM to stage it
> [approver 15:16:15] (rig) fell through toward the search loop, as BEHIND and first-install should
> (subshell exit=0)
>
> --- case: inst=[] pin=[cert-manager-operator.v1.19.1] ---
> [approver 15:16:15] installedCSV=<none yet> — first install
> [approver 15:16:15] (rig) fell through toward the search loop, as BEHIND and first-install should
> (subshell exit=0)
> ```
>
> (A sixth case, `inst=cert-manager-operator.1.21.0` — AHEAD across a catalog rename — produced the
> same AHEAD transcript as the v1.20.0 case with the v-less name substituted, and is elided here
> only to avoid printing the identical block twice.) Same run, the two baseline-sed probes quoted in
> §1:
>
> ```
> -- doc baseline on a dotted package (shipped greedy sed and the candidate both yield 1.2.3) --
> pkg.v1.2.3
> -- doc claim: greedy .* with optional v backtracks to the last dot and yields a single digit --
> 1
> ```

> **Fable:** **Run 3 (`fable-csvcmp-3`) — edge probes** backing the guard decision and the cosmic
> edge in §5:
>
> ```
> sort (GNU coreutils) 8.30
> rc as PIN, final installed:   inst=cert-manager-operator.v1.20.1 pin=cert-manager-operator.v1.20.1-rc.1 -> BEHIND (truth AHEAD: an rc pin is OLDER than its final; BEHIND means a 600s burn -- why the guard must keep refusing rc pins)
> empty version after .v:       inst=cert-manager-operator.v pin=cert-manager-operator.v1.19.1 -> BEHIND (cosmic edge: OLM never writes a versionless CSV name; documented, not fixed)
> ```

> **Fable:** **Render-guard checks (Helm-side, so run where Helm renders — not pod userland):** both
> malformed shapes still refuse to render, a well-formed pin renders clean:
>
> ```
> $ helm template guard-probe charts/cert-manager-venafi --set operator.startingCSV=cert-manager-operator.1.19.1
> Error: execution error at (cert-manager-venafi/templates/installplan-approver-job.yaml:58:4): operator.startingCSV is "cert-manager-operator.1.19.1", which is not a CSV name this chart can compare versions against. [...]
> $ helm template guard-probe charts/cert-manager-venafi --set operator.startingCSV=cert-manager-operator.v1.20.1-rc.1
> Error: execution error at (cert-manager-venafi/templates/installplan-approver-job.yaml:58:4): operator.startingCSV is "cert-manager-operator.v1.20.1-rc.1", which is not a CSV name this chart can compare versions against. [...]
> $ helm template guard-probe charts/cert-manager-venafi --set operator.startingCSV=cert-manager-operator.v1.19.1 >/dev/null; echo exit=$?
> well-formed pin renders: exit=0
> ```

### 5. Failure modes deliberately NOT fixed

> **Fable:** 1. **A pre-release as `installedCSV` against its own final pin still reads AHEAD**
> (matrix row: `v1.20.1-rc.1` vs `v1.20.1`), because `sort -V` orders the rc after its final. This is
> byte-identical to today's verdict, and it is the SAFE wrong answer: the Job approves nothing, warns,
> and exits 0 — no budget burn, and the log prints both names. Fixing it means a semver-aware
> comparator hand-rolled in shell for a shape this catalog has never published (12 of 12 CSVs are
> plain `vX.Y.Z`) and that the guard refuses as a pin. Note the other three rc rows are actually
> CORRECT under `sort -V` (rc vs an older final, rc vs a newer final, rc pin measured in run 3), so
> the exposure is exactly one pair, in the harmless direction.
>
> 2. **An rc PIN would classify BEHIND and burn the budget — so the guard keeps refusing it** (run 3
> output above). Not a fix deferred; a fix refused: render-time rejection is strictly better than any
> runtime handling of a pin the catalog cannot stage.
>
> 3. **A dotted package name combined with a v-less version** (`my.pkg.1.2.3` as `installedCSV`)
> extracts `pkg.1.2.3` — garbage, and the verdict is then decided by digit-before-letter ordering.
> Leaving it: this requires the hypothetical catalog rename AND a dotted package at once; this
> chart's package has no dot; and §3's own blessed baseline has the same first-dot anchor for ALL
> names, where the replacement confines it to v-less names only (and measurably beats the baseline
> on dotted v-ful names, §1).
>
> 4. **A wholly alien `installedCSV`.** A dotless name (`garbage`) compares raw and lands AHEAD —
> safe, measured (digits sort before letters, so the numeric pin is never the newer side). The one
> pathological spelling that misclassifies expensively is a name ending in `.v` with no version
> (`cert-manager-operator.v` → empty → BEHIND → search-loop wait, run 3). OLM writes `installedCSV`
> only from a CSV it actually installed, and no such CSV name can exist in a catalog; if it ever
> happens, the search loop's existing timeout message names the exact diagnostic commands. Guarding
> a value only OLM can write, against a shape OLM cannot write, is dead code.
>
> 5. **A pin naming the wrong PACKAGE at the same version** (`wrong-operator.v1.19.1` against an
> installed `cert-manager-operator.v1.19.1`) reports as the same version spelled two ways and exits
> 0. The message prints both full names side by side and directs the reader to make the pin match
> the cluster's spelling exactly — which surfaces the wrong package name to a human. Package
> identity is bound by the Subscription lookup in step 1, not by this comparison; teaching the
> version comparator to parse package identity would duplicate that job, badly.
>
> 6. **A well-formed pin for a version the channel never offers** (`v9.9.9`) still classifies BEHIND
> and waits out the search loop into its existing failure message ("most often operator.startingCSV
> names a version this channel does not offer", with the `packagemanifest` command). No string
> comparison can know the catalog's contents; the timeout path already names the cause and the
> probe.
