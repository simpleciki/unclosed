#!/usr/bin/env python3
"""Scoring: comparing what a run said against what was true.

Kept apart from the runner for the same reason the gates are kept apart from
retrieval -- a scorer that could issue a query while deciding could widen its
own evidence, and a scoring rule that needs a cluster cannot be tested.

The two directions are not summed
---------------------------------
This skill's whole position is that it would rather miss something than claim
something. A single accuracy figure hides exactly that, so misses and false
alarms are counted separately and never averaged into one number:

    missed_artifact      a fabricated incident passed Gate 1. The dangerous one
                         for this tool: an investigation now runs on a premise
                         nobody checked
    false_alarm          a genuine incident was called an artifact. Real damage,
                         opposite direction: a live problem gets dismissed
    over_claimed         a window with nothing in it came back SUBSTANTIATED
    missed_concentration a real concentration was reported as "elevated, not
                         established". Under-claiming. Counted, not hidden
    false_concentration  a concentration was confirmed where none exists. This
                         is the count that has to be zero, because the whole
                         argument for stopping at INCONCLUSIVE is that the
                         alternative is worse
    closed_the_unclosable a chain closed on an incident whose cause is not in
                         this index at all

Probe attribution is scored too
-------------------------------
A case designed to be caught by one probe and caught by a different one passed
for the wrong reason. The SUBSTANTIATED rule is "every attempt ran and every one
failed", and its strength rests entirely on the attempts being independent -- so
a probe firing on someone else's case is not a bonus, it is evidence against the
rule that makes a pass mean anything.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class RunResult:
    """What one run of the three gates reported. Plain data, extracted by the runner."""

    gate1_verdict: str
    refuting_probes: tuple = ()
    could_not_run_probes: tuple = ()

    #: (dimension, value) pairs the traversal CONFIRMED as carrying the effect.
    #: Empty when Gate 2 ran and confirmed nothing, which is a different fact
    #: from Gate 2 not having run at all -- hence the separate flag.
    confirmed_concentrations: tuple = ()
    gate2_ran: bool = True

    closed: bool = False
    accounting: Optional[str] = None
    open_branches: tuple = ()

    #: The effect as each estimator read it, and the divergence Gate 1 computed.
    reported_effect: Optional[float] = None
    reported_effect_alt: Optional[float] = None
    estimator_divergence: Optional[float] = None

    #: Did the run measure the same window the corpus knows the truth about?
    #: False when nobody named a window and the tool selected its own, in which
    #: case the estimate and the exact value describe different buckets.
    #: Differencing them would produce a large, authoritative-looking error
    #: figure about nothing -- a wrong baseline dressed as a measurement, which
    #: is one of the failures this project exists to name.
    audited_planted_window: bool = True

    error: Optional[str] = None  # the run failed outright


@dataclass(frozen=True)
class Score:
    case: str

    gate1_ok: bool
    gate1_verdict: str
    gate1_error: Optional[str] = None

    #: `None` when the case named no probe to attribute, or Gate 1 found no artifact.
    attribution_ok: Optional[bool] = None
    unexpected_refuters: tuple = ()

    #: `None` when Gate 2 did not run, so there is nothing to score.
    concentration_ok: Optional[bool] = None
    concentration_error: Optional[str] = None
    true_share: Optional[float] = None
    max_apparent_share: Optional[float] = None

    closure_ok: bool = True
    closure_error: Optional[str] = None
    closed: bool = False

    #: Limit B, measured. How far each estimator's reading of the effect sits
    #: from the exact value, and whether their disagreement is large enough to
    #: cover that gap. When it is not, the divergence understates the error and
    #: must not be read as a confidence interval.
    ruler_error_primary: Optional[float] = None
    ruler_error_alt: Optional[float] = None
    ruler_disagreement: Optional[float] = None
    disagreement_covers_error: Optional[bool] = None

    run_error: Optional[str] = None


def _relative(reported: Optional[float], true: Optional[float]) -> Optional[float]:
    if reported is None or true is None or not true:
        return None
    return round((reported - true) / abs(true), 4)


def score_gate1(truth, result: RunResult):
    """Correct, or wrong in which direction.

    The direction is read off what the corpus *planted*, never off which
    verdicts it would have accepted. Those are not the same question, and
    conflating them mislabels the negative control: ARTIFACT is an acceptable
    answer there -- on flat data the "effect" is noise and a probe refuting it
    has done its job -- so an acceptable-verdicts test would read a
    SUBSTANTIATED as a missed artifact. Nothing was planted to miss. The failure
    is that it substantiated an empty window, which is the opposite direction
    and the more serious one.
    """
    if result.gate1_verdict in truth.gate1_expected:
        return True, None
    if truth.artifact_kind and result.gate1_verdict != "ARTIFACT":
        return False, "missed_artifact"
    if truth.premise_is_real and result.gate1_verdict == "ARTIFACT":
        return False, "false_alarm"
    if result.gate1_verdict == "SUBSTANTIATED":
        return False, "over_claimed"
    return False, "unexpected_verdict"


def score_attribution(truth, result: RunResult):
    """Was the artifact caught by the probe it was built for, and only that one?

    Returns (ok, unexpected_refuters). `ok` is None when there is nothing to
    attribute -- either the case plants no artifact, or Gate 1 did not find one.
    """
    if not truth.artifact_kind or not result.refuting_probes:
        return None, ()
    unexpected = tuple(p for p in result.refuting_probes if p != truth.artifact_kind)
    return (truth.artifact_kind in result.refuting_probes and not unexpected), unexpected


def score_concentration(truth, result: RunResult):
    """Did the tree find the concentration that is there, and only that one?

    `None` when Gate 2 did not run: a premise that failed Gate 1 gets no
    traversal, and scoring an absent tree as a miss would count one failure
    twice.
    """
    if not result.gate2_ran:
        return None, None
    planted = truth.concentrated_in
    found = tuple(result.confirmed_concentrations)
    if planted is None:
        return (True, None) if not found else (False, "false_concentration")
    if planted in found:
        # Extra dimensions alongside the planted one are not scored as a second
        # failure: two dimensions can isolate the same documents from different
        # angles, which is why the assembler lets only one carry magnitude.
        return True, None
    return (False, "missed_concentration") if not found else (False, "wrong_value")


def score_closure(truth, result: RunResult):
    """One-sided on purpose.

    Failing to close is not scored as an error. On a request-log index the
    hypotheses that would close a chain -- a deploy landed, a dependency slowed,
    the host lost resources -- are not answerable from the data at all, and the
    assembler records them as unwalked rather than pretending otherwise. So no
    traversal built from this index can close, and grading that would be grading
    the index.

    Closing something unclosable is a different matter entirely, and is the one
    direction this checks.
    """
    if result.closed and not truth.cause_in_index:
        return False, "closed_the_unclosable"
    return True, None


def score_case(name: str, truth, result: RunResult) -> Score:
    if result.error:
        return Score(case=name, gate1_ok=False, gate1_verdict="ERROR",
                     gate1_error="run_failed", run_error=result.error)

    gate1_ok, gate1_error = score_gate1(truth, result)
    attribution_ok, unexpected = score_attribution(truth, result)
    concentration_ok, concentration_error = score_concentration(truth, result)
    closure_ok, closure_error = score_closure(truth, result)

    err_primary = err_alt = divergence = covers = None
    if result.audited_planted_window:
        err_primary = _relative(result.reported_effect, truth.true_effect)
        err_alt = _relative(result.reported_effect_alt, truth.true_effect)
        divergence = result.estimator_divergence
        if divergence is not None and err_primary is not None:
            covers = divergence >= abs(err_primary)

    return Score(
        case=name,
        gate1_ok=gate1_ok, gate1_verdict=result.gate1_verdict, gate1_error=gate1_error,
        attribution_ok=attribution_ok, unexpected_refuters=unexpected,
        concentration_ok=concentration_ok, concentration_error=concentration_error,
        true_share=truth.true_share, max_apparent_share=truth.max_apparent_share,
        closure_ok=closure_ok, closure_error=closure_error, closed=result.closed,
        ruler_error_primary=err_primary, ruler_error_alt=err_alt,
        ruler_disagreement=divergence, disagreement_covers_error=covers,
    )


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

#: Every failure this corpus can express. Listed rather than derived so a
#: category that never fires still appears in the report as a zero -- a count
#: that is absent because nothing triggered it reads identically to a count that
#: is absent because nobody looked for it.
FAILURE_KINDS = (
    "missed_artifact",
    "false_alarm",
    "over_claimed",
    "unexpected_verdict",
    "missed_concentration",
    "false_concentration",
    "wrong_value",
    "closed_the_unclosable",
    "run_failed",
)


@dataclass
class Summary:
    total: int = 0
    gate1_correct: int = 0
    gate1_scored: int = 0
    attribution_correct: int = 0
    attribution_scored: int = 0
    concentration_correct: int = 0
    concentration_scored: int = 0
    closure_correct: int = 0
    failures: dict = field(default_factory=lambda: {k: 0 for k in FAILURE_KINDS})

    @property
    def miss_rate(self) -> Optional[float]:
        """Under-claims as a share of the cases where one was possible.

        Deliberately not merged with the false-alarm rate. They are different
        harms and a tool that trades one for the other has not improved.
        """
        possible = self.gate1_scored + self.concentration_scored
        if not possible:
            return None
        misses = self.failures["missed_artifact"] + self.failures["missed_concentration"]
        return round(misses / possible, 4)

    @property
    def over_claim_rate(self) -> Optional[float]:
        possible = self.gate1_scored + self.concentration_scored
        if not possible:
            return None
        over = (self.failures["false_alarm"] + self.failures["over_claimed"]
                + self.failures["false_concentration"] + self.failures["wrong_value"]
                + self.failures["closed_the_unclosable"])
        return round(over / possible, 4)


def summarize(scores) -> Summary:
    s = Summary()
    for sc in scores:
        s.total += 1
        s.gate1_scored += 1
        if sc.gate1_ok:
            s.gate1_correct += 1
        if sc.attribution_ok is not None:
            s.attribution_scored += 1
            if sc.attribution_ok:
                s.attribution_correct += 1
        if sc.concentration_ok is not None:
            s.concentration_scored += 1
            if sc.concentration_ok:
                s.concentration_correct += 1
        if sc.closure_ok:
            s.closure_correct += 1
        for err in (sc.gate1_error, sc.concentration_error, sc.closure_error):
            if err in s.failures:
                s.failures[err] += 1
    return s


@dataclass(frozen=True)
class DetectionPoint:
    """One rung of the sweep: at this true strength, how often did it fire?"""

    label: str
    true_share_mean: Optional[float]
    trials: int
    confirmed: int
    false_alarms: int

    @property
    def rate(self) -> float:
        return round(self.confirmed / self.trials, 4) if self.trials else 0.0


def detection_curve(rows) -> list:
    """Group sweep results by strength.

    `rows` are (label, true_share, planted, confirmed) tuples. Ordered by the
    measured share rather than by the parameter that produced it, because the
    curve is a statement about strength, and the parameter is only a way of
    reaching one.
    """
    grouped = {}
    for label, true_share, planted, confirmed in rows:
        grouped.setdefault(label, []).append((true_share, planted, confirmed))

    points = []
    for label, entries in grouped.items():
        shares = [s for s, _, _ in entries if s is not None]
        planted_any = any(p for _, p, _ in entries)
        confirmed_n = sum(1 for _, _, c in entries if c)
        points.append(DetectionPoint(
            label=label,
            true_share_mean=round(sum(shares) / len(shares), 4) if shares else None,
            trials=len(entries),
            confirmed=confirmed_n,
            false_alarms=0 if planted_any else confirmed_n,
        ))
    points.sort(key=lambda p: (p.true_share_mean if p.true_share_mean is not None else -1e9))
    return points


def detection_floor(points, threshold: float = 1.0) -> Optional[float]:
    """The weakest measured strength at which every trial fired.

    Reported as a share of the window's own median rise, the same unit the
    probe's threshold is stated in, so the two can be compared directly. `None`
    when no rung reached the rate -- which is itself the finding, and is not the
    same as a floor of zero.
    """
    for p in points:
        if p.true_share_mean is None or p.true_share_mean <= 0:
            continue
        if p.rate >= threshold:
            return p.true_share_mean
    return None
