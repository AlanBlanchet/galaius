"""Detected desktop UI elements: the :class:`Box` geometry leaf, :class:`DesktopElement`
(merge/IOU dedup, ref caching per window), and the WM-titlebar-button filter."""

import json
import logging
import re
from typing import Self

from pydantic import BaseModel, computed_field

from interact.desktop.coords import CoordTransform
from interact.desktop.geometry import BBox, BoxArray
from interact.parsing import Parse
from interact.state import Element, InteractiveElement

_log = logging.getLogger("interact")
_IOU_OVERLAP_THRESHOLD = 0.3
_IOU_MERGE_MIN = 0.05
_IOU_MERGE_HIGH = 0.5
_MERGE_CENTER_DIST = 50
_MIN_DIM = 10
_MIN_DIM_BOTH = 15
_TITLEBAR_Y = 40


class Box(Element):
    @computed_field
    @property
    def center_x(self) -> int:
        return self.x + self.w // 2

    @computed_field
    @property
    def center_y(self) -> int:
        return self.y + self.h // 2

    @computed_field
    @property
    def x2(self) -> int:
        return self.x + self.w

    @computed_field
    @property
    def y2(self) -> int:
        return self.y + self.h

    def as_xyxy(self) -> tuple[float, float, float, float]:
        return (
            float(self.x),
            float(self.y),
            float(self.x + self.w),
            float(self.y + self.h),
        )

    def iou(self, other: Self) -> float:
        return BBox.from_xyxy(self.as_xyxy()).iou(BBox.from_xyxy(other.as_xyxy()))

    def clamp(self, img_w: int, img_h: int) -> Self | None:
        x2 = min(self.x + self.w, img_w)
        y2 = min(self.y + self.h, img_h)
        x, y = max(0, self.x), max(0, self.y)
        w, h = x2 - x, y2 - y
        if w <= 0 or h <= 0:
            return None
        return self.model_copy(update={"x": x, "y": y, "w": w, "h": h})

    def scale(self, sx: float, sy: float) -> Self:
        return self.model_copy(
            update={
                "x": round(self.x * sx),
                "y": round(self.y * sy),
                "w": round(self.w * sx),
                "h": round(self.h * sy),
            }
        )

    def translate(self, dx: int, dy: int) -> Self:
        return self.model_copy(update={"x": self.x + dx, "y": self.y + dy})

    def transform(self, coord: CoordTransform) -> Self:
        return self.scale(coord.scale_x, coord.scale_y).translate(
            coord.crop_x, coord.crop_y
        )


_element_cache: dict[int, list] = {}
_page_sig: dict[int, str] = {}  # last page signature (screenshot content hash) per wid → clear refs on change


