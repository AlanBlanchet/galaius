import base64
import io
import json

from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, model_validator
from playwright.async_api import Page

_ANNOTATION_COLORS = ["#FF4444", "#44AA44", "#4444FF", "#FF8800", "#AA44AA", "#00AAAA"]
_NAME_MAX_LEN = 30
_BADGE_W = 22
_BADGE_H = 16


def ref_locator(ref: str) -> str:
    return f'[data-interact-ref="{ref}"]'


class Element(BaseModel):
    """Minimal shared interface for detected UI elements (desktop and browser)."""

    x: int
    y: int
    w: int
    h: int
    role: str = "element"
    name: str = ""
    index: int = 0

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + self.w // 2, self.y + self.h // 2)

    def label(self) -> str:
        parts = [self.role]
        if self.name:
            parts.append(f": '{self.name}'")
        return " ".join(parts)


class InteractiveElement(Element):
    ref: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _remap_dimensions(cls, values):
        """Normalise raw DOM-scan geometry at construction — the boundary where browser JS dicts
        enter. Accept ``width``/``height`` as aliases for ``w``/``h``, and round sub-pixel
        coordinates to pixel ints: ``getBoundingClientRect`` returns fractional px (e.g.
        ``y=364.390625``) but ``Element.{x,y,w,h}`` are ints, so passing the float straight
        through crashed ``get_interactive_elements`` with a strict-int validation error. Resolve
        here once rather than letting floats reach (and fail) the field validators."""
        if isinstance(values, dict):
            if "width" in values and "w" not in values:
                values["w"] = values.pop("width")
            if "height" in values and "h" not in values:
                values["h"] = values.pop("height")
            for key in ("x", "y", "w", "h"):
                if values.get(key) is not None:
                    values[key] = round(values[key])
        return values

    @property
    def width(self) -> float:
        return float(self.w)

    @property
    def height(self) -> float:
        return float(self.h)

    @property
    def center_x(self) -> int:
        return self.center[0]

    @property
    def center_y(self) -> int:
        return self.center[1]

    @property
    def playwright_ref(self) -> str | None:
        if self.ref is None:
            return None
        return ref_locator(self.ref)


def annotate_screenshot(
    screenshot_bytes: bytes, elements: list[Element], uniform_color: str | None = None
) -> bytes:
    """Draw element boxes. ``uniform_color`` paints every box the same colour — used
    for the completeness judge, so already-detected elements read as one "covered"
    layer and anything still un-boxed (a missed drop zone) visibly stands out."""
    img = Image.open(io.BytesIO(screenshot_bytes)).convert("RGB")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()

    for el in elements:
        color = uniform_color or _ANNOTATION_COLORS[el.index % len(_ANNOTATION_COLORS)]
        draw.rectangle([el.x, el.y, el.x + el.w, el.y + el.h], outline=color, width=2)
        badge_x, badge_y = el.x, el.y
        draw.rectangle(
            [badge_x, badge_y, badge_x + _BADGE_W, badge_y + _BADGE_H], fill=color
        )
        draw.text((badge_x + 3, badge_y + 2), str(el.index), fill="white", font=font)
        name = (
            el.name[:_NAME_MAX_LEN] + "..." if len(el.name) > _NAME_MAX_LEN else el.name
        )
        label = f"{el.role}: {name}"
        lbox = font.getbbox(label)
        lw, lh = lbox[2] - lbox[0], lbox[3] - lbox[1]
        lx = badge_x + _BADGE_W + 2
        draw.rectangle([lx, badge_y, lx + lw + 4, badge_y + lh + 4], fill=color)
        draw.text((lx + 2, badge_y + 2), label, fill="white", font=font)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def format_element_list(elements: list[Element]) -> str:
    return "\n".join(
        f"  [{el.index}] {el.role}: {el.name!r}  ref={getattr(el, 'ref', None)}"
        for el in elements
    )


_SLUG_MAX = 40


def bytes_to_b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class PageState(BaseModel):
    url: str
    title: str
    accessibility_tree: str
    screenshot_base64: str
    visible_text: str
    focused_element: str | None

    @classmethod
    async def capture(cls, page: Page, scope: str | None = None):
        url = page.url
        title = await page.title()

        target = page
        if scope:
            target = page.locator(scope).first

        try:
            if scope:
                accessibility_tree = await target.aria_snapshot()
            else:
                snapshot = await page.accessibility.snapshot()
                accessibility_tree = json.dumps(snapshot, indent=2) if snapshot else ""
        except Exception:
            accessibility_tree = ""

        screenshot_bytes = await target.screenshot(type="png")
        screenshot_base64 = bytes_to_b64(screenshot_bytes)

        try:
            if scope:
                visible_text = (await target.inner_text())[:2000]
            else:
                visible_text = (await page.inner_text("body"))[:2000]
        except Exception:
            visible_text = ""

        try:
            props = await page.evaluate(
                "[document.activeElement?.tagName,"
                " document.activeElement?.id,"
                " document.activeElement?.className]"
            )
            tag, el_id, el_class = props
            focused_element = f"{tag}({el_id or el_class or ''})" if tag else None
        except Exception:
            focused_element = None

        return cls(
            url=url,
            title=title,
            accessibility_tree=accessibility_tree,
            screenshot_base64=screenshot_base64,
            visible_text=visible_text,
            focused_element=focused_element,
        )

    def text_summary(self) -> str:
        return f"{self.title}\n\n{self.visible_text}"


def _diff_text_and_focus(
    before_text: str,
    after_text: str,
    before_focus: str | None,
    after_focus: str | None,
) -> list[str]:
    parts: list[str] = []
    if before_focus != after_focus:
        parts.append(f"Focus: {before_focus} -> {after_focus}")
    before_words = set(before_text.split())
    after_words = set(after_text.split())
    added = after_words - before_words
    removed = before_words - after_words
    if added:
        parts.append(f"New text: {' '.join(list(added)[:20])}")
    if removed:
        parts.append(f"Removed text: {' '.join(list(removed)[:20])}")
    return parts


class StateChange(BaseModel):
    before: PageState
    after: PageState
    description: str = ""

    @classmethod
    def compute(cls, before: PageState, after: PageState):
        parts: list[str] = []
        if before.url != after.url:
            parts.append(f"URL: {before.url} -> {after.url}")
        if before.title != after.title:
            parts.append(f"Title: {before.title} -> {after.title}")
        parts.extend(
            _diff_text_and_focus(
                before.visible_text,
                after.visible_text,
                before.focused_element,
                after.focused_element,
            )
        )
        return cls(
            before=before,
            after=after,
            description="\n".join(parts) if parts else "",
        )


class DesktopState(BaseModel):
    window_name: str
    visible_text: str
    focused_element: str | None

    @classmethod
    def capture(cls, window_name: str):
        from interact.desktop.atspi import AtSpi  # noqa: PLC0415 — platform-guarded optional native dep

        return cls(
            window_name=window_name,
            visible_text=AtSpi.window_text(window_name),
            focused_element=AtSpi.focused_element(window_name),
        )

    @classmethod
    def compute_change(cls, before: "DesktopState", after: "DesktopState") -> str:
        parts = _diff_text_and_focus(
            before.visible_text,
            after.visible_text,
            before.focused_element,
            after.focused_element,
        )
        return "\n".join(parts) if parts else ""
