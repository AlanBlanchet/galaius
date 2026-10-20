"""`speech_infer.spoken_language`: the blob's voice is told apart between the languages he speaks
(French and English) before it is transcribed — a French command on an English screen stays
French, and a language Whisper half-hears outside that pair never wins."""

import pytest

from galaius.speech_infer import spoken_language


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
