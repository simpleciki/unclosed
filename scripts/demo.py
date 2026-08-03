#!/usr/bin/env python3
"""Drive the demo video, end to end, against a live cluster.

The video is designed to work silent: every banner is on-screen narration, so
recording it is one take of this script with no typing and no voiceover. Each
command is printed before it runs and its output is live -- nothing shown is
pre-rendered.

    uv run python scripts/demo.py              # advance with Enter (rehearsal)
    uv run python scripts/demo.py --auto 6     # advance itself, 6s per banner
                                               # (the recording mode)

Requires the demo cluster:

    docker start unclosed-opensearch           # 127.0.0.1:9250

ASCII output only.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = "skills/opensearch-skills/observability/unclosed/scripts"
WIDTH = 78

PAUSE = None  # None = wait for Enter; number = seconds


def banner(text, top=False):
    print()
    print("=" * WIDTH)
    for para in text.split("\n"):
        for line in textwrap.wrap(para, WIDTH - 4) or [""]:
            print("  " + line)
    print("=" * WIDTH)
    if PAUSE is None:
        input()
    else:
        time.sleep(PAUSE if not top else max(PAUSE, 3))


def run(args, quiet=False):
    cmd = ["uv", "run", "python"] + args
    if not quiet:
        print()
        print("$ " + "python " + " ".join(args))
        print()
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=quiet, text=True, shell=True)
    if proc.returncode != 0:
        print(f"COMMAND FAILED ({proc.returncode}) -- aborting the take.")
        if quiet:
            print(proc.stdout or "", proc.stderr or "")
        sys.exit(1)
    return proc.stdout if quiet else None


def excerpt(path, start_marker, end_marker, label):
    text = (ROOT / path).read_text(encoding="utf-8")
    i = text.index(start_marker)
    j = text.index(end_marker, i)
    print()
    print(f"$ (from {path})")
    print()
    print(text[i:j].rstrip())


def main() -> int:
    global PAUSE
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--auto", type=float, default=None, metavar="SECONDS",
                    help="advance automatically; omit to advance with Enter")
    ap.add_argument("--endpoint", default="http://127.0.0.1:9250")
    args = ap.parse_args()
    PAUSE = args.auto
    ep = ["--endpoint", args.endpoint]

    banner("unclosed\n\n"
           "Log root-cause analysis that audits its own premise\n"
           "and refuses to name a cause it cannot close.\n\n"
           "OpenSearch Agent Skills Hackathon -- everything below runs live.", top=True)

    # ---- ACT 1 -----------------------------------------------------------
    banner("ACT 1 -- Two incidents. The same 5x spike. One is real.\n"
           "The other never happened.")
    run(["scripts/seed_logs.py", "--scenario", "real-spike", "--recreate"] + ep)
    run(["scripts/seed_logs.py", "--scenario", "fake-spike", "--recreate"] + ep)
    banner("Both indices show p99 rising ~5x. A threshold cannot tell them apart,\n"
           "and 'why is it slow?' is already the wrong first question.\n"
           "The first question is whether the spike is real.")

    # Discover the window the tool picks for the real spike (silent), so the
    # later runs can hand it back as an external report -- named in advance.
    probe = run([f"{SKILL}/audit_window.py", "--index", "unclosed-real-spike"] + ep, quiet=True)
    m = re.search(r"FOCUS WINDOW: (\S+) \(\+(\d+)m\)", probe)
    if not m:
        print("could not discover the focus window; aborting.")
        return 1
    focus = m.group(1)
    start = datetime.fromisoformat(focus.replace("Z", "+00:00"))
    end = start + timedelta(minutes=int(m.group(2)))
    reported_at = end.isoformat().replace("+00:00", "Z")
    reported_early = (start + timedelta(minutes=2)).isoformat().replace("+00:00", "Z")

    # ---- ACT 2 -----------------------------------------------------------
    banner("ACT 2 -- Gate 1 does not inspect the observation.\n"
           "It tries to refute it, eight different ways.")
    run([f"{SKILL}/audit_window.py", "--index", "unclosed-fake-spike"] + ep)
    banner("ARTIFACT: the 5x spike is a percentile over almost no data.\n"
           "No investigation should start from this premise --\n"
           "and the report says which story explained it.")
    run([f"{SKILL}/audit_window.py", "--index", "unclosed-real-spike"] + ep)
    banner("The REAL spike does not pass either -- UNDECIDABLE.\n"
           "The tool selected this window itself, and a target drawn around\n"
           "the arrows can never be substantiated. It needs an external report.")
    run([f"{SKILL}/audit_window.py", "--index", "unclosed-real-spike",
         "--focus-window", focus, "--reported-at", reported_at] + ep)
    banner("SUBSTANTIATED: named in advance, judged after the window finished,\n"
           "every refutation ran and every one failed.\n"
           "The record above is the pass -- not the absence of a complaint.")

    # ---- ACT 3 -----------------------------------------------------------
    banner("ACT 3 -- Same window. Same documents. One change:\n"
           "the report was filed two minutes in.")
    run([f"{SKILL}/audit_window.py", "--index", "unclosed-real-spike",
         "--focus-window", focus, "--reported-at", reported_early] + ep)
    banner("ARTIFACT: the reporter judged a window that did not exist yet.\n"
           "The tool also rebuilds the number the reporter saw -- printed in the\n"
           "evidence -- so nobody downstream picks the percentile that suits\n"
           "their story.")

    # ---- ACT 4 -----------------------------------------------------------
    banner("ACT 4 -- The full traversal: three gates, every branch recorded,\n"
           "including the ones nothing here can walk.")
    run([f"{SKILL}/assemble_traversal.py", "--index", "unclosed-real-spike",
         "--focus-window", focus, "--reported-at", reported_at] + ep)
    banner("NOT CLOSED is a valid result, and here it is the correct one.\n"
           "The chain names exactly what is missing -- deploy events, dependency\n"
           "latency, host metrics -- instead of promoting the first correlate\n"
           "in the logs to a cause. unclosed never outputs 'the cause is X'.")

    # ---- ACT 5 -----------------------------------------------------------
    banner("ACT 5 -- The part that usually gets left out:\n"
           "what this skill gets wrong, measured and published.")
    excerpt("examples/miss-rate.txt", "  fixed corpus:", "these are not averaged".capitalize(),
            "miss-rate.txt, section 6")
    print()
    print("$ (from examples/mutation-verification.txt)")
    print()
    tail = (ROOT / "examples/mutation-verification.txt").read_text(encoding="utf-8").strip().splitlines()
    print("\n".join(tail[-2:]))
    banner("Misses and false alarms are never averaged into one number.\n"
           "65 load-bearing rules were broken on purpose; 0 survived the suite.\n"
           "And an A/B against NOT having the skill is published too --\n"
           "including the part where the unaided agent fabricated nothing.")

    # ---- END -------------------------------------------------------------
    banner("unclosed\n\n"
           "github.com/simpleciki/unclosed\n"
           "OpenSearch Agent Skills Hackathon -- submission #92\n\n"
           "A chain that will not close, and a tool that says so.", top=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
