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

Known limit: the concentration probe is under-powered
-----------------------------------------------------
On a window of a few hundred documents split across a handful of values, one
subgroup's median differs from the rest's by a visible fraction of the window's
own movement **with no concentration present at all**. Measured on this
project's own uniformly-distributed fixture, where the true concentration is
zero, the largest apparent excess runs 20-30% of the window's median rise.

Answering that correctly needs a null: how large a gap appears between
subgroups when nothing is happening, estimated **at the same subgroup sizes**.
That is a real piece of statistics and it is not built. Picking a threshold that
happens to clear this fixture would be fitting the constant to the data it is
graded on, which is the same move as grading a detector against a corpus written
to match it.

So the probe stops at INCONCLUSIVE in that band and shows the numbers it
measured. It under-claims: a genuine concentration below ~50% of the median rise
is reported as "elevated, not established" rather than found. That is a miss,
it is the safe direction of one -- it never closes a chain it should not -- and
it is counted in the evaluation's miss rate rather than left for a reader to
discover.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

sys.path.insert(0, __file__.rsplit("assemble_traversal.py", 1)[0])
from audit_window import build_observation  # noqa: E402
from closure_audit import audit_closure, closes  # noqa: E402
from premise_audit import Verdict, audit, estimator_divergence  # noqa: E402
from traversal import Gate1Carryover, Node, NodeState, Traversal  # noqa: E402

DEFAULT_ENDPOINT = "http://127.0.0.1:9250"

#: A value's own p99 must exceed the rest of the window's by at least this share
#: of the observed effect before the effect counts as concentrated in it.
CONCENTRATION_SHARE = 0.50

#: Below this, no value's distribution stands out and the dimension is not where
#: the answer lives.
SPREAD_SHARE = 0.10

#: Both sides of the comparison need this many documents. Below it a percentile
#: is not estimable, and a probe that answered anyway would be reporting sample
#: size while appearing to report concentration -- the same confound Gate 1's
#: population_shift guard exists to prevent, one gate up.
MIN_SUBPOP_N = 30

