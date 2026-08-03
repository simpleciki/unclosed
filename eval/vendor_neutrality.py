#!/usr/bin/env python3
"""Run the same audit against two OpenSearch deployment shapes and diff the answers.

"Portable" is a claim, and the cheap version of it -- the endpoint is a flag, no
hostname is hardcoded -- is true of code that still cannot reach any cluster a
person would run in production. Every managed OpenSearch and every default
self-managed install has the security plugin on: it serves HTTPS with a
certificate the system trust store has never seen, and answers 401 without
credentials. A client that speaks only plaintext HTTP works on exactly one
deployment shape, the demo container with security switched off, which is also
the only shape a hackathon submission is ever tested against.

So this compares two shapes:

    plaintext    security plugin disabled, http, no credentials
    secured      security plugin enabled, https, self-signed cert, basic auth

Both are seeded from the same generator with the same seed, and both audits are
pinned to the same `--as-of`, so the two reports should agree line for line. Any
difference is either a portability defect or a fact about the deployment that
the skill failed to record -- and the diff says which lines.

The failure paths are checked too, because "cannot connect" is a thing a user
will hit long before they hit a verdict, and a traceback is a worse answer than
a sentence.

Set the password in the environment:

    export OPENSEARCH_PASSWORD=...
    uv run python eval/vendor_neutrality.py \
        --plain http://127.0.0.1:9250 \
        --secure https://127.0.0.1:9251 --username admin --insecure

ASCII output only.
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "opensearch-skills" / "observability" / "unclosed" / "scripts"
AUDIT = SCRIPTS / "audit_window.py"
SEED = ROOT / "scripts" / "seed_logs.py"
CAPTURE = ROOT / "examples" / "vendor-neutrality.txt"

#: Lines that legitimately differ between two clusters and are not portability
#: defects: the endpoint itself, and the transport description whose whole job
#: is to be different.
EXPECTED_TO_DIFFER = re.compile(r"^(TRANSPORT|indexed into|transport)\b")


def run(argv, env=None):
    proc = subprocess.run([sys.executable, *argv], cwd=ROOT, capture_output=True,
                          text=True, env=env, timeout=300)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def audit_args(endpoint, index, as_of, username=None, insecure=False, ca_cert=None):
    argv = [str(AUDIT), "--index", index, "--endpoint", endpoint, "--as-of", as_of]
    if username:
        argv += ["--username", username]
    if insecure:
        argv += ["--insecure"]
    if ca_cert:
        argv += ["--ca-cert", ca_cert]
    return argv


def first_line(text, default="(no output)"):
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return default


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plain", required=True, help="endpoint with security disabled")
    ap.add_argument("--secure", required=True, help="endpoint with the security plugin on")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--insecure", action="store_true",
                    help="skip cert verification against --secure (a demo cert is self-signed)")
    ap.add_argument("--ca-cert", default=None)
    ap.add_argument("--index", default="unclosed-real-spike")
    ap.add_argument("--seed", type=int, default=20260801)
    ap.add_argument("--as-of", default=None,
                    help="pin both audits to one moment; default is the newest doc in --plain")
    ap.add_argument("--no-capture", action="store_true")
    args = ap.parse_args()

    if os.environ.get("OPENSEARCH_PASSWORD") is None:
        raise SystemExit("Set $OPENSEARCH_PASSWORD before running this.")

    auth = {"username": args.username, "insecure": args.insecure, "ca_cert": args.ca_cert}
    env = dict(os.environ)
    out = [
        "=" * 78,
        "unclosed -- vendor neutrality: the same audit on two deployment shapes",
        "=" * 78,
        "",
        f"  plaintext : {args.plain}   (security plugin disabled)",
        f"  secured   : {args.secure}   (security plugin on, self-signed cert, basic auth)",
        "",
        "Both seeded from the same generator with the same seed. Both audits pinned",
        "to the same --as-of, so anything that differs below is a portability",
        "defect rather than a difference in the data.",
        "",
        "-" * 78,
        "1. THE FAILURE PATHS  (what a user hits before they ever see a verdict)",
        "-" * 78,
        "",
    ]

    # -- failure paths, checked against the secured cluster -----------------
    no_creds = dict(env)
    no_creds.pop("OPENSEARCH_PASSWORD", None)
    no_creds.pop("OPENSEARCH_USERNAME", None)
    checks = [
        ("https, no credentials",
         audit_args(args.secure, args.index, "2026-01-01T00:00:00Z", insecure=args.insecure,
                    ca_cert=args.ca_cert), no_creds),
        ("http against a TLS port",
         audit_args(args.secure.replace("https://", "http://"), args.index,
                    "2026-01-01T00:00:00Z", **auth), env),
        ("https, self-signed cert, no --insecure and no --ca-cert",
         audit_args(args.secure, args.index, "2026-01-01T00:00:00Z", username=args.username),
         env),
    ]
    for label, argv, use_env in checks:
        rc, text = run(argv, env=use_env)
        line = first_line(text)
        traceback = "Traceback (most recent call last)" in text
        status = "TRACEBACK" if traceback else ("exits 0" if rc == 0 else "refuses")
        out.append(f"  [{status:>9}] {label}")
        out.append(f"              {line[:200]}")
        if traceback:
            out.append("              ^^ an uncaught exception is not an error message")
    out.append("")

    # -- the verdicts -------------------------------------------------------
    as_of = args.as_of
    if as_of is None:
        # Discover the anchor by running with no --as-of at all. Passing a
        # placeholder here would be honoured as the anchor, and the report would
        # then describe the placeholder rather than the index.
        argv = [str(AUDIT), "--index", args.index, "--endpoint", args.plain]
        rc, text = run(argv)
        match = re.search(r"anchored on newest `[^`]+` in \S+ at (\S+)", text)
        if not match:
            raise SystemExit(
                "Could not read the scan anchor from a plain run against "
                f"{args.plain}; nothing below would mean anything. Output was:\n{text[:600]}")
        as_of = match.group(1)

    out += [
        "-" * 78,
        "2. THE SAME AUDIT, BOTH SHAPES",
        "-" * 78,
        "",
        f"  pinned to --as-of {as_of}",
        "",
    ]

    rc_plain, plain = run(audit_args(args.plain, args.index, as_of))
    rc_secure, secure = run(audit_args(args.secure, args.index, as_of, **auth))
    out.append(f"  plaintext exit {rc_plain}    secured exit {rc_secure}")
    verdicts = {}
    for label, text in (("plaintext", plain), ("secured", secure)):
        verdict = next((ln for ln in text.splitlines() if ln.startswith("VERDICT:")), None)
        transport = next((ln for ln in text.splitlines() if ln.startswith("TRANSPORT:")), "(none)")
        verdicts[label] = verdict
        out.append(f"    {label:<10} {verdict or '(NO VERDICT -- the run did not get that far)'}")
        out.append(f"    {'':<10} {transport}")
    out.append("")

    # Two runs that failed the same way produce an empty diff, and an empty diff
    # is what this script prints as a pass. That is the failure this project
    # exists to catch, in this project's own harness: a check that is green
    # because nothing happened. So a comparison is only evidence if both sides
    # actually reached a verdict.
    both_ran = all(verdicts.values())
    if not both_ran:
        out += [
            "  ABORTED: at least one shape produced no verdict, so the diff below would",
            "  be comparing two failures. Two identical errors are not evidence that the",
            "  same question gets the same answer -- they are evidence that it was never",
            "  asked. Fix the connection and rerun.",
            "",
        ]
        report = "\n".join(out) + "\n"
        print(report)
        if not args.no_capture:
            CAPTURE.write_text(report, encoding="ascii")
        return 1

    diff = [ln for ln in difflib.unified_diff(
        plain.splitlines(), secure.splitlines(), "plaintext", "secured", lineterm="", n=0)]
    material = [ln for ln in diff
                if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))
                and not EXPECTED_TO_DIFFER.match(ln[1:].strip())]

    out += [
        "-" * 78,
        "3. DIFF",
        "-" * 78,
        "",
    ]
    if not diff:
        out.append("  The two reports are identical, including the endpoint lines.")
    else:
        out.append("  Every differing line, with the transport lines marked as expected:")
        out.append("")
        for ln in diff:
            if ln.startswith(("+++", "---", "@@")):
                continue
            expected = EXPECTED_TO_DIFFER.match(ln[1:].strip())
            out.append(f"    {'(expected) ' if expected else '(MATERIAL)  '}{ln}")
    out += [
        "",
        f"  material differences: {len(material)}",
        "",
        "  A material difference here is a portability defect: the same question,",
        "  the same data, two deployments, two answers.",
        "",
    ]

    report = "\n".join(out) + "\n"
    print(report)
    if not args.no_capture:
        CAPTURE.write_text(report, encoding="ascii")
        print(f"written: {CAPTURE.relative_to(ROOT)}")
    return 0 if not material and rc_plain == 0 and rc_secure == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
