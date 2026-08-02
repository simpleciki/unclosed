# unclosed

**Log root-cause analysis that audits its own premise and refuses to name a
cause it cannot close.**

An OpenSearch Agent Skills hackathon entry ([issue #92](https://github.com/opensearch-project/opensearch-agent-skills/issues/92)).
Theme: observability / log analytics.

> **Status: in development.** The skill contract is specified; the gates are
> being built and mutation-verified one at a time. See
> [`SKILL.md`](skills/opensearch-skills/observability/unclosed/SKILL.md).

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

## What it gets wrong

[`examples/miss-rate.txt`](examples/miss-rate.txt) is the evaluation: indices
whose truth is known because they were generated, scored against what the gates
said about them, with misses and false alarms counted separately rather than
averaged into one number.

It reports a detection floor, an estimator error the two-estimator probe does
not bound, and two defects it found in this skill. The first was a concentration
confirmed on a negative control in 3 runs of 5, which the documentation had
called impossible; that one is fixed, and the harness measures the fix. The
second is a limit of the fix -- a permanently slower subgroup is confirmed as a
concentration in 5 runs of 5 -- and it is open.

Any of those would have been cheaper to leave out. A skill that publishes a miss
rate and omits the miss its own harness found is not publishing a miss rate.

## Layout

This repository mirrors the target path in
[`opensearch-project/opensearch-agent-skills`](https://github.com/opensearch-project/opensearch-agent-skills)
so that contribution is a copy rather than a reorganization:

```
skills/opensearch-skills/observability/unclosed/SKILL.md
scripts/seed_logs.py      synthetic fixtures (stdlib only)
eval/                     corpus with known truth, and the scoring
tests/                    no running cluster required
examples/                 captured runs, including the miss rate
```

## Running the fixtures

Requires Docker and [uv](https://docs.astral.sh/uv/). No other dependencies.

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
9200 -- relying on the default is how a hardcoded endpoint gets into the code,
and portability across OpenSearch deployments is an explicit judging dimension.

## License

Apache-2.0. No paid, proprietary, or license-restricted dependencies; the
implementation uses the Python standard library only.
