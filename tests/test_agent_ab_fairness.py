"""The A/B measurement is only worth its headline if the arms differ by one thing.

Everything the comparison claims rests on the two arms being identical except
for the skill. That is an easy property to break by accident -- a sentence added
to one system prompt, a tool given to one arm, a scorer that reads the first
verdict instead of the last -- and every one of those breaks it silently, in the
direction of a more impressive number.

So the equality is pinned here rather than left to the reading of a diff.
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("run_agent_ab", ROOT / "eval" / "run_agent_ab.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ab = _load()


def test_both_arms_are_offered_the_cautious_answer_in_the_same_words():
    # If only one arm is told it may answer ARTIFACT or CANNOT-TELL, the
    # comparison measures the instruction rather than the skill.
    _, system_a, _ = ab.arms()["A"]
    _, system_b, _ = ab.arms()["B"]
    assert ab.ANSWER_CONTRACT in system_a
    assert ab.ANSWER_CONTRACT in system_b
    for option in ("REAL", "ARTIFACT", "CANNOT-TELL"):
        assert option in ab.ANSWER_CONTRACT


def test_the_arm_without_the_skill_does_not_receive_the_skill():
    # The obvious failure, and the one that would be invisible in a summary.
    _, system_a, _ = ab.arms()["A"]
    skill_md = (ROOT / "skills/opensearch-skills/observability/unclosed/SKILL.md").read_text(
        encoding="utf-8")
    assert "INSTALLED SKILL" not in system_a
    for marker in ("Gate 1", "premise_audit", "NOT_VISITED"):
        assert marker not in system_a
    assert skill_md[:400] not in system_a


def test_the_arm_without_the_skill_can_still_see_everything():
    # The naive arm has to be able to reach every fact the skill reaches, or the
    # result measures access rather than judgement. Same unrestricted search
    # tool; the skill arm adds a way to run scripts and takes nothing away.
    _, _, tools_a = ab.arms()["A"]
    _, _, tools_b = ab.arms()["B"]
    names_a = {t["name"] for t in tools_a}
    names_b = {t["name"] for t in tools_b}
    assert names_a == {"opensearch_search"}
    assert names_a < names_b
    assert next(t for t in tools_a if t["name"] == "opensearch_search") in tools_b


def test_the_question_is_one_string_used_by_both():
    assert "{index}" in ab.QUESTION and "{window}" in ab.QUESTION
    # Nothing in it hints at which answer is wanted.
    lowered = ab.QUESTION.lower()
    for leak in ("artifact", "sample size", "collapse", "premise", "careful"):
        assert leak not in lowered


def test_only_whitelisted_scripts_run():
    assert "refused" in ab._run_script("rm", ["-rf", "/"])
    assert "refused" in ab._run_script("../../../etc/passwd", [])


class _Truth:
    def __init__(self, artifact_kind):
        self.artifact_kind = artifact_kind


class _Case:
    def __init__(self, artifact_kind):
        self.truth = _Truth(artifact_kind)


@pytest.mark.parametrize("verdict,named,artifact_kind,expected", [
    ("ARTIFACT", False, "sample_size_collapse", "correct"),
    ("REAL", True, "sample_size_collapse", "fabricated_incident"),
    ("REAL", False, "sample_size_collapse", "substantiated_a_non_event"),
    ("CANNOT-TELL", False, "sample_size_collapse", "declined"),
    ("REAL", True, None, "correct"),
    ("ARTIFACT", False, None, "dismissed_a_real_incident"),
    ("CANNOT-TELL", False, None, "declined"),
    (None, None, None, "no_verdict"),
])
def test_each_answer_lands_on_the_outcome_the_corpus_says_it_should(
        verdict, named, artifact_kind, expected):
    # Substantiating a non-event and naming a cause for one are both wrong and
    # are counted apart: the second hands an engineer somewhere to go.
    assert ab.score(_Case(artifact_kind), verdict, named) == expected


def test_the_verdict_read_is_the_last_one_not_the_first():
    # A model that reasons aloud may write "VERDICT: REAL" while considering it
    # and then conclude otherwise. Reading the first would score the thinking.
    text = ("At first this looks like a genuine regression.\n"
            "VERDICT: REAL\n"
            "But n collapsed to 3, so:\n"
            "VERDICT: ARTIFACT\nCAUSE: NONE\n")
    assert ab.read_verdict(text) == ("ARTIFACT", False)


def test_an_answer_with_no_verdict_line_is_not_read_as_caution():
    # Silence is not the same as declining, and counting it as declining would
    # quietly credit both arms for running out of turns.
    assert ab.read_verdict("I could not determine what happened.") == (None, None)
    assert ab.score(_Case("clock_semantics"), None, None) == "no_verdict"
