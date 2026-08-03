"""Scoring rules for the evaluation.

These decide what the published miss rate says, which makes them the one place
in the project where a silent bug produces a *number* rather than a wrong
verdict -- and a number is harder to disbelieve. So the rules are pinned here,
including the ones whose whole content is a refusal to score something.
"""

import pytest

from corpus import Truth
from score import (RunResult, detection_curve, detection_floor, score_case,
                   score_closure, score_concentration, score_gate1, summarize)


def truth(**overrides):
    base = dict(
        premise_is_real=True, gate1_expected=("SUBSTANTIATED", "UNDECIDABLE"),
        artifact_kind=None, concentrated_in=None, true_share=None,
        max_apparent_share=None, cause_in_index=False,
        true_focus_p99=450.0, true_baseline_p99=90.0,
        true_focus_median=400.0, true_baseline_median=80.0,
        focus_n=200, baseline_typical_n=200,
    )
    base.update(overrides)
    return Truth(**base)


# --------------------------------------------------------------------------
# Gate 1
# --------------------------------------------------------------------------

def test_either_acceptable_verdict_passes():
    # UNDECIDABLE is not a failure on a real incident: it is the tool admitting
    # a gap, which is the behaviour the design exists to produce.
    for verdict in ("SUBSTANTIATED", "UNDECIDABLE"):
        ok, err = score_gate1(truth(), RunResult(gate1_verdict=verdict))
        assert ok and err is None


def test_letting_an_artifact_through_is_named_as_a_miss():
    planted = truth(premise_is_real=False, gate1_expected=("ARTIFACT",),
                    artifact_kind="sample_size_collapse")
    ok, err = score_gate1(planted, RunResult(gate1_verdict="SUBSTANTIATED"))
    assert not ok and err == "missed_artifact"


def test_calling_a_real_incident_an_artifact_is_the_other_direction():
    ok, err = score_gate1(truth(), RunResult(gate1_verdict="ARTIFACT"))
    assert not ok and err == "false_alarm"


def test_substantiating_an_empty_window_is_over_claiming():
    control = truth(premise_is_real=False, gate1_expected=("UNDECIDABLE", "ARTIFACT"))
    ok, err = score_gate1(control, RunResult(gate1_verdict="SUBSTANTIATED"))
    assert not ok and err == "over_claimed"


# --------------------------------------------------------------------------
# Probe attribution
# --------------------------------------------------------------------------

def test_a_case_caught_by_a_different_probe_passed_for_the_wrong_reason():
    planted = truth(premise_is_real=False, gate1_expected=("ARTIFACT",),
                    artifact_kind="clock_semantics")
    score = score_case("replay", planted,
                       RunResult(gate1_verdict="ARTIFACT",
                                 refuting_probes=("population_shift",), gate2_ran=False))
    assert score.gate1_ok           # the verdict is right
    assert score.attribution_ok is False   # and it is right for the wrong reason
    assert score.unexpected_refuters == ("population_shift",)


def test_a_second_probe_firing_alongside_the_right_one_still_fails_attribution():
    # "Every attempt ran and every one failed" is only as strong as the attempts
    # being independent, so a spare refutation is evidence against the rule.
    planted = truth(premise_is_real=False, gate1_expected=("ARTIFACT",),
                    artifact_kind="sample_size_collapse")
    score = score_case("collapse", planted,
                       RunResult(gate1_verdict="ARTIFACT", gate2_ran=False,
                                 refuting_probes=("sample_size_collapse", "population_shift")))
    assert score.attribution_ok is False


def test_a_case_with_no_planted_artifact_is_not_scored_for_attribution():
    ok, unexpected = score_case("real", truth(), RunResult(gate1_verdict="SUBSTANTIATED")), None
    assert ok.attribution_ok is None


# --------------------------------------------------------------------------
# Concentration
# --------------------------------------------------------------------------

def test_a_tree_that_never_ran_is_not_scored_as_a_miss():
    # Gate 1 already counted that failure. Counting it again here would report
    # one mistake as two.
    planted = truth(concentrated_in=("endpoint", "/api/checkout"))
    ok, err = score_concentration(planted, RunResult(gate1_verdict="ARTIFACT", gate2_ran=False))
    assert ok is None and err is None


def test_finding_the_planted_concentration_passes():
    planted = truth(concentrated_in=("endpoint", "/api/checkout"))
    ok, err = score_concentration(
        planted, RunResult(gate1_verdict="SUBSTANTIATED",
                           confirmed_concentrations=(("endpoint", "/api/checkout"),)))
    assert ok and err is None


def test_confirming_nothing_where_something_was_planted_is_an_under_claim():
    planted = truth(concentrated_in=("endpoint", "/api/checkout"))
    ok, err = score_concentration(planted, RunResult(gate1_verdict="SUBSTANTIATED"))
    assert not ok and err == "missed_concentration"


def test_confirming_something_where_nothing_was_planted_is_the_count_that_must_be_zero():
    ok, err = score_concentration(
        truth(), RunResult(gate1_verdict="SUBSTANTIATED",
                           confirmed_concentrations=(("region", "us-west-2"),)))
    assert not ok and err == "false_concentration"


def test_confirming_the_wrong_value_is_worse_than_confirming_none():
    planted = truth(concentrated_in=("endpoint", "/api/checkout"))
    ok, err = score_concentration(
        planted, RunResult(gate1_verdict="SUBSTANTIATED",
                           confirmed_concentrations=(("endpoint", "/api/search"),)))
    assert not ok and err == "wrong_value"


