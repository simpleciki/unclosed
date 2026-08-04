#!/usr/bin/env python3
"""Mutation verification: break every load-bearing rule on purpose.

A test that has never failed has not been shown to test anything. Each entry
below removes one rule the design depends on and reruns the WHOLE suite. A rule
whose removal leaves the suite green is not being tested by it -- it is
decoration, and the coverage number that includes it is misleading.

Two properties are deliberate and neither is free:

  * Every mutation runs the entire suite, never a hand-picked test. Pointing a
    mutation at one chosen test lets the author decide what counts as caught.

  * A mutation whose anchor text is not found is an ERROR, not a skip. A patch
    that anchors to text which is not there applies nothing and reports success.
    That failure mode has already bitten this project once.

Source files are restored after each mutation whether or not the suite passed,
and the suite is rerun at the end to prove the restore worked.

Usage:
    uv run python eval/mutations.py              # every group, writes the capture
    uv run python eval/mutations.py --list       # names only, runs nothing
    uv run python eval/mutations.py --group gate1

ASCII output only -- this is read in a cp1252 terminal.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "opensearch-skills" / "observability" / "unclosed" / "scripts"

PREMISE = SCRIPTS / "premise_audit.py"
TRAVERSAL = SCRIPTS / "traversal.py"
CLOSURE = SCRIPTS / "closure_audit.py"
ASSEMBLE = SCRIPTS / "assemble_traversal.py"
AUDIT_WINDOW = SCRIPTS / "audit_window.py"
NULL = SCRIPTS / "concentration_null.py"

CAPTURE = ROOT / "examples" / "mutation-verification.txt"


# (group, label, target file, anchor, replacement)
MUTATIONS = [
    # -- Gate 1: the premise audit ----------------------------------------
    ("gate1", "decide(): let an incomplete sweep grant a pass", PREMISE,
     "    if all(p.outcome is Outcome.NOT_REFUTED for p in probes):\n"
     "        return Verdict.SUBSTANTIATED",
     "    if True:\n"
     "        return Verdict.SUBSTANTIATED"),
    ("gate1", "population_shift: remove the small-sample independence guard", PREMISE,
     "    if obs.focus_count is not None and obs.focus_count < MIN_COMPOSITION_N:",
     "    if False:"),
    ("gate1", "ProbeResult: allow COULD_NOT_RUN without naming what is missing", PREMISE,
     "        if self.outcome is Outcome.COULD_NOT_RUN and not self.missing:",
     "        if False:"),
    ("gate1", "unanchored_report: treat a report naming no moment as answerable", PREMISE,
     '    if start is None or end is None:\n'
     '        return ProbeResult(\n'
     '            "unanchored_report", story, Outcome.REFUTED,',
     '    if False:\n'
     '        return ProbeResult(\n'
     '            "unanchored_report", story, Outcome.REFUTED,'),
    ("gate1", "window_provenance: let a self-selected window earn a pass", PREMISE,
     "    if obs.provenance is Provenance.SELF_SELECTED:",
     "    if False:"),
    ("gate1", "observation_moment: stop checking whether the window had finished", PREMISE,
     "    if fraction < MIN_WINDOW_ELAPSED:",
     "    if False:"),

    # -- Gate 1, eighth probe: the ruler -----------------------------------
    ("gates", "Gate 1: drop the estimator probe out of the sweep entirely", PREMISE,
     "    probe_estimator_choice,", "    # probe_estimator_choice,"),
    ("gates", "Gate 1: stop refuting an effect only one estimator can see", PREMISE,
     "    if ratio < ESTIMATOR_AGREEMENT:", "    if False:"),

    # -- Gate 2 -------------------------------------------------------------
    ("gates", "Gate 2 OPEN: let an unruled-out alternative stop blocking closure", TRAVERSAL,
     "OPEN = frozenset({NodeState.INCONCLUSIVE, NodeState.PENDING, NodeState.NOT_VISITED})",
     "OPEN = frozenset({NodeState.PENDING, NodeState.NOT_VISITED})"),
    ("gates", "Gate 2 closed_chains(): block only on movable branches, not on live alternatives", TRAVERSAL,
     "            if any(c.state in OPEN for c in node.children):",
     "            if any(c.state in MOVABLE for c in node.children):"),
    ("gates", "Gate 2 closed_chains(): stop treating an all-ruled-out node as the end of a chain", TRAVERSAL,
     "            onward = [c for c in node.children if c.state is NodeState.CONFIRMED]",
     "            onward = list(node.children)"),
    ("gates", "Gate 2 Node: allow PENDING without naming what it awaits", TRAVERSAL,
     "            if not self.awaiting:", "            if False:"),
    ("gates", "Gate 2 Node: allow PENDING without recording when its state was read", TRAVERSAL,
     "            if not self.observed_at:", "            if False:"),
    ("gates", "Gate 2 Gate1Carryover: stop carrying an unverified premise into the tree", TRAVERSAL,
     "        return bool(self.missing_inputs)", "        return False"),
    ("gates", "Gate 2 Node: let an unconfirmed branch contribute to the magnitude arithmetic", TRAVERSAL,
     "        if self.magnitude_accounted is not None and self.state is not NodeState.CONFIRMED:",
     "        if False:"),
    ("gates", "Gate 2 Node: let a branch nobody walked carry evidence anyway", TRAVERSAL,
     "        if self.state in MOVABLE and (self.probe or self.evidence):", "        if False:"),

    # -- Gate 3 -------------------------------------------------------------
    ("gates", "Gate 3 Node: allow the same effect to be counted at two depths", TRAVERSAL,
     "            if deeper:", "            if False:"),
    ("gates", "Gate 3: widen the residual tolerance until anything passes", CLOSURE,
     "RESIDUAL_TOLERANCE = 0.20", "RESIDUAL_TOLERANCE = 1.00"),
    ("gates", "Gate 3: stop rejecting explanations that overshoot the effect", CLOSURE,
     "    elif frac < -tolerance:", "    elif False:"),
    ("gates", "Gate 3: treat an unmeasured confirmed branch as if it explained nothing", CLOSURE,
     "    if unquantified or not contributions:", "    if not contributions:"),
    ("gates", "Gate 3 closes(): ignore the magnitude verdict", CLOSURE,
     "    if magnitude.verdict is not Accounting.ACCOUNTED:", "    if False:"),
    ("gates", "Gate 3 Node: let a branch that restates the observation account for it", TRAVERSAL,
     "        if self.magnitude_accounted is not None and not self.explanatory:", "        if False:"),
    ("gates", "Gate 3: count a descriptive branch as a missing measurement", CLOSURE,
     "                          if n.magnitude_accounted is None and n.is_leaf and n.explanatory)",
     "                          if n.magnitude_accounted is None and n.is_leaf)"),
    ("gates", "Gate 3 closes(): ignore everything Gate 2 left open", CLOSURE,
     "    reasons = list(traversal.unclosed_reasons())", "    reasons = []"),
    ("gates", "Gate 3: claim a residual finer than the ruler that measured it", CLOSURE,
     "    if measurement_uncertainty is not None and measurement_uncertainty > RESIDUAL_TOLERANCE:",
     "    if False:"),

    # -- The scan anchor: which clock chose the window ----------------------
    ("scan-anchor", "anchor back on the wall clock", AUDIT_WINDOW,
     "        anchor = index_max_timestamp(endpoint, index, time_field)",
     "        anchor = datetime.now(timezone.utc)"),
    ("scan-anchor", "--as-of silently ignored", AUDIT_WINDOW,
     "    if as_of:\n        anchor = _parse_iso(as_of)",
     "    if False:\n        anchor = _parse_iso(as_of)"),
    ("scan-anchor", "right edge left unaligned (bucket containing the newest doc gets cut)", AUDIT_WINDOW,
     "    lt = _ceil_to_bucket(anchor, bucket_minutes)", "    lt = anchor"),
    ("scan-anchor", "right edge floored instead of ceiled (newest bucket dropped)", AUDIT_WINDOW,
     "    lt = _ceil_to_bucket(anchor, bucket_minutes)",
     "    lt = _floor_to_bucket(anchor, bucket_minutes)"),
    ("scan-anchor", "left edge left unaligned (oldest bucket sliced -- the original defect)", AUDIT_WINDOW,
     "    gte = _floor_to_bucket(lt - timedelta(hours=lookback_hours), bucket_minutes)",
     "    gte = lt - timedelta(hours=lookback_hours) - timedelta(seconds=137)"),
    ("scan-anchor", "empty index falls back to now instead of refusing", AUDIT_WINDOW,
     '            raise SystemExit(f"{index} holds no `{time_field}` values, so there is no window to scan.")',
     "            anchor = datetime.now(timezone.utc)"),
    ("scan-anchor", "bucket_stats queries something other than the resolved edges", AUDIT_WINDOW,
     '        "query": _range(time_field, _iso(gte), _iso(lt)),',
     '        "query": _range(time_field, _iso(gte - timedelta(minutes=3)), _iso(lt)),'),
    ("scan-anchor", "the clock is resolved but never printed", PREMISE,
     '        if self.scan_summary:\n            lines.append(f"SCAN: {self.scan_summary}")',
     '        if False:\n            lines.append(f"SCAN: {self.scan_summary}")'),
    ("scan-anchor", "the report names a window but not the clock that chose it", PREMISE,
     '    anchor = f" anchored on {obs.scan_anchor_source}" if obs.scan_anchor_source else ""',
     '    anchor = ""'),

    # -- The reporter's reading, reconstructed rather than picked -----------
    ("as-reported", "the ingest-clock filter is dropped (reads the settled window instead)", AUDIT_WINDOW,
     '            {"range": {second_clock: {"lt": reported_at}}},\n', ""),
    ("as-reported", "reconstructs a different statistic than the claim is about", AUDIT_WINDOW,
     '\n        "aggs": _percentile_aggs(metric),\n    }\n    res = _get(endpoint, f"/{index}/_search", body)',
     '\n        "aggs": _percentile_aggs(metric, percents=(50,)),\n    }\n'
     '    res = _get(endpoint, f"/{index}/_search", body)'),
    ("as-reported", "an index with one clock silently reconstructs anyway", AUDIT_WINDOW,
     "    if not second_clock or not reported_at:\n        return None, None",
     "    if not reported_at:\n        return None, None\n    second_clock = second_clock or '@timestamp'"),
    ("as-reported", "a matching reconstruction clears the early report (the escape hatch)", PREMISE,
     '    if fraction < MIN_WINDOW_ELAPSED:\n'
     '        return ProbeResult("observation_moment", story, Outcome.REFUTED,',
     '    if fraction < MIN_WINDOW_ELAPSED and obs.focus_value_as_reported != obs.focus_value:\n'
     '        return ProbeResult("observation_moment", story, Outcome.REFUTED,'),
    ("as-reported", "the reconstruction is called what the reporter saw", PREMISE,
     '    return ev + (". Ingest order is the closest the index can come to what the reporter queried; "\n'
     '                 "it is not that screen")',
     '    return ev + ". This is what the reporter saw"'),
    ("as-reported", "an unreconstructable gap is described as small rather than unquantified", PREMISE,
     '        return (". The reading the reporter had cannot be reconstructed: that needs an ingest "\n'
     '                "clock to say which documents existed at the report time, and this index has none. "\n'
     '                "How far the two differ is unquantified -- which is not the same as small")',
     '        return ". The two readings are close enough not to matter here"'),
    ("as-reported", "the reconstruction is computed but never printed", PREMISE,
     '                                "and the data audited here is not the data that triggered the report"\n'
     '                           + _reconstruction(obs))',
     '                                "and the data audited here is not the data that triggered the report")'),
    ("as-reported", "no documents ingested by then is read as a zero rather than named", PREMISE,
     '        if obs.focus_count_as_reported == 0:\n'
     '            return (". No document had been ingested by then, so the reported number cannot be "\n'
     '                    "reconstructed at all")',
     '        if obs.focus_count_as_reported == 0:\n            return ". Reconstructed reading: 0"'),

    # -- The concentration null --------------------------------------------
    ("concentration", "stop measuring each group against its own normal (keep the bare gap)", NULL,
     "        kept = [(v - offsets[g], g) for v, g in zip(values, labels) if g in offsets]",
     "        kept = [(v, g) for v, g in zip(values, labels) if g in offsets]"),
    ("concentration", "build the null by shuffling labels instead of redrawing each group", NULL,
     """    grouped = _by_label(values, labels)
    level = statistics.median(values)
    pools = {group: [v - statistics.median(vs) + level for v in vs]
             for group, vs in grouped.items()}

    rng = random.Random(seed)
    null = []
    for _ in range(trials):
        drawn_values, drawn_labels = [], []
        for group, pool in pools.items():
            drawn_values.extend(rng.choices(pool, k=len(pool)))
            drawn_labels.extend([group] * len(pool))
        drawn = _excesses(drawn_values, drawn_labels, eligible)
        if drawn is not None:
            null.append(drawn[1])
    return null""",
     """    rng = random.Random(seed)
    shuffled = list(labels)
    null = []
    for _ in range(trials):
        rng.shuffle(shuffled)
        drawn = _excesses(values, shuffled, eligible)
        if drawn is not None:
            null.append(drawn[1])
    return null"""),
    ("concentration", "accept a baseline too thin to establish a normal, and answer the weaker question", NULL,
     "    if offered and not referenced:", "    if False:"),

    # -- IMMATERIAL: the state that could become an escape hatch ------------
    ("immaterial", "an unmeasured branch becomes immaterial (the escape hatch)", ASSEMBLE,
     "    if excess is None or not observed_effect:\n        return None",
     "    if excess is None:\n        return True\n    if not observed_effect:\n        return None"),
    ("immaterial", "a zero effect divides instead of declining", ASSEMBLE,
     "    if excess is None or not observed_effect:\n        return None",
     "    if excess is None:\n        return None"),
    ("immaterial", "the bar becomes absolute instead of a share of the effect", ASSEMBLE,
     "    return excess < tolerance * abs(observed_effect)", "    return excess < 100.0"),
    ("immaterial", "at the floor counts as immaterial", ASSEMBLE,
     "    return excess < tolerance * abs(observed_effect)",
     "    return excess <= tolerance * abs(observed_effect)"),
    ("immaterial", "a widened tolerance is ignored", ASSEMBLE,
     "    return excess < tolerance * abs(observed_effect)",
     "    return excess < RESIDUAL_TOLERANCE * abs(observed_effect)"),
    ("immaterial", "a negative effect is read as having no size", ASSEMBLE,
     "    return excess < tolerance * abs(observed_effect)",
     "    return excess < tolerance * observed_effect"),
    ("immaterial", "IMMATERIAL slips into the open set and blocks chains again", TRAVERSAL,
     "OPEN = frozenset({NodeState.INCONCLUSIVE, NodeState.PENDING, NodeState.NOT_VISITED})",
     "OPEN = frozenset({NodeState.INCONCLUSIVE, NodeState.PENDING, NodeState.NOT_VISITED,\n"
     "                 NodeState.IMMATERIAL})"),
    ("immaterial", "IMMATERIAL stops being disposed", TRAVERSAL,
     "DISPOSED = frozenset({NodeState.CONFIRMED, NodeState.RULED_OUT, NodeState.IMMATERIAL})",
     "DISPOSED = frozenset({NodeState.CONFIRMED, NodeState.RULED_OUT})"),
    ("immaterial", "IMMATERIAL no longer has to record what it measured", TRAVERSAL,
     "_REQUIRES_EVIDENCE = frozenset({NodeState.CONFIRMED, NodeState.RULED_OUT,\n"
     "                                NodeState.INCONCLUSIVE, NodeState.IMMATERIAL})",
     "_REQUIRES_EVIDENCE = frozenset({NodeState.CONFIRMED, NodeState.RULED_OUT,\n"
     "                                NodeState.INCONCLUSIVE})"),
    ("immaterial", "an unmeasurable dimension is called immaterial rather than left open", ASSEMBLE,
     '            statement, NodeState.INCONCLUSIVE, probe=f"grouped the focus window by `{dim}`",',
     '            statement, NodeState.IMMATERIAL, probe=f"grouped the focus window by `{dim}`",'),

    # -- The shape reading and the sampler's truncation flag ----------------
    ("shape", "the signed ratio gets the abs() the review suggested", ASSEMBLE,
     "    share = median_shift / observed_effect if observed_effect else 0.0",
     "    share = abs(median_shift / observed_effect) if observed_effect else 0.0"),
    ("shape", "a median that moved against the tail is called held again", ASSEMBLE,
     "    if share <= -MEDIAN_SHIFT_SHARE:",
     "    if False:"),
    ("sampler", "the truncation flag is wired shut", ASSEMBLE,
     "    return rows, bool(total and total > len(rows))",
     "    return rows, False"),

    # -- The transport, which is the whole of the portability claim ---------
    ("transport", "the password becomes printable again", AUDIT_WINDOW,
     "                f\"password={'***' if self.password else None}, \"",
     '                f"password={self.password!r}, "'),
    ("transport", "half a credential is sent when the password is absent", AUDIT_WINDOW,
     "        if not self.username or self.password is None:\n            return None",
     "        if not self.username:\n            return None"),
    ("transport", "a run that skipped certificate verification stops saying so", AUDIT_WINDOW,
     '            return f"{self.url}, TLS with VERIFICATION DISABLED, {auth}"',
     '            return f"{self.url}, TLS, {auth}"'),
    ("transport", "verification is silently skipped on every TLS connection", AUDIT_WINDOW,
     "        return ssl.create_default_context(cafile=self.ca_cert)",
     "        ctx = ssl.create_default_context(cafile=self.ca_cert)\n"
     "        ctx.check_hostname = False\n"
     "        ctx.verify_mode = ssl.CERT_NONE\n"
     "        return ctx"),
    ("transport", "a username with no password in the environment is accepted", AUDIT_WINDOW,
     "    if username and password is None:\n        raise SystemExit(",
     "    if False:\n        raise SystemExit("),
    ("transport", "the Authorization header is built and never attached", AUDIT_WINDOW,
     '    auth = ep.auth_header()\n    if auth:\n        req.add_header("Authorization", auth)',
     '    auth = ep.auth_header()\n    if False:\n        req.add_header("Authorization", auth)'),
    ("transport", "the TLS context is computed and never passed to the connection", AUDIT_WINDOW,
     "        with urllib.request.urlopen(req, timeout=30, context=ep.ssl_context()) as resp:",
     "        with urllib.request.urlopen(req, timeout=30) as resp:"),
    ("transport", "a plaintext request to a TLS port goes back to being a traceback", AUDIT_WINDOW,
     "    except http.client.HTTPException as exc:",
     "    except NotImplementedError as exc:"),
    ("transport", "a 401 stops naming the credential it wants", AUDIT_WINDOW,
     "        if exc.code in (401, 403):", "        if False:"),
    ("transport", "the second transport comes back, and it cannot authenticate", ASSEMBLE,
     "    return _request(endpoint, path, body)",
     '    req = urllib.request.Request(\n'
     '        str(endpoint) + path, data=json.dumps(body).encode("utf-8"), method="POST",\n'
     '        headers={"Content-Type": "application/json"})\n'
     "    with urllib.request.urlopen(req, timeout=30) as resp:\n"
     '        return json.loads(resp.read().decode("utf-8"))'),
]

GROUPS = ["gate1", "gates", "scan-anchor", "as-reported", "concentration", "immaterial",
          "shape", "sampler", "transport"]

HEADINGS = {
    "gate1": "Gate 1 -- the premise audit",
    "gates": "Gates 1(ruler), 2 and 3 -- traversal, closure, magnitude",
    "scan-anchor": "The scan anchor -- which clock chose the window",
    "as-reported": "The reporter's reading -- reconstructed rather than picked",
    "concentration": "The concentration null -- what a meaningless label can produce",
    "immaterial": "IMMATERIAL -- the state that must not become an escape hatch",
    "shape": "The shape reading -- a signed ratio, and wording that matches its numbers",
    "sampler": "The sampler -- a truncation flag that must stay live",
    "transport": "The transport -- credentials, TLS, and saying which was used",
}


def run_suite():
    """Run the whole suite. Returns (returncode, pytest's summary line).

    `-q` is deliberately not passed here: pyproject already sets it in addopts,
    and a second one suppresses the summary line entirely -- leaving a capture
    that says a run was green without saying how many tests agreed.
    """
    proc = subprocess.run(
        ["uv", "run", "pytest", "--tb=no", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, shell=True,
    )
    out = (proc.stdout or "").splitlines()
    summary = next((ln.strip() for ln in reversed(out)
                    if "passed" in ln or "failed" in ln or "error" in ln), "no summary line")
    return proc.returncode, summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--group", action="append", choices=GROUPS,
                    help="only this group (repeatable); default is all of them")
    ap.add_argument("--list", action="store_true", help="print the mutations and exit")
    ap.add_argument("--no-capture", action="store_true",
                    help="print the report without writing examples/mutation-verification.txt")
    args = ap.parse_args()

    wanted = args.group or GROUPS
    selected = [m for m in MUTATIONS if m[0] in wanted]

    if args.list:
        for group in wanted:
            rows = [m for m in selected if m[0] == group]
            print(f"\n{group} ({len(rows)})")
            for _, label, target, _, _ in rows:
                print(f"  {label}   [{target.name}]")
        print(f"\ntotal: {len(selected)}")
        return 0

    rc, tail = run_suite()
    lines = [
        "=" * 78,
        "unclosed -- mutation verification",
        "=" * 78,
        "",
        "Each row removes one load-bearing rule and reruns the WHOLE suite. A row",
        "that stays green is a mutation nothing detects: a hole in the tests rather",
        "than a harmless change. A row that ERRORs is a mutation that was never",
        "applied, which is not the same as one that was caught.",
        "",
        f"baseline (unmutated): {'GREEN' if rc == 0 else 'RED'}  {tail}",
        "",
    ]
    if rc != 0:
        lines.append("ABORTED: the suite is not green before mutating, so nothing below means anything.")
        print("\n".join(lines))
        return 1

    survivors, errors = 0, 0
    for group in wanted:
        rows = [m for m in selected if m[0] == group]
        if not rows:
            continue
        lines += [f"-- {HEADINGS[group]} ({len(rows)})", ""]
        for _, label, target, old, new in rows:
            original = target.read_text(encoding="utf-8")
            found = original.count(old)
            if found != 1:
                lines.append(f"  [ERROR   ] {label}")
                lines.append(f"             anchor matched {found} times -- mutation was never applied")
                errors += 1
                continue
            target.write_text(original.replace(old, new, 1), encoding="utf-8")
            try:
                rc, tail = run_suite()
            finally:
                target.write_text(original, encoding="utf-8")
                assert target.read_text(encoding="utf-8") == original, f"restore failed for {target.name}"
            caught = rc != 0
            survivors += 0 if caught else 1
            lines.append(f"  [{'RED     ' if caught else 'SURVIVED'}] {label}")
            lines.append(f"             {tail}")
        lines.append("")

    rc_after, tail_after = run_suite()
    lines += [
        f"mutations applied: {len(selected)}    survived (undetected): {survivors}    "
        f"never applied: {errors}",
        f"restored suite: {'GREEN' if rc_after == 0 else 'RED'}  {tail_after}",
    ]

    report = "\n".join(lines) + "\n"
    print(report)
    if not args.no_capture and not args.group:
        CAPTURE.write_text(report, encoding="ascii")
        print(f"written: {CAPTURE.relative_to(ROOT)}")
    return 0 if survivors == 0 and errors == 0 and rc_after == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