#: A shift that reaches the median is not a tail-only event. Expressed as a
#: fraction of the p99 movement so it scales with the incident.
MEDIAN_SHIFT_SHARE = 0.10


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
                        observed_effect, median_rise):
    """Is one value of this dimension slower than the rest of the same window?

    Asked on the **median**, and the choice is the whole probe.

    Two wrong ways to ask it, both of which report sample size while appearing
    to report concentration:

      remove the value and re-measure the window. Taking 198 of 200 documents
      out moves p99 whatever those documents were.

      compare the value's own p99 to the rest's. A p99 estimated on n=49 is
      noisy and biased high -- measurably so: on this project's own fixtures
      the aggregation reads between +0.14% and +21.75% above the true value at
      n=200, and smaller subgroups are worse. Differences that size appear
      between subgroups drawn from one identical distribution.

    Both are Gate 1's population_shift confound rebuilt one gate up. The median
    survives estimation at these sizes, and a subgroup that is genuinely slower
    moves it. A subgroup whose p99 is elevated but whose median matches the rest
    has a fat tail, not a slowdown -- and declining to confirm that is the
    conservative reading, not a miss.

    Once a concentration is established this way, removal is a fair way to
    *size* it in the metric the observation was made in.
    """
    probe = f"compared each `{dim}` value's own median against the rest of the same window"
    if median_rise is None or median_rise <= 0:
        return None, Node(
            statement, NodeState.INCONCLUSIVE, probe=probe,
            evidence=("the window's own median did not rise, so this is a tail-only event and the "
                      "median cannot localise it; a percentile estimated on one value's documents "
                      "would be reporting subgroup size instead"),
        )
    buckets = _terms(endpoint, index, focus_q, dim)
    if len(buckets) < 2:
        only = buckets[0]["key"] if buckets else "nothing"
        return None, Node(
            statement, NodeState.INCONCLUSIVE, probe=f"grouped the focus window by `{dim}`",
            evidence=(f"only one value present ({only}); a dimension with nothing to compare "
                      "cannot separate a concentrated rise from a uniform one"),
        )

    total = sum(b["doc_count"] for b in buckets)
    excesses, too_small = [], []
    for b in buckets:
        n_in, n_out = b["doc_count"], total - b["doc_count"]
        if n_in < MIN_SUBPOP_N or n_out < MIN_SUBPOP_N:
            too_small.append(f"{b['key']} (n={n_in} vs {n_out})")
            continue
        inside = {"bool": {"must": [focus_q, {"term": {dim: b["key"]}}]}}
        outside = {"bool": {"must": [focus_q], "must_not": [{"term": {dim: b["key"]}}]}}
        p_in = _percentiles(endpoint, index, inside, metric, [50]).get("50.0")
        p_out = _percentiles(endpoint, index, outside, metric, [50]).get("50.0")
        if p_in is None or p_out is None:
            continue
        excesses.append((b["key"], n_in, p_in, p_out, p_in - p_out))

    if not excesses:
        return None, Node(
            statement, NodeState.INCONCLUSIVE, probe=probe,
            evidence=(f"no value has {MIN_SUBPOP_N} documents on both sides of the comparison "
                      f"({'; '.join(too_small)}); at that size the comparison would be reporting "
                      "subgroup size, not concentration"),
        )

    value, n_in, p_in, p_out, excess = max(excesses, key=lambda e: e[4])
    share = excess / median_rise
    ev = (f"`{dim}={value}` (n={n_in}) has median {p_in:.2f} against {p_out:.2f} for the rest of the "
          f"window -- {excess:+.2f}, or {share:.0%} of the window's own median rise of "
          f"{median_rise:+.2f}; largest of {len(excesses)} values compared")
    if too_small:
        ev += f"; {len(too_small)} value(s) too small to compare"

    if share >= CONCENTRATION_SHARE:
        # Now that the concentration is established without reference to sample
        # size, removal is a fair way to size it.
        without = {"bool": {"must": [focus_q], "must_not": [{"term": {dim: value}}]}}
        p = _percentiles(endpoint, index, without, metric, [99]).get("99.0")
        drop = focus_p99 - p if p is not None else None
        if drop is None:
            return None, Node(f"{statement} ({dim}={value})", NodeState.CONFIRMED, probe=probe,
                              evidence=ev + " -- concentrated, but removing it left nothing to measure against")
        return drop, Node(f"{statement} ({dim}={value})", NodeState.CONFIRMED, probe=probe,
                          evidence=ev + f"; removing it takes the window down by {drop:+.2f}",
                          magnitude_accounted=round(drop, 2))
    if share < SPREAD_SHARE:
        return None, Node(statement, NodeState.RULED_OUT, probe=probe,
                          evidence=ev + " -- no value's distribution stands out; the rise is spread")
    return None, Node(statement, NodeState.INCONCLUSIVE, probe=probe,
                      evidence=ev + " -- elevated, but not enough to say the effect lives here")


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


def assemble(endpoint, index, metric, time_field, dimension, bucket_minutes, lookback_hours,
             focus_window=None, reported_at=None):
    obs, focus = build_observation(endpoint, index, metric, time_field, dimension,
                                   bucket_minutes, lookback_hours, focus_window, reported_at)
    premise = audit(obs)

    observed_effect = obs.focus_value - obs.baseline_value
    focus_q = _window(time_field, obs.window_start, obs.window_end)
    baseline_q = {"bool": {"must_not": [focus_q]}}

    # The shape reading comes first: it produces the window's own median rise,
    # which is what the concentration probes measure a subgroup against.
    shape, median_rise = _shape_node(endpoint, index, metric, focus_q, baseline_q, observed_effect)

    children = []
    for dim, statement in CONCENTRATION_DIMENSIONS:
        _, node = _concentration_node(endpoint, index, dim, statement, metric,
                                      focus_q, obs.focus_value, observed_effect, median_rise)
        children.append(node)

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
    return premise, traversal, observed_effect, estimator_divergence(obs)


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

    premise, traversal, observed_effect, uncertainty = assemble(
        args.endpoint, args.index, args.metric, args.time_field, args.dimension,
        args.bucket_minutes, args.lookback_hours, args.focus_window, args.reported_at)

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
