"""`wait` accepts a DURATION on every action, not only bare seconds (#97).

`wait: "8s"` worked on navigate, but `wait: "1500ms"` on a click fell through to
`Page.wait_for_selector` and threw `Unexpected token "1500ms" while parsing css selector`. One
field carrying two grammars is easy to trip over mid-batch, so every duration literal an agent
plausibly writes is parsed as a duration; a selector still means a selector.
"""

import pytest

from interact.server.capture import _parse_wait_seconds


@pytest.mark.parametrize(
    "text, expected",
    [
        ("3", 3.0),           # bare number = seconds (#63)
        ("8s", 8.0),
        (" 2.5s ", 2.5),
        ("1500ms", 1.5),      # the reported failure
        ("500ms", 0.5),
        ("250MS", 0.25),      # case-insensitive
        ("1m", 60.0),
        ("0.5", 0.5),
    ],
)
def test_duration_literals_parse(text, expected):
    assert _parse_wait_seconds(text) == pytest.approx(expected)


@pytest.mark.parametrize(
    "text",
    ["button", "#id", ".cls", "text=Save", "div > span", "networkidle", "", "sms", "ms"],
)
def test_selectors_and_load_states_are_not_durations(text):
    assert _parse_wait_seconds(text) is None


def test_a_negative_duration_is_not_a_duration():
    assert _parse_wait_seconds("-2s") is None  # never sleep on a nonsense value


# ── type_text into whatever is already focused (#97, second half) ────────────────────────────
# "click the field, then type" had to repeat the selector, because type_text errored without a
# ref/selector. After a click the field IS focused, so typing at the keyboard is the natural act.


@pytest.mark.asyncio
async def test_type_text_without_a_target_types_into_the_focused_element():
    from interact.actions import TypeTextAction

    typed = []

    class _Kb:
        async def type(self, text, **kw):
            typed.append(text)

        async def press(self, key):
            typed.append(f"<{key}>")

    class _Page:
        keyboard = _Kb()

    await TypeTextAction(text="hello").execute(_Page())
    assert "hello" in typed
