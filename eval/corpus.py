#!/usr/bin/env python3
"""The evaluation corpus: indices whose truth is known because we generated them.

Why a corpus rather than a demo
-------------------------------
Every claim this skill makes is a claim about *judgement* -- that it separates a
regression from an artifact, that it does not confirm a concentration which is
not there, that it declines to close a chain it cannot close. A captured run
against one fixture shows the tool producing output. It does not show the output
being right, and it cannot show how often it is wrong.

So each case carries a `Truth`, and the Truth is **measured from the generated
documents**, not asserted alongside them. `true_share` is not the parameter that
was passed in; it is the value that parameter actually produced, computed
locally with the same nearest-rank definition the rest of the project uses. A
corpus whose labels are its own inputs grades the generator, not the gate.

The estimator error is measured the same way and for the same reason. The exact
p99 of a set of documents is computable here, from the list that was indexed.
Whatever OpenSearch reports for those documents is an *estimate* of it, and the
gap between the two is a fact about the measurement rather than the incident.

What is deliberately in here
----------------------------
- artifacts of four kinds, each of which a *different* probe must catch. Which
  probe fires is recorded, because a probe that catches a case meant for another
  is not a bonus -- it is evidence the two are not independent, and the
  SUBSTANTIATED rule rests on their independence
- a genuine regression whose cause is not in this index at all. The correct
  answer is "not closed, and here is what is missing". A run that closes it is
  wrong, and this is the case SKILL.md promises
- a negative control with no incident, queried the way a scan queries: with no
  window named. It must not come back substantiated
- a graded sweep across known concentration strengths, including a true zero,
  which is what turns "the probe is under-powered" into a number

Standard library only.
"""

from __future__ import annotations

import dataclasses
import math
import random
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from seed_logs import INDEX_MAPPING, REGIONS, bulk_load  # noqa: E402,F401

BUCKET_MINUTES = 10
BUCKETS = 36
NORMAL_VOLUME = 200
COLLAPSED_VOLUME = 3
SPIKE_BUCKET_FROM_END = 4

BASE_MEDIAN_MS = 80.0
SIGMA = 0.6

ENDPOINTS = ("/api/checkout", "/api/search", "/api/cart", "/api/profile")
TARGET_ENDPOINT = "/api/checkout"

#: Share of requests that fail. Left where a real storefront sits rather than
#: raised to make the dimension estimable: `status` is then too rare to compare,
#: the probe says so, and that is a true thing about request logs which a corpus
#: should not tune away in order to look better against itself.
ERROR_RATE = 0.02

UNIFORM_MIX = {e: 1.0 for e in ENDPOINTS}


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _lognormal(rng: random.Random, median_ms: float) -> float:
    return round(rng.lognormvariate(math.log(median_ms), SIGMA), 2)


def _pick(rng: random.Random, mix: dict) -> str:
    keys = sorted(mix)
    return rng.choices(keys, weights=[mix[k] for k in keys], k=1)[0]


def _doc(rng: random.Random, event_time: datetime, endpoint: str, latency: float, lag_s: float):
    return {
        "@timestamp": _iso(event_time),
        "ingested_at": _iso(event_time + timedelta(seconds=lag_s)),
        "service": "storefront",
        "endpoint": endpoint,
        "region": rng.choice(REGIONS),
        "status": 200 if rng.random() > ERROR_RATE else 500,
        "latency_ms": latency,
    }


def _bucket(rng: random.Random, start: datetime, volume: int, profile: dict, mix: dict,
            lag_s: Optional[float] = None, fixed_latencies=None):
    """One 10-minute bucket.

    `profile` maps endpoint -> median latency; `mix` maps endpoint -> weight.
    Keeping them separate is what lets a scenario move the latency of one
    endpoint (a regression) independently of how many requests hit it (a
    population shift). Those two produce the same percentile chart, so they must
    not be the same knob here.
    """
    docs = []
    if fixed_latencies is not None:
        for latency in fixed_latencies:
            offset = rng.uniform(0, BUCKET_MINUTES * 60)
            docs.append(_doc(rng, start + timedelta(seconds=offset), _pick(rng, mix), latency,
                             rng.uniform(0.5, 4.0) if lag_s is None else lag_s))
        return docs
    for _ in range(volume):
        offset = rng.uniform(0, BUCKET_MINUTES * 60)
        ep = _pick(rng, mix)
        docs.append(_doc(rng, start + timedelta(seconds=offset), ep,
                         _lognormal(rng, profile[ep]),
                         rng.uniform(0.5, 4.0) if lag_s is None else lag_s))
    return docs


