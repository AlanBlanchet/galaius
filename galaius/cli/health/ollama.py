"""The local Ollama server and the models it holds."""

from galaius.inference import ollama


#: Enough to see the shortlist without burying the rest of `doctor` — a cloud catalog is ~19 rows.
OLLAMA_SHOWN = 8


def print_ollama(indent: str = "  ") -> None:
    """Name what the Ollama daemon actually serves, so "did galaius see the model I pulled?" is
    answerable without guessing.

    Vision models are named individually — those are the ones galaius can DRIVE; the rest are
    counted, not listed, since a wall of text-only ids would bury the rest of doctor with nothing
    actionable. Silent when there's no daemon — someone not running Ollama must not read a line
    about it.
    """

    found = ollama.serving()
    if not found:
        return
    # Where they CAME from, not where we looked first: discovery walks a ladder, and this is also
    # the endpoint a completion is sent to — the two stay checkable against each other.
    where = found[0].base or ollama.candidate_bases()[0]
    vision = sorted((m for m in found if m.vision), key=lambda m: m.name)
    rest = sorted((m for m in found if not m.vision), key=lambda m: m.name)

    usable = f"{len(vision)} vision-capable" if vision else "none vision-capable"
    print(f"{indent}{'ollama':<14}: {len(found)} available on {where} — {usable}")
    for model in vision[:OLLAMA_SHOWN]:
        print(f"{indent}  · {model.describe()}")
    if len(vision) > OLLAMA_SHOWN:
        print(f"{indent}  · … +{len(vision) - OLLAMA_SHOWN} more vision models")
    if rest:
        names = ", ".join(m.name for m in rest[:OLLAMA_SHOWN])
        more = f", +{len(rest) - OLLAMA_SHOWN} more" if len(rest) > OLLAMA_SHOWN else ""
        print(f"{indent}  · text-only ({len(rest)}): {names}{more}")
