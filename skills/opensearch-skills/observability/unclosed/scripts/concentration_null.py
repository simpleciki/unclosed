#!/usr/bin/env python3
"""How large a gap appears between subgroups when nothing is happening.

The question a concentration probe is really asking is not "is one endpoint's
median higher than the rest's" -- one of them always is. It is "is it higher
than it would be if the labels meant nothing". Without an answer to the second,
the first is a statement about arithmetic.

The previous version of this probe answered it with a constant: a value's excess
had to reach 50% of the window's own median rise. That failed in both directions
at once, and the evaluation measured both. It missed a real concentration until
it reached 68% of the rise, and -- because a window whose median barely moved
still has a median rise greater than zero -- it divided a few milliseconds of
subgroup wobble by a few milliseconds of drift, got a large number, and confirmed
a concentration on an index where nothing had happened, in 3 runs of 5.

One cause: there was no null. The absence cut both ways, which is what the
absence of a null does.

The test
--------
The labels are shuffled. Every latency stays exactly where it is; only the
question of which endpoint it belonged to is randomised, several hundred times.
Each shuffle produces a largest-subgroup-excess, and together they are the
distribution of that statistic **when the labels carry no information at all** --
at these subgroup sizes, on this data's own spread. The observed excess is then
just one more draw, and the only question is where it falls among the others.

That last clause is the part a constant cannot do. Noise between medians scales
with the spread of the data, so a 60ms gap is unremarkable in a window centred at
300ms and decisive in one centred at 80ms. A threshold expressed as a share of
anything is blind to this. The shuffled distribution is built from the same
numbers, so it carries the scale with it.

Two details that are not incidental
-----------------------------------
**The null is a null of the maximum.** The probe reports the largest excess among
several values, so the null has to be the distribution of the largest excess too.
Comparing a maximum against the null of a single comparison would fire on the
most extreme of four groups at four times the intended rate -- the same mistake
as reading the highest bucket in a chart and treating it as if someone had
nominated it in advance, which is the failure Gate 1 exists to catch.

**An empirical p of zero is not reported.** With K shuffles the smallest
defensible p is 1/(K+1), so the count of exceedances is incremented by one before
dividing. Printing 0.000 would claim a precision K draws cannot deliver, and the
number would then travel.

Standard library only. No retrieval here: this decides, and a decision that can
issue a query can widen its own evidence while making it.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass
from typing import Optional

#: Shuffles. Enough that the 95th percentile is stable across runs and the
#: floor on the p-value (1/401) sits well below the level being tested.
DEFAULT_TRIALS = 400

#: A group needs this many documents on both sides, or its median is a statement
#: about sample size wearing the clothes of a statement about latency.
MIN_SUBPOP_N = 30

#: The false-positive rate this probe accepts. Declared, not fitted -- it is a
#: choice about how often to be wrong in the confirming direction, made before
#: seeing any data, and the evaluation measures whether the choice holds.
ALPHA = 0.05

#: A fixed seed so a captured verdict can be reproduced. The shuffling is a
#: measurement instrument, and an instrument that reads differently every time
#: is not one.
SEED = 20260802


@dataclass(frozen=True)
class NullResult:
    """The observed excess, and the distribution it has to stand out from."""

    group: Optional[str]
    excess: Optional[float]
    median_in: Optional[float]
    median_out: Optional[float]
    n_in: Optional[int]

    null_median: Optional[float]
    null_threshold: Optional[float]
    p_value: Optional[float]
    trials: int
    compared: int
    too_small: tuple = ()

    #: How many documents the test actually saw, and whether that was all of
    #: them. A null estimated on a truncated sample is a null for that sample.
    sample_n: int = 0
    sample_truncated: bool = False

    @property
    def stands_out(self) -> bool:
        """Beyond what shuffled labels produce at this rate."""
        return self.p_value is not None and self.p_value <= ALPHA

    @property
    def is_ordinary(self) -> bool:
        """At or below the middle of the null: exactly what noise does."""
        return (self.excess is not None and self.null_median is not None
                and self.excess <= self.null_median)

    def describe(self) -> str:
        if self.excess is None:
            return "no value had enough documents on both sides to compare"
        base = (f"`{self.group}` (n={self.n_in}) has median {self.median_in:.2f} against "
                f"{self.median_out:.2f} for the rest -- {self.excess:+.2f}. Shuffling the "
                f"labels {self.trials} times over the same {self.sample_n} documents puts the "
                f"typical largest gap at {self.null_median:.2f} and the {1 - ALPHA:.0%} point at "
                f"{self.null_threshold:.2f} (p={self.p_value:.3f}); {self.compared} value(s) compared")
        if self.too_small:
            base += f"; {len(self.too_small)} too small to compare ({'; '.join(self.too_small)})"
        if self.sample_truncated:
            base += (f"; the window holds more than {self.sample_n} documents, so this is a test "
                     "on the sample that was read, not on the window")
        return base


def _excesses(values, labels, eligible):
    """Largest (median inside - median outside) over the eligible labels."""
    best = None
    for label in eligible:
        inside = [v for v, g in zip(values, labels) if g == label]
        outside = [v for v, g in zip(values, labels) if g != label]
        if not inside or not outside:
            continue
        excess = statistics.median(inside) - statistics.median(outside)
        if best is None or excess > best[1]:
            best = (label, excess, statistics.median(inside), statistics.median(outside), len(inside))
    return best


def assess(values, labels, trials: int = DEFAULT_TRIALS, seed: int = SEED,
           sample_truncated: bool = False) -> NullResult:
    """Is the largest subgroup excess larger than shuffled labels produce?

    `values` and `labels` are parallel: one latency and one group per document,
    both read from the *same* sample. Deriving the observed excess from an
    aggregation and the null from a sample would compare two different
    estimators and call the difference a finding -- two rulers again, one gate
    further in.
    """
    n = len(values)
    counts = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1

    eligible = [g for g, c in counts.items() if c >= MIN_SUBPOP_N and n - c >= MIN_SUBPOP_N]
    too_small = tuple(f"{g} (n={c} vs {n - c})" for g, c in sorted(counts.items())
                      if g not in eligible)

    if not eligible:
        return NullResult(None, None, None, None, None, None, None, None,
                          trials, 0, too_small, n, sample_truncated)

    observed = _excesses(values, labels, eligible)
    if observed is None:
        return NullResult(None, None, None, None, None, None, None, None,
                          trials, 0, too_small, n, sample_truncated)
    group, excess, median_in, median_out, n_in = observed

    # The latencies stay put; only which group each belongs to is randomised.
    # Sizes are preserved, so the null is estimated at the sizes actually
    # present -- which is the whole point, since a smaller subgroup produces a
    # larger gap for free.
    rng = random.Random(seed)
    shuffled = list(labels)
    null = []
    for _ in range(trials):
        rng.shuffle(shuffled)
        drawn = _excesses(values, shuffled, eligible)
        if drawn is not None:
            null.append(drawn[1])

    if not null:
        return NullResult(group, excess, median_in, median_out, n_in, None, None, None,
                          trials, len(eligible), too_small, n, sample_truncated)

    null.sort()
    # +1 in both places: with K draws the smallest supportable p is 1/(K+1), and
    # a printed 0.000 would be a precision this many shuffles cannot deliver.
    exceeded = sum(1 for value in null if value >= excess)
    p_value = (exceeded + 1) / (len(null) + 1)
    threshold = null[min(len(null) - 1, int(round((1 - ALPHA) * len(null))))]

    return NullResult(
        group=group, excess=excess, median_in=median_in, median_out=median_out, n_in=n_in,
        null_median=statistics.median(null), null_threshold=threshold, p_value=p_value,
        trials=len(null), compared=len(eligible), too_small=too_small,
        sample_n=n, sample_truncated=sample_truncated,
    )
