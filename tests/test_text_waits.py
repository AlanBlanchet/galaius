"""`wait_for text="…"` on a desktop target: read the screen (OCR, one engine per OS) until the text
appears — or, with state="hidden", is gone. Replaces the pixel-diff waits a blinking caret fooled:
a caret is not text, so it can neither satisfy nor block a text wait.

The reader test at the bottom runs the REAL engine of the OS it runs on (tesseract on Linux,
Windows.Media.Ocr on Windows, Vision on macOS); CI runs it on all three."""

import asyncio
import io
import os
import sys

import pytest
from PIL import Image, ImageDraw, ImageFont

from galaius.actions.models import KeyPressAction, WaitForAction
from galaius.desktop import DesktopWindow
from galaius.desktop import ocr
from galaius.desktop.backend import PortableBackend
from galaius.desktop.waits import DesktopWaitTimeout, text_matches, until_text


def _card(text: str, size=(640, 160), dark: bool = False) -> bytes:
    """A launcher-like result row: `text` in a 28-px sans font on a plain background."""
    bg, fg = ((32, 32, 36), (235, 235, 235)) if dark else ((250, 250, 250), (20, 20, 20))
    img = Image.new("RGB", size, bg)
    ImageDraw.Draw(img).text((24, 56), text, fill=fg, font=ImageFont.load_default(size=28))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# --- matching: OCR output is noisy; the wait must still recognise the words ----------------------


@pytest.mark.parametrize(
    "needle, seen, expected",
    [
        ("whispering", "Search results for whispering", True),
        ("Whispering", "search  RESULTS for\nwhispering", True),  # case, spacing, line breaks
        ("whispering", "Search results for whisperlng", True),  # one OCR slip (i → l)
        ("Open Settings", "0pen Settings  >", True),
        ("whispering", "Search results for whistle", False),
        ("Notepad", "", False),
        ("a", "banana", False),  # one short word matches words, not letters inside them
        ("Results for whispering", "Results for: whispering", True),
        ("whispering", "whisper ing", True),  # OCR split one word in two
        ("Results for", "Resultsfor", True),  # OCR merged two words
        # whole tokens only: a prefix still being typed, or a word inside a longer one, is not it
        ("Notepad", "notepa", False),
        ("Calculator", "calculat", False),
        ("Connected", "Disconnected", False),
        ("Save", "Saved", False),
        ("Run", "Runs", False),
        ("Settings", "Setting", False),
        ("File not found", "File found", False),
        ("Visual Studio Code", "Visual Studio", False),
    ],
)
def test_text_matches_whole_tokens_and_tolerates_ocr_lookalikes(needle, seen, expected):
    assert text_matches(needle, seen) is expected


@pytest.mark.parametrize(
    "needle, typed, seen, expected",
    [
        # the field echoes what galaius typed: that echo alone never satisfies the wait
        ("calculator", "calculator", "calculator", False),
        ("calculator", "calculator", "calculator  Top hit  Calculator", True),
        ("whispering", "whispering", "whispering", False),
        ("whispering", "whispering", "whispering\nResults for whispering", True),
        # the echo is the typed text alone on its line (a search icon read as "Q" may lead it); a
        # read that missed the field must not lose the result row that contains the same word
        ("whispering", "whispering", "Q whispering\nResults for whispering", True),
        ("whispering", "whispering", "Results for whispering", True),
        ("Results for whispering", "whispering", "whispering\fResults for whispering", True),
        ("calculator", "calculator", "Q calculator", False),
        # typed text that is not the needle changes nothing
        ("Notepad", "notep", "notep  Notepad  App", True),
        ("Notepad", "notep", "notep", False),
    ],
)
def test_text_galaius_typed_itself_never_satisfies_the_wait(needle, typed, seen, expected):
    assert text_matches(needle, seen, typed=[typed]) is expected


# --- the wait itself ---------------------------------------------------------------------------


def test_until_text_appears_then_disappears():
    reads = iter(["Search", "Search whisp", "Search results: whispering"])
    assert "whispering" in asyncio.run(
        until_text(lambda: next(reads), "whispering", present=True, timeout_s=2, poll_s=0)
    )
    gone = iter(["whispering", "whispering", ""])
    assert "gone" in asyncio.run(
        until_text(lambda: next(gone), "whispering", present=False, timeout_s=2, poll_s=0)
    )


