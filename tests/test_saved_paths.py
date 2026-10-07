"""#120: a caller-supplied output `path` must land where the CALLER can find it, and the tool must
SAY where.

Every `path` a tool accepts funnels into `core._save_to_path`, which used to be a bare
`Path(path).write_bytes` — a RELATIVE path landed against the MCP server's cwd (whatever the editor
started it with, invisible to the agent), and the tool then said nothing about the location or
echoed the caller's own relative string back ("Saved to clip.webm."). One rule now, for every tool:
`~` expands, an absolute path is kept, a relative path lands under `config.debug_dir`
(~/.galaius/out, where every other galaius artifact already lives) — and the reply names the
absolute file written plus its size."""

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import galaius.server as srv
from galaius.browser import BrowserManager
from galaius.desktop import DesktopWindow
from galaius.runtime import config
from galaius.vision import VLMResult
from tests.support import varied_png

_DATA = b"MP4DATA"

# (what the caller passes, where it must land) — the three shapes of #120 plus a nested relative
# one, templated over the sandboxed HOME / output dir / tmp root.
_SHAPES = [
    ("~/x.webm", "{home}/x.webm"),                 # ~ expands
    ("{tmp}/abs/x.webm", "{tmp}/abs/x.webm"),      # absolute: kept as given
    ("clip.webm", "{out}/clip.webm"),              # relative: galaius's output dir, not the cwd
    ("clips/clip.webm", "{out}/clips/clip.webm"),  # relative with a subdir: parents created
]


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """HOME, galaius's output dir and the server's cwd all relocated under tmp: the tests never
    touch the real home, and a write that still lands cwd-relative shows up as a MISSING file, not
    a pass."""
    home, out, cwd = tmp_path / "home", tmp_path / "out", tmp_path / "server-cwd"
    home.mkdir()
    cwd.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows' home (Path.home, ~)
    monkeypatch.setattr(config, "debug_dir", out)
    monkeypatch.chdir(cwd)
    return {"home": home, "out": out, "tmp": tmp_path, "cwd": cwd}


def _note(dest: Path, data: bytes) -> str:
    """The one sentence every saving tool must end with."""
    return f"Saved to {dest} ({len(data)} bytes)"


def _vlm_returning(text: str):
    async def fake(*_a, **_kw):
        return VLMResult(text=text, elapsed=0.1, model="m")

    return fake


@pytest.mark.parametrize("given, expected", _SHAPES)
def test_save_to_path_writes_at_the_resolved_location_and_returns_it(sandbox, given, expected):
    given, expected = given.format(**sandbox), Path(expected.format(**sandbox))
    dest = srv.core._save_to_path(given, _DATA)
    assert dest == expected and dest.is_absolute()
    assert expected.read_bytes() == _DATA
    assert not list(sandbox["cwd"].iterdir()), "nothing may land in the server's cwd"


# --- the tools: each names the ABSOLUTE file it wrote, on every exit that wrote one -------------


@pytest.fixture
def rec_win():
    win = MagicMock(spec=DesktopWindow)
    win.name, win.w, win.h = "aino", 412, 780
    win.capture_video.return_value = _DATA
    return win


@pytest.fixture
def recording_metadata(monkeypatch):
    monkeypatch.setattr(srv.tools_desktop, "_record_metadata", AsyncMock(return_value=(5.0, 2.0)))
    monkeypatch.setattr(srv.tools_desktop, "sample_video_frames", AsyncMock(return_value=[]))


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [None, "what animates?"])
async def test_record_desktop_names_the_absolute_file_it_wrote(
    sandbox, rec_win, recording_metadata, monkeypatch, query
):
    """Capture-only and analyzed recordings retain their typed artifact path."""
    monkeypatch.setattr(srv.vlm, "_vlm", _vlm_returning("a token slides in"))
    out = await srv.tools_desktop._record_desktop(
        rec_win, query=query, start=True, duration=2.0, fps=None, path="clip.mp4"
    )
    dest = sandbox["out"] / "clip.mp4"
    assert out.capture.status == "captured"
    assert out.capture.artifact == str(dest) and dest.is_absolute()
    assert dest.read_bytes() == _DATA
    assert out.analysis.text == ("a token slides in" if query else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [None, "what animates?"])
async def test_record_browser_names_the_absolute_file_it_wrote(sandbox, recording_metadata, monkeypatch, query):
    """With and without a query, the typed result points to the saved bytes."""
    mgr = MagicMock(spec=BrowserManager)
    mgr.recording_requested_fps = None
    mgr.stop_recording.return_value = b"WEBM"
    monkeypatch.setattr(srv.vlm, "_vlm", _vlm_returning("a token slides in"))
    out = await srv.tools_desktop._record_browser(
        mgr, start=False, query=query, path="clip.webm", session="default"
    )
    dest = sandbox["out"] / "clip.webm"
    assert out.capture.status == "captured"
    assert out.capture.artifact == str(dest) and dest.is_absolute()
    assert dest.read_bytes() == b"WEBM"
    assert "Reacquire refs" in out.analysis.text
    assert ("a token slides in" in out.analysis.text) == bool(query)


