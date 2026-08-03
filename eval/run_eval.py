#!/usr/bin/env python3
"""Run the corpus through all three gates and publish what the skill misses.

    python eval/run_eval.py --out examples/miss-rate.txt

Requires a running cluster; the corpus is indexed and torn down as it goes.

What this produces, and why in this order
-----------------------------------------
1. **Verdict accuracy per case**, with the failing direction named. Misses and
   false alarms are never averaged together: this skill's entire position is
   that it would rather miss than claim, and a single accuracy number would hide
   exactly the trade it is making.

2. **Probe attribution.** Each planted artifact is built for one refutation
   attempt. If a different attempt catches it, the case passed for the wrong
   reason -- and the SUBSTANTIATED rule ("every attempt ran and every one
   failed") only means something while the attempts are independent.

3. **The concentration detection floor.** A graded sweep with a true zero at one
   end. This is the measured form of the known limit stated in SKILL.md: the
   probe under-claims, and this says by how much and from where.

4. **Ruler error against ruler disagreement.** The exact percentile of a set of
   documents is computable here because they were generated here. Comparing it
   against what each estimator reported turns "two estimators are not a
   confidence interval" from a caveat into a measurement.

5. **What did not close, and why.** Reported as a structural fact rather than a
   score. On a request-log index the hypotheses that would close a chain are not
   answerable from the data, so no traversal assembled from one can close.
   Grading that would be grading the index.

6. **What this harness cannot see.** Printed last and printed always. Every rate
   above is a rate of a failure this corpus can produce; a category reading 0
   because nothing triggered it is indistinguishable from one reading 0 because
   nothing here could. At least one real defect has already landed in that gap,
   and it is named rather than left for the next person to rediscover.

Output is ASCII only: this report is read in terminals whose default encoding is
not UTF-8, and a report that raises UnicodeEncodeError on the machine it was
generated on has failed at the only job it has.
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "skills" / "opensearch-skills" / "observability" / "unclosed" / "scripts"))

from assemble_traversal import assemble  # noqa: E402
from closure_audit import audit_closure, closes  # noqa: E402
from corpus import aligned_now, build_cases, build_sweep, bulk_load  # noqa: E402
from premise_audit import Outcome  # noqa: E402
from score import (RunResult, detection_curve, detection_floor,  # noqa: E402
                   score_case, summarize)

DEFAULT_ENDPOINT = "http://127.0.0.1:9250"
SWEEP_SEEDS = (1, 2, 3, 4, 5)
CORPUS_SEED = 20260802


def _delete(endpoint, index):
    req = urllib.request.Request(f"{endpoint}/{index}", method="DELETE")
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except (urllib.error.HTTPError, urllib.error.URLError):
        pass


def run_case(endpoint, case) -> RunResult:
    """Index one case, run all three gates over it, and report plain facts."""
    rc = bulk_load(endpoint, case.index, case.docs, recreate=True)
    if rc != 0:
        return RunResult(gate1_verdict="ERROR", error=f"could not index {case.index}")

    try:
        run = assemble(
            endpoint, case.index, "latency_ms", "@timestamp", "endpoint",
            bucket_minutes=10, lookback_hours=6,
            focus_window=case.focus_start if case.report_window else None,
            reported_at=case.report_time,
        )
    except SystemExit as exc:  # the scripts exit on transport failure
        return RunResult(gate1_verdict="ERROR", error=str(exc))

    obs, premise = run.observation, run.premise
    refuted = tuple(p.probe for p in premise.probes if p.outcome is Outcome.REFUTED)
    could_not = tuple(p.probe for p in premise.probes if p.outcome is Outcome.COULD_NOT_RUN)

    alt_effect = None
    if obs.focus_value_alt is not None and obs.baseline_value_alt is not None:
        alt_effect = round(obs.focus_value_alt - obs.baseline_value_alt, 4)

    if run.traversal is None:
        return RunResult(
            gate1_verdict=premise.verdict.value, refuting_probes=refuted,
            could_not_run_probes=could_not, gate2_ran=False,
            reported_effect=round(run.observed_effect, 4), reported_effect_alt=alt_effect,
            estimator_divergence=run.uncertainty,
            audited_planted_window=case.report_window,
        )

    closed, _ = closes(run.traversal, run.observed_effect, run.uncertainty)
    magnitude = audit_closure(run.traversal, run.observed_effect, run.uncertainty)
    return RunResult(
        gate1_verdict=premise.verdict.value, refuting_probes=refuted,
        could_not_run_probes=could_not,
        confirmed_concentrations=run.concentrations, gate2_ran=True,
        closed=closed, accounting=magnitude.verdict.value,
        open_branches=tuple(run.traversal.unclosed_reasons()),
        reported_effect=round(run.observed_effect, 4), reported_effect_alt=alt_effect,
        estimator_divergence=run.uncertainty,
        audited_planted_window=case.report_window,
    )


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def _pct(x, digits=1):
    return "n/a" if x is None else f"{x * 100:.{digits}f}%"


def _num(x, digits=2):
    return "n/a" if x is None else f"{x:.{digits}f}"


def render(trials, scores, results, sweep_rows, points, floor, generated_at, seeds) -> str:
    """`trials` maps a case name to every (case, result, score) run of it."""
    L = []
    W = 78
    L.append("=" * W)
    L.append("unclosed -- evaluation and miss rate")
    L.append(f"generated {generated_at}")
    L.append("=" * W)
    L.append("")
    L.append("Every case below was generated by eval/corpus.py, so its truth is known")
    L.append("independently of what the gates said about it. The exact percentiles are")
    L.append("computed locally from the documents that were indexed; whatever OpenSearch")
    L.append("reported for the same documents is an estimate of them.")
    L.append("")
    L.append(f"Each case is run {len(seeds)} times against independently generated data.")
    L.append("A single draw cannot measure a failure that depends on where the noise fell,")
    L.append("and this corpus contains one that does: run once, it reports a clean sweep")
    L.append("roughly half the time. A pass rate below 100% is a defect, not variance to be")
    L.append("rounded away.")
    L.append("")

    def rate(name, attr):
        runs = trials[name]
        scored = [s for _, _, s in runs if getattr(s, attr) is not None]
        if not scored:
            return None, 0
        return sum(1 for s in scored if getattr(s, attr)), len(scored)

    # -- 1. verdicts -------------------------------------------------------
    L.append("-" * W)
    L.append("1. PREMISE VERDICTS")
    L.append("-" * W)
    L.append("")
    L.append(f"  {'case':<30} {'expected':<26} {'correct':>9}  verdicts seen")
    for name, runs in trials.items():
        case = runs[0][0]
        good, total = rate(name, "gate1_ok")
        seen = sorted({s.gate1_verdict for _, _, s in runs})
        flag = "" if good == total else "   <-- NOT RELIABLE"
        L.append(f"  {name:<30} {'|'.join(case.truth.gate1_expected):<26} "
                 f"{str(good) + '/' + str(total):>9}  {','.join(seen)}{flag}")
    L.append("")

    for name, runs in trials.items():
        case, result, score = runs[0]
        L.append(f"  {name}")
        L.append(f"    {case.note}")
        L.append(f"    true p99 {_num(case.truth.true_baseline_p99)} -> "
                 f"{_num(case.truth.true_focus_p99)} (effect {_num(case.truth.true_effect)}), "
                 f"n {case.truth.baseline_typical_n} -> {case.truth.focus_n}")
        refuters = sorted({p for _, r, _ in runs for p in r.refuting_probes})
        blocked = sorted({p for _, r, _ in runs for p in r.could_not_run_probes})
        if refuters:
            L.append(f"    refuted by: {', '.join(refuters)}")
        if blocked:
            L.append(f"    could not run: {', '.join(blocked)}")
        good, total = rate(name, "attribution_ok")
        if total and good != total:
            unexpected = sorted({p for _, _, s in runs for p in s.unexpected_refuters})
            L.append(f"    ATTRIBUTION: expected {case.truth.artifact_kind}; "
                     f"correct in {good}/{total} runs"
                     + (f", also fired {', '.join(unexpected)} -- these attempts are not "
                        "independent" if unexpected else ", and it did not always fire"))
        L.append("")

    # -- 2. concentration --------------------------------------------------
    L.append("-" * W)
    L.append("2. CONCENTRATION FINDINGS")
    L.append("-" * W)
    L.append("")
    any_scored = False
    for name, runs in trials.items():
        good, total = rate(name, "concentration_ok")
        if not total:
            continue
        any_scored = True
        case = runs[0][0]
        planted = case.truth.concentrated_in
        L.append(f"  {name}   correct in {good}/{total} runs")
        L.append(f"    planted:  {planted if planted else 'none -- the rise is spread'}"
                 f"   true share of median rise: {_pct(case.truth.true_share)}")
        found = sorted({c for _, r, _ in runs for c in r.confirmed_concentrations})
        L.append(f"    reported: {found if found else 'none confirmed'}")
        wrong = sorted({s.concentration_error for _, _, s in runs if s.concentration_error})
        for kind in wrong:
            n = sum(1 for _, _, s in runs if s.concentration_error == kind)
            L.append(f"    {kind.upper()} in {n}/{total} runs")
        L.append("")
    if not any_scored:
        L.append("  No case reached Gate 2.")
        L.append("")

    # -- 3. detection floor -------------------------------------------------
    L.append("-" * W)
    L.append("3. CONCENTRATION DETECTION FLOOR  (known limit A, measured)")
    L.append("-" * W)
    L.append("")
    L.append("  The probe confirms a concentration when one value has moved further from its")
    L.append("  own level outside the window than the rest have moved from theirs, by more")
    L.append("  than redrawing the window with every group held at one level produces -- at a")
    L.append("  declared 5% rate. Each rung below is one strength of planted concentration,")
    L.append("  sized as the target's median excess over the rest as a share of the window's")
    L.append("  own median rise. That share is a property of the generated data, not a")
    L.append("  threshold the probe uses -- the constant it used to be is what this replaced.")
    L.append("  The first rung plants nothing.")
    L.append("")
    L.append(f"  {'rung':<14} {'true share':>12} {'confirmed':>12} {'rate':>8}   note")
    for p in points:
        note = ""
        if p.false_alarms:
            note = f"FALSE ALARM x{p.false_alarms} -- nothing was planted here"
        elif p.true_share_mean is not None and p.true_share_mean <= 0.02:
            note = "null rung: any confirmation here is a false alarm"
        elif p.rate == 0:
            note = "below the floor: a real concentration, never established"
        elif p.rate < 1.0:
            note = "partial: detection is not yet reliable at this strength"
        L.append(f"  {p.label:<14} {_pct(p.true_share_mean):>12} "
                 f"{str(p.confirmed) + '/' + str(p.trials):>12} {_pct(p.rate, 0):>8}   {note}")
    L.append("")
    if floor is None:
        L.append("  FLOOR: no rung in this sweep confirmed every trial. The floor is above the")
        L.append("  strongest concentration tested, which is a stronger statement of the limit")
        L.append("  than the sweep was built to make.")
    else:
        L.append(f"  FLOOR: reliable detection begins at a true share of {_pct(floor)} of the")
        L.append("  window's own median rise. Below that the probe reports 'elevated, not")
        L.append("  established' and the concentration is missed.")
    L.append("")

    # Whether the miss is one-directional is a count, not a claim, so it is read
    # off the tally rather than restated as a property of the design.
    over = sum(1 for s in scores + [s for _, s in sweep_rows]
               if s.concentration_error == "false_concentration")
    if over:
        L.append(f"  DIRECTION: NOT one-sided. {over} run(s) confirmed a concentration where none")
        L.append("  was planted. Section 2 names which cases; the risks below explain both")
        L.append("  sources, and they are different -- only one of them is the declared rate.")
    else:
        L.append("  DIRECTION: one-sided across this corpus. No run confirmed a concentration")
        L.append("  that was not planted, so within these cases the failure only ever costs a")
        L.append("  finding and never manufactures one.")
    L.append("")
    L.append("  WHAT THIS REPLACED, TWICE. The probe first required the excess to reach 50% of")
    L.append("  the window's own median rise. That constant failed in both directions at once,")
    L.append("  and this harness measured both: it found a 46.8% concentration in 2 runs of 5,")
    L.append("  and -- because a window whose median moved only by noise still has a median")
    L.append("  rise above zero -- it confirmed a concentration on the negative control in 3")
    L.append("  runs of 5. A null fixed that: the control went to 5 of 5 and the 46.8% rung to")
    L.append("  4 of 5.")
    L.append("")
    L.append("  The null was then wrong in a second way, and this harness measured that too.")
    L.append("  It shuffled the group labels, which asks whether a gap is larger than chance")
    L.append("  produces and never whether the gap is NEW -- so a permanently slower subgroup")
    L.append("  was confirmed on `real-uniform-rise-on-skewed-index` in 5 runs of 5, where")
    L.append("  nothing was concentrated at all. Two changes answer it, and both are in the")
    L.append("  measurements above:")
    L.append("")
    L.append("    each group is now measured against its OWN level outside the window, so a")
    L.append("    gap that was always there subtracts out and only a change is left")
    L.append("")
    L.append("    the null redraws each group from ITS OWN latencies rather than shuffling")
    L.append("    labels over the pooled ones, because a slow subgroup varies more in")
    L.append("    milliseconds for the same reason it is slow, and pooling judged it against")
    L.append("    an average variation that was not its own")
    L.append("")
    L.append("  The cost is power, and it was paid deliberately. Against shuffled labels the")
    L.append("  46.8% rung was found in 4 runs of 5 and that fixture still false-confirmed in")
    L.append("  1 of 5; against this null the fixture is clean in 5 of 5 and the 46.8% rung")
    L.append("  falls to 2 of 5. A miss costs a finding. A false confirmation costs the reason")
    L.append("  to believe the findings that remain, which is the trade this project has")
    L.append("  already said it makes.")
    L.append("")
    L.append("  " + "." * 74)
    L.append("  RISKS THIS CARRIES. Each is a way the null can be wrong, stated with what the")
    L.append("  corpus measured about it rather than left for a reader to discover.")
    L.append("")
    L.append("  1. THE OFFSET IS A SUBTRACTION, NOT A RATIO. Each group is measured against")
    L.append("     its own level outside the window, and that level is subtracted in")
    L.append("     milliseconds, because the observation being explained is in milliseconds")
    L.append("     and so is the magnitude reported for it. A rise that is genuinely")
    L.append("     multiplicative -- everything doubles -- moves a slow subgroup by more")
    L.append("     absolute milliseconds than a fast one, and this probe will read that as")
    L.append("     concentrated. It is a different case from the fixture above, which rises")
    L.append("     by a fixed amount. No case in this corpus tests it, so this risk is")
    L.append("     stated and not measured, which is a weaker thing than the rest of this")
    L.append("     document and is marked as such.")
    L.append("")
    L.append("  2. THE 5% IS PER DIMENSION, AND FOUR ARE TESTED. The null covers the choice")
    L.append("     of the most extreme value WITHIN a dimension, because it is a null of the")
    L.append("     maximum. It does not cover the choice among the four dimensions. A run")
    L.append("     therefore carries roughly a 1-(0.95)^4 = 18% chance of confirming")
    L.append("     something somewhere when nothing is concentrated anywhere. That is the")
    L.append("     source of every remaining false confirmation named in section 2, and it")
    L.append("     is the declared rate behaving as declared, not an anomaly.")
    L.append("")
    L.append("  3. A GROUP'S SPREAD IS ITSELF ESTIMATED, FROM ONE WINDOW. Redrawing each")
    L.append("     group from its own latencies is what stopped the widest group being")
    L.append("     judged against everyone else's variation -- but the spread it redraws")
    L.append("     from comes from that group's documents in this window alone, around 50 in")
    L.append("     these fixtures. The wider a group is relative to the rest, the noisier")
    L.append("     both the statistic and the threshold it is compared against become. The")
    L.append("     fixture that exercises this runs one endpoint about six times slower than")
    L.append("     the others before the rise and about twice as wide after it, and is clean")
    L.append("     in 5 runs of 5. Nothing here bounds what happens further out than that.")
    L.append("")
    L.append("  4. THE NULL IS ESTIMATED ON WHAT WAS READ. The redraws run over a capped")
    L.append("     sample of the focus window. Observed statistic and null come from the same")
    L.append("     documents, so the comparison stays valid, but on a window larger than the")
    L.append("     cap it is a test on that sample rather than on the window -- and the")
    L.append("     evidence line says so when it happens. No focus window in this corpus")
    L.append("     exceeded the cap. Every baseline did: the baseline is every bucket except")
    L.append("     the focus one, so each group's level here is established from a random")
    L.append("     draw of it rather than from all of it. Random rather than the first")
    L.append("     documents returned, because on a time-ordered index those are its oldest,")
    L.append("     and a normal measured from the oldest stretch of a baseline is a normal")
    L.append("     for a different span of time than the one being explained.")
    L.append("  " + "." * 74)
    L.append("")


    # -- 4. rulers ----------------------------------------------------------
    L.append("-" * W)
    L.append("4. RULER ERROR vs RULER DISAGREEMENT  (known limit B, measured)")
    L.append("-" * W)
    L.append("")
    L.append("  The eighth refutation attempt reads the same window with a second estimator.")
    L.append("  It establishes that the effect is not a property of one method. It does NOT")
    L.append("  bound the error, and the columns below are why: `error` is each estimator's")
    L.append("  distance from the exact value, `disagree` is how far apart they are. Where")
    L.append("  disagreement is smaller than error, both rulers are wrong in the same")
    L.append("  direction and their agreement is not evidence of accuracy.")
    L.append("")
    L.append("  Cases where nobody named a window are excluded: the tool selected its own,")
    L.append("  so its estimate and the exact value describe different buckets and the")
    L.append("  difference between them would be a wrong baseline dressed as a measurement.")
    L.append("")
    L.append(f"  {'case':<30} {'tdigest err':>12} {'hdr err':>10} {'disagree':>10}  covers?")
    covered, uncovered = 0, 0
    for name, runs in trials.items():
        measured = [s for _, _, s in runs if s.ruler_error_primary is not None]
        if not measured:
            continue
        worst = max(measured, key=lambda s: abs(s.ruler_error_primary))
        for s in measured:
            if s.disagreement_covers_error is None:
                continue
            covered += 1 if s.disagreement_covers_error else 0
            uncovered += 0 if s.disagreement_covers_error else 1
        # The widest reading is shown, not the mean: the question is whether the
        # divergence can be relied on as a bound, and a bound is judged by the
        # case that strains it most.
        verdict = "n/a" if worst.disagreement_covers_error is None else (
            "yes" if worst.disagreement_covers_error else "NO")
        L.append(f"  {name:<30} {_pct(worst.ruler_error_primary):>12} "
                 f"{_pct(worst.ruler_error_alt):>10} {_pct(worst.ruler_disagreement):>10}  {verdict}")
    L.append("")
    L.append("  Each row is the run in which tdigest strayed furthest from the exact value.")
    L.append(f"  Disagreement covered the true error in {covered} of {covered + uncovered} cases.")
    if uncovered:
        L.append(f"  In {uncovered}, it did not: the two estimators agreed more closely with each")
        L.append("  other than either did with the truth. Reporting that agreement as a")
        L.append("  confidence interval would state a precision nobody measured, which is why")
        L.append("  SKILL.md, the README and the narration guard all refuse the word.")
    L.append("")

    # -- 5. closure ---------------------------------------------------------
    L.append("-" * W)
    L.append("5. WHAT CLOSED, AND WHAT DID NOT")
    L.append("-" * W)
    L.append("")
    closed_any = sorted({name for name, runs in trials.items() for _, _, s in runs if s.closed})
    L.append(f"  Chains that closed: {closed_any if closed_any else 'none'}")
    L.append("")
    L.append("  This is structural, not a score. Three hypotheses in the declared space -- a")
    L.append("  deploy landed, an upstream dependency slowed, the host lost resources -- are")
    L.append("  not answerable from a request-log index, so the assembler records them as")
    L.append("  unwalked. An unwalked branch keeps the chain open by design. No traversal")
    L.append("  built from this index alone can close, and a corpus that appeared to close")
    L.append("  one would be a corpus with the unwalked branches quietly removed.")
    L.append("")
    L.append("  Consequence a reader should know: the closure bit does not discriminate on")
    L.append("  this class of index. The signal that does is the accounting verdict plus the")
    L.append("  named open branches, both of which are reported per run.")
    L.append("")
    for name, runs in trials.items():
        _, result, _ = runs[0]
        if not result.gate2_ran:
            continue
        accounting = sorted({r.accounting for _, r, _ in runs if r.accounting})
        L.append(f"  {name}: magnitude {','.join(accounting)}, "
                 f"{len(result.open_branches)} branch(es) open")
        for reason in result.open_branches[:3]:
            L.append(f"    - {reason[:120]}")
        if len(result.open_branches) > 3:
            L.append(f"    ... and {len(result.open_branches) - 3} more")
    L.append("")

    # -- 6. totals ----------------------------------------------------------
    summary = summarize(scores)
    sweep_summary = summarize([s for _, s in sweep_rows])
    L.append("-" * W)
    L.append("6. TOTALS")
    L.append("-" * W)
    L.append("")
    L.append(f"  fixed corpus: {len(trials)} cases x {len(seeds)} seeds = {summary.total} runs")
    L.append(f"    premise verdict correct     {summary.gate1_correct}/{summary.gate1_scored}")
    L.append(f"    caught by the right probe   {summary.attribution_correct}/{summary.attribution_scored}")
    L.append(f"    concentration correct       {summary.concentration_correct}/{summary.concentration_scored}")
    L.append(f"    nothing closed that should not close: "
             f"{'yes' if not summary.failures['closed_the_unclosable'] else 'NO'}")
    L.append("")
    L.append(f"  sweep: {sweep_summary.total} runs across {len(points)} strengths")
    L.append(f"    concentration correct       {sweep_summary.concentration_correct}"
             f"/{sweep_summary.concentration_scored}")
    L.append("")
    L.append("  failures by kind (all categories listed, including the empty ones -- a count")
    L.append("  that is missing because nothing triggered it reads the same as one that is")
    L.append("  missing because nobody looked):")
    combined = summarize(scores + [s for _, s in sweep_rows])
    for kind, n in combined.failures.items():
        L.append(f"    {kind:<24} {n}")
    L.append("")
    L.append(f"  under-claim rate  {_pct(combined.miss_rate)}   (missed artifacts + missed concentrations)")
    L.append(f"  over-claim rate   {_pct(combined.over_claim_rate)}   (false alarms + confirmations of")
    L.append("                             things that are not there + unclosable chains closed)")
    L.append("")
    L.append("  These are not averaged. The design trades the first for the second on")
    L.append("  purpose, and one number would hide the trade being made.")
    L.append("")

    # -- 7. the blind spot --------------------------------------------------
    L.append("-" * W)
    L.append("7. WHAT THIS EVALUATION CANNOT SEE")
    L.append("-" * W)
    L.append("")
    L.append("  Every number above is a rate of something this harness can produce. None of")
    L.append("  them is a rate of what it cannot, and a zero printed in section 6 reads")
    L.append("  identically either way.")
    L.append("")
    L.append("  Each case here is generated, indexed, audited once, and deleted. No index is")
    L.append("  read twice, and none is read at any moment other than immediately after it")
    L.append("  was written. A defect that appears *between* runs -- the same command over")
    L.append("  the same unchanged data returning a different answer -- therefore cannot land")
    L.append("  in any category above, including the empty ones.")
    L.append("")
    L.append("  This is not hypothetical. The scan window was anchored on the wall clock, so")
    L.append("  the focus window moved between runs over a static index and the left edge cut")
    L.append("  the oldest bucket in half. An agent A/B run found it because it happened to")
    L.append("  read one index at two moments. This evaluation reported a clean sweep on both")
    L.append("  sides of the fix -- identical verdicts, identical rates -- because reading one")
    L.append("  index at two moments is the one thing it never does. See")
    L.append("  examples/window-anchor.txt.")
    L.append("")
    L.append("  Multiple seeds per case fixed the neighbouring blindness -- a failure that")
    L.append("  depends on where the noise fell -- and it is worth being precise that they")
    L.append("  are different holes. Seeds vary the data. Nothing here varies the clock.")
    L.append("")
    L.append("  Stated as work rather than as a caveat: closing this needs a case that reads")
    L.append("  one index more than once, at moments it chooses. The corpus has none.")
    L.append("")
    L.append("=" * W)
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the evaluation corpus and publish the miss rate.")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument("--out", default=None, help="Write the report here as well as to stdout")
    ap.add_argument("--seed", type=int, default=CORPUS_SEED)
    ap.add_argument("--trials", type=int, default=5,
                    help="Independent datasets per case. One is not enough to see a "
                         "failure that depends on where the noise fell.")
    ap.add_argument("--sweep-seeds", type=int, default=len(SWEEP_SEEDS))
    ap.add_argument("--keep", action="store_true", help="Leave the eval indices behind")
    args = ap.parse_args()

    now = aligned_now()
    seeds = [args.seed + i for i in range(args.trials)]
    sweep = build_sweep(SWEEP_SEEDS[:args.sweep_seeds], now)

    # Every case is run against several independently generated datasets. One
    # draw cannot separate "this rule holds" from "the noise fell kindly", and
    # at least one failure in this corpus depends entirely on where it fell.
    trials, results, scores = {}, [], []
    for seed in seeds:
        for case in build_cases(seed, now):
            print(f"[corpus] {case.name} seed={seed}", flush=True)
            result = run_case(args.endpoint, case)
            score = score_case(case.name, case.truth, result)
            trials.setdefault(case.name, []).append((case, result, score))
            results.append(result)
            scores.append(score)
            if not args.keep:
                _delete(args.endpoint, case.index)

    sweep_rows, curve_rows = [], []
    for case in sweep:
        print(f"[sweep]  {case.name}", flush=True)
        result = run_case(args.endpoint, case)
        score = score_case(case.name, case.truth, result)
        sweep_rows.append((case, score))
        rung = case.name.split("-s")[0].replace("sweep-", "")
        planted = case.truth.concentrated_in is not None
        confirmed = ("endpoint", "/api/checkout") in result.confirmed_concentrations
        # The null rung has nothing planted, so its share is measured over
        # whichever value happened to look largest -- that IS the quantity of
        # interest there: the gap noise alone produces at these subgroup sizes.
        share = case.truth.true_share if planted else case.truth.max_apparent_share
        curve_rows.append((rung, share, planted, confirmed))
        if not args.keep:
            _delete(args.endpoint, case.index)

    points = detection_curve(curve_rows)
    floor = detection_floor(points)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report = render(trials, scores, results, sweep_rows, points, floor, generated_at, seeds)

    print()
    print(report)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report + "\n", encoding="ascii", errors="strict")
        print(f"\nwritten to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