class DesktopElement(Box):
    @classmethod
    def store(cls, wid: int, elements: list[Self]):
        _element_cache[wid] = elements

    @classmethod
    def invalidate(cls, wid: int) -> None:
        """Drop all cached refs + the page signature for a window, so the next detection starts empty
        and returns only the live frame's elements. The recovery path when a cache has gone stale or
        accumulated jittery duplicates across many detects (#57). Idempotent."""
        _element_cache.pop(wid, None)
        _page_sig.pop(wid, None)

    @classmethod
    def merge_into(cls, wid: int, elements: list[Self], signature: str) -> list[Self]:
        """Accumulate detections for a window across detect calls, keyed by a page
        ``signature`` (a content fingerprint of the screenshot — NOT the title, which is
        constant in single-window apps). Same screen → union with the existing refs (a
        second/targeted detect *adds* to what we already found). Screen changed → drop the
        now-stale refs first, since those elements are gone. Returns the full current set
        (re-indexed), which becomes the live ref table for this window.
        """
        if _page_sig.get(wid) != signature:
            _element_cache[wid] = []
            _page_sig[wid] = signature
        merged = cls.merge_keeping(_element_cache.get(wid, []), elements)
        _element_cache[wid] = merged
        return merged

    @classmethod
    def get_by_index(cls, wid: int, index: int) -> Self | None:
        for el in _element_cache.get(wid, []):
            if el.index == index:
                return el
        return None

    @classmethod
    def cached(cls, wid: int) -> list[Self] | None:
        return _element_cache.get(wid) or None

    @classmethod
    def cached_for(cls, wid: int, signature: str) -> list[Self] | None:
        """Cached refs ONLY if they were detected on the currently-displayed frame (its content
        ``signature`` matches the one stored when the refs were detected). After a navigation the
        live frame's signature differs, so the prior screen's refs are NOT surfaced — preventing a
        screenshot from listing refs for a screen that's no longer shown, and clicks landing on gone
        targets (#19)."""
        if _page_sig.get(wid) != signature:
            return None
        return _element_cache.get(wid) or None

    @staticmethod
    def ref_to_index(ref: str) -> int:
        return int(ref.removeprefix("e"))

    @classmethod
    def to_interactive(cls, elements: list[Self]) -> list[InteractiveElement]:
        return [
            InteractiveElement(
                index=e.index,
                role=e.role,
                name=e.name,
                x=e.x,
                y=e.y,
                w=e.w,
                h=e.h,
            )
            for e in elements
        ]

    @classmethod
    def format_list(cls, elements: list[Self]) -> str:
        # LLM-facing output: emit ref (index) + role/name only. Pixel coords
        # are an implementation detail of the click/hover dispatch and must
        # NOT leak into model context — agents reference elements by index.
        return "\n".join(f"  [{el.index}] {el.role}: {el.name!r}" for el in elements)

    @staticmethod
    def infer_role(label: str) -> str:
        for role, pattern in _ROLE_PATTERNS:
            if pattern.search(label):
                return role
        return "element"

    @classmethod
    def from_vlm_dict(cls, entry: dict, index: int) -> Self:
        return cls(
            index=index,
            x=int(entry["x"]),
            y=int(entry["y"]),
            w=int(entry["w"]),
            h=int(entry["h"]),
            role=str(entry.get("role", "element")),
            name=str(entry.get("name", "")),
        )

    @classmethod
    def parse_vlm(cls, response: str) -> list[Self] | None:
        raw = Parse.extract_json_array(response)
        if raw is None:
            raw = []
            for m in re.finditer(r"\{[^{}]+\}", response):
                try:
                    raw.append(json.loads(m.group()))
                except json.JSONDecodeError:
                    continue
        if not raw:
            return None
        elements = []
        for i, entry in enumerate(raw):
            try:
                elements.append(cls.from_vlm_dict(entry, i + 1))
            except (KeyError, ValueError, TypeError):
                continue
        return elements or None

    @classmethod
    def fuse(cls, vlm_els: list[Self], atspi_els: list[Self]) -> list[Self]:
        matched_atspi: set[int] = set()
        result = []
        if vlm_els and atspi_els:
            iou_mat = BoxArray.from_boxes(vlm_els).iou_matrix(
                BoxArray.from_boxes(atspi_els)
            )
        else:
            iou_mat = None
        for i, vel in enumerate(vlm_els):
            best_iou, best_idx, best_ael = 0.0, -1, None
            if iou_mat is not None:
                row = iou_mat[i]
                j = int(row.argmax())
                score = float(row[j])
                if score > best_iou:
                    best_iou, best_idx, best_ael = score, j, atspi_els[j]
            if best_ael and best_iou > _IOU_OVERLAP_THRESHOLD:
                matched_atspi.add(best_idx)
                result.append(
                    cls(
                        index=len(result) + 1,
                        x=best_ael.x,
                        y=best_ael.y,
                        w=best_ael.w,
                        h=best_ael.h,
                        role=vel.role,
                        name=vel.name or best_ael.name,
                    )
                )
            else:
                result.append(vel.model_copy(update={"index": len(result) + 1}))
        for j, ael in enumerate(atspi_els):
            if j not in matched_atspi:
                result.append(ael.model_copy(update={"index": len(result) + 1}))
        return result

    @classmethod
    def filter_junk(
        cls, elements: list[Self], titlebar_y: int = _TITLEBAR_Y
    ) -> list[Self]:
        filtered: list[Self] = []
        for el in elements:
            name = el.name.strip()
            if _JUNK_NAME_RE.match(name):
                continue
            if min(el.w, el.h) < _MIN_DIM:
                continue
            if el.w < _MIN_DIM_BOTH and el.h < _MIN_DIM_BOTH:
                continue
            if el.y < titlebar_y and name in _WM_BUTTON_NAMES:
                continue
            if _NUMERIC_NAME_RE.match(name):
                continue
            if len(name) == 1:
                continue
            if name in ("", "button", "element", el.role):
                el.name = f"unnamed at ({el.x},{el.y})"
            if el.role == "element":
                el.role = cls.infer_role(el.name)
            filtered.append(el)
        for i, el in enumerate(filtered):
            el.index = i + 1
        return filtered

    @classmethod
    def merge_keeping(cls, existing: list[Self], extra: list[Self]) -> list[Self]:
        """NMS-style union: append boxes from ``extra`` that don't overlap (IoU >
        threshold) anything already kept, then re-index. Used to fold in additional
        detection passes — region refinement and targeted re-prompts — without
        duplicating boxes the first pass already found.
        """
        merged = list(existing)
        for element in extra:
            if not any(element.iou(kept) > _IOU_OVERLAP_THRESHOLD for kept in merged):
                merged.append(element)
        for i, element in enumerate(merged):
            element.index = i + 1
        return merged

    @classmethod
    def merge_fragments(cls, elements: list[Self]) -> list[Self]:
        merged = list(elements)
        changed = True
        while changed:
            changed = False
            result: list[Self] = []
            used: set[int] = set()
            for i, a in enumerate(merged):
                if i in used:
                    continue
                best = a
                for j, b in enumerate(merged):
                    if j <= i or j in used:
                        continue
                    na, nb = a.name.lower().strip(), b.name.lower().strip()
                    names_related = na and nb and (na in nb or nb in na)
                    names_exact = na and na == nb
                    overlap = a.iou(b)
                    center_dist = abs(a.center_x - b.center_x) + abs(
                        a.center_y - b.center_y
                    )
                    should_merge = (
                        (overlap > _IOU_MERGE_MIN and names_related)
                        or (names_exact and center_dist < _MERGE_CENTER_DIST)
                        or overlap > _IOU_MERGE_HIGH
                    )
                    if should_merge:
                        if b.w * b.h > best.w * best.h:
                            best = b
                        used.add(j)
                        changed = True
                result.append(best)
                if best is not a:
                    used.add(i)
            merged = result
        for i, el in enumerate(merged):
            el.index = i + 1
        return merged