@pytest.mark.asyncio
async def test_download_asset_names_the_absolute_file_it_wrote(sandbox, monkeypatch):
    mgr = MagicMock(spec=BrowserManager)
    mgr.drain_recovery_notes.return_value = []
    response = MagicMock()
    response.body = AsyncMock(return_value=b"%PDF")
    mgr.get_page.return_value.context.request.get = AsyncMock(return_value=response)
    monkeypatch.setattr(srv.core._sessions, "get", lambda s: mgr)
    out = await srv.download_asset("https://example.test/a.pdf", "docs/a.pdf")
    dest = sandbox["out"] / "docs" / "a.pdf"
    assert _note(dest, b"%PDF") in out and dest.read_bytes() == b"%PDF"


@pytest.mark.asyncio
@pytest.mark.parametrize("preexisting", [False, True])
async def test_screenshot_names_the_absolute_file_it_wrote(sandbox, monkeypatch, preexisting):
    """Browser capture branch: the reply names the file; an overwrite is still announced (#44), and
    that check looks at the RESOLVED location — not at a cwd-relative one that never existed."""
    png = varied_png()
    dest = sandbox["out"] / "shots" / "page.png"
    if preexisting:
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"OLD")
    state = SimpleNamespace(
        screenshot_base64=base64.b64encode(png).decode(), text_summary=lambda: "page"
    )
    monkeypatch.setattr(srv.targets, "_resolve_target", lambda *a, **k: (None, object(), None))
    monkeypatch.setattr(srv.capture, "_capture", AsyncMock(return_value=state))
    monkeypatch.setattr(srv.capture, "_scan_elements", AsyncMock(return_value=[]))
    out = await srv.screenshot(target="browser", path="shots/page.png")
    assert _note(dest, png) in out and dest.read_bytes() == png
    assert ("overwrote existing file" in out) is preexisting


@pytest.mark.asyncio
async def test_measure_ui_names_the_absolute_file_it_wrote(sandbox, monkeypatch):
    png = varied_png()
    captured = AsyncMock(return_value=(png, "Page: Home", None, None, None))
    monkeypatch.setattr(srv.capture, "_capture_or_file", captured)
    out = await srv.measure_ui(path="measure.png")
    dest = sandbox["out"] / "measure.png"
    assert _note(dest, png) in out and dest.read_bytes() == png


@pytest.mark.asyncio
async def test_session_save_and_load_share_the_same_resolved_path(sandbox, monkeypatch):
    """A relative session-state path round-trips: save and load resolve it by the one rule, and
    both replies name the absolute file."""
    mgr = MagicMock(spec=BrowserManager)
    mgr.save_state.return_value = {"cookies": ["c"]}
    monkeypatch.setattr(srv.core._sessions, "get", lambda s: mgr)
    dest = sandbox["out"] / "sessions" / "s1.json"
    out = await srv.session("save", name="s1", path="sessions/s1.json")
    assert str(dest) in out and json.loads(dest.read_text()) == {"cookies": ["c"]}
    out = await srv.session("load", name="s1", path="sessions/s1.json")
    assert str(dest) in out
    mgr.load_state.assert_awaited_once_with({"cookies": ["c"]})


def test_a_per_call_debug_dir_follows_the_same_rule(sandbox):
    """`Debug.dump_dir("run1")` resolved against the server's cwd — the class of #120 with one
    more door in: ~ expands, an absolute dir is kept, a RELATIVE one lands under the output dir."""
    from galaius.debug_utils import Debug

    assert Debug.dump_dir("run1") == sandbox["out"] / "run1"
    assert Debug.dump_dir("~/dumps") == sandbox["home"] / "dumps"
    assert Debug.dump_dir(str(sandbox["tmp"] / "abs")) == sandbox["tmp"] / "abs"


@pytest.mark.asyncio
async def test_review_ui_names_the_absolute_file_it_wrote(sandbox, monkeypatch):
    """review_ui / verify_ui saved through the shared capture path and said nothing about where."""
    png = varied_png()
    monkeypatch.setattr(srv.capture, "_capture_or_file", AsyncMock(return_value=(png, "Page: Home", None, None, None)))
    monkeypatch.setattr(srv.vlm, "_vlm", _vlm_returning("[]"))
    out = await srv.review_ui(path="review.png")
    dest = sandbox["out"] / "review.png"
    assert _note(dest, png) in out and dest.read_bytes() == png


def test_the_resolved_dump_records_an_unresolvable_model_instead_of_failing_the_call(monkeypatch):
    """The dump is diagnostics written BEFORE the call; a machine with no vision model must reach
    the call, which reports that itself, instead of crashing on the dump."""
    def _nothing(model_override, role):
        raise RuntimeError("no model available for role 'image'")

    monkeypatch.setattr(srv.vlm, "_effective_model", _nothing)
    resolved = srv.vlm._resolved_config(None, "image")
    assert resolved["effective_model"] is None
    assert "no model available" in resolved["effective_model_error"]
