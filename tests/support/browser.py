"""Reaching a browser from a test, the same way everywhere."""

from __future__ import annotations

import pytest

from interact.browser import BrowserManager
from interact.config import Config
from interact.state import InteractiveElement


def interactive_element(
    index: int = 1,
    ref: str | None = None,
    *,
    role: str = "button",
    name: str = "",
    x: int = 0,
    y: int = 0,
    w: int = 10,
    h: int = 10,
) -> InteractiveElement:
    """A DOM-scanned element with the usual defaults — four files each rebuilt this shape
    (``_el`` in three, ``_make_element`` in the fourth) with drifted field names and positions.
    A caller only names what its scenario needs."""
    return InteractiveElement(index=index, ref=ref, role=role, name=name, x=x, y=y, w=w, h=h)


def browser_config(**overrides) -> Config:
    """A headless-chromium `Config`. `overrides` are merged rather than passed alongside the
    defaults as literal keywords, so overriding `browser_type` itself doesn't collide with the
    default `browser_type="chromium"` keyword already in the call. Some tests need the bare
    `Config` (`SessionRegistry`, or a `BrowserManager` built without going through
    `browser_manager`'s own default `session_id`)."""
    return Config(**{"headless": True, "browser_type": "chromium", **overrides})


def browser_manager(session_id: str = "default", **overrides) -> BrowserManager:
    """A headless chromium manager, composed from `browser_config`. `session_id` reaches
    `BrowserManager` itself (its own default, `"default"`) — a persistent-profile test needs a
    real name so two managers land in two distinct `<base>/<session_id>` subdirs.

    A unique id per call was tried and reverted: it did not stop a foreign page appearing in
    `_context.pages` (tests/test_tabs_and_refs.py, "Example" where the test opened "BBB") and it
    launches one browser per test, which on a loaded host cost a recording its video.
    """
    return BrowserManager(browser_config(**overrides), session_id)


async def ready_or_skip(manager: BrowserManager) -> None:
    """Start the browser, or skip: a bare machine with no provisioned browser must not turn the
    whole suite red, and the skip has to say why."""
    try:
        await manager.ensure_ready()
    except Exception as exc:  # no browser provisioned (bare CI)
        pytest.skip(f"no browser available: {exc}")
