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

ASCII output only. The verdict words are colored where the terminal accepts
ANSI sequences (escape codes are themselves ASCII); anywhere it does not,
the output falls back to plain text unchanged. --color forces it on.
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


def _vt_enabled():
    """Can this stdout take ANSI sequences?

    Windows Terminal and modern shells already accept them; the legacy console
    needs ENABLE_VIRTUAL_TERMINAL_PROCESSING flipped once. If that cannot be
    done, the answer is no and everything prints plain -- a take with no color
    beats a take full of raw escape codes.
    """
    if not sys.stdout.isatty():
        return False
    if sys.platform != "win32":
        return True
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.GetStdHandle(-11)
    mode = ctypes.c_ulong()
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return False
    return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))


COLOR = False  # decided in main(), after --color is parsed

DIM = "90"     # frame lines: recede, so the words carry the screen
CMD = "96"     # commands: tell "what was typed" from "what came back"
HEAD = "1;96"  # the title and the ACT headings

#: The sequence the video exists to show. Red for a premise that failed the
#: audit, yellow for the two open states, green for the one earned pass.
VERDICTS = {
    "ARTIFACT": "1;91",
    "UNDECIDABLE": "1;93",
    "NOT CLOSED": "1;93",
    "SUBSTANTIATED": "1;92",
}


def _c(code, text):
    return f"\x1b[{code}m{text}\x1b[0m" if COLOR else text


def _verdicts(line):
    """Color the verdict words wherever they appear -- banner or tool output.

    The tool's own reports stay untouched on disk and in the PR; this recolors
    the demo's forwarding of them, nothing upstream.
    """
    for word, code in VERDICTS.items():
        if word in line:
            line = line.replace(word, _c(code, word))
    return line


def banner(text, top=False):
    print()
    print(_c(DIM, "=" * WIDTH))
    head = top
    for para in text.split("\n"):
        for line in textwrap.wrap(para, WIDTH - 4) or [""]:
            if head:
                line, head = _c(HEAD, line), False
            elif line.startswith("ACT "):
                line = _c(HEAD, line)
            else:
                line = _verdicts(line)
            print("  " + line)
    print(_c(DIM, "=" * WIDTH))
    if PAUSE is None:
        input()
    else:
        time.sleep(PAUSE if not top else max(PAUSE, 3))


def run(args, quiet=False):
    cmd = ["uv", "run", "python"] + args
    if not quiet:
        print()
        print(_c(CMD, "$ " + "python " + " ".join(args)))
        print()
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, shell=True)
    if not quiet:
        for line in (proc.stdout or "").splitlines():
            print(_verdicts(line))
        if proc.stderr:
            print(proc.stderr, end="")
    if proc.returncode != 0:
        print(_c("1;91", f"COMMAND FAILED ({proc.returncode}) -- aborting the take."))
        if quiet:
            print(proc.stdout or "", proc.stderr or "")
        sys.exit(1)
    return proc.stdout if quiet else None


def excerpt(path, start_marker, end_marker, label):
    text = (ROOT / path).read_text(encoding="utf-8")
    i = text.index(start_marker)
    j = text.index(end_marker, i)
    print()
    print(_c(CMD, f"$ (from {path})"))
    print()
    for line in text[i:j].rstrip().splitlines():
        print(_verdicts(line))


def main() -> int:
    global PAUSE, COLOR
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--auto", type=float, default=None, metavar="SECONDS",
                    help="advance automatically; omit to advance with Enter")
    ap.add_argument("--endpoint", default="http://127.0.0.1:9250")
    ap.add_argument("--color", action="store_true",
                    help="force ANSI color even when stdout looks like a pipe")
    args = ap.parse_args()
    PAUSE = args.auto
    COLOR = True if args.color else _vt_enabled()
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
    print(_c(CMD, "$ (from examples/mutation-verification.txt)"))
    print()
    tail = (ROOT / "examples/mutation-verification.txt").read_text(encoding="utf-8").strip().splitlines()
    print("\n".join(tail[-2:]))
    # The banner quotes the capture just printed, never a number of its own:
    # a hardcoded count here already went stale once (65, while the file said
    # 69) -- narration contradicting its evidence line, in the demo of a tool
    # whose whole point is refusing exactly that.
    counts = re.search(r"applied: (\d+)\s+survived \(undetected\): (\d+)", "\n".join(tail[-2:]))
    broken = (f"{counts.group(1)} load-bearing rules were broken on purpose; "
              f"{counts.group(2)} survived the suite." if counts else
              "Every load-bearing rule was broken on purpose; the suite caught each one.")
    banner("Misses and false alarms are never averaged into one number.\n"
           + broken + "\n"
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