def test_until_text_times_out_quoting_what_it_read():
    with pytest.raises(DesktopWaitTimeout, match="Terminal"):
        asyncio.run(until_text(lambda: "Terminal  Files", "Notepad", present=True, timeout_s=0.05, poll_s=0.01))


def test_wait_for_text_runs_on_the_desktop_and_takes_a_region():
    assert WaitForAction(text="Notepad").runs_on_desktop
    assert WaitForAction(text="Notepad", state="hidden", region=(0, 0, 400, 300)).region == (0, 0, 400, 300)
    assert WaitForAction(text="Notepad").region == "window"
    assert WaitForAction(text="Notepad", region="screen").region == "screen"
    assert not WaitForAction(selector="#x").runs_on_desktop
    with pytest.raises(ValueError, match="positive"):
        WaitForAction(text="x", region=(0, 0, 0, 10))
    with pytest.raises(ValueError, match="region"):
        WaitForAction(window="x", region=(0, 0, 10, 10))
    with pytest.raises(ValueError, match="region"):
        WaitForAction(window="x", region="screen")


def test_a_browser_text_wait_refuses_a_region():
    with pytest.raises(ValueError, match="desktop"):
        asyncio.run(WaitForAction(text="x", region=(0, 0, 10, 10)).execute(None))


def test_the_default_read_region_is_the_active_window_and_screen_is_opt_in(monkeypatch):
    """A text wait on a screen target reads the ACTIVE window unless told otherwise — the launcher,
    not the terminal or editor beside it; region="screen" reads everything."""
    from galaius.actions import dispatch
    from galaius.desktop.geometry import HostWindow
    from galaius.state import DesktopState

    sizes: list[tuple[int, int]] = []

    class Reader(ocr.TextReader):
        def read(self, png: bytes) -> str:
            sizes.append(Image.open(io.BytesIO(png)).size)
            return "Calculator"

    class Host(PortableBackend):
        def __init__(self):
            pass

        def capture_region(self, x, y, w, h):
            return _card("", size=(w, h))

        def active_window(self):
            return HostWindow(handle=7, title="Spotlight", x=-100, y=100, w=500, h=300)

    monkeypatch.setattr(ocr, "text_reader", lambda: Reader())
    monkeypatch.setattr(
        DesktopState, "capture",
        classmethod(lambda cls, name: cls(window_name=name, visible_text="", focused_element=None)),
    )
    screen = DesktopWindow(name="screen", wid=-1, x=-200, y=0, w=1200, h=800, is_screen=True)
    screen._backend = Host()
    for region, size in ((None, (500, 300)), ("screen", (1200, 800))):
        sizes.clear()
        action = WaitForAction(text="Calculator", timeout=1000) if region is None else \
            WaitForAction(text="Calculator", region=region, timeout=1000)
        report = asyncio.run(dispatch._run_actions_desktop(screen, [action], None))
        assert "appeared" in report, report
        assert set(sizes) == {size}, (region, sizes)


def test_run_actions_never_counts_its_own_typing(monkeypatch):
    """type_text "calculator" then wait_for text="calculator": the field's echo is not a result."""
    from galaius.actions import dispatch
    from galaius.actions.models import TypeTextAction
    from galaius.state import DesktopState

    class Reader(ocr.TextReader):
        def read(self, png: bytes) -> str:
            return host.field

    class Host(PortableBackend):
        def __init__(self):
            self.field = ""

        def capture_region(self, x, y, w, h):
            return _card("", size=(w, h))

        def active_window(self):
            return None

        def type_text(self, text):
            self.field += text

        def key(self, name):
            pass

    monkeypatch.setattr(ocr, "text_reader", lambda: Reader())
    monkeypatch.setattr(
        DesktopState, "capture",
        classmethod(lambda cls, name: cls(window_name=name, visible_text="", focused_element=None)),
    )
    host = Host()
    screen = DesktopWindow(name="screen", wid=-1, x=0, y=0, w=400, h=300, is_screen=True)
    screen._backend = host
    report = asyncio.run(dispatch._run_actions_desktop(screen, [
        TypeTextAction(text="calculator", clear_first=False),
        WaitForAction(text="calculator", timeout=800),
    ], None))
    assert "did not appear" in report, report