def test_an_extra_dimension_beside_the_right_one_is_not_a_second_failure():
    # Two dimensions can isolate the same documents from different angles, which
    # is why the assembler lets only one carry magnitude.
    planted = truth(concentrated_in=("endpoint", "/api/checkout"))
    ok, err = score_concentration(
        planted, RunResult(gate1_verdict="SUBSTANTIATED",
                           confirmed_concentrations=(("endpoint", "/api/checkout"),
                                                     ("region", "us-west-2"))))
    assert ok and err is None


# --------------------------------------------------------------------------
# Closure
# --------------------------------------------------------------------------

def test_failing_to_close_is_not_scored_against_the_tool():
    # On a request-log index the branches that would close a chain are not
    # answerable from the data. Grading that would be grading the index.
    ok, err = score_closure(truth(cause_in_index=True),
                            RunResult(gate1_verdict="SUBSTANTIATED", closed=False))
    assert ok and err is None


def test_closing_a_chain_whose_cause_is_not_in_the_index_is_scored_against_it():
    ok, err = score_closure(truth(cause_in_index=False),
                            RunResult(gate1_verdict="SUBSTANTIATED", closed=True))
    assert not ok and err == "closed_the_unclosable"


# --------------------------------------------------------------------------
# Estimator error
# --------------------------------------------------------------------------

def test_ruler_error_is_measured_against_the_exact_value():
    # true effect 360; the estimators read 396 and 378.
    score = score_case("real", truth(),
                       RunResult(gate1_verdict="SUBSTANTIATED", reported_effect=396.0,
                                 reported_effect_alt=378.0, estimator_divergence=0.0455))
    assert score.ruler_error_primary == pytest.approx(0.10, abs=1e-3)
    assert score.ruler_error_alt == pytest.approx(0.05, abs=1e-3)
    # The two rulers agree with each other more closely than either agrees with
    # the truth. That is exactly why their divergence is not an error bound.
    assert score.disagreement_covers_error is False


def test_a_self_selected_window_is_not_compared_against_a_window_it_did_not_measure():
    # The tool picked its own bucket; the corpus knows the truth about another
    # one. Differencing them would produce an authoritative-looking error figure
    # about nothing -- a wrong baseline dressed as a measurement.
    score = score_case("control", truth(),
                       RunResult(gate1_verdict="UNDECIDABLE", reported_effect=111.6,
                                 reported_effect_alt=99.0, estimator_divergence=0.2,
                                 audited_planted_window=False))
    assert score.ruler_error_primary is None
    assert score.disagreement_covers_error is None


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

def test_every_failure_kind_is_reported_even_at_zero():
    # A count absent because nothing triggered it reads identically to a count
    # absent because nobody looked for it.
    summary = summarize([score_case("clean", truth(), RunResult(gate1_verdict="SUBSTANTIATED"))])
    assert set(summary.failures) and all(v == 0 for v in summary.failures.values())


def test_the_two_directions_are_counted_separately_and_never_merged():
    planted = truth(premise_is_real=False, gate1_expected=("ARTIFACT",),
                    artifact_kind="sample_size_collapse")
    scores = [
        score_case("a", planted, RunResult(gate1_verdict="SUBSTANTIATED", gate2_ran=False)),
        score_case("b", planted, RunResult(gate1_verdict="UNDECIDABLE", gate2_ran=False)),
        score_case("c", truth(), RunResult(gate1_verdict="ARTIFACT", gate2_ran=False)),
    ]
    summary = summarize(scores)
    assert summary.failures["missed_artifact"] == 2
    assert summary.failures["false_alarm"] == 1
    # Two of three are under-claims and one is an over-claim. A single accuracy
    # figure would report 0% either way and hide which trade was made.
    assert summary.miss_rate == pytest.approx(2 / 3, abs=1e-4)
    assert summary.over_claim_rate == pytest.approx(1 / 3, abs=1e-4)


def test_a_run_that_failed_outright_is_a_failure_not_a_pass():
    score = score_case("broken", truth(), RunResult(gate1_verdict="ERROR", error="no cluster"))
    assert not score.gate1_ok
    assert summarize([score]).failures["run_failed"] == 1


# --------------------------------------------------------------------------
# The detection curve
# --------------------------------------------------------------------------

def test_the_curve_is_ordered_by_measured_strength_not_by_the_knob():
    points = detection_curve([
        ("x030", 2.0, True, True), ("x010", 0.1, False, False), ("x014", 0.5, True, False),
    ])
    assert [p.label for p in points] == ["x010", "x014", "x030"]


def test_a_confirmation_on_a_rung_with_nothing_planted_is_counted_as_a_false_alarm():
    points = detection_curve([("x010", 0.1, False, True), ("x010", 0.1, False, False)])
    assert points[0].false_alarms == 1
    assert points[0].rate == pytest.approx(0.5)


def test_the_floor_is_the_weakest_rung_that_fired_every_time():
    points = detection_curve(
        [("x014", 0.47, True, False)] * 3
        + [("x016", 0.68, True, True)] * 3
        + [("x020", 1.09, True, True)] * 3
    )
    assert detection_floor(points) == pytest.approx(0.68)


def test_no_floor_is_reported_when_no_rung_was_reliable():
    # Absent is not zero: "the floor is above everything tested" is a stronger
    # statement than the sweep was built to make, and must not read as "0%".
    points = detection_curve([("x014", 0.47, True, False), ("x016", 0.68, True, False)])
    assert detection_floor(points) is None


def test_the_null_rung_never_becomes_the_floor():
    # Nothing was planted there, so a rung of noise cannot be the strength at
    # which detection becomes reliable.
    points = detection_curve([("x010", 0.0, False, True)] * 3)
    assert detection_floor(points) is None
