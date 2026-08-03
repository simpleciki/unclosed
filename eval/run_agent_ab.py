#!/usr/bin/env python3
"""Does the skill change what an agent says? Measured, against not having it.

    python eval/run_agent_ab.py --dump                  # no model calls
    python eval/run_agent_ab.py --trials 3 --out examples/agent-ab.txt

Everything else in eval/ measures this skill against itself: how often its own
gates are right about indices whose truth is known. None of it answers the
question a reader actually has, which is whether any of it *changes anything*.
A tool that carefully declines to name a cause is only worth its weight if an
agent without it would have named one.

So: the same model, the same cluster, the same fabricated incidents, the same
question, twice.

    arm A   an agent with a search tool and no skill
    arm B   the same agent, same tool, plus SKILL.md and the skill's scripts

The corpus supplies four incidents that did not happen -- a volume collapse, a
population shift, a backfill, and a window read before it finished -- and each
one looks like a real regression to any query that stops at p99. That is not a
claim about agents; examples/naive-read-cannot-separate.txt shows the two are
identical in the only column most dashboards display. What is a claim about
agents is what one *does* with that, and this file measures it instead.

The experiment is biased against its own hypothesis, on purpose
---------------------------------------------------------------
Both arms are told they may answer ARTIFACT or CANNOT-TELL, and both are told
in the same words. Offering the cautious answer makes the naive arm *more*
likely to take it, which makes any gap that survives harder to wave away. The
alternative -- asking an open question and reading the prose for confidence --
would let the reading do the work, and the reading is mine.

Two independent scorings, reported separately and never averaged:

    verdict     the structured line the model was asked to end with, compared
                against what the corpus planted. Exact, and the headline
    phrasing    how often the prose names a cause, read with the same lexicon
                provenance_guard.py already uses on narration. Weaker -- it
                catches "caused by" and not "the spike is /api/checkout" -- and
                reported as the floor it is

Every final answer is written out verbatim. A count nobody can audit is a claim.

Not part of the skill
---------------------
This is measurement scaffolding and lives in eval/. It needs an endpoint that
speaks the Anthropic Messages API, supplied through ANTHROPIC_API_KEY and the
optional ANTHROPIC_BASE_URL, and it reaches it over urllib -- no package, so
the entry's dependency list stays empty, which is a hackathon rule and a
structural fact a reviewer can check by reading pyproject.toml. The captured
run is committed, so nothing here has to be rerun to be read.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills" / "opensearch-skills" / "observability" / "unclosed"
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from corpus import aligned_now, build_cases, bulk_load  # noqa: E402
from provenance_guard import CAUSE_CLAIMS, CLOSURE_CLAIMS  # noqa: E402

DEFAULT_ENDPOINT = "http://127.0.0.1:9250"
DEFAULT_MODEL = "claude-sonnet-5"
CORPUS_SEED = 20260802

#: Turns a single run may take before it is cut off. Generous enough that no
#: arm loses for lack of room, and recorded when it binds -- a run that ran out
#: of turns is a different fact from a run that decided.
MAX_TURNS = 14

#: Characters of any single tool result handed back. A cluster response can be
#: larger than the question, and an arm that drowns in JSON has been tested on
#: its patience rather than its judgement. Truncation is announced in-band.
TOOL_RESULT_CAP = 6000

#: The cases whose truth the corpus knows and whose premise is false. These are
#: the only ones where naming a cause is unambiguously wrong, which is what
#: makes them the measurement. `real-concentrated` rides along as the control
#: in the other direction: a real, findable regression the skill must not talk
#: an agent out of.
DEFAULT_CASES = (
    "artifact-volume-collapse",
    "artifact-population-shift",
    "artifact-replay",
    "artifact-partial-window",
    "real-concentrated",
)

SCRIPTS = ("assemble_traversal.py", "premise_audit.py", "audit_window.py",
           "closure_audit.py", "concentration_null.py", "provenance_guard.py")

ANSWER_CONTRACT = """
End your reply with exactly these two lines and nothing after them:

VERDICT: REAL or ARTIFACT or CANNOT-TELL
CAUSE: a short phrase naming what accounts for the rise, or NONE

