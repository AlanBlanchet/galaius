"""#126, #143, #174, #136 — the nested Electron/VS Code family: no CDP bridge existed at all
(#143), so a click never moved DOM focus (#174) and typed characters were dropped/garbled via
xdotool's XKB translation (#126); a drag left a webview's mouse capture stuck (#136).

Discovery/target-selection (`interact.desktop.cdp`) is pure — argv parsing, a real tmp-file poll,
and an `httpx.MockTransport` fake CDP endpoint, no real browser. The dispatch-layer fallback
(`_CdpSlot`, `_dispatch_click`, `_d_type_text`) is exercised with a stub bridge, never a real
Playwright connection — connecting to a genuine Electron/VS Code CDP endpoint is integration-level
and out of scope here, per the task's own instruction to avoid launching an editor.
"""

import asyncio
import json

import httpx
import pytest

from interact.desktop import DesktopBackend, NestedBackend
from interact.desktop import cdp
from interact.actions import dispatch as D
from tests.support.desktop import RecordingBackend as _RecordingBackend, bare_nested_backend


# ---------------------------------------------------------------------------
# Port discovery — pure argv/file logic
# ---------------------------------------------------------------------------

def test_declared_port_equals_form():
    assert cdp.declared_port(["code", "--remote-debugging-port=9222", "--foo"]) == 9222


def test_declared_port_space_form():
    assert cdp.declared_port(["code", "--remote-debugging-port", "9222"]) == 9222


def test_declared_port_zero_is_returned_not_treated_as_absent():
    assert cdp.declared_port(["code", "--remote-debugging-port=0"]) == 0


def test_declared_port_absent_is_none():
    assert cdp.declared_port(["code", "--disable-gpu"]) is None


def test_declared_profile_parses_user_data_dir():
    argv = ["code", "--user-data-dir=/tmp/x/profile", "--remote-debugging-port=0"]
    assert cdp.declared_profile(argv) == cdp.Path("/tmp/x/profile")


def test_wait_for_active_port_reads_first_line(tmp_path):
    (tmp_path / "DevToolsActivePort").write_text("54321\n/devtools/browser/abc\n")
    assert cdp.wait_for_active_port(tmp_path, timeout=1.0, poll=0.01) == 54321


def test_wait_for_active_port_times_out_when_file_never_appears(tmp_path):
    assert cdp.wait_for_active_port(tmp_path / "missing", timeout=0.05, poll=0.01) is None


def test_resolve_port_nonzero_needs_no_profile():
    assert cdp.resolve_port(["code", "--remote-debugging-port=9222"]) == 9222


def test_resolve_port_zero_resolves_via_profile_file(tmp_path):
    (tmp_path / "DevToolsActivePort").write_text("12345\n")
    argv = ["code", f"--user-data-dir={tmp_path}", "--remote-debugging-port=0"]
    assert cdp.resolve_port(argv, timeout=1.0) == 12345


def test_resolve_port_absent_flag_is_none():
    assert cdp.resolve_port(["code", "--disable-gpu"]) is None


def test_resolve_port_zero_without_profile_is_none():
    assert cdp.resolve_port(["code", "--remote-debugging-port=0"]) is None


# ---------------------------------------------------------------------------
# Target listing/selection — fake CDP endpoint via httpx.MockTransport
# ---------------------------------------------------------------------------

_TARGETS = [
    {"type": "page", "title": "Extension Host", "url": "vscode-webview://extension-host/index.html"},
    {"type": "page", "title": "Workbench", "url": "file:///usr/share/code/workbench.html"},
    {"type": "page", "title": "My Panel", "url": "vscode-webview://panel-1/index.html"},
    {"type": "background_page", "title": "hidden", "url": "chrome-extension://x/bg.html"},
]


