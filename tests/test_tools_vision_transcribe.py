"""transcribe(query=…) must say WHAT it heard and how it answered (#94).

Two failure modes were reported on the same clean file: the tool answered an acoustic-quality
question over a TRANSCRIPT without saying so, and — even on a genuinely listening model — the
verdict flipped with the wording and inverted an A/B comparison against an objectively measured
relationship. interact can't make a model hear better, but it can refuse to present a caption as
a measurement: label the evidence basis, and never let a quality question pass unqualified.
"""

import pytest

from interact.server.tools_vision import _asks_about_sound_quality


@pytest.mark.parametrize(
    "query",
    [
        "rate acoustic fidelity 1-10",
        "describe the artifacts",
        "which of these two is cleaner?",
        "how is the audio quality?",
        "is there any distortion or noise?",
        "does it sound robotic?",
        "compare the sound quality of both segments",
        "any metallic or watery artefacts?",
    ],
)
def test_quality_questions_are_recognised(query):
    assert _asks_about_sound_quality(query) is True


@pytest.mark.parametrize(
    "query",
    [
        "what did the speaker say?",
        "how many speakers are there?",
        "summarise this call",
        "what language is this?",
        "at what timestamp is the beep?",
    ],
)
def test_content_questions_are_not_quality_questions(query):
    assert _asks_about_sound_quality(query) is False


def test_no_query_is_not_a_quality_question():
    assert _asks_about_sound_quality(None) is False
