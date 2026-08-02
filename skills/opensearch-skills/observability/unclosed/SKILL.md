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

> **Status: under construction (hackathon entry, in development).**
> The three gates below are specified but not yet implemented. This file is the
> skeleton; gate contracts land as they are built and mutation-verified.

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
| 1 | Is the reported observation real, or an artifact of how it was measured? | in development |
| 2 | What is the hypothesis space, and which branches were actually traversed? | not started |
| 3 | Does the accounted-for magnitude add up to the observed effect? | not started |

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
