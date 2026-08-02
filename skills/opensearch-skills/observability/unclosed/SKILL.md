---
name: unclosed
description: >
  Audit the premise of a log-based incident before investigating its cause, and
  record the full derivation tree instead of asserting a root cause. Use this
  skill when someone reports that a service got slower, error rates rose, a
  metric spiked, or an incident needs a root cause -- especially when the claim
  comes from a dashboard, an alert, or another agent rather than from direct
  measurement. Activate even if the user says latency spike, p99 regression,
  error rate jump, root cause analysis, RCA, postmortem, incident investigation,
  why is this slow, what caused this, or blameless retro without mentioning
  OpenSearch. Use this BEFORE log-analytics when the question is "is this real
  and what would prove it", and log-analytics when the question is already
  "query the logs for X".
compatibility: >
  Requires a running OpenSearch cluster. PPL queries require the SQL plugin
  (built-in). No paid, proprietary, or license-restricted dependencies --
  Python standard library only.
metadata:
  author: simpleciki
  version: "0.1"
---

# unclosed

Root-cause analysis for logs that audits its own premise and refuses to name a
cause it cannot close.

> **Status: in development (hackathon entry).**
> All three gates are implemented, mutation-verified — every rule below has been
> deliberately broken and the suite goes red for each — and run end to end
> against a live cluster. A captured run over all three fixtures is in
> [`examples/gates-on-real-data.txt`](../../../../examples/gates-on-real-data.txt).
> Known limits are stated below rather than left to be discovered.

## The problem this addresses

An investigation agent pointed at logs tends to fail in three separate ways:

1. **The premise is never tried.** "p99 spiked at 14:20" is accepted as fact.
   But a spike can be an artifact of the measurement rather than the system --
   and an artifact looks exactly like an incident in a percentile chart.
2. **The search runs one way and stops early.** The agent follows the direction
   the question implies and halts at the first plausible correlate. What it
   found was *a* related signal; what it reports is *the* cause.
3. **The chain never has to close.** An explanation that accounts for 8ms of a
   400ms regression is not an explanation, but nothing forces that arithmetic.

## The contract

**Logs cannot establish causation.** If this skill emitted "the cause is X", it
would be committing the error it exists to catch.

So it never does. The output is the traversal itself:

- which steps have evidence, and what that evidence was
- how much of the observed effect is actually accounted for
- which alternative explanations were checked and not ruled out
- **which branches were never explored** -- recorded explicitly, not skipped
  silently

An investigation that does not close is reported as **not closed**, naming the
missing link. That is a successful run.

## Boundaries

- **Read-only.** This skill never writes to OpenSearch.
- **Does not replace [log-analytics](../log-analytics/SKILL.md).** It runs
  *before* it. log-analytics answers "query the logs for X"; `unclosed` answers
  "is X real, and what would it take to prove it".
- **Reuses [`ppl-reference.md`](../ppl-reference.md)** rather than restating PPL
  syntax.
- **Publishes its own miss rate.** The evaluation reports cases this skill fails
  to catch, including at least one case with no discoverable cause.

## Gates

| Gate | Question | Status |
|---|---|---|
| 1 | Is the reported observation real, or an artifact of how it was measured? | implemented |
| 2 | What is the hypothesis space, and which branches were actually traversed? | implemented |
| 3 | Does the accounted-for magnitude add up to the observed effect? | implemented |

### Gate 1

Every check is an **attempt to refute** the observation by telling a specific
story in which nothing actually got worse. An attempt returns `REFUTED`,
`NOT_REFUTED`, or `COULD_NOT_RUN` -- and `COULD_NOT_RUN` must name the input it
lacked, which the constructor enforces.

| Verdict | Requires |
|---|---|
| `ARTIFACT` | any single attempt refuted it. Finding the artifact does not require finishing the sweep |
| `SUBSTANTIATED` | every attempt ran **and** every one failed. A pass is an optimistic claim, safe only because something pessimistic verified it -- the record *is* the pass |
| `UNDECIDABLE` | nothing refuted it, but a line of attack was unavailable. Not knocked down is not the same as cleared |

This precedence makes `UNDECIDABLE` unreachable by mere uncertainty: it needs a
probe that wanted a *nameable* input which was absent. "I am not sure" cannot
produce it.

**The auditor must not author the premise.** Three of the eight attempts examine
the claim rather than the system, because every dataset has a maximum and a tool
that selects its own worst bucket will always find one:

- a report that names **no moment** is `ARTIFACT`, not an open question. Nothing
  can contradict it, so nothing can support it -- and the remedy is the
  reporter, not more data
- a **self-selected** window can never be `SUBSTANTIATED`. It can still be
  refuted: discovering an artifact does not depend on the window having been
  named in advance
