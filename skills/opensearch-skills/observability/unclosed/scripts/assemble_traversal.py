#!/usr/bin/env python3
"""Build a Gate 2 traversal from a real index, then run Gates 2 and 3 over it.

Gates 2 and 3 judge a tree. This is where the tree comes from. Keeping the two
apart is the same rule Gate 1 follows: a probe that could issue another query
while deciding could widen its own evidence, and judgment that needs a cluster
cannot be tested without one.

The hypothesis space is declared, not discovered
------------------------------------------------
`NOT_VISITED` only means something against a space that was written down in
advance. A tool that reports "I explored everything I thought of" has said
nothing -- the set it is complete with respect to is the set it happened to
think of. So HYPOTHESES below is a fixed catalog, every entry appears in the
tree whether or not it was probed, and entries this index cannot answer are
recorded as unwalked with the data that would be needed named.

Magnitude by counterfactual
---------------------------
"How much does this explain" is answered by removing the subpopulation and
measuring again: `focus_p99 - focus_p99_without_v`. If dropping one endpoint
collapses the window's p99, that endpoint carries the effect. If dropping it
moves p99 by 8ms out of 400, it is a correlate that happens to be there -- and
Gate 3 will say so rather than accepting it.

A restatement may not account for anything
------------------------------------------
"The whole distribution shifted up" is true, useful for deciding where to look
next, and is *the observation said a second way*. Letting it account for the
effect would close the chain on "it got slower because it got slower", which is
the most confident-sounding empty answer available. It is structurally barred
from carrying magnitude.

Concentration is decided against a null, not against a constant
--------------------------------------------------------------
An earlier version of this probe required a value's median excess to reach 50%
of the window's own median rise. The evaluation measured that rule failing in
both directions at once: it missed a real concentration until it reached 68% of
the rise, and it confirmed one that did not exist on 3 negative-control runs in
5 -- because a window whose median moved only by noise still has a median rise
above zero, so dividing a few milliseconds of subgroup wobble by it produced a
large share and a CONFIRMED verdict.

One cause for both: there was no null. `concentration_null.py` builds one by
shuffling the group labels over the same documents, several hundred times, and
asking where the observed excess falls among the results. See that module for
why the null has to be a null of the maximum, and why an empirical p of zero is
never printed.

The rate of confirming something that is not there is now a declared 5% rather
than a consequence of an arbitrary constant, and examples/miss-rate.txt reports
what that choice actually costs -- along with the assumption it rests on, which
is that under the null any request could equally have carried any label. A
subgroup that is *permanently* slower violates that, and the corpus contains a
case built to find out how badly.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import NamedTuple, Optional

sys.path.insert(0, __file__.rsplit("assemble_traversal.py", 1)[0])
from audit_window import build_observation  # noqa: E402
from closure_audit import audit_closure, closes  # noqa: E402
from concentration_null import MIN_SUBPOP_N as NULL_MIN_N  # noqa: E402
from concentration_null import assess  # noqa: E402
from premise_audit import Verdict, audit, estimator_divergence  # noqa: E402
from traversal import Gate1Carryover, Node, NodeState, Traversal  # noqa: E402

DEFAULT_ENDPOINT = "http://127.0.0.1:9250"

#: A shift that reaches the median is not a tail-only event. Expressed as a
#: fraction of the p99 movement so it scales with the incident.
MEDIAN_SHIFT_SHARE = 0.10

#: Documents read from the focus window for the permutation test. Larger than
#: any window this project's fixtures produce, so the null is normally estimated
#: on the whole window. When a real window exceeds it the test is still valid --
#: observed statistic and null come from the same sample -- but it is a test on
#: that sample, and the report says so rather than implying otherwise.
SAMPLE_CAP = 5000


def _post(endpoint, path, body):
    req = urllib.request.Request(
        endpoint + path, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"OpenSearch returned {exc.code} for {path}: {exc.read().decode('utf-8')[:300]}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"Cannot reach OpenSearch at {endpoint}: {exc.reason}")


def _window(time_field, start, end):
    return {"range": {time_field: {"gte": start, "lt": end}}}


def _percentiles(endpoint, index, query, metric, percents):
    body = {"size": 0, "query": query,
            "aggs": {"p": {"percentiles": {"field": metric, "percents": percents}}}}
    values = _post(endpoint, f"/{index}/_search", body)["aggregations"]["p"]["values"]
    return {k: v for k, v in values.items()}


def _terms(endpoint, index, query, field, size=20):
    body = {"size": 0, "query": query, "aggs": {"t": {"terms": {"field": field, "size": size}}}}
    return _post(endpoint, f"/{index}/_search", body)["aggregations"]["t"]["buckets"]


def _sample(endpoint, index, query, metric, dimensions, cap=SAMPLE_CAP):
    """Raw values from the focus window, once, for every dimension at the same time.

    The permutation test needs the documents themselves -- an aggregation
    returns summaries, and a summary cannot be shuffled. Fetching once for all
    dimensions keeps this to a single extra request per run rather than one per
    hypothesis.
    """
    body = {"size": cap, "query": query, "_source": [metric] + list(dimensions)}
    res = _post(endpoint, f"/{index}/_search", body)
    total = res["hits"]["total"]
    total = total["value"] if isinstance(total, dict) else total
    rows = []
    for hit in res["hits"]["hits"]:
        src = hit["_source"]
        if metric in src:
            rows.append(src)
    return rows, bool(total and total > len(rows))


# --------------------------------------------------------------------------
# The declared hypothesis space
# --------------------------------------------------------------------------

#: Subpopulation hypotheses: the rise is confined to one value of a dimension.
#: Each is answerable from a latency log alone, which is why they are here.
CONCENTRATION_DIMENSIONS = (
    ("endpoint", "the rise is confined to one endpoint"),
    ("region", "the rise is confined to one region"),
    ("service", "the rise is confined to one service"),
    ("status", "the rise is confined to failing requests"),
)

#: Hypotheses a latency log cannot answer, named anyway. Leaving them out would
#: make the tree look complete; naming them is the difference between "nothing
#: else to check" and "the rest is not in this data".
OUT_OF_INDEX = (
    ("a deploy or config change landed in this window",
     "deploy/change events -- this index carries request logs only"),
    ("an upstream dependency got slower",
     "downstream call spans or a dependency's own latency series"),
    ("the host or container lost resources",
     "node-level CPU/memory/disk metrics, which are not request logs"),
)


def _concentration_node(endpoint, index, dim, statement, metric, focus_q, focus_p99,
                        sample, truncated):
    """Is one value of this dimension slower than the rest of the same window?

    Asked on the **median**, and against a **null**. Both choices are the probe.

    The median, because the two obvious alternatives report sample size while
    appearing to report concentration:

      remove the value and re-measure the window. Taking 198 of 200 documents
      out moves p99 whatever those documents were.

      compare the value's own p99 to the rest's. A p99 estimated on n=49 is
      noisy and biased high -- measurably so: on this project's own fixtures
      the aggregation reads between +0.14% and +21.75% above the true value at
      n=200, and smaller subgroups are worse.

    Both are Gate 1's population_shift confound rebuilt one gate up.

    The null, because a median gap between subgroups is never zero and a
    constant cannot say when one is large. Shuffling the labels over the same
    documents produces the distribution of that gap when the labels mean
    nothing, at these sizes and on this data's own spread -- see
    `concentration_null.py`. A constant expressed as a share of anything is
    blind to scale, and the evaluation caught the earlier one failing in both
    directions because of it.

    Once a concentration stands out from the null, removal is a fair way to
    *size* it in the metric the observation was made in.

    Returns `(confirmed_value, node)`. The value is returned rather than parsed
    back out of the node's text later: a reader that has to recover structure
    from a sentence is one rewording away from silently finding nothing.
    """
    probe = (f"compared each `{dim}` value's median against the rest of the window, and against "
             f"the gap that shuffling the labels produces at the same subgroup sizes")

    rows = [r for r in sample if dim in r]
    labels = {r[dim] for r in rows}
    if len(labels) < 2:
        only = next(iter(labels)) if labels else "nothing"
        return None, Node(
            statement, NodeState.INCONCLUSIVE, probe=f"grouped the focus window by `{dim}`",
            evidence=(f"only one value present ({only}); a dimension with nothing to compare "
                      "cannot separate a concentrated rise from a uniform one"),
        )

    result = assess([float(r[metric]) for r in rows], [r[dim] for r in rows],
                    sample_truncated=truncated)
    if result.excess is None:
        return None, Node(
            statement, NodeState.INCONCLUSIVE, probe=probe,
            evidence=(f"no value has {NULL_MIN_N} documents on both sides of the comparison "
                      f"({'; '.join(result.too_small)}); at that size the comparison would be "
                      "reporting subgroup size, not concentration"),
        )

    ev = result.describe()
    value = result.group

    if result.stands_out:
        without = {"bool": {"must": [focus_q], "must_not": [{"term": {dim: value}}]}}
        p = _percentiles(endpoint, index, without, metric, [99]).get("99.0")
        drop = focus_p99 - p if p is not None else None
        if drop is None:
            return value, Node(f"{statement} ({dim}={value})", NodeState.CONFIRMED, probe=probe,
                               evidence=ev + " -- stands out from the null, but removing it left "
                                             "nothing to measure against")
        return value, Node(f"{statement} ({dim}={value})", NodeState.CONFIRMED, probe=probe,
                           evidence=ev + f"; removing it takes the window down by {drop:+.2f}",
                           magnitude_accounted=round(drop, 2))
    if result.is_ordinary:
        return None, Node(statement, NodeState.RULED_OUT, probe=probe,
                          evidence=ev + " -- at or below what shuffled labels produce; "
                                        "the rise is spread")
    return None, Node(statement, NodeState.INCONCLUSIVE, probe=probe,
                      evidence=ev + " -- elevated, but not beyond what the labels produce by "
                                    "chance; not enough to say the effect lives here")


def _shape_node(endpoint, index, metric, focus_q, baseline_q, observed_effect):
    """Did the median move too, or only the tail?

    Deliberately carries no magnitude. It restates the observation in more
    detail rather than explaining it, and an explanation that is the observation
    said twice would close the chain on nothing.
    """
    f = _percentiles(endpoint, index, focus_q, metric, [50, 99])
    b = _percentiles(endpoint, index, baseline_q, metric, [50, 99])
    if f.get("50.0") is None or b.get("50.0") is None:
        return (Node("the whole distribution moved, not just the tail", NodeState.INCONCLUSIVE,
                     probe="compared p50 and p99 across focus and baseline", explanatory=False,
                     evidence="a percentile came back empty; the comparison could not be made"), None)

    median_shift = f["50.0"] - b["50.0"]
    share = median_shift / observed_effect if observed_effect else 0.0
    ev = (f"p50 {b['50.0']:.2f} -> {f['50.0']:.2f} ({median_shift:+.2f}), "
          f"p99 moved {observed_effect:+.2f}; the median carries {share:.0%} of it")

    if share >= MEDIAN_SHIFT_SHARE:
        return (Node("the whole distribution moved, not just the tail", NodeState.CONFIRMED,
                     probe="compared p50 and p99 across focus and baseline", explanatory=False,
                     evidence=ev + " -- a uniform shift. This says where to look next, "
                                   "and it is the observation restated, so it explains nothing on its own"),
                median_shift)
    return (Node("the whole distribution moved, not just the tail", NodeState.RULED_OUT,
                 probe="compared p50 and p99 across focus and baseline", explanatory=False,
                 evidence=ev + " -- the median held; this is a tail-only event"), median_shift)


class Assembled(NamedTuple):
    """Everything one run produced, named rather than positional.

    `observation` and `concentrations` are here so that a caller checking this
    tool's answers -- the evaluation harness, most of all -- reads structure
    instead of re-parsing the report's prose. A grader that recovers its facts
    from rendered sentences fails silently the first time a sentence is reworded,
    and reports a perfect score while measuring nothing.
    """

    premise: object
    traversal: Traversal
    observed_effect: float
    uncertainty: Optional[float]
    observation: object
    #: (dimension, value) pairs the traversal ended up CONFIRMING -- after the
    #: single-carrier rule below, not before it.
    concentrations: tuple


def assemble(endpoint, index, metric, time_field, dimension, bucket_minutes, lookback_hours,
             focus_window=None, reported_at=None) -> Assembled:
    obs, focus = build_observation(endpoint, index, metric, time_field, dimension,
                                   bucket_minutes, lookback_hours, focus_window, reported_at)
    premise = audit(obs)

    observed_effect = obs.focus_value - obs.baseline_value

    # The refusal is structural, not cosmetic. Below an artifact there is no
    # hypothesis space worth walking -- and a traversal that gets built and then
    # merely goes unprinted is one refactor away from being printed. `traversal`
    # is None here because there is nothing to traverse, which is a different
    # statement from an empty tree.
    if premise.verdict is Verdict.ARTIFACT:
        return Assembled(premise, None, observed_effect, estimator_divergence(obs), obs, ())

    focus_q = _window(time_field, obs.window_start, obs.window_end)
    baseline_q = {"bool": {"must_not": [focus_q]}}

    shape, _ = _shape_node(endpoint, index, metric, focus_q, baseline_q, observed_effect)

    # One fetch of the raw window, shared by every dimension. The permutation
    # test shuffles documents, and a summary cannot be shuffled.
    sample, truncated = _sample(endpoint, index, focus_q, metric,
                                [dim for dim, _ in CONCENTRATION_DIMENSIONS])

    children, candidates = [], []
    for dim, statement in CONCENTRATION_DIMENSIONS:
        value, node = _concentration_node(endpoint, index, dim, statement, metric,
                                          focus_q, obs.focus_value, sample, truncated)
        children.append(node)
        candidates.append((dim, value))

    # At most one dimension may carry magnitude. Two dimensions that each
    # isolate the window may be isolating the *same documents* seen from two
    # angles, and summing them would explain the effect twice. The strongest
    # keeps its number; the others are recorded as still standing, because
    # "might be the same thing" is not grounds for ruling either out.
    carriers = [n for n in children if n.magnitude_accounted is not None]
    if len(carriers) > 1:
        carriers.sort(key=lambda n: -n.magnitude_accounted)
        for weaker in carriers[1:]:
            children[children.index(weaker)] = Node(
                weaker.hypothesis, NodeState.INCONCLUSIVE, probe=weaker.probe,
                evidence=(weaker.evidence + " -- another dimension isolates the window at least as "
                          "strongly, and these may be the same documents seen from two angles; "
                          "not independently established"),
            )

    # Read after the single-carrier rule, never before it: a dimension that was
    # downgraded above is not a finding, and a scorer told otherwise would credit
    # the tool with a confirmation it withdrew.
    concentrations = tuple(
        (dim, value)
        for (dim, value), node in zip(candidates, children)
        if value is not None and node.state is NodeState.CONFIRMED
    )

    children.append(shape)
    for statement, needs in OUT_OF_INDEX:
        children.append(Node(statement, NodeState.NOT_VISITED,
                             not_visited_reason=f"needs {needs}"))

    root = Node(
        f"{metric} rose {observed_effect:+.2f} in {obs.window_start}",
        NodeState.CONFIRMED,
        probe=f"p99 per {bucket_minutes}m bucket over the last {lookback_hours}h",
        evidence=f"focus {obs.focus_value:.2f} vs baseline {obs.baseline_value:.2f} (n={obs.focus_count})",
        children=tuple(children),
    )

    traversal = Traversal(
        observation=f"{metric} p99 {obs.baseline_value:.2f} -> {obs.focus_value:.2f} in {obs.window_start}",
        root=root,
        gate1=Gate1Carryover(premise.verdict.value, tuple(premise.missing_inputs)),
    )
    return Assembled(premise, traversal, observed_effect, estimator_divergence(obs),
                     obs, concentrations)


def main() -> int:
    ap = argparse.ArgumentParser(description="Assemble a traversal from an index and run all three gates.")
    ap.add_argument("--index", required=True)
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument("--metric", default="latency_ms")
    ap.add_argument("--time-field", default="@timestamp")
    ap.add_argument("--dimension", default="endpoint")
    ap.add_argument("--bucket-minutes", type=int, default=10)
    ap.add_argument("--lookback-hours", type=int, default=6)
    ap.add_argument("--focus-window", default=None)
    ap.add_argument("--reported-at", default=None)
    args = ap.parse_args()

    run = assemble(args.endpoint, args.index, args.metric, args.time_field, args.dimension,
                   args.bucket_minutes, args.lookback_hours, args.focus_window, args.reported_at)
    premise, traversal = run.premise, run.traversal
    observed_effect, uncertainty = run.observed_effect, run.uncertainty

    print("=" * 78)
    print(premise.to_text())
    print()
    if premise.verdict is Verdict.ARTIFACT:
        print("=" * 78)
        print("Gate 2 not run: the premise did not survive Gate 1.")
        print("Walking a hypothesis space below an artifact would produce a tidy tree")
        print("explaining something that did not happen.")
        return 0

    print("=" * 78)
    print(traversal.to_text())
    print()
    print("=" * 78)
    print(audit_closure(traversal, observed_effect, uncertainty).to_text())
    print()
    closed, reasons = closes(traversal, observed_effect, uncertainty)
    print("=" * 78)
    print("ALL FOUR CONDITIONS: %s" % ("closed" if closed else "NOT CLOSED"))
    for r in reasons:
        print(f"  - {r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