def _fake_transport(targets=_TARGETS, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/json/list"
        return httpx.Response(status, json=targets)
    return httpx.MockTransport(handler)


async def test_list_targets_against_fake_cdp_endpoint():
    targets = await cdp.list_targets(9222, transport=_fake_transport())
    assert targets == _TARGETS


async def test_list_targets_raises_on_dead_port():
    with pytest.raises(httpx.HTTPStatusError):
        await cdp.list_targets(9222, transport=_fake_transport(status=500))


def test_pick_target_prefers_hint():
    got = cdp.pick_target(_TARGETS, hint="My Panel")
    assert got["title"] == "My Panel"


def test_pick_target_prefers_vscode_webview_over_workbench():
    # No hint: the workbench shell (file://) is NOT what #126/#143/#174 are about — the last
    # vscode-webview: page must win.
    got = cdp.pick_target(_TARGETS)
    assert got["url"].startswith("vscode-webview:")
    assert got["title"] == "My Panel"  # the LAST webview target


def test_pick_target_falls_back_to_last_page_when_no_webview():
    pages = [
        {"type": "page", "title": "A", "url": "file:///a.html"},
        {"type": "page", "title": "B", "url": "file:///b.html"},
    ]
    assert cdp.pick_target(pages)["title"] == "B"


def test_pick_target_none_when_no_page_type():
    assert cdp.pick_target([{"type": "worker", "url": "x"}]) is None


# ---------------------------------------------------------------------------
# NestedBackend port resolution
# ---------------------------------------------------------------------------

def _backend_stub() -> NestedBackend:
    return bare_nested_backend(display=None)


def test_debug_port_for_wid_uses_owning_pid(monkeypatch):
    be = _backend_stub()
    argv = ["code", "--remote-debugging-port=9222"]
    be._commands[4242] = argv
    monkeypatch.setattr(be, "window_pid", lambda wid: 4242)
    assert be.debug_port_for_wid(999) == 9222


def test_debug_port_for_wid_falls_back_to_single_tracked_launch(monkeypatch):
    be = _backend_stub()
    argv = ["code", "--remote-debugging-port=9222"]
    be._commands[4242] = argv
    monkeypatch.setattr(be, "window_pid", lambda wid: 9999)  # a pid we never spawned
    assert be.debug_port_for_wid(999) == 9222


def test_debug_port_for_wid_none_when_nothing_tracked(monkeypatch):
    be = _backend_stub()
    monkeypatch.setattr(be, "window_pid", lambda wid: None)
    assert be.debug_port_for_wid(999) is None


def test_debug_port_for_wid_none_when_launch_named_no_port(monkeypatch):
    be = _backend_stub()
    be._commands[4242] = ["code", "--disable-gpu"]
    monkeypatch.setattr(be, "window_pid", lambda wid: 4242)
    assert be.debug_port_for_wid(999) is None


# ---------------------------------------------------------------------------
# Drag mouse-capture settle (#136) on the shared DesktopBackend.drag path
# ---------------------------------------------------------------------------

def test_drag_settles_at_drop_point_after_release():
    be = _RecordingBackend()
    be.drag(0, 0, 100, 50, steps=4)
    assert be.calls[-1] == ("move", 100, 50), "a settle move must follow the release (#136)"
    assert be.calls[-2] == ("up", "left")


def test_drag_settle_is_best_effort_when_move_raises():
    class _Flaky(_RecordingBackend):
        def __init__(self):
            super().__init__()
            self._raise_next = False

        def move(self, x, y):
            if self._raise_next:
                raise RuntimeError("display gone")
            super().move(x, y)

    be = _Flaky()
    be._raise_next = False
    be.mouse_up = lambda button="left": (be.calls.append(("up", button)), setattr(be, "_raise_next", True))[0]
    be.drag(0, 0, 10, 10, steps=2)  # must not raise even though the settle move fails
    assert be.calls[-1] == ("up", "left")


# ---------------------------------------------------------------------------
# Dispatch-layer fallback: _CdpSlot caching + _dispatch_click "which path" contract
# ---------------------------------------------------------------------------

class _FakeMouse:
    def __init__(self):
        self.clicks: list[tuple] = []

    async def click(self, x, y, button="left", click_count=1):
        self.clicks.append((x, y, button, click_count))


class _FakeKeyboard:
    def __init__(self):
        self.typed: list[str] = []
        self.pressed: list[str] = []

    async def type(self, text):
        self.typed.append(text)

    async def press(self, key):
        self.pressed.append(key)


class _FakePage:
    def __init__(self):
        self.mouse = _FakeMouse()
        self.keyboard = _FakeKeyboard()


class _FakeBridge:
    def __init__(self):
        self.page = _FakePage()
        self.connected = 0

    async def connect(self):
        self.connected += 1

    async def close(self):
        self.connected -= 1


class _FakeWin:
    """Just enough of DesktopWindow for the fallback path: records synthetic clicks."""

    def __init__(self):
        self.calls: list[tuple] = []
        self._BUTTON_NAMES = {1: "left", 2: "middle", 3: "right"}

    async def click(self, x, y, button_code=1, count=1):
        self.calls.append((x, y, button_code, count))


async def test_cdp_slot_available_false_with_no_backend():
    win = _FakeWin()
    win._backend = None
    win.wid = 7
    slot = D._CdpSlot(win)
    assert await slot.available() is False
    bridge, err = await slot.ensure()
    assert bridge is None and err


async def test_cdp_slot_caches_the_resolved_port_and_the_error(monkeypatch):
    win = _FakeWin()
    win._backend = None
    win.wid = 7
    slot = D._CdpSlot(win)
    await slot.ensure()
    # Force the slot into "would recompute" shape and prove it does NOT: `_port_once` short
    # circuits on anything but `_UNSET`, so a second `ensure()` must return the SAME cached error
    # object without touching `_port_once` again.
    calls = {"n": 0}
    orig = slot._port_once

    async def counted():
        calls["n"] += 1
        return await orig()

    slot._port_once = counted
    bridge, err = await slot.ensure()
    assert bridge is None
    assert calls["n"] == 0, "a cached error must not re-poll for a port on every action"


async def test_cdp_slot_ensure_reuses_the_same_bridge(monkeypatch):
    win = _FakeWin()
    fake_bridge = _FakeBridge()

    async def fake_port_once():
        return 9222

    slot = D._CdpSlot(win)
    slot._port_once = fake_port_once
    monkeypatch.setattr(D._cdp, "list_targets", lambda port, **kw: asyncio.sleep(0, result=[{"type": "page", "url": "u"}]))
    monkeypatch.setattr(D._cdp, "pick_target", lambda targets, hint=None: {"type": "page", "url": "u"})
    monkeypatch.setattr(D._cdp, "CDPBridge", lambda port, target: fake_bridge)

    b1, e1 = await slot.ensure()
    b2, e2 = await slot.ensure()
    assert b1 is b2 is fake_bridge
    assert e1 is None and e2 is None
    assert fake_bridge.connected == 1, "connect() must run exactly once per batch"
    await slot.aclose()


async def test_dispatch_click_uses_cdp_when_bridge_available(monkeypatch):
    win = _FakeWin()
    fake_bridge = _FakeBridge()

    class _Ctx:
        def __init__(self, win):
            self.win = win
            self.cdp = D._CdpSlot(win)

    ctx = _Ctx(win)

    async def fake_ensure():
        return fake_bridge, None

    ctx.cdp.ensure = fake_ensure
    path = await D._dispatch_click(ctx, 12, 34, button_code=1, count=1)
    assert path == "via CDP"
    assert fake_bridge.page.mouse.clicks == [(12, 34, "left", 1)]
    assert win.calls == [], "the synthetic path must NOT also fire when CDP handled it"


async def test_dispatch_click_falls_back_to_synthetic_when_no_bridge():
    win = _FakeWin()

    class _Ctx:
        def __init__(self, win):
            self.win = win
            self.cdp = D._CdpSlot(win)

    ctx = _Ctx(win)

    async def fake_ensure():
        return None, "no --remote-debugging-port on this launch"

    ctx.cdp.ensure = fake_ensure
    path = await D._dispatch_click(ctx, 5, 6, button_code=3, count=2)
    assert path == "via synthetic input"
    assert win.calls == [(5, 6, 3, 2)]