- an external report with **no report timestamp** is an unbacked claim. The
  timestamp is what makes provenance checkable rather than typed
- a window **judged before it finished** is an artifact: the reporter and the
  auditor are looking at two different datasets that share a name

**And the ruler is part of the claim.** A percentile from an aggregation is not
a measurement of the data, it is an estimate computed from it, and which
estimator produced it is a choice almost nobody records. The eighth attempt asks
the same window with a second one — `tdigest` and `hdr`, both core to every
OpenSearch distribution, both sub-aggregations of the same request, so neither a
dependency nor an extra round trip.

This is not hypothetical. On this project's fixtures the default estimator reads
**+0.14% to +21.75%** above the true value at n=200, and on the negative control
that error decides *which bucket is selected as the incident* — it picks one
whose true p99 is lower than another's. A single-estimator reading cannot be
separated from the method that produced it, so it may not substantiate anything.

The two kinds of disagreement are not the same finding:

- **about existence** — one ruler sees a 5x jump and the other sees nothing. The
  jump belongs to the ruler, and that is a refutation
- **about size** — both see it, sized differently. Not grounds to discard the
  observation; grounds to stop claiming a precision nobody has. The divergence is
  handed to Gate 3 rather than absorbed

Measured on the fixtures: 2% at n=3, 8% on the real spike, and **11% on the
negative control** — the smaller the effect, the larger the share of it that is
ruler.

### Gate 2

Gate 1 asked whether the observation is real. This gate asks what was actually
looked at on the way to explaining it — and records every branch that was named
and never walked.

A branch is not bought-or-not. Five states divide on **two independent axes**,
and collapsing them into one loses a fact that the reader needs:

| State | Re-running moves it? | Branch disposed of? | Next action |
|---|---|---|---|
| `CONFIRMED` | no | yes | — |
| `RULED_OUT` | no | yes | — |
| `INCONCLUSIVE` | no | **no** | needs different evidence, which may not exist |
| `PENDING` | **yes** | no | wait, then re-run |
| `NOT_VISITED` | **yes** | no | go and look |

The first axis decides **what to do next**; the second decides **whether a chain
may close**. `INCONCLUSIVE` is the only state on both — finished work, branch
still standing — and it is the state that can make a chain *permanently*
unclosable. That case is not a defect. It is the honest answer, and the
evaluation contains one.

A confirmed branch closes nothing while an alternative to it is still standing.
That is the whole gate in one sentence: an explanation is not established by
being found, it is established by the others being disposed of. Reporting the
first plausible correlate is what this looks like when the alternatives are
invisible.

**`PENDING` is guarded, or it becomes the escape hatch** that Gate 1's verdict
precedence exists to close — anything inconvenient gets labelled "not answered
yet" and the tree never has to close. So it must name **what it awaits** and
**the moment its state was read**, and the constructor enforces both. A pending
branch with no timestamp is a claim about a present that has already passed.

**An unverified premise travels with the tree.** An `UNDECIDABLE` verdict from
Gate 1 does not stop the traversal — the branches below are still worth walking
and the evidence found there is still real. But Gate 1's missing inputs are
carried on the tree and keep it open, because otherwise every node can close and
the report reads as complete while resting on a premise nobody checked.

Closure therefore takes four conditions, and this gate owns three:

1. a root-to-leaf path exists on which every node is `CONFIRMED`
2. no decision point on it has a live alternative still standing
3. Gate 1 left no unresolved gap
4. the accounted-for magnitude adds up to the observed effect — **Gate 3's**, and
   Gate 2 deliberately does not claim it

A chain that satisfies all of them is reported as *a path with nothing left open
on it*. It is not a cause, and this skill will not call it one.

Evidence collected on one branch is never invalidated by another branch being
blocked. A traversal that discarded finished work whenever something else was
open would make an incomplete run indistinguishable from a run that found
nothing — which is most runs.

### Gate 3

An explanation that covers 8ms of a 400ms regression is not a small
explanation. It is the wrong one, or a fragment of the right one. Gate 2 passes
it — every step is true and every alternative is disposed of — so only the
arithmetic catches it.

| Verdict | Means |
|---|---|
| `ACCOUNTED` | the size of the explanation matches the size of the effect |
| `UNDER_ACCOUNTED` | the confirmed branches explain far less than what happened |
| `OVER_ACCOUNTED` | they explain far **more** — usually double counting, sometimes a wrong baseline |
| `NOT_QUANTIFIED` | a confirmed branch carries no number, so the arithmetic cannot run. It names the branch |

`OVER_ACCOUNTED` exists because an explanation that overshoots is exactly as
unusable as one that falls short, and nothing in an ordinary investigation looks
for it.