def _flat_profile(median_ms: float = BASE_MEDIAN_MS) -> dict:
    return {e: median_ms for e in ENDPOINTS}


def aligned_now() -> datetime:
    """Snap to a bucket boundary.

    `fixed_interval` buckets are epoch-aligned. A synthetic bucket starting at
    an arbitrary minute is split across two reported ones, and a collapsed
    bucket stops being visibly collapsed -- the scenario has to survive being
    queried the way an analyst would actually query it.
    """
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    return now - timedelta(minutes=now.minute % BUCKET_MINUTES)


def generate(seed: int, now: datetime, focus_profile: dict, baseline_profile: Optional[dict] = None,
             focus_volume: int = NORMAL_VOLUME, focus_mix: Optional[dict] = None,
             baseline_mix: Optional[dict] = None, focus_lag_s: Optional[float] = None,
             focus_fixed_latencies=None):
    """Build a full history with one modified bucket, and say where that bucket is."""
    rng = random.Random(seed)
    baseline_profile = baseline_profile or _flat_profile()
    baseline_mix = baseline_mix or UNIFORM_MIX
    focus_mix = focus_mix or baseline_mix

    window_start = now - timedelta(minutes=BUCKET_MINUTES * BUCKETS)
    spike_index = BUCKETS - SPIKE_BUCKET_FROM_END

    docs, focus_start = [], None
    for i in range(BUCKETS):
        start = window_start + timedelta(minutes=BUCKET_MINUTES * i)
        if i == spike_index:
            focus_start = start
            docs.extend(_bucket(rng, start, focus_volume, focus_profile, focus_mix,
                                focus_lag_s, focus_fixed_latencies))
        else:
            docs.extend(_bucket(rng, start, NORMAL_VOLUME, baseline_profile, baseline_mix))
    return docs, focus_start


# --------------------------------------------------------------------------
# Measuring what was actually generated
# --------------------------------------------------------------------------

def exact_percentile(values, pct: float) -> Optional[float]:
    """Nearest-rank, the same definition `scripts/seed_logs.py` uses.

    Identical on purpose: the estimator error this corpus reports has to be
    comparable to the figures already published elsewhere in the project, and a
    second definition of "the true p99" would make those numbers quietly
    incomparable while both looked authoritative.
    """
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, math.ceil(pct * len(ordered)) - 1)
    return round(ordered[idx], 4)


def _split(docs, focus_start: datetime):
    focus_end = focus_start + timedelta(minutes=BUCKET_MINUTES)
    focus, baseline = [], []
    for d in docs:
        ts = datetime.fromisoformat(d["@timestamp"].replace("Z", "+00:00"))
        (focus if focus_start <= ts < focus_end else baseline).append(d)
    return focus, baseline


@dataclass(frozen=True)
class Truth:
    """What is true about a generated index, computed from its documents.

    Every field is derived from the list that was indexed. None of them is the
    parameter that produced it -- a label that is its own input grades the
    generator rather than the gate.
    """

    #: Did something in the system actually get slower?
    premise_is_real: bool
    #: Gate 1 verdicts that are correct for this case.
    gate1_expected: tuple
    #: Which refutation attempt is the one that should fire, if any. Recorded
    #: separately from the verdict because a case caught by the *wrong* probe
    #: passed for the wrong reason, and the SUBSTANTIATED rule ("every attempt
    #: ran and every one failed") is only as strong as the attempts being
    #: independent.
    artifact_kind: Optional[str]

    #: (dimension, value) the rise genuinely lives in, or None if it is spread.
    concentrated_in: Optional[tuple]
    #: Concentration expressed the way the probe expresses it: the target's
    #: median excess over the rest, as a share of the window's own median rise.
    #: Measured, not requested.
    true_share: Optional[float]
    #: The largest such share across *any* value of the dimension, whether or
    #: not one was planted. On a case with no concentration this is the size of
    #: the gap that noise alone produces at these subgroup sizes -- the number
    #: the probe's threshold has to clear.
    max_apparent_share: Optional[float]

    #: Is there anything in *this index* that could close the chain? For a
    #: request log the answer is usually no, and saying so is the point.
    cause_in_index: bool

    true_focus_p99: float
    true_baseline_p99: float
    true_focus_median: float
    true_baseline_median: float
    focus_n: int
    baseline_typical_n: int

    @property
    def true_effect(self) -> float:
        return round(self.true_focus_p99 - self.true_baseline_p99, 4)

    @property
    def true_median_rise(self) -> float:
        return round(self.true_focus_median - self.true_baseline_median, 4)