_ROLE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "tab",
        re.compile(
            r"\b(problems|output|debug console|terminal|ports|gitlens|explorer|extensions|source control|testing)\b",
            re.IGNORECASE,
        ),
    ),
    ("menu", re.compile(r"\b(file|edit|selection|view|go|run|help)\b", re.IGNORECASE)),
    (
        "button",
        re.compile(
            r"\b(button|btn|save|open|close|cancel|ok|apply|submit|send|undo|redo|refresh|collapse|new|delete|remove|copy|paste|cut)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "input",
        re.compile(
            r"\b(search|filter|find|input|describe|type|enter)\b|text\s*(box|field|input|area)",
            re.IGNORECASE,
        ),
    ),
    ("link", re.compile(r"\b(link|href|url)\b", re.IGNORECASE)),
    (
        "dropdown",
        re.compile(
            r"\b(select|choose|pick|dropdown|combobox|combo\s*box|branch|model)\b",
            re.IGNORECASE,
        ),
    ),
    ("toggle", re.compile(r"\b(toggle|switch|checkbox|check\s*box)\b", re.IGNORECASE)),
    (
        "icon-button",
        re.compile(
            r"\b(notifications?|bell|settings?|gear|account|profile|volume|mute)\b",
            re.IGNORECASE,
        ),
    ),
]


_WM_BUTTON_NAMES = frozenset(
    {"Minimise", "Minimize", "Maximise", "Maximize", "Close", "Restore"}
)
_JUNK_NAME_RE = re.compile(
    r"^(Ctrl|Alt|Shift|Cmd|Meta|Tab|Enter|Esc|Space|Backspace|Delete|Home|End|PageUp|PageDown|F\d+|[A-Z])$"
)
_NUMERIC_NAME_RE = re.compile(r"^[+-]?\d+$")


