#!/usr/bin/env python3
"""Gate 1 -- premise audit.

Before asking *why* an observation happened, ask whether the observation is
real. A percentile chart cannot tell a regression from an artifact of its own
measurement, and both look identical to a threshold.

Design
------
Each check is not an inspection. It is an **attempt to refute the observation**
by telling a specific alternative story in which nothing actually got worse.
An attempt returns one of three outcomes:

    REFUTED        the alternative story holds; the observation is an artifact
    NOT_REFUTED    the attempt ran in full and failed to knock the observation down
    COULD_NOT_RUN  the attempt needs an input that is absent, and names it

Verdict precedence
------------------
    ARTIFACT       any single attempt refuted the observation.
                   Finding the artifact is decisive and does not require
                   completing the sweep.

    SUBSTANTIATED  every attempt ran AND every attempt failed to refute.
                   A pass is an optimistic claim, and an optimistic claim is
                   only safe when something pessimistic verified it. This is
                   that verification: the observation stands because a full
                   adversarial sweep came back empty, not because nothing was
                   noticed.

    UNDECIDABLE    nothing refuted it, but at least one attempt could not run.
                   Failing to knock something down is not the same as clearing
                   it when one line of attack was never available.

This precedence is what keeps UNDECIDABLE from becoming an escape hatch: it is
unreachable by uncertainty. It requires a probe that needed a *nameable* input
which was absent. "I am not sure" cannot produce it.

Every verdict carries the full probe record. For SUBSTANTIATED that record is
not decoration -- it is the entire basis of the pass.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Outcome(str, Enum):
    REFUTED = "REFUTED"
    NOT_REFUTED = "NOT_REFUTED"
    COULD_NOT_RUN = "COULD_NOT_RUN"


class Verdict(str, Enum):
    ARTIFACT = "ARTIFACT"
    SUBSTANTIATED = "SUBSTANTIATED"
    UNDECIDABLE = "UNDECIDABLE"


@dataclass(frozen=True)
class ProbeResult:
    probe: str
    artifact_story: str
    outcome: Outcome
    evidence: str
    missing: Optional[str] = None

    def __post_init__(self):
        # A COULD_NOT_RUN without a named missing input would be exactly the
        # escape hatch this design exists to prevent. Fail loudly instead.
        if self.outcome is Outcome.COULD_NOT_RUN and not self.missing:
            raise ValueError(f"probe {self.probe!r}: COULD_NOT_RUN must name the missing input")
        if self.outcome is not Outcome.COULD_NOT_RUN and self.missing:
            raise ValueError(f"probe {self.probe!r}: only COULD_NOT_RUN may name a missing input")


@dataclass(frozen=True)
class Observation:
    """The facts a premise audit reasons over.

    Deliberately plain data. Retrieval lives elsewhere so that judgment can be
    tested without a running cluster, and so a probe can never quietly widen
    its own evidence by issuing another query mid-decision.

    `None` means "this input was not available", which is materially different
    from zero and is what drives COULD_NOT_RUN.
    """

    metric: str
    focus_value: float
    baseline_value: float

    focus_count: Optional[int] = None
    baseline_typical_count: Optional[int] = None

    # Categorical composition, e.g. {"/api/checkout": 0.51, ...}. Absent when
    # the index carries no dimension to group by.
    focus_composition: Optional[dict] = None
    baseline_composition: Optional[dict] = None

    # Seconds between event time and ingest time. Absent when the index has
    # only one clock -- in which case the question cannot be asked at all.
    focus_ingest_lag_p50_s: Optional[float] = None
    baseline_ingest_lag_p50_s: Optional[float] = None
    second_clock_field: Optional[str] = None

    # Concrete indices the window resolved to, and the mapping type of the
    # measured field in each.
    resolved_indices: Optional[list] = None
    metric_field_types: Optional[dict] = None


@dataclass
class AuditReport:
    verdict: Verdict
    observation_summary: str
    probes: list = field(default_factory=list)

    @property
    def missing_inputs(self):
        return [p.missing for p in self.probes if p.outcome is Outcome.COULD_NOT_RUN]

    def to_text(self) -> str:
        lines = [
            f"VERDICT: {self.verdict.value}",
            f"OBSERVATION: {self.observation_summary}",
            "",
            "REFUTATION ATTEMPTS (what was actually checked):",
        ]
        for p in self.probes:
            lines.append(f"  [{p.outcome.value:<13}] {p.probe}")
            lines.append(f"       tried to show: {p.artifact_story}")
            lines.append(f"       {p.evidence}")
            if p.missing:
                lines.append(f"       MISSING: {p.missing}")
        lines.append("")
        if self.verdict is Verdict.ARTIFACT:
            killers = [p.probe for p in self.probes if p.outcome is Outcome.REFUTED]
            lines.append(f"The observation is explained by how it was measured: {', '.join(killers)}.")
            lines.append("No causal investigation should proceed from this premise.")
        elif self.verdict is Verdict.SUBSTANTIATED:
            lines.append("Every refutation attempt ran and every one failed. The observation stands.")
            lines.append("This pass rests on the record above, not on the absence of a complaint.")
        else:
            lines.append("Nothing refuted the observation, but the sweep is incomplete.")
            lines.append("Not knocked down is not the same as cleared. Missing:")
            for m in self.missing_inputs:
                lines.append(f"  - {m}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Refutation attempts
# --------------------------------------------------------------------------

COLLAPSE_RATIO = 0.25  # focus volume below this fraction of baseline is a collapse
MIN_TRUSTWORTHY_N = 30
COMPOSITION_SHIFT = 0.25  # total variation distance above this is a different population
MIN_COMPOSITION_N = 30  # below this, composition is not estimable and the probe must decline
INGEST_LAG_MULTIPLE = 10.0  # focus lag this many times baseline suggests replay/backfill


def probe_sample_size(obs: Observation) -> ProbeResult:
    story = "the change is arithmetic noise because almost no data landed in the window"
    if obs.focus_count is None or obs.baseline_typical_count is None:
        return ProbeResult(
            "sample_size_collapse", story, Outcome.COULD_NOT_RUN,
            "document counts per window were not retrieved",
            missing="per-window document counts (focus and baseline)",
        )
    ratio = obs.focus_count / obs.baseline_typical_count if obs.baseline_typical_count else 0.0
    ev = f"focus n={obs.focus_count} vs baseline typical n={obs.baseline_typical_count} (ratio {ratio:.3f})"
    if obs.focus_count < MIN_TRUSTWORTHY_N or ratio < COLLAPSE_RATIO:
        return ProbeResult("sample_size_collapse", story, Outcome.REFUTED,
                           ev + f" -- below n={MIN_TRUSTWORTHY_N} or {COLLAPSE_RATIO:.0%} of baseline")
    return ProbeResult("sample_size_collapse", story, Outcome.NOT_REFUTED,
                       ev + " -- volume held; the change is not a small-sample effect")


def probe_population_shift(obs: Observation) -> ProbeResult:
    story = "nothing got worse; a different mix of things is being measured"
    if not obs.focus_composition or not obs.baseline_composition:
        return ProbeResult(
            "population_shift", story, Outcome.COULD_NOT_RUN,
            "no categorical breakdown was retrieved for either window",
            missing="a grouping dimension (e.g. endpoint, region, client) broken down per window",
        )

    # Independence guard. Below a usable sample size the observed composition
    # cannot match a large baseline no matter what the system did, so this probe
    # would fire on volume alone -- re-detecting what probe_sample_size already
    # found, while looking like a second, independent line of attack. That
    # inflates confidence in ARTIFACT and, worse, manufactures ARTIFACT verdicts
    # for legitimately low-traffic windows (off-peak hours, small regions).
    #
    # The verdict rule "every attempt ran and failed" is only as strong as the
    # attempts being independent. So this one declines to answer instead.
    if obs.focus_count is not None and obs.focus_count < MIN_COMPOSITION_N:
        return ProbeResult(
            "population_shift", story, Outcome.COULD_NOT_RUN,
            f"focus window holds {obs.focus_count} documents; composition is not estimable at that size",
            missing=(f"at least {MIN_COMPOSITION_N} documents in the focus window to compare composition "
                     f"(have {obs.focus_count})"),
        )

    keys = set(obs.focus_composition) | set(obs.baseline_composition)
    tvd = 0.5 * sum(abs(obs.focus_composition.get(k, 0.0) - obs.baseline_composition.get(k, 0.0)) for k in keys)
    ev = f"composition distance {tvd:.3f} across {len(keys)} categories"
    if tvd > COMPOSITION_SHIFT:
        return ProbeResult("population_shift", story, Outcome.REFUTED,
                           ev + f" -- exceeds {COMPOSITION_SHIFT}; the two windows are not comparable")
    return ProbeResult("population_shift", story, Outcome.NOT_REFUTED,
                       ev + " -- same population; the comparison is like-for-like")


def probe_clock_semantics(obs: Observation) -> ProbeResult:
    story = "the window is an ingest artifact -- a replay or backfill, not live traffic"
    if obs.second_clock_field is None or obs.focus_ingest_lag_p50_s is None or obs.baseline_ingest_lag_p50_s is None:
        return ProbeResult(
            "clock_semantics", story, Outcome.COULD_NOT_RUN,
            "the index exposes a single time field, so event time and ingest time cannot be compared",
            missing="a second time field (ingest/observed time) alongside the event timestamp",
        )
    base = obs.baseline_ingest_lag_p50_s or 0.001
    multiple = obs.focus_ingest_lag_p50_s / base
    ev = (f"ingest lag p50: focus {obs.focus_ingest_lag_p50_s:.1f}s vs baseline "
          f"{obs.baseline_ingest_lag_p50_s:.1f}s ({multiple:.1f}x), second clock `{obs.second_clock_field}`")
    if multiple >= INGEST_LAG_MULTIPLE:
        return ProbeResult("clock_semantics", story, Outcome.REFUTED,
                           ev + " -- these events were written long after they occurred")
    return ProbeResult("clock_semantics", story, Outcome.NOT_REFUTED,
                       ev + " -- both clocks agree; the window reflects when things happened")


def probe_measurement_continuity(obs: Observation) -> ProbeResult:
    story = "the ruler changed mid-window, so the two numbers are not the same measurement"
    if obs.resolved_indices is None or obs.metric_field_types is None:
        return ProbeResult(
            "measurement_continuity", story, Outcome.COULD_NOT_RUN,
            "index resolution and field mappings were not retrieved",
            missing=f"the concrete indices the window resolved to and the mapping of `{obs.metric}` in each",
        )
    types = {obs.metric_field_types.get(i) for i in obs.resolved_indices}
    ev = f"window spans {len(obs.resolved_indices)} index(es); `{obs.metric}` mapped as {sorted(str(t) for t in types)}"
    if len(types) > 1 or None in types:
        return ProbeResult("measurement_continuity", story, Outcome.REFUTED,
                           ev + " -- the field is not the same measurement across the window")
    return ProbeResult("measurement_continuity", story, Outcome.NOT_REFUTED,
                       ev + " -- one consistent mapping; the ruler did not change")


PROBES = (probe_sample_size, probe_population_shift, probe_clock_semantics, probe_measurement_continuity)


def decide(probes) -> Verdict:
    """Verdict precedence. See module docstring."""
    if any(p.outcome is Outcome.REFUTED for p in probes):
        return Verdict.ARTIFACT
    if all(p.outcome is Outcome.NOT_REFUTED for p in probes):
        return Verdict.SUBSTANTIATED
    return Verdict.UNDECIDABLE


def audit(obs: Observation) -> AuditReport:
    results = [probe(obs) for probe in PROBES]
    summary = (f"{obs.metric} in focus window = {obs.focus_value} vs baseline {obs.baseline_value} "
               f"({obs.focus_value / obs.baseline_value:.1f}x)" if obs.baseline_value else obs.metric)
    return AuditReport(verdict=decide(results), observation_summary=summary, probes=results)