def test_a_text_wait_through_run_actions_reads_only_the_region(monkeypatch):
    """`key_press super` opens a launcher; `wait_for text=… region=…` OCRs just that rectangle of
    the screen target until the result row shows."""
    from galaius.actions import dispatch
    from galaius.state import DesktopState

    sizes: list[tuple[int, int]] = []

    class Reader(ocr.TextReader):
        def read(self, png: bytes) -> str:
            sizes.append(Image.open(io.BytesIO(png)).size)
            return "Notepad  App" if host.opened else ""

    class Host(PortableBackend):
        def __init__(self):
            self.opened = False

        def capture_region(self, x, y, w, h):
            return _card("", size=(w, h))

        def active_window(self):
            return None

        def key(self, name):
            self.opened = True

    monkeypatch.setattr(ocr, "text_reader", lambda: Reader())
    monkeypatch.setattr(
        DesktopState, "capture",
        classmethod(lambda cls, name: cls(window_name=name, visible_text="", focused_element=None)),
    )
    host = Host()
    screen = DesktopWindow(name="screen", wid=-1, x=0, y=0, w=800, h=600, is_screen=True)
    screen._backend = host
    report = asyncio.run(dispatch._run_actions_desktop(screen, [
        KeyPressAction(key="super"),
        WaitForAction(text="notepad", region=(100, 50, 300, 200), timeout=2000),
    ], None))
    assert "notepad" in report.lower() and "ERROR" not in report, report
    assert sizes and set(sizes) == {(300, 200)}, sizes


# --- the real engine of this OS ------------------------------------------------------------------


def _engine_or_skip() -> ocr.TextReader:
    try:
        return ocr.text_reader()
    except ocr.NoTextReader as exc:
        if os.environ.get("GALAIUS_LIVE_DESKTOP") == "1":
            raise  # CI installs / ships an engine on every OS: missing is a failure there
        pytest.skip(str(exc))


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_this_os_engine_reads_a_launcher_row(dark):
    reader = _engine_or_skip()
    seen = reader.read(_card("Search results for whispering", dark=dark))
    assert text_matches("whispering", seen), f"{type(reader).__name__} on {sys.platform} read {seen!r}"
    assert not text_matches("Notepad", seen)


def test_mac_active_window_is_the_launcher_panel_above_normal_windows():
    """CGWindowList is front-to-back; the Dock and the menu bar are chrome, Spotlight's panel sits
    above the frontmost app's window."""
    from galaius.desktop.host import MacDesktop

    rows = [
        {"kCGWindowNumber": 1, "kCGWindowLayer": 25, "kCGWindowOwnerName": "Window Server",
         "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 1440, "Height": 24}},
        {"kCGWindowNumber": 2, "kCGWindowLayer": 20, "kCGWindowOwnerName": "Dock",
         "kCGWindowBounds": {"X": 0, "Y": 800, "Width": 1440, "Height": 100}},
        {"kCGWindowNumber": 3, "kCGWindowLayer": 3, "kCGWindowOwnerName": "Spotlight",
         "kCGWindowBounds": {"X": 380, "Y": 200, "Width": 680, "Height": 400}},
        {"kCGWindowNumber": 4, "kCGWindowLayer": 0, "kCGWindowOwnerName": "Finder", "kCGWindowName": "Home",
         "kCGWindowBounds": {"X": 0, "Y": 24, "Width": 900, "Height": 600}},
    ]
    active = MacDesktop.topmost_from_quartz(rows)
    assert (active.handle, active.title, active.w) == (3, "Spotlight", 680)
    assert MacDesktop.topmost_from_quartz(rows[3:]).title == "Home — Finder"


def test_separate_reads_never_join_into_one_match():
    """Two OCR passes over one frame are returned form-feed separated: a word ending one pass and a
    word starting the next are not neighbours on screen."""
    assert not text_matches("Results for", "results\ffor")
    assert text_matches("Results for", "noise\fResults for whispering")
