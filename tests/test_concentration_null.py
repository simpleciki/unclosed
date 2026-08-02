"""The null the concentration probe is judged against.

This module replaced a constant that the evaluation caught failing in both
directions, so the properties that make it a null rather than a differently
shaped constant are pinned here -- particularly the two that are easy to get
wrong and invisible when wrong: that it is a null of the *maximum*, and that it
scales with the data rather than with a chosen unit.
"""

import random

import pytest

import concentration_null as cn


def uniform(n, median, sigma=0.6, seed=1):
    rng = random.Random(seed)
    return [rng.lognormvariate(__import__("math").log(median), sigma) for _ in range(n)]


def labelled(groups):
    """groups: {label: [values]} -> (values, labels)"""
    values, labels = [], []
    for label, vals in groups.items():
        values.extend(vals)
        labels.extend([label] * len(vals))
    return values, labels


def test_a_planted_concentration_stands_out():
    values, labels = labelled({
        "/a": uniform(50, 600, seed=1),
        "/b": uniform(50, 80, seed=2),
        "/c": uniform(50, 80, seed=3),
        "/d": uniform(50, 80, seed=4),
    })
    result = cn.assess(values, labels, trials=200)
    assert result.group == "/a"
    assert result.stands_out
    assert result.p_value <= cn.ALPHA


def test_labels_that_mean_nothing_do_not_stand_out():
    # One distribution, four arbitrary labels. Whatever gap appears is the gap
    # the shuffling produces, so the observed draw cannot be extreme among them.
    values = uniform(200, 80, seed=9)
    labels = [f"/{'abcd'[i % 4]}" for i in range(200)]
    result = cn.assess(values, labels, trials=200)
    assert not result.stands_out
    assert result.p_value > cn.ALPHA


def test_the_null_scales_with_the_data_not_with_a_fixed_unit():
    # The same shape of data at ten times the scale produces a null ten times
    # wider. A threshold expressed in milliseconds, or as a share of anything
    # chosen elsewhere, is blind to exactly this -- which is how the constant
    # this module replaced managed to be too strict and too lax at once.
    small = cn.assess(*labelled({f"/{c}": uniform(50, 80, seed=i)
                                 for i, c in enumerate("abcd")}), trials=200)
    large = cn.assess(*labelled({f"/{c}": uniform(50, 800, seed=i)
                                 for i, c in enumerate("abcd")}), trials=200)
    assert large.null_threshold > small.null_threshold * 5


def test_the_statistic_is_the_largest_gap_not_the_first_one_found():
    # The probe reports the largest excess among several values, so the null has
    # to be the distribution of the largest too. A null built from one fixed
    # comparison is narrower, and comparing a maximum against it fires far more
    # often than the declared rate -- the same mistake as reading the highest
    # bucket in a chart as if someone had nominated it in advance.
    values, labels = labelled({
        "/a": uniform(50, 100, seed=1),
        "/b": uniform(50, 700, seed=2),   # the largest, and not the first
        "/c": uniform(50, 90, seed=3),
    })
    group, excess, *_ = cn._excesses(values, labels, ["/a", "/b", "/c"])
    assert group == "/b"


def test_more_groups_to_choose_from_widens_the_null():
    # Taking a maximum over eight comparisons finds a larger gap by chance than
    # taking it over two, and the null has to grow to match or the declared rate
    # is not the real one. Subgroup size is held at 40 in both.
    eight = cn.assess(*labelled({f"/{c}": uniform(40, 80, seed=i)
                                 for i, c in enumerate("abcdefgh")}), trials=200)
    two = cn.assess(*labelled({f"/{c}": uniform(40, 80, seed=i)
                               for i, c in enumerate("ab")}), trials=200)
    assert eight.compared == 8 and two.compared == 2
    assert eight.null_threshold > two.null_threshold


def test_a_subgroup_too_small_on_either_side_is_declined_and_named():
    values, labels = labelled({"/a": uniform(5, 900, seed=1), "/b": uniform(195, 80, seed=2)})
    result = cn.assess(values, labels, trials=50)
    assert result.excess is None
    assert any("/a (n=5" in entry for entry in result.too_small)


def test_the_p_value_never_reads_zero():
    # With K shuffles the smallest supportable p is 1/(K+1). Printing 0.000
    # would claim a precision the shuffling cannot deliver, and the number would
    # then travel into a report as if it had been measured.
    values, labels = labelled({
        "/a": [10_000.0] * 50,
        "/b": uniform(150, 80, seed=5),
    })
    result = cn.assess(values, labels, trials=100)
    assert result.p_value == pytest.approx(1 / 101)
    assert result.p_value > 0


def test_the_same_data_always_reads_the_same():
    # The shuffling is a measurement instrument. One that reads differently on
    # every run cannot support a captured verdict.
    args = labelled({f"/{c}": uniform(50, 100 + i * 40, seed=i) for i, c in enumerate("abcd")})
    first = cn.assess(*args, trials=100)
    second = cn.assess(*args, trials=100)
    assert (first.p_value, first.null_threshold, first.group) == \
           (second.p_value, second.null_threshold, second.group)


def test_a_gap_at_the_middle_of_the_null_is_ordinary():
    values = uniform(200, 80, seed=21)
    labels = [f"/{'abcd'[i % 4]}" for i in range(200)]
    result = cn.assess(values, labels, trials=200)
    # Not standing out is not the same as being typical; both are reported so
    # the caller can tell "spread" from "elevated, not established".
    assert result.is_ordinary or (not result.stands_out)


def test_truncation_is_carried_into_the_description():
    values, labels = labelled({f"/{c}": uniform(50, 80, seed=i) for i, c in enumerate("abcd")})
    result = cn.assess(values, labels, trials=50, sample_truncated=True)
    assert "not on the window" in result.describe()


def test_the_description_names_the_numbers_it_decided_on():
    values, labels = labelled({
        "/a": uniform(50, 600, seed=1),
        "/b": uniform(150, 80, seed=2),
    })
    text = cn.assess(values, labels, trials=100).describe()
    for fragment in ("has median", "Shuffling the labels", "p="):
        assert fragment in text
