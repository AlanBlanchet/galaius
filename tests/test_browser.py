from interact.browser import BrowserManager
from interact.config import LOG_MAXLEN, Config
from interact.state import InteractiveElement, ref_locator


def _el(index: int, ref: str | None = None) -> InteractiveElement:
    return InteractiveElement(
        index=index,
        ref=ref,
        role="button",
        name=f"btn{index}",
        x=0,
        y=0,
        width=10,
        height=10,
    )


# --- BrowserManager element map ---


def test_element_map_per_tab_isolation():
    mgr = BrowserManager(Config())
    mgr.set_element_map(0, [_el(1), _el(2)])
    mgr.set_element_map(1, [_el(3)])

    assert mgr.get_element(1, tab=0) is not None
    assert mgr.get_element(2, tab=0) is not None
    assert mgr.get_element(3, tab=0) is None
    assert mgr.get_element(3, tab=1) is not None
    assert mgr.get_element(1, tab=1) is None


def test_element_map_tab_overwrite():
    mgr = BrowserManager(Config())
    mgr.set_element_map(0, [_el(1)])
    mgr.set_element_map(0, [_el(2)])
    assert mgr.get_element(1, tab=0) is None
    assert mgr.get_element(2, tab=0) is not None


def test_get_element_missing_tab():
    mgr = BrowserManager(Config())
    assert mgr.get_element(1, tab=5) is None


def test_get_element_default_tab():
    mgr = BrowserManager(Config())
    mgr.set_element_map(0, [_el(7)])
    assert mgr.get_element(7) is not None


# --- InteractiveElement ref ---


def test_element_ref_none_by_default():
    el = InteractiveElement(
        index=1, role="button", name="x", x=0, y=0, width=10, height=10
    )
    assert el.ref is None


def test_element_playwright_ref():
    el = InteractiveElement(
        index=1, ref="e42", role="button", name="x", x=0, y=0, width=10, height=10
    )
    assert el.playwright_ref == '[data-interact-ref="e42"]'


def test_element_center_coords():
    el = InteractiveElement(
        index=1, role="button", name="x", x=10, y=20, width=80, height=40
    )
    assert el.center_x == 50
    assert el.center_y == 40


# --- ref_locator ---


def test_ref_locator():
    assert ref_locator("e5") == '[data-interact-ref="e5"]'


# --- drain_network_log / drain_console_log ---


def test_drain_network_log_returns_entries():
    mgr = BrowserManager(Config())
    mgr._network_log.append({"method": "GET", "url": "https://example.com"})
    mgr._network_log.append({"method": "POST", "url": "https://example.com/api"})
    entries = mgr.drain_network_log()
    assert len(entries) == 2
    assert entries[0]["method"] == "GET"
    assert len(mgr._network_log) == 2  # not cleared


def test_drain_network_log_clear():
    mgr = BrowserManager(Config())
    mgr._network_log.append({"method": "GET", "url": "https://example.com"})
    entries = mgr.drain_network_log(clear=True)
    assert len(entries) == 1
    assert len(mgr._network_log) == 0


def test_drain_console_log_returns_entries():
    mgr = BrowserManager(Config())
    mgr._console_log.append({"level": "log", "text": "hello"})
    entries = mgr.drain_console_log()
    assert len(entries) == 1
    assert entries[0]["text"] == "hello"
    assert len(mgr._console_log) == 1  # not cleared


def test_drain_console_log_clear():
    mgr = BrowserManager(Config())
    mgr._console_log.append({"level": "error", "text": "oops"})
    entries = mgr.drain_console_log(clear=True)
    assert len(entries) == 1
    assert len(mgr._console_log) == 0


def test_log_deque_maxlen():
    mgr = BrowserManager(Config())
    assert mgr._network_log.maxlen == LOG_MAXLEN
    assert mgr._console_log.maxlen == LOG_MAXLEN


def test_is_recording_default_false():
    mgr = BrowserManager(Config())
    assert mgr.is_recording is False