def _shares(focus, dim, rise):
    """Every value's median excess over the rest, as a share of the window's rise."""
    if not rise:
        return {}
    values = {d[dim] for d in focus}
    out = {}
    for v in values:
        inside = [d["latency_ms"] for d in focus if d[dim] == v]
        outside = [d["latency_ms"] for d in focus if d[dim] != v]
        m_in, m_out = exact_percentile(inside, 0.50), exact_percentile(outside, 0.50)
        if m_in is None or m_out is None:
            continue
        out[v] = round((m_in - m_out) / rise, 4)
    return out


def measure(docs, focus_start: datetime, premise_is_real: bool, gate1_expected: tuple,
            artifact_kind: Optional[str], concentrated_dim: Optional[str],
            concentrated_value: Optional[str], cause_in_index: bool) -> Truth:
    """Compute the Truth of a generated index from the documents themselves."""
    focus, baseline = _split(docs, focus_start)
    f_lat = [d["latency_ms"] for d in focus]

    # The baseline the tool compares against is the median of the other buckets'
    # p99s, not the p99 of every other document pooled. Measured the same way
    # here, or the two numbers would describe different quantities.
    per_bucket = {}
    for d in baseline:
        ts = datetime.fromisoformat(d["@timestamp"].replace("Z", "+00:00"))
        key = ts.replace(minute=(ts.minute // BUCKET_MINUTES) * BUCKET_MINUTES,
                         second=0, microsecond=0)
        per_bucket.setdefault(key, []).append(d["latency_ms"])
    bucket_p99s = [exact_percentile(v, 0.99) for v in per_bucket.values()]
    bucket_medians = [exact_percentile(v, 0.50) for v in per_bucket.values()]

    focus_median = exact_percentile(f_lat, 0.50)
    baseline_median = round(statistics.median(bucket_medians), 4)
    rise = focus_median - baseline_median

    shares = _shares(focus, concentrated_dim or "endpoint", rise)
    true_share = shares.get(concentrated_value) if concentrated_value else None
    max_apparent = max(shares.values()) if shares else None

    return Truth(
        premise_is_real=premise_is_real,
        gate1_expected=gate1_expected,
        artifact_kind=artifact_kind,
        concentrated_in=(concentrated_dim, concentrated_value) if concentrated_value else None,
        true_share=true_share,
        max_apparent_share=max_apparent,
        cause_in_index=cause_in_index,
        true_focus_p99=exact_percentile(f_lat, 0.99),
        true_baseline_p99=round(statistics.median(bucket_p99s), 4),
        true_focus_median=focus_median,
        true_baseline_median=baseline_median,
        focus_n=len(focus),
        baseline_typical_n=int(statistics.median(len(v) for v in per_bucket.values())),
    )


@dataclass(frozen=True)
class Case:
    name: str
    index: str
    docs: list
    focus_start: str
    focus_end: str
    truth: Truth
    #: `False` means nobody named a window, so the tool selects its own -- which
    #: it may never substantiate, however clean the data looks.
    report_window: bool = True
    #: Report time other than the window's end, for the partial-window case.
    reported_at: Optional[str] = None
    note: str = ""

    @property
    def report_time(self) -> Optional[str]:
        if not self.report_window:
            return None
        return self.reported_at or self.focus_end


#: An artifact has to be named as one.
ARTIFACT = ("ARTIFACT",)

#: A real regression must survive. Either verdict is correct: SUBSTANTIATED when
#: the full sweep ran, UNDECIDABLE when an input for one attempt was absent.
#: Treating UNDECIDABLE as a failure here would punish the tool for admitting a
#: gap, which is the behaviour the design exists to produce.
REAL = ("SUBSTANTIATED", "UNDECIDABLE")

#: On a window with nothing in it, picked by the tool itself, only one answer is
#: wrong. UNDECIDABLE is right because the window's provenance cannot be
#: established. ARTIFACT is *also* right: on flat data the "effect" is noise, and
#: a probe refuting it has done its job. SUBSTANTIATED is the only failure, and
#: it is the one that matters -- it would mean the tool found an incident it
#: selected for itself.
NOT_SUBSTANTIABLE = ("UNDECIDABLE", "ARTIFACT")


def _case(name, seed, now, gate1_expected, premise_is_real, artifact_kind, concentrated_dim,
          concentrated_value, cause_in_index, note, index_prefix="unclosed-eval", **gen):
    docs, focus_start = generate(seed, now, **gen)
    truth = measure(docs, focus_start, premise_is_real, gate1_expected, artifact_kind,
                    concentrated_dim, concentrated_value, cause_in_index)
    return Case(
        name=name,
        index=f"{index_prefix}-{name}",
        docs=docs,
        focus_start=_iso(focus_start),
        focus_end=_iso(focus_start + timedelta(minutes=BUCKET_MINUTES)),
        truth=truth,
        note=note,
    )


# --------------------------------------------------------------------------
# The cases
# --------------------------------------------------------------------------

#: One endpoint is naturally slower than the others, in every bucket. Nothing
#: about it changes; a shift in *who is calling* moves the window's p99 on its
#: own, and no threshold on the ratio can see the difference.
SKEWED_PROFILE = {"/api/checkout": 420.0, "/api/search": 70.0,
                  "/api/cart": 70.0, "/api/profile": 70.0}
SKEWED_BASELINE_MIX = {"/api/checkout": 0.3, "/api/search": 1.0,
                       "/api/cart": 1.0, "/api/profile": 1.0}
SKEWED_FOCUS_MIX = {"/api/checkout": 12.0, "/api/search": 1.0,
                    "/api/cart": 1.0, "/api/profile": 1.0}


def build_cases(seed: int, now: Optional[datetime] = None) -> list:
    """The fixed part of the corpus. The graded sweep is built separately."""
    now = now or aligned_now()
    cases = []

    # -- artifacts: four kinds, and a different probe must catch each one ----

    cases.append(_case(
        "artifact-volume-collapse", seed, now,
        gate1_expected=ARTIFACT, premise_is_real=False,
        artifact_kind="sample_size_collapse",
        concentrated_dim=None, concentrated_value=None, cause_in_index=False,
        note="volume collapsed to n=3; p99 over almost no data is arithmetic, not performance",
        focus_profile=_flat_profile(), focus_volume=COLLAPSED_VOLUME,
        focus_fixed_latencies=(110.4, 1850.2, 2210.7),
    ))

    cases.append(_case(
        "artifact-population-shift", seed, now,
        gate1_expected=ARTIFACT, premise_is_real=False,
        artifact_kind="population_shift",
        concentrated_dim=None, concentrated_value=None, cause_in_index=False,
        note="nothing got slower; a slow endpoint's share of the traffic went up",
        focus_profile=SKEWED_PROFILE, baseline_profile=SKEWED_PROFILE,
        focus_mix=SKEWED_FOCUS_MIX, baseline_mix=SKEWED_BASELINE_MIX,
    ))

    cases.append(_case(
        "artifact-replay", seed, now,
        gate1_expected=ARTIFACT, premise_is_real=False,
        artifact_kind="clock_semantics",
        concentrated_dim=None, concentrated_value=None, cause_in_index=False,
        note="events written two hours after they happened; a backfill, not live traffic",
        focus_profile=_flat_profile(450.0), focus_lag_s=7200.0,
    ))

    partial = _case(
        "artifact-partial-window", seed, now,
        gate1_expected=ARTIFACT, premise_is_real=False,
        artifact_kind="observation_moment",
        concentrated_dim=None, concentrated_value=None, cause_in_index=False,
        note="a real-looking regression reported two minutes into a ten-minute bucket; "
             "reporter and auditor are reading different datasets that share a name",
        focus_profile=_flat_profile(450.0),
    )
    cases.append(dataclasses.replace(
        partial,
        reported_at=_iso(datetime.fromisoformat(partial.focus_start.replace("Z", "+00:00"))
                         + timedelta(minutes=2)),
    ))

    # -- real regressions ----------------------------------------------------

    cases.append(_case(
        "real-concentrated", seed, now,
        gate1_expected=REAL, premise_is_real=True, artifact_kind=None,
        concentrated_dim="endpoint", concentrated_value=TARGET_ENDPOINT, cause_in_index=True,
        note="a genuine regression confined to one endpoint; the tree should find it",
        focus_profile={**_flat_profile(), TARGET_ENDPOINT: 600.0},
    ))

    # Built to attack the permutation null's one assumption: that under the
    # null any request could equally have carried any label. Here one endpoint
    # is permanently four times slower than the others -- in every bucket,
    # before and after -- and then everything rises by the same amount. The gap
    # between subgroups is large, real, and *unchanged*, so it explains none of
    # the rise. A test that shuffles labels within the focus window alone cannot
    # see that the gap was already there, and should be expected to confirm a
    # concentration that did not happen. How badly is measured, not assumed.
    cases.append(_case(
        "real-uniform-rise-on-skewed-index", seed, now,
        gate1_expected=REAL, premise_is_real=True, artifact_kind=None,
        concentrated_dim=None, concentrated_value=None, cause_in_index=False,
        note="one endpoint is permanently slower; everything then rises by the same amount, "
             "so the subgroup gap is unchanged and explains none of the rise",
        baseline_profile=SKEWED_PROFILE,
        focus_profile={k: v + 300.0 for k, v in SKEWED_PROFILE.items()},
    ))

    # The case SKILL.md promises: real, and the answer is not in this index.
    cases.append(_case(
        "real-no-discoverable-cause", seed, now,
        gate1_expected=REAL, premise_is_real=True, artifact_kind=None,
        concentrated_dim=None, concentrated_value=None, cause_in_index=False,
        note="everything got slower by the same amount; nothing in a request log separates "
             "a noisy neighbour from a slow dependency, and the honest answer is 'not closed'",
        focus_profile=_flat_profile(450.0),
    ))

    # -- negative control ----------------------------------------------------

    control = _case(
        "control-nothing-happened", seed, now,
        gate1_expected=NOT_SUBSTANTIABLE, premise_is_real=False, artifact_kind=None,
        concentrated_dim=None, concentrated_value=None, cause_in_index=False,
        note="no incident anywhere, queried the way a scan queries: no window named. "
             "Every dataset has a maximum, and substantiating the one you picked yourself "
             "is drawing the target around the arrows",
        focus_profile=_flat_profile(),
    )
    cases.append(dataclasses.replace(control, report_window=False))

    return cases


#: How much slower the target endpoint runs in the focus window, as a multiple
#: of the others. Chosen to bracket the probe's 50% threshold from both sides.
#: The share each factor actually produces is measured, never assumed.
SWEEP_FACTORS = (1.0, 1.2, 1.4, 1.6, 1.8, 2.0, 3.0)

#: Everything moves in the sweep, so the window's own median rise is large and
#: a subgroup's excess is measured against it rather than against the baseline.
SWEEP_BASE_MS = 300.0


def build_sweep(seeds, now: Optional[datetime] = None, factors=SWEEP_FACTORS) -> list:
    """Graded concentration, including a true zero.

    The probe's threshold is stated as a share of the window's own median rise,
    so the sweep is parameterised in that same quantity: at each strength, does
    the probe confirm? Repeated across seeds, because one draw is a coin flip
    and a detection floor read off a single draw is a story about that draw.

    `factor=1.0` is the null: every endpoint moves by the same amount, the true
    concentration is zero, and any confirmation there is a false alarm.
    """
    now = now or aligned_now()
    cases = []
    for factor in factors:
        for seed in seeds:
            profile = {**_flat_profile(SWEEP_BASE_MS), TARGET_ENDPOINT: SWEEP_BASE_MS * factor}
            planted = factor > 1.0
            cases.append(_case(
                f"sweep-x{int(round(factor * 10)):03d}-s{seed}", seed, now,
                gate1_expected=REAL, premise_is_real=True, artifact_kind=None,
                concentrated_dim="endpoint",
                concentrated_value=TARGET_ENDPOINT if planted else None,
                cause_in_index=planted,
                note=f"{TARGET_ENDPOINT} runs {factor:g}x the others in the focus window",
                focus_profile=profile,
            ))
    return cases