`NOT_QUANTIFIED` is the same discipline Gate 1 applies to `COULD_NOT_RUN`: a
gate that cannot decide must say which input it lacked, or "cannot decide"
becomes the place everything goes to hide. It also blocks the quiet substitution
that this gate is most likely to make — **treating an unmeasured branch as
contributing zero**, which turns "nobody measured this" into "this contributed
nothing", an assertion no one made.

**No double counting, enforced by shape.** "The deploy accounts for 380ms" and
"the query plan under it accounts for 380ms" are one 380ms described at two
depths; summed, they explain 760ms of a 400ms effect. A node whose descendants
already account for magnitude may not account for it too, and the constructor
refuses to build one — so the arithmetic is impossible to write rather than
merely discouraged.

**The residual is always reported**, whatever the verdict. A chain accounting
for 92% of the effect leaves 8% nobody has explained. That is a fact about the
incident, not a rounding error to be absorbed into a pass.

**The tolerance cannot be finer than the ruler.** Gate 1's estimator probe hands
down how much the effect's size depends on which estimator measured it. When
that exceeds the residual tolerance, the tolerance widens to match: an
explanation covering 75% of an effect known only to within 30% has not fallen
short of anything a reader could act on. The widening is printed with its
reason — a pass at a loosened tolerance is a weaker claim and has to read like
one, or the report launders imprecision into agreement.

Gate 3 does no filtering of its own: magnitude may only come from `CONFIRMED`
nodes, and that is enforced upstream at construction. If this gate decided which
numbers to trust, it would be re-litigating Gate 2's verdicts with less evidence
than Gate 2 had.

`closes()` is the only place all four conditions are asserted together. A tree
that satisfies all four is reported as a chain that survived being checked — and
still not as a cause, because the thing that would license that claim, knowing
the counterfactual, is not in the logs.

## Building the tree from an index

`assemble_traversal.py` runs Gate 1, and if the premise survives, walks a
hypothesis space against the index and hands the result to Gates 2 and 3.

**The hypothesis space is declared, not discovered.** `NOT_VISITED` means
nothing against a space the tool assembled as it went — "I explored everything I
thought of" is complete only with respect to what it happened to think of. So
the catalog is fixed, every entry appears in the tree whether or not it was
probed, and hypotheses this index *cannot* answer (a deploy landed, an upstream
dependency slowed, the host lost resources) are recorded as unwalked with the
data that would be needed named. On a request-log index those three are the
usual answer, and saying so is more use than a confident tour of the four
questions the logs can answer.

**Concentration is asked on the median, and that choice is the probe.** Two
wrong ways to ask whether the rise lives in one endpoint or region, both of
which report sample size while looking like they report concentration:

- *remove the value and re-measure the window* — taking 198 of 200 documents out
  moves p99 whatever those documents were
- *compare the value's own p99 to the rest's* — a p99 estimated on n=49 is noisy
  and biased high. On this project's fixtures the aggregation reads **+0.14% to
  +21.75%** above the true value at n=200, and smaller subgroups are worse

Both are Gate 1's `population_shift` confound rebuilt one gate up. The median
survives estimation at these sizes. Once a concentration is established that
way, removal is a fair way to *size* it.

### Known limits

**The concentration probe is under-powered.** On a few hundred documents split
across a handful of values, one subgroup's median differs from the rest's by
20–30% of the window's own movement *with no concentration present* — measured
on a fixture where the true concentration is zero. Answering that correctly
needs a null estimated at the same subgroup sizes, which is not built. Choosing
a threshold that happens to clear this fixture would be fitting the constant to
the data it is graded on. So the probe stops at `INCONCLUSIVE` in that band and
shows its numbers: a genuine concentration below roughly half the median rise is
reported as *elevated, not established*. It under-claims, never over-claims, and
the miss is counted rather than hidden.

**Two estimators are not a confidence interval.** The eighth probe establishes
that the reading is not an artifact of one particular method. It does not bound
the error — both estimators could be wrong in the same direction, and on these
fixtures both read high against the exact value. What it rules out is the
failure where the whole effect belongs to the estimator; what it does not
provide is a true measurement uncertainty.

## Development fixtures

`scripts/seed_logs.py` generates three scenarios against a local cluster:

| Scenario | Volume in the anomalous bucket | What it is |
|---|---|---|
| `baseline` | normal throughout | negative control -- nothing to find |
| `real-spike` | unchanged (n=200) | genuine regression: the distribution moved |
| `fake-spike` | collapsed (n=3) | artifact: p99 over almost no data |

Both spike scenarios produce a ~5x p99 jump over the same baseline. A threshold
on the ratio alone cannot separate them; a test asserts they stay confusable, so
the gate is never graded against a strawman.
