# unclosed

**Log root-cause analysis that audits its own premise and refuses to name a
cause it cannot close.**

An OpenSearch Agent Skills hackathon entry
([issue #92](https://github.com/opensearch-project/opensearch-agent-skills/issues/92)).
Theme: observability / log analytics. Python standard library only, no paid or
proprietary dependencies, Apache-2.0.

The skill itself is
[`SKILL.md`](skills/opensearch-skills/observability/unclosed/SKILL.md). The rest
of this file is about whether it works, because that is the part that is easy to
assert and expensive to check.

## Why

Ask an agent why a service got slow and it will usually tell you. That is the
problem. Three failures hide inside a confident answer:

- the reported spike was never checked for being an artifact of measurement
- the search ran in the direction the question implied and stopped at the first
  plausible correlate
- the explanation was never made to account for the size of the effect

`unclosed` treats "I cannot close this chain" as a valid, and often correct,
result. It reports the traversal -- evidence, magnitude accounted for,
alternatives not ruled out, and branches never explored -- instead of a verdict.
It never names a cause. Logs cannot establish causation, and a tool that says
otherwise is committing the failure it exists to catch.

Three gates, in order: a premise audit that tries to refute the observation
before anything investigates it; a traversal that records every branch it did
not walk; and a closure audit that refuses a chain whose explanations do not add
up to the effect. The design is in `SKILL.md`.

## What it gets wrong

### The evaluation

[`examples/miss-rate.txt`](examples/miss-rate.txt) is the evaluation: indices
whose truth is known because they were generated, scored against what the gates
said about them, with misses and false alarms counted **separately** rather than
averaged into one number. One accuracy figure would hide the trade this design
makes on purpose.

    fixed corpus   8 cases x 5 seeds       premise verdicts 40/40
    sweep          35 runs, 7 strengths    concentration    27/35
    under-claim rate  4.6%                 over-claim rate  3.1%

Multiple seeds per case are not decoration. Run once, and a defect that depends
on where the noise fell reads as a clean pass; this corpus reported
`false_concentration = 0` on a single seed and `8/20` on twenty.

### The detection floor

Reliable concentration detection begins at a true share of **68.3%** of the
window's own median rise. Below that the probe reports "elevated, not
established" and the concentration is missed: 46.8% is found in 2 runs of 5,
23.8% in 1 of 5, 14.4% in none. The floor is published because a miss rate
without a floor is a number without a scale.

### Two of the defects it reports are in this skill

The documentation once said the skill under-claims and never over-claims. The
harness confirmed a concentration on a negative control in 3 runs of 5, which
made that sentence false. The fix -- a permutation null in place of a fitted
constant -- introduced the second: a subgroup that is *permanently* slower was
confirmed in 5 runs of 5, on a rise spread evenly across every group. Both are
fixed, both fixes are measured, and the cost of the second is published too:
detection power at the bottom of the sweep, given up to buy back the false
confirmations.

Any of those would have been cheaper to leave out. A skill that publishes a miss
rate and omits the miss its own harness found is not publishing a miss rate.

### What the evaluation cannot see

Every case is generated, indexed, audited once, and deleted. No index is ever
read twice. So a defect that appears *between* runs -- the same command over the
same unchanged data returning a different answer -- cannot land in any category
of the report, **including the categories that print zero**.

That is not hypothetical. The scan window was anchored on the wall clock, so the
focus window drifted between runs over a static index and the left edge sliced
the oldest bucket in half. The evaluation reported a clean sweep before and
after the fix, with identical verdicts and identical rates, because reading one
index at two moments is the one thing it never does. It was found by the A/B
below, and the blind spot is now printed in the report itself rather than
described here only.
See [`examples/window-anchor.txt`](examples/window-anchor.txt).

## Does it beat not having it?

Tested rather than claimed, and the answer on the headline axis is **no**.

[`examples/agent-ab.txt`](examples/agent-ab.txt): the same model, the same
cluster, the same incidents, the same question, twice -- once with the skill and
once without, 18 runs. Both arms were offered ARTIFACT and CANNOT-TELL in
identical words, which biases the comparison *toward* the arm without the skill
answering cautiously.

- On the two cases the corpus can score, both arms were correct in 3 runs of 3.
  **Six of six either way.** The starting hypothesis was that an unaided agent
  would fabricate a root cause. It did not.
- The only measured difference is phrasing: language the skill's own narration
  guard refuses outright appeared in 2 of 9 unaided runs and 0 of 9 aided ones.
  That is a floor, not a headline -- an answer can hand an engineer a cause
  without ever saying "caused by".
- A third case does show a gap, 0 of 3 against 2 of 3, and is excluded from the
  totals rather than counted. The report was filed 20% into the bucket and
  overstated p99 by 77%, but a genuine 5.8x slowdown survives window completion.
  Both answers are defensible, so the gap is not evidence about either arm.

What the experiment did produce was six defects -- two in this skill, one in the
corpus, one in the A/B harness itself, one in a sentence that was about to
become marketing copy, and one in shipped code. That is the argument for running
it, and it is a different argument from the one it was set up to make.

The full 18 answers are in
[`examples/agent-ab-answers/`](examples/agent-ab-answers/), verbatim, including
the ones that make the skill look unnecessary.

## Portability, checked rather than asserted

The cheap version of portable -- the endpoint is a flag, no hostname is
hardcoded -- was true of this code while it could still only reach one kind of
cluster. **Every managed OpenSearch and every default self-managed install runs
the security plugin**: HTTPS, a certificate the system trust store has never
seen, and 401 without credentials. A submission tested only against a demo
container with security switched off has not been tested for portability at all.

[`eval/vendor_neutrality.py`](eval/vendor_neutrality.py) seeds two deployment
shapes from the same generator with the same seed, runs the same audit against
both pinned to the same `--as-of`, and diffs the reports line by line.

    plaintext   http, security plugin disabled, no credentials
    secured     https, security plugin on, self-signed cert, basic auth

Result: both return `VERDICT: UNDECIDABLE`, and the **only** line that differs
between the two reports is the `TRANSPORT:` line, whose job is to differ.
Material differences: 0. Capture:
[`examples/vendor-neutrality.txt`](examples/vendor-neutrality.txt).

The failure paths are in the same capture, because a user meets those long
before they meet a verdict: no credentials, `http` against a TLS port, and an
untrusted certificate each produce a sentence naming what to do about it rather
than a traceback.

Passwords are read from `$OPENSEARCH_PASSWORD` and never accepted on the command
line -- argv is readable by other processes on the host. `--insecure` exists, and
a run that used it says `TLS with VERIFICATION DISABLED` in every report, on the
same principle as recording which clock chose the window: a reader cannot
otherwise tell whether the cluster reached is the cluster named.

## Every guard has been broken on purpose

A test that has never failed has not been shown to test anything.
[`eval/mutations.py`](eval/mutations.py) removes one load-bearing rule at a time
-- 69 of them -- and reruns the **whole** suite after each. Pointing a mutation
at one hand-picked test would let the author decide what counts as caught. A
mutation whose anchor text is not found is an error rather than a skip, because
a patch that anchors to text which is not there applies nothing and reports
success.

```bash
uv run python eval/mutations.py --list     # the 69, grouped, runs nothing
uv run python eval/mutations.py            # applies them, writes the capture
```

Latest capture: [`examples/mutation-verification.txt`](examples/mutation-verification.txt).

## Layout

This repository mirrors the target path in
[`opensearch-project/opensearch-agent-skills`](https://github.com/opensearch-project/opensearch-agent-skills)
so that contribution is a copy rather than a reorganization:

```
skills/opensearch-skills/observability/unclosed/
                          SKILL.md and scripts/ -- the deliverable
scripts/seed_logs.py      synthetic fixtures (stdlib only)
eval/                     corpus with known truth, scoring, the mutations,
                          and the two-deployment portability check
tests/                    no running cluster required
examples/                 captured runs, including everything above
```

## Running the fixtures

Requires Docker and [uv](https://docs.astral.sh/uv/). No other dependencies.
`uv run pytest -q` needs no cluster at all; the rest of this section is for
reproducing the captures.

```bash
docker run -d --name unclosed-opensearch \
  -p 127.0.0.1:9250:9200 \
  -e discovery.type=single-node \
  -e DISABLE_SECURITY_PLUGIN=true \
  -e OPENSEARCH_JAVA_OPTS="-Xms512m -Xmx512m" \
  opensearchproject/opensearch:2.19.3

python scripts/seed_logs.py --scenario real-spike --recreate
python scripts/seed_logs.py --scenario fake-spike --recreate

uv run pytest -q
```

The container is bound to loopback and the port is deliberately not the default
9200 -- relying on the default is how a hardcoded endpoint gets into the code.

To reproduce the portability check, add a second cluster with security **on**
and run the comparison against both:

```bash
docker run -d --name unclosed-secure \
  -p 127.0.0.1:9251:9200 \
  -e discovery.type=single-node \
  -e "OPENSEARCH_INITIAL_ADMIN_PASSWORD=$OPENSEARCH_PASSWORD" \
  -e OPENSEARCH_JAVA_OPTS="-Xms512m -Xmx512m" \
  opensearchproject/opensearch:2.19.3

python scripts/seed_logs.py --scenario real-spike --recreate --seed 20260801
python scripts/seed_logs.py --scenario real-spike --recreate --seed 20260801 \
  --endpoint https://127.0.0.1:9251 --username admin --insecure

uv run python eval/vendor_neutrality.py \
  --plain http://127.0.0.1:9250 \
  --secure https://127.0.0.1:9251 --username admin --insecure
```

`--insecure` is used here because the demo certificate is self-signed. Against a
cluster whose CA you have, pass `--ca-cert` instead and the reports say so.

## License

Apache-2.0. No paid, proprietary, or license-restricted dependencies; the
implementation uses the Python standard library only.
