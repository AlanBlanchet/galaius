"""`speech_infer.spoken_language`: the blob's voice is told apart between the languages he speaks
(French and English) before it is transcribed — a French command on an English screen stays
French, and a language Whisper half-hears outside that pair never wins."""

import pytest

from galaius.speech_infer import corrected, misheard, spelled, spoken_language


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


NAMES = ("Galaius", "my-project", "Arrête", "Blob")


@pytest.mark.parametrize(("heard", "expected"), [
    # Whisper's spellings of the name are hinted, joined words included.
    ("Galeus, open the agents.", [(0, 1, "Galaius")]),
    ("Demande à Gal ius de lancer", [(2, 4, "Galaius")]),
    ("open my projet please", [(1, 3, "my-project")]),
    # Real words near the name never trigger: a hint would turn them into it. (« galets » does, and stays
    # « galets »: the hinted pass writes « galais », which is not the name — `corrected` keeps the plain words.)
    ("Goliath beat David.", []),
    ("Galilée observait Jupiter.", []),
    # Already written as said (also inside a longer name), or too short to guess: no second pass.
    ("Galaius, ouvre les agents.", []),
    ("Galaius core is ready", []),
    ("Arrête ça", []),
    ("open my project please", []),
    ("Blobs", []),
])
def test_only_a_misheard_name_asks_for_a_second_pass(heard, expected):
    assert misheard(heard.split(), NAMES) == expected


@pytest.mark.parametrize(("plain", "hinted", "expected"), [
    # The name is taken from the hinted pass; every other word stays from the plain one.
    ("Galeus, euh, open the agents.", "Galaius, E. Open the agents.", "Galaius, euh, open the agents."),
    # The hinted pass did not write the name there: the plain words stay.
    ("Galeus, open the agents.", "Gallus, open the agents.", "Galeus, open the agents."),
    ("Les galets de la plage.", "Les galais de la plage.", "Les galets de la plage."),
])
def test_only_the_misheard_place_is_corrected(plain, hinted, expected):
    assert corrected(plain, hinted, misheard(plain.split(), NAMES)) == expected


@pytest.mark.parametrize(("heard", "expected"), [
    ("Demande à Galaïus, vite.", "Demande à Galaius, vite."),
    ("ouvre myproject", "ouvre my-project"),
    ("arrete", "Arrête"),
    ("Tell Galai us that", "Tell Galaius that"),
    ("Dis à Gal ai us, vite", "Dis à Galaius, vite"),
    # A name written with a hyphen reads naturally split: kept. Case alone is how the sentence writes it: kept.
    ("galaius, ouvre", "galaius, ouvre"),
    ("Galaius core", "Galaius core"),
    ("open my project", "open my project"),
    ("Galai, us", "Galai, us"),
])
def test_a_name_with_other_accents_is_spelled_as_given(heard, expected):
    assert spelled(heard, NAMES) == expected
