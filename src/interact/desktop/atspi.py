"""AT-SPI accessibility tree integration for desktop element detection.

Electron apps (VS Code, Chrome, etc.) do not expose AT-SPI nodes by default.
Launch with ``--force-renderer-accessibility`` or set the env var
``ACCESSIBILITY_ENABLED=1`` for the tree to be populated.

For VS Code specifically, enable ``accessibility.verbosity.*`` settings
so that additional ARIA labels appear in the accessibility tree.
"""

import logging
from typing import ClassVar

from interact.desktop.window import (
    CoordTransform,
    DesktopElement,
    DesktopWindow,
)

_log = logging.getLogger("interact")

try:
    import gi  # noqa: PLC0415 — optional native dep (PyGObject/AT-SPI)

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi as _Atspi  # noqa: PLC0415 — optional native dep
except (ImportError, ValueError):
    _Atspi = None


class AtSpi:
    """Namespace class wrapping libatspi access for desktop a11y queries."""

    _WM_APPS: ClassVar[frozenset[str]] = frozenset(
        {"mutter-x11-frames", "marco", "metacity"}
    )

    _INTERACTIVE_ROLE_NAMES: ClassVar[frozenset[str]] = frozenset(
        {
            "push button",
            "toggle button",
            "check box",
            "radio button",
            "combo box",
            "text",
            "entry",
            "password text",
            "spin button",
            "slider",
            "link",
            "menu item",
            "check menu item",
            "radio menu item",
            "page tab",
        }
    )

    _CONTAINER_ROLES: ClassVar[frozenset[str]] = frozenset({"panel", "filler"})

    _MAX_TEXT_NODES: ClassVar[int] = 2000

    @staticmethod
    def available() -> bool:
        return _Atspi is not None

    @classmethod
    def _find_app_for_window(cls, window_name: str):
        """Find AT-SPI application containing a window matching the name."""
        desktop_obj = _Atspi.get_desktop(0)
        hint = window_name.lower()
        for i in range(desktop_obj.get_child_count()):
            app = desktop_obj.get_child_at_index(i)
            if app is None:
                continue
            app_name = (app.get_name() or "").lower()
            if app_name in cls._WM_APPS:
                continue
            if app_name and (app_name in hint or hint in app_name):
                return app
            for j in range(app.get_child_count()):
                frame = app.get_child_at_index(j)
                if frame is None:
                    continue
                frame_name = (frame.get_name() or "").lower()
                if frame_name and (frame_name in hint or hint in frame_name):
                    return app
        return None

    @staticmethod
    def _has_actions(node) -> bool:
        """Check if node has AT-SPI actions (drag, click, etc.)."""
        try:
            action_iface = node.get_action_iface()
            if action_iface and action_iface.get_n_actions() > 0:
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _child_label_name(node) -> str:
        """Get name from first child with role 'label', checking up to 3 children."""
        try:
            n = min(node.get_child_count(), 3)
            for i in range(n):
                child = node.get_child_at_index(i)
                if child is None:
                    continue
                try:
                    if child.get_role_name() == "label":
                        label_name = child.get_name() or ""
                        if label_name:
                            return label_name
                except Exception:
                    continue
        except Exception:
            pass
        return ""

    @classmethod
    def _collect_elements(cls, root, coord_type) -> list[DesktopElement]:
        """Walk AT-SPI tree iteratively, collecting interactive elements."""
        elements: list[DesktopElement] = []
        stack = [root]
        while stack:
            node = stack.pop()
            try:
                role_name = node.get_role_name()
                name = node.get_name() or ""
            except Exception:
                continue
            include = False
            if role_name in cls._INTERACTIVE_ROLE_NAMES:
                include = True
            elif cls._has_actions(node):
                include = True
            elif role_name in cls._CONTAINER_ROLES:
                component = node.get_component_iface()
                if component:
                    try:
                        extents = component.get_extents(coord_type)
                        w, h = extents.width, extents.height
                    except Exception:
                        w, h = 0, 0
                    if 10 <= w and 10 <= h <= 200:
                        label = cls._child_label_name(node)
                        if label:
                            include = True
                            if not name:
                                name = label
            if include:
                component = node.get_component_iface()
                if component:
                    try:
                        extents = component.get_extents(coord_type)
                        x, y, w, h = (
                            extents.x,
                            extents.y,
                            extents.width,
                            extents.height,
                        )
                    except Exception:
                        x, y, w, h = 0, 0, 0, 0
                    if (
                        w > 0
                        and h > 0
                        and (x + w) > 0
                        and (y + h) > 0
                        and (name or role_name)
                    ):
                        elements.append(
                            DesktopElement(
                                index=len(elements) + 1,
                                x=max(0, x),
                                y=max(0, y),
                                w=w,
                                h=h,
                                role=role_name,
                                name=name,
                            )
                        )
            try:
                n = node.get_child_count()
                for i in range(n - 1, -1, -1):
                    child = node.get_child_at_index(i)
                    if child:
                        stack.append(child)
            except Exception:
                pass
        return elements

    @staticmethod
    def _apply_frame_offsets(
        elements: list[DesktopElement], window_name: str
    ) -> list[DesktopElement]:
        win = DesktopWindow.find(window_name)
        if not win:
            return elements
        if CoordTransform.has(win.wid):
            offsets = CoordTransform.get(win.wid)
        else:
            offsets = CoordTransform.from_xprop(win.wid)
            CoordTransform.store(win.wid, offsets)
        return [el.translate(0, offsets.decoration_top) for el in elements]

    @classmethod
    def detect_elements(cls, window_name: str) -> list[DesktopElement] | None:
        """Detect interactive elements via AT-SPI accessibility tree.

        Returns None if AT-SPI is unavailable or no elements found.
        """
        if _Atspi is None:
            return None
        app = cls._find_app_for_window(window_name)
        if app is None:
            return None
        elements = cls._collect_elements(app, _Atspi.CoordType.WINDOW)
        if not elements:
            return None
        elements = cls._apply_frame_offsets(elements, window_name)
        for i, el in enumerate(elements):
            el.index = i + 1
        return elements

    @classmethod
    def find_element_by_name(
        cls, window_name: str, selector: str
    ) -> DesktopElement | None:
        """Find a single element matching the selector by name or role."""
        if _Atspi is None:
            return None
        app = cls._find_app_for_window(window_name)
        if app is None:
            return None
        elements = cls._collect_elements(app, _Atspi.CoordType.WINDOW)
        elements = cls._apply_frame_offsets(elements, window_name)
        hint = selector.lower()
        for el in elements:
            if el.name.lower() == hint:
                return el
        for el in elements:
            if hint in el.name.lower() or hint in el.role.lower():
                return el
        return None

    @classmethod
    def find_element(
        cls, window_name: str, *, name: str, role: str | None = None
    ) -> DesktopElement | None:
        """Find a single element by name substring, optionally filtered by role.

        Checks the desktop element cache first, falls back to AT-SPI detection.
        Raises ValueError if multiple elements match.
        """
        win = DesktopWindow.find(window_name)
        elements = DesktopElement.cached(win.wid) if win else None
        if not elements:
            elements = cls.detect_elements(window_name)
        if not elements:
            return None

        hint = name.lower()
        matches = [e for e in elements if hint in e.name.lower()]
        if role:
            role_hint = role.lower()
            matches = [e for e in matches if role_hint in e.role.lower()]

        if len(matches) == 1:
            return matches[0]
        if not matches:
            _log.debug(
                "find_element: no match for '%s' among %d elements: %s",
                name,
                len(elements),
                [e.name for e in elements[:10]],
            )
            return None
        desc = ", ".join(f"'{e.name}' ({e.role})" for e in matches[:5])
        raise ValueError(f"Ambiguous match for '{name}': {desc}")

    @classmethod
    def window_text(cls, window_name: str) -> str:
        """Collect all visible text from the AT-SPI tree for a window."""
        if _Atspi is None:
            return ""
        app = cls._find_app_for_window(window_name)
        if app is None:
            return ""
        texts: list[str] = []
        stack = [app]
        visited = 0
        while stack:
            if visited >= cls._MAX_TEXT_NODES:
                break
            visited += 1
            node = stack.pop()
            try:
                name = node.get_name() or ""
            except Exception:
                continue
            if name:
                texts.append(name)
            try:
                text_iface = node.get_text_iface()
                if text_iface:
                    count = text_iface.get_character_count()
                    if count > 0:
                        content = text_iface.get_text(0, count)
                        if content and content != name:
                            texts.append(content)
            except Exception:
                pass
            try:
                n = node.get_child_count()
                for i in range(n - 1, -1, -1):
                    child = node.get_child_at_index(i)
                    if child:
                        stack.append(child)
            except Exception:
                pass
        return "\n".join(texts)

    @classmethod
    def focused_element(cls, window_name: str) -> str | None:
        """Find the deepest focused element in the window's AT-SPI tree."""
        if _Atspi is None:
            return None
        app = cls._find_app_for_window(window_name)
        if app is None:
            return None
        result: str | None = None
        stack = [app]
        while stack:
            node = stack.pop()
            try:
                state_set = node.get_state_set()
                if state_set and state_set.contains(_Atspi.StateType.FOCUSED):
                    role_name = node.get_role_name()
                    name = node.get_name() or ""
                    result = f"{role_name}: {name}"
            except Exception:
                pass
            try:
                n = node.get_child_count()
                for i in range(n - 1, -1, -1):
                    child = node.get_child_at_index(i)
                    if child:
                        stack.append(child)
            except Exception:
                pass
        return result