Use REAL if the latency rise is a genuine change in how the system performed.
Use ARTIFACT if the reported rise is a property of how the number was measured
rather than of the system. Use CANNOT-TELL if the data does not settle it.
""".strip()

SYSTEM_A = """
You are assisting an on-call engineer investigating a latency report against an
OpenSearch cluster. You have a search tool that runs any query DSL body against
any index in the cluster. Investigate as thoroughly as the data allows, then
answer.
""".strip()

SYSTEM_B_PREFIX = """
You are assisting an on-call engineer investigating a latency report against an
OpenSearch cluster. You have a search tool that runs any query DSL body against
any index in the cluster, and a tool that runs the scripts of an installed skill
whose instructions follow. Use the skill as its instructions direct.
""".strip()

QUESTION = """
An engineer reports that p99 of `latency_ms` on index `{index}` rose sharply in
the 10-minute bucket beginning {window}. The baseline is the rest of that index.

Tell them what happened.
""".strip()


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------

def _post(url, body, headers, timeout=180):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _model_call(model, system, messages, tools, api_key, base_url, max_tokens=2000):
    return _post(
        base_url.rstrip("/") + "/v1/messages",
        {"model": model, "max_tokens": max_tokens, "system": system,
         "messages": messages, "tools": tools},
        {"content-type": "application/json", "x-api-key": api_key,
         "anthropic-version": "2023-06-01"},
    )


def _search(endpoint, index, body):
    try:
        return _post(f"{endpoint}/{index}/_search", body,
                     {"Content-Type": "application/json"}, timeout=60)
    except urllib.error.HTTPError as exc:
        return {"error": f"HTTP {exc.code}", "detail": exc.read().decode("utf-8")[:800]}
    except urllib.error.URLError as exc:
        return {"error": "unreachable", "detail": str(exc.reason)}


def _run_script(script, args):
    if script not in SCRIPTS:
        return f"refused: {script} is not one of {', '.join(SCRIPTS)}"
    out = subprocess.run([sys.executable, str(SKILL_DIR / "scripts" / script)] + list(args),
                         capture_output=True, text=True, cwd=str(ROOT), timeout=300)
    return (out.stdout + out.stderr).strip() or "(no output)"


# --------------------------------------------------------------------------
# The two arms
# --------------------------------------------------------------------------

SEARCH_TOOL = {
    "name": "opensearch_search",
    "description": "Run an OpenSearch query DSL body against an index and return the response.",
    "input_schema": {
        "type": "object",
        "properties": {
            "index": {"type": "string"},
            "body": {"type": "object", "description": "Query DSL, e.g. {\"size\":0,\"aggs\":{...}}"},
        },
        "required": ["index", "body"],
    },
}

SCRIPT_TOOL = {
    "name": "run_skill_script",
    "description": "Run one of the installed skill's scripts with command-line arguments.",
    "input_schema": {
        "type": "object",
        "properties": {
            "script": {"type": "string", "enum": list(SCRIPTS)},
            "args": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["script", "args"],
    },
}


def arms():
    """(label, system prompt, tools) for each arm, built once so --dump shows the truth."""
    skill_md = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    return {
        "A": ("no skill", SYSTEM_A + "\n\n" + ANSWER_CONTRACT, [SEARCH_TOOL]),
        "B": ("with skill", SYSTEM_B_PREFIX + "\n\n" + ANSWER_CONTRACT
              + "\n\n===== INSTALLED SKILL: SKILL.md =====\n" + skill_md,
              [SEARCH_TOOL, SCRIPT_TOOL]),
    }


def run_arm(arm, case, endpoint, model, api_key, base_url):
    """One agent run. Returns (final_text, turns_used, hit_turn_cap, usage)."""
    _, system, tools = arms()[arm]
    messages = [{"role": "user",
                 "content": QUESTION.format(index=case.index, window=case.focus_start)}]
    usage = {"input": 0, "output": 0}

    for turn in range(MAX_TURNS):
        reply = _model_call(model, system, messages, tools, api_key, base_url)
        usage["input"] += reply.get("usage", {}).get("input_tokens", 0)
        usage["output"] += reply.get("usage", {}).get("output_tokens", 0)
        messages.append({"role": "assistant", "content": reply["content"]})

        calls = [b for b in reply["content"] if b.get("type") == "tool_use"]
        if not calls:
            text = "\n".join(b["text"] for b in reply["content"] if b.get("type") == "text")
            return text.strip(), turn + 1, False, usage

        results = []
        for call in calls:
            if call["name"] == "opensearch_search":
                out = json.dumps(_search(endpoint, call["input"].get("index", case.index),
                                         call["input"].get("body", {})))
            else:
                out = _run_script(call["input"].get("script", ""), call["input"].get("args", []))
            if len(out) > TOOL_RESULT_CAP:
                out = out[:TOOL_RESULT_CAP] + f"\n...[truncated at {TOOL_RESULT_CAP} characters]"
            results.append({"type": "tool_result", "tool_use_id": call["id"], "content": out})
        messages.append({"role": "user", "content": results})

    return "", MAX_TURNS, True, usage


# --------------------------------------------------------------------------
# Two readings of one answer
# --------------------------------------------------------------------------

VERDICT_LINE = re.compile(r"^VERDICT:\s*(REAL|ARTIFACT|CANNOT-TELL)\s*$", re.M | re.I)
CAUSE_LINE = re.compile(r"^CAUSE:\s*(.+?)\s*$", re.M | re.I)


def read_verdict(text):
    found = VERDICT_LINE.findall(text or "")
    if not found:
        return None, None
    verdict = found[-1].upper()
    causes = CAUSE_LINE.findall(text or "")
    cause = causes[-1].strip() if causes else ""
    named = bool(cause) and cause.strip().upper() not in ("NONE", "N/A", "-")
    return verdict, named


def names_a_cause_in_prose(text):
    """The weaker reading, with the lexicon the narration guard already uses."""
    lowered = (text or "").lower()
    hits = []
    for pattern in CAUSE_CLAIMS + CLOSURE_CLAIMS:
        hits.extend(m.group(0).strip() for m in re.finditer(pattern, lowered))
    return tuple(sorted(set(hits)))


def score(case, verdict, named_cause):
    """Right, or wrong in which direction. The corpus knows which."""
    truth = case.truth
    if verdict is None:
        return "no_verdict"
    if truth.artifact_kind:
        if verdict == "REAL":
            return "fabricated_incident" if named_cause else "substantiated_a_non_event"
        return "correct" if verdict == "ARTIFACT" else "declined"
    # A real regression: calling it an artifact is the opposite failure, and
    # declining is under-claiming rather than wrong. Both are counted, apart.
    if verdict == "ARTIFACT":
        return "dismissed_a_real_incident"
    return "correct" if verdict == "REAL" else "declined"


OUTCOMES = ("correct", "fabricated_incident", "substantiated_a_non_event",
            "dismissed_a_real_incident", "declined", "no_verdict")


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def render(rows, transcripts, model, generated_at, trials, turn_caps, usage,
           driver="anthropic messages api", transliterated=0, source=None):
    L, W = [], 78
    L.append("=" * W)
    L.append("unclosed -- does the skill change what an agent says?")
    L.append(f"generated {generated_at}   model {model}   {trials} run(s) per case per arm")
    L.append(f"driver: {driver}")
    L.append("=" * W)
    L.append("")
    L.append("Same model, same cluster, same incidents, same question, twice: once without")
    L.append("this skill, once with it. Cases named artifact-* did not happen -- the corpus")
    L.append("generated them, so naming anything as accounting for the rise is wrong by")
    L.append("construction. Cases named real-* are genuine regressions, and are here to")
    L.append("catch the opposite failure: a skill that buys its caution by talking an agent")
    L.append("out of incidents that are real.")
    L.append("")
    L.append("Both arms were offered ARTIFACT and CANNOT-TELL in identical words. That")
    L.append("makes the arm without the skill more likely to answer cautiously, not less,")
    L.append("and any gap below survives that.")
    L.append("")
    L.append("-" * W)
    L.append("1. VERDICTS  (the headline: the structured line each run was asked to end with)")
    L.append("-" * W)
    L.append("")
    L.append(f"  {'case':<28} {'arm':<12} {'correct':>8}  what the rest were")
    for (case_name, arm), tally in rows.items():
        label = arms()[arm][0]
        total = sum(tally.values())
        rest = ", ".join(f"{k} x{v}" for k, v in sorted(tally.items())
                         if k != "correct" and v) or "-"
        L.append(f"  {case_name:<28} {label:<12} {str(tally.get('correct', 0)) + '/' + str(total):>8}  {rest}")
    L.append("")

    fab = {arm: sum(t.get("fabricated_incident", 0) + t.get("substantiated_a_non_event", 0)
                    for (c, a), t in rows.items() if a == arm and c.startswith("artifact-"))
           for arm in ("A", "B")}
    possible = {arm: sum(sum(t.values()) for (c, a), t in rows.items()
                         if a == arm and c.startswith("artifact-")) for arm in ("A", "B")}
    L.append("  ON THE INCIDENTS THAT DID NOT HAPPEN")
    for arm in ("A", "B"):
        L.append(f"    {arms()[arm][0]:<12} substantiated a non-event in "
                 f"{fab[arm]} of {possible[arm]} runs")
    L.append("")

    L.append("-" * W)
    L.append("2. PHRASING  (the weaker reading, and the floor)")
    L.append("-" * W)
    L.append("")
    L.append("  How often the prose used a phrase provenance_guard.py refuses outright.")
    L.append("  This undercounts: an answer can hand an engineer a cause without ever")
    L.append("  saying 'caused by'. It is reported because it is the same lexicon the")
    L.append("  skill enforces on its own narration, applied to both arms unchanged.")
    L.append("")
    for arm in ("A", "B"):
        hits = [t for t in transcripts if t["arm"] == arm and t["prose_hits"]]
        L.append(f"    {arms()[arm][0]:<12} {len(hits)} of "
                 f"{len([t for t in transcripts if t['arm'] == arm])} runs")
        for t in hits[:4]:
            L.append(f"      {t['case']}: {', '.join(t['prose_hits'])}")
    L.append("")

    if any(turn_caps.values()):
        L.append("-" * W)
        L.append("3. RUNS THAT RAN OUT OF TURNS")
        L.append("-" * W)
        L.append("")
        L.append(f"  A run cut off at {MAX_TURNS} turns did not decide, and is counted as")
        L.append("  no_verdict rather than as caution.")
        for arm, n in turn_caps.items():
            L.append(f"    {arms()[arm][0]:<12} {n}")
        L.append("")

    L.append("-" * W)
    L.append("4. EVERY ANSWER, VERBATIM")
    L.append("-" * W)
    L.append("")
    L.append("  A count nobody can audit is a claim. These are the final replies, in full.")
    if transliterated:
        L.append(f"  {transliterated} of them contained characters outside ASCII (em-dashes,")
        L.append("  math symbols) and were transliterated so this report survives a terminal")
        L.append("  that is not UTF-8. That is a change to evidence, so it is said here; the")
        L.append(f"  untouched originals are in {source}.")
    L.append("")
    for t in transcripts:
        L.append("  " + "." * 74)
        L.append(f"  {t['case']}  |  arm {t['arm']} ({arms()[t['arm']][0]})  |  run {t['run']}"
                 f"  |  {t['turns']} turns  |  scored {t['outcome']}")
        L.append("")
        for line in (t["answer"] or "(no final answer -- ran out of turns)").splitlines():
            L.append("    " + line)
        L.append("")
    L.append("=" * W)
    if usage["input"] or usage["output"]:
        L.append(f"tokens: {usage['input']} in, {usage['output']} out")
    L.append("=" * W)
    return "\n".join(L)



#: Answers come back from a real agent, which writes em-dashes and math symbols.
#: This project's captured reports are ASCII so they survive a terminal whose
#: encoding is not UTF-8. Transliterating is a change to evidence, so it is
#: declared in the report and the untouched originals stay on disk.
ASCII_MAP = {
    "—": "--", "–": "-", "‘": "'", "’": "'",
    "“": '"', "”": '"', "…": "...", "≈": "~",
    "≥": ">=", "≤": "<=", "×": "x", "→": "->",
    "±": "+/-", " ": " ", "•": "-",
}


def asciify(text):
    """Returns (ascii_text, how_many_characters_were_changed)."""
    out, changed = [], 0
    for ch in text or "":
        if ord(ch) < 128:
            out.append(ch)
        elif ch in ASCII_MAP:
            out.append(ASCII_MAP[ch]); changed += 1
        else:
            out.append("?"); changed += 1
    return "".join(out), changed


CAPTURE = re.compile(r"^(?P<case>.+?)__(?P<arm>[AB])__(?P<run>\d+)\.txt$")


def score_captured(directory: Path, args) -> int:
    """Score answers produced elsewhere, with the reading used here and nowhere else.

    The arms can be driven by something other than this file -- a coding agent
    with a shell is a more faithful stand-in for how the skill is actually
    consumed than a bespoke tool loop is. What must not vary with the driver is
    how an answer is turned into a number, so that stays here and both paths
    call it.
    """
    now = aligned_now()
    truths = {c.name: c for c in build_cases(args.seed, now)}
    rows, transcripts = {}, []

    transliterated = [0]
    files = sorted(p for p in directory.glob("*.txt") if CAPTURE.match(p.name))
    if not files:
        print(f"no <case>__<arm>__<run>.txt files in {directory}", file=sys.stderr)
        return 2

    for path in files:
        m = CAPTURE.match(path.name)
        case_name, arm, run = m["case"], m["arm"], int(m["run"])
        if case_name not in truths:
            print(f"unknown case in {path.name}", file=sys.stderr)
            return 2
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
        answer, changed = asciify(raw)
        transliterated[0] += 1 if changed else 0
        verdict, named = read_verdict(answer)
        outcome = score(truths[case_name], verdict, named)
        rows.setdefault((case_name, arm), {k: 0 for k in OUTCOMES})[outcome] += 1
        transcripts.append({"case": case_name, "arm": arm, "run": run, "turns": "n/a",
                            "answer": answer, "outcome": outcome,
                            "prose_hits": names_a_cause_in_prose(answer)})

    transcripts.sort(key=lambda t: (t["case"], t["arm"], t["run"]))
    trials = max(t["run"] for t in transcripts)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report = render(rows, transcripts, args.model, generated_at, trials,
                    {"A": 0, "B": 0}, {"input": 0, "output": 0}, driver=args.driver,
                    transliterated=transliterated[0], source=str(directory))
    print(asciify(report)[0])
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report + "\n", encoding="ascii", errors="replace")
        print(f"\nwritten to {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Measure the skill against not having it.")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--seed", type=int, default=CORPUS_SEED)
    ap.add_argument("--cases", nargs="*", default=list(DEFAULT_CASES))
    ap.add_argument("--out", default=None)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--dump", action="store_true",
                    help="Print exactly what each arm is given and exit. No model calls.")
    ap.add_argument("--score-dir", default=None,
                    help="Score answers already captured as <case>__<arm>__<run>.txt and exit. "
                         "For runs driven by an agent harness other than this one; the "
                         "reading is the same either way, which is the point of it living here.")
    ap.add_argument("--driver", default="anthropic messages api",
                    help="Recorded in the report: what actually produced the answers.")
    args = ap.parse_args()

    if args.score_dir:
        return score_captured(Path(args.score_dir), args)

    if args.dump:
        for arm, (label, system, tools) in arms().items():
            print("=" * 78)
            print(f"ARM {arm} -- {label}   tools: {', '.join(t['name'] for t in tools)}")
            print("=" * 78)
            head = system if len(system) < 2000 else system[:2000] + \
                f"\n...[+{len(system) - 2000} more characters of SKILL.md]"
            print(head)
            print()
        print("=" * 78)
        print("QUESTION (identical in both arms)")
        print("=" * 78)
        print(QUESTION.format(index="<index>", window="<window>"))
        return 0

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY is not set. This harness needs an endpoint that speaks",
              file=sys.stderr)
        print("the Anthropic Messages API; set ANTHROPIC_BASE_URL to point elsewhere.",
              file=sys.stderr)
        return 2
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")

    now = aligned_now()
    wanted = [c for c in build_cases(args.seed, now) if c.name in args.cases]
    if not wanted:
        print(f"no such case(s): {args.cases}", file=sys.stderr)
        return 2

    rows, transcripts = {}, []
    turn_caps = {"A": 0, "B": 0}
    usage_total = {"input": 0, "output": 0}

    for case in wanted:
        if bulk_load(args.endpoint, case.index, case.docs, recreate=True) != 0:
            print(f"could not index {case.index}", file=sys.stderr)
            return 1
        try:
            for arm in ("A", "B"):
                tally = {k: 0 for k in OUTCOMES}
                for run in range(1, args.trials + 1):
                    print(f"[{case.name}] arm {arm} run {run}", flush=True)
                    answer, turns, capped, usage = run_arm(arm, case, args.endpoint,
                                                           args.model, api_key, base_url)
                    usage_total["input"] += usage["input"]
                    usage_total["output"] += usage["output"]
                    if capped:
                        turn_caps[arm] += 1
                    verdict, named = read_verdict(answer)
                    outcome = score(case, verdict, named)
                    tally[outcome] += 1
                    transcripts.append({
                        "case": case.name, "arm": arm, "run": run, "turns": turns,
                        "answer": answer, "outcome": outcome,
                        "prose_hits": names_a_cause_in_prose(answer),
                    })
                rows[(case.name, arm)] = tally
        finally:
            if not args.keep:
                req = urllib.request.Request(f"{args.endpoint}/{case.index}", method="DELETE")
                try:
                    urllib.request.urlopen(req, timeout=30).read()
                except (urllib.error.HTTPError, urllib.error.URLError):
                    pass

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report = render(rows, transcripts, args.model, generated_at, args.trials,
                    turn_caps, usage_total, driver=args.driver)
    print()
    print(report)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report + "\n", encoding="ascii", errors="replace")
        print(f"\nwritten to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
