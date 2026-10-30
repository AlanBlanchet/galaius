"""`speech_infer.spoken_language`: the blob's voice is told apart between the languages he speaks
(French and English) before it is transcribed — a French command on an English screen stays
French, and a language Whisper half-hears outside that pair never wins."""

import json
from pathlib import Path

import pytest

from galaius.speech_infer import Place, fold, heard_as, misheard, spelled, spoken_language


@pytest.mark.parametrize(("probabilities", "expected"), [
    ([("fr", 0.62), ("en", 0.30), ("de", 0.05)], "fr"),
    ([("en", 0.81), ("fr", 0.12)], "en"),
    # Accented French heard as Italian first: of the two he speaks, French is the likelier.
    ([("it", 0.55), ("fr", 0.30), ("en", 0.10)], "fr"),
    # Neither of his languages scored at all: the first one he speaks, never a third.
    ([("de", 0.9), ("nl", 0.1)], "fr"),
])
def test_the_spoken_language_is_one_he_speaks(probabilities, expected):
    assert spoken_language(probabilities, ("fr", "en")) == expected


#: The rule's golden cases, shared with every other implementation of it (the web app's copy reads this file too).
CASES = json.loads((Path(__file__).parent / "speech_vocabulary_cases.json").read_text(encoding="utf-8"))
NAMES = CASES["names"]


@pytest.mark.parametrize(("text", "expected"), CASES["fold"])
def test_names_are_compared_letters_and_digits_only_in_any_script(text, expected):
    assert fold(text) == expected


@pytest.mark.parametrize(("heard", "expected"), CASES["misheard"])
def test_only_a_name_a_few_letters_off_is_a_candidate(heard, expected):
    assert [list(place) for place in misheard(heard.split(), NAMES)] == expected


@pytest.mark.parametrize("case", CASES["heard_as"], ids=[case["heard"] for case in CASES["heard_as"]])
def test_a_candidate_is_written_only_where_the_audio_agrees(case):
    assert heard_as(case["heard"].split(), [Place(*place) for place in case["places"]], case["scores"], case["bar"]) == case["expected"]


@pytest.mark.parametrize(("heard", "known", "expected"), CASES["misheard_known"])
def test_a_name_the_model_knows_is_only_taken_as_written(heard, known, expected):
    assert [list(place) for place in misheard(heard.split(), NAMES, known=lambda name: name in known)] == expected


@pytest.mark.parametrize(("heard", "expected"), CASES["spelled"])
def test_a_name_with_other_accents_or_split_is_spelled_as_given(heard, expected):
    assert spelled(heard, NAMES) == expected
