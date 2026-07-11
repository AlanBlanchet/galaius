"""launch_app display sizing: a phone/tablet app needs a correctly-shaped nested screen, not the
1280x800 desktop default. A `size`/`device` picks the resolution, and asking for a new size
respawns the shared sandbox (the first app's size no longer wins forever)."""

import pytest

from interact import server as srv


@pytest.mark.parametrize(
    "size, device, expected",
    [
        ("412x915", None, "412x915"),
        ("412X915", None, "412x915"),  # uppercase X normalized
        (" 800x600 ", None, "800x600"),  # trimmed
        (None, "phone", "412x915"),
        (None, "tablet", "820x1180"),
        (None, "desktop", "1280x800"),
        (None, "PHONE", "412x915"),  # case-insensitive device
        ("412x915", "desktop", "412x915"),  # explicit size wins over device
        (None, None, None),  # neither → caller keeps the configured default
    ],
)
def test_resolve_nested_size_picks_the_right_shape(size, device, expected):
    resolved, err = srv._resolve_nested_size(size, device)
    assert err is None
    assert resolved == expected


@pytest.mark.parametrize("size, device", [("nonsense", None), ("1280", None), (None, "watch")])
def test_resolve_nested_size_rejects_bad_input(size, device):
    resolved, err = srv._resolve_nested_size(size, device)
    assert resolved is None
    assert err and err.startswith("ERROR")


@pytest.fixture
def _restore_sandbox():
    saved = srv.sandbox._sandbox
    srv.sandbox._sandbox = None
    yield
    srv.sandbox._sandbox = saved


def test_get_sandbox_respawns_only_on_size_change(monkeypatch, _restore_sandbox):
    created: list[str] = []

    class FakeBackend:
        def __init__(self, display, size, headless=False):
            self.size = size
            self._alive = True
            created.append(size)

        def is_alive(self):
            return self._alive

        def touch(self):
            pass

        def close(self):
            self._alive = False

    monkeypatch.setattr("interact.desktop.backend.NestedBackend", FakeBackend)

    b1 = srv._get_sandbox("412x915")
    assert b1.size == "412x915"
    assert srv._get_sandbox("412x915") is b1  # same size → reuse, no respawn
    b3 = srv._get_sandbox("800x600")  # different size → respawn
    assert b3 is not b1
    assert b3.size == "800x600"
    assert created == ["412x915", "800x600"]


def test_get_sandbox_without_size_never_resizes_a_running_sandbox(monkeypatch, _restore_sandbox):
    """A phone/tablet sandbox must survive every attach/capture tool. Those reach it via
    _resolve_nested_target → _get_sandbox() with NO size; collapsing None to the 1280x800 default
    used to respawn the phone sandbox landscape on the FIRST screenshot/run_actions — the user saw
    the window open portrait, close, and reopen landscape, then the tools reported an empty sandbox.
    size=None means "attach to whatever is running", never resize."""
    created: list[str] = []

    class FakeBackend:
        def __init__(self, display, size, headless=False):
            self.size = size
            self._alive = True
            created.append(size)

        def is_alive(self):
            return self._alive

        def touch(self):
            pass

        def close(self):
            self._alive = False

    monkeypatch.setattr("interact.desktop.backend.NestedBackend", FakeBackend)
    monkeypatch.setattr(srv.config, "nested_size", "1280x800")

    phone = srv._get_sandbox("412x915")  # launch_app(device="phone")
    assert phone.size == "412x915"
    # screenshot / run_actions / get_interactive_elements all attach with size=None:
    assert srv._get_sandbox() is phone  # must NOT respawn at the 1280x800 default
    assert phone.is_alive()
    assert created == ["412x915"]  # exactly one server — never a landscape respawn


def test_get_sandbox_without_size_creates_at_default_when_none_running(monkeypatch, _restore_sandbox):
    """With no sandbox yet, size=None still creates one at the configured default — the attach
    path must work cold, it just must not RESIZE an existing one."""
    created: list[str] = []

    class FakeBackend:
        def __init__(self, display, size, headless=False):
            self.size = size
            self._alive = True
            created.append(size)

        def is_alive(self):
            return self._alive

        def touch(self):
            pass

        def close(self):
            self._alive = False

    monkeypatch.setattr("interact.desktop.backend.NestedBackend", FakeBackend)
    monkeypatch.setattr(srv.config, "nested_size", "1280x800")

    b = srv._get_sandbox()
    assert b.size == "1280x800"
    assert created == ["1280x800"]
