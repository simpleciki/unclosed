#!/usr/bin/env python3
"""Regenerate examples/gates-on-real-data.txt against the current code.

A capture that no longer matches what the tool prints misrepresents the tool,
which is the failure this project exists to argue against. So the capture is
regenerated rather than annotated, and the script that produces it lives here
rather than in a scratch directory -- a reader who cannot rerun it is being
asked to take the output on trust.

Requires a cluster at --endpoint. Everything in tests/ runs without one.

ASCII output only.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = "skills/opensearch-skills/observability/unclosed/scripts"
CAPTURE = ROOT / "examples" / "gates-on-real-data.txt"


def run(*args):
    proc = subprocess.run(["uv", "run", "python", *args], cwd=ROOT,
                          capture_output=True, text=True, shell=True)
    if proc.returncode != 0:
        print(f"FAILED: {' '.join(args)}\n{proc.stdout}\n{proc.stderr}")
        sys.exit(1)
    return proc.stdout.replace("\r\n", "\n").rstrip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--endpoint", default="http://127.0.0.1:9250")
    ap.add_argument("--captured-on", required=True,
                    help="Local date to stamp the header with, e.g. 2026-08-03. Passed in "
                         "rather than read from the clock: the runs below are timestamped in "
                         "UTC, and a header that silently mixes the two is the defect this "
                         "project spent a day removing from the window anchor.")
    args = ap.parse_args()

    endpoint = ["--endpoint", args.endpoint]
    for scenario in ("fake-spike", "real-spike", "baseline"):
        run("scripts/seed_logs.py", "--scenario", scenario, "--recreate", *endpoint)

    # Discover the window the tool selects for the real spike, so the fourth run
    # can hand it back as an external report. Discovery only -- not in the file.
    probe = run(f"{SKILL}/audit_window.py", "--index", "unclosed-real-spike", *endpoint)
    match = re.search(r"FOCUS WINDOW: (\S+) \(\+(\d+)m\)", probe)
    if not match:
        print("could not discover the focus window from:\n" + probe)
        return 1
    focus = match.group(1)
    start = datetime.fromisoformat(focus.replace("Z", "+00:00"))
    end = start + timedelta(minutes=int(match.group(2)))
    reported_at = end.isoformat().replace("+00:00", "Z")
    reported_early = (start + timedelta(minutes=2)).isoformat().replace("+00:00", "Z")

    assemble = [f"{SKILL}/assemble_traversal.py"]
    runs = [
        ("unclosed-fake-spike  (no reported window -- self-selected)",
         [*assemble, "--index", "unclosed-fake-spike", *endpoint]),
        ("unclosed-real-spike  (no reported window -- self-selected)",
         [*assemble, "--index", "unclosed-real-spike", *endpoint]),
        ("unclosed-baseline  (no reported window -- self-selected)",
         [*assemble, "--index", "unclosed-baseline", *endpoint]),
        ("unclosed-real-spike  (external report, window named in advance)",
         [*assemble, "--index", "unclosed-real-spike", "--focus-window", focus,
          "--reported-at", reported_at, *endpoint]),
        ("unclosed-real-spike  (same window, same data, reported 2 minutes in)",
         [*assemble, "--index", "unclosed-real-spike", "--focus-window", focus,
          "--reported-at", reported_early, *endpoint]),
    ]

    out = [
        f"All three gates against a live OpenSearch 2.19.3 -- captured {args.captured_on} local.",
        "Every timestamp in the runs below is UTC.",
        f"Cluster at {args.endpoint}. Fixtures from scripts/seed_logs.py.",
        "",
        "The first three runs give the tool no reported window, so it selects the worst",
        "bucket itself and Gate 1 refuses to substantiate a target it drew around its own",
        "arrows. The fourth supplies an external report and the premise clears.",
        "",
        "The fifth is the fourth with one thing changed: the same window over the same",
        "documents, reported two minutes in instead of at the end. It is an artifact, and",
        "the probe that refutes it also prints what the same statistic read over the",
        "documents an ingest clock places before that moment -- so a reader has the",
        "reporter's number rather than a choice of percentile to compute one from.",
        "",
        "Regenerate this file:",
        f"  uv run python eval/capture_gates.py --captured-on {args.captured_on}",
        "",
        "or run the commands individually:",
        "  python scripts/seed_logs.py --scenario {fake-spike,real-spike,baseline} --recreate",
    ]
    for _, cmd in runs:
        out.append("  python " + " ".join(cmd))
    out += [
        "",
        "The window handed to runs four and five is the one the tool picks for itself in",
        "run two -- named in advance here so the provenance probe has a claim to check",
        "rather than a target this tool drew around its own arrows.",
    ]

    for title, cmd in runs:
        out += ["#" * 64, f"# {title}", "#" * 64, run(*cmd)]

    text = "\n".join(out) + "\n"
    CAPTURE.write_text(text, encoding="ascii", errors="strict")
    print(f"written: {CAPTURE.relative_to(ROOT)} ({len(text.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
