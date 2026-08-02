"""Gate 1 verdict precedence.

No running cluster: judgment operates on plain fact objects by design.

These tests exist to pin the rule that makes a pass mean something -- a pass is
an optimistic claim, valid only because a full pessimistic sweep ran and came
back empty. Weaken any part of that and one of these should go red.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "skills" / "opensearch-skills" / "observability" / "unclosed" / "scripts"),
)

from premise_audit import (  # noqa: E402
    MIN_COMPOSITION_N,
    Observation,
    Outcome,
    ProbeResult,
    Verdict,
    audit,
    decide,
)


EVEN = {"/api/checkout": 0.25, "/api/search": 0.25, "/api/cart": 0.25, "/api/profile": 0.25}


def healthy(**overrides):
    base = dict(
        metric="latency_ms",
        focus_value=1800.0,
        baseline_value=330.0,
        focus_count=200,
        baseline_typical_count=200,
        focus_composition=EVEN,
        baseline_composition=EVEN,
        focus_ingest_lag_p50_s=2.2,
        baseline_ingest_lag_p50_s=2.3,
        second_clock_field="ingested_at",
        resolved_indices=["logs-000001"],
        metric_field_types={"logs-000001": "float"},
    )
    base.update(overrides)
    return Observation(**base)


# --- precedence -----------------------------------------------------------


def test_pass_requires_every_attempt_to_have_run_and_failed():
    assert audit(healthy()).verdict is Verdict.SUBSTANTIATED


def test_one_successful_refutation_is_decisive():
    """Finding the artifact does not require finishing the sweep."""
    report = audit(healthy(focus_count=3))
    assert report.verdict is Verdict.ARTIFACT


def test_unrunnable_probe_blocks_the_pass():
    """Not knocked down is not cleared when a line of attack was never available."""
    report = audit(healthy(second_clock_field=None, focus_ingest_lag_p50_s=None, baseline_ingest_lag_p50_s=None))
    assert report.verdict is Verdict.UNDECIDABLE
    assert any("second time field" in m for m in report.missing_inputs)


def test_refutation_outranks_an_unrunnable_probe():
    report = audit(healthy(focus_count=3, resolved_indices=None, metric_field_types=None))
    assert report.verdict is Verdict.ARTIFACT


# --- the third state cannot be an escape hatch ----------------------------


def test_undecidable_must_name_what_is_missing():
    with pytest.raises(ValueError):
        ProbeResult("p", "story", Outcome.COULD_NOT_RUN, "no idea", missing=None)


def test_every_undecidable_verdict_carries_at_least_one_named_gap():
    report = audit(healthy(focus_composition=None, baseline_composition=None))
    assert report.verdict is Verdict.UNDECIDABLE
    assert report.missing_inputs and all(m for m in report.missing_inputs)


# --- probe independence ---------------------------------------------------


def test_composition_probe_declines_instead_of_firing_on_small_samples():
    """The population check must not re-detect what the volume check found.

    Below a usable sample size the composition cannot match a large baseline no
    matter what the system did. If this probe fired there, ARTIFACT would rest
    on two lines of evidence that are really one -- and legitimately low-traffic
    windows would be judged artifacts.
    """
    skewed = {"/api/checkout": 1.0}
    report = audit(healthy(focus_count=3, focus_composition=skewed))
    population = next(p for p in report.probes if p.probe == "population_shift")
    assert population.outcome is Outcome.COULD_NOT_RUN
    assert str(MIN_COMPOSITION_N) in population.missing


def test_composition_probe_does_fire_when_the_sample_supports_it():
    """The guard must not neuter the probe -- it still catches real shifts."""
    skewed = {"/api/checkout": 0.95, "/api/search": 0.05}
    report = audit(healthy(focus_composition=skewed))
    population = next(p for p in report.probes if p.probe == "population_shift")
    assert population.outcome is Outcome.REFUTED
    assert report.verdict is Verdict.ARTIFACT


# --- the record is the basis of the pass, not decoration ------------------


def test_pass_reports_every_attempt_it_relied_on():
    report = audit(healthy())
    assert len(report.probes) == 4
    assert all(p.evidence for p in report.probes)
    assert "rests on the record above" in report.to_text()


def test_decide_rejects_a_pass_built_from_an_incomplete_sweep():
    probes = [
        ProbeResult("a", "s", Outcome.NOT_REFUTED, "ran"),
        ProbeResult("b", "s", Outcome.COULD_NOT_RUN, "did not run", missing="input b"),
    ]
    assert decide(probes) is Verdict.UNDECIDABLE
