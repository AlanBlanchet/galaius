import asyncio
import re
from typing import Annotated, ClassVar, Literal
from pathlib import Path

import httpx
from pydantic import BaseModel, Field, field_validator, model_validator
from playwright.async_api import Page

from interact.config import DEFAULT_LIMIT
from interact.state import ref_locator

_DND_DISPATCH_JS = (Path(__file__).parent / "js" / "dnd_dispatch.js").read_text()

_JS_NEEDS_ASYNC = re.compile(r"\b(return|await)\b")

# A script the agent already wrote AS a function — an arrow (`(a) => …`, `a => …`) or a `function`
# expression, optionally `async`. Playwright invokes such a string itself (passing `args` as the
# parameter), so it MUST pass through unwrapped: wrapping `() => { return x }` in another IIFE
# defines the inner arrow without ever calling it, so the value is lost (page.evaluate returns
# undefined) — that was the root of "evaluate_js return value is blank" for any function-bodied
# script (e.g. `() => { const r = el.getBoundingClientRect(); return r.width }`).
_JS_IS_FUNCTION = re.compile(
    r"""^\s*(async\s+)?(
        function\b              # function () { … } / async function () { … }
      | \([^)]*\)\s*=>          # (args) => …
      | [A-Za-z_$][\w$]*\s*=>   # arg => …   (single param, no parens)
    )""",
    re.VERBOSE,
)


def _wrap_js(script: str, has_args: bool = False) -> str:
    """Prepare a script for ``page.evaluate`` so agents can write natural JS — a bare expression,
    a statement body that ``return``s, or a full function — and always get the value back.

    - Already a function (arrow or ``function``, maybe ``async``): pass through untouched —
      Playwright calls it itself (with the serialised ``args`` as the parameter). Re-wrapping it
      would define the function without calling it and lose the return value.
    - Otherwise with args: emit an ``async (args) => {{ … }}`` so the body reads ``args``.
    - Otherwise a body with top-level ``return``/``await``: wrap in an async IIFE so it's legal.
    - Otherwise a single expression (``document.title``): pass through so its value is returned."""
    src = script.strip()
    if _JS_IS_FUNCTION.match(src):
        return src
    if has_args:
        return f"async (args) => {{ {src} }}"
    if _JS_NEEDS_ASYNC.search(src):
        return f"(async () => {{ {src} }})()"
    return src


class Action(BaseModel):
    mutates: ClassVar[bool] = True
    wait: str | None = None
    observe: str | None = None


class ObservationAction(Action):
    mutates: ClassVar[bool] = False


class TargetedAction(Action):
    ref: str | None = None
    selector: str | None = None

    @model_validator(mode="after")
    def _require_target(self):
        if not self.ref and not self.selector:
            raise ValueError("Provide ref or selector")
        return self

    def _locator(self, page: Page):
        return (
            page.locator(ref_locator(self.ref))
            if self.ref
            else page.locator(self.selector)
        )


class _CoordinateTargetMixin(TargetedAction):
    x: int | None = None
    y: int | None = None
    name: str | None = None
    role: str | None = None

    def _targeting_groups(self) -> int:
        return sum(
            [
                self.name is not None,
                self.selector is not None,
                self.x is not None and self.y is not None,
                self.ref is not None,
            ]
        )

    def _validate_targeting(self):
        if self.role and not self.name:
            raise ValueError("role requires name")
        if (self.x is not None) != (self.y is not None):
            raise ValueError("Provide both x and y, or neither")
        if self._targeting_groups() > 1:
            provided = [
                label
                for label, present in (
                    ("ref", bool(self.ref)),
                    ("selector", bool(self.selector)),
                    ("name", bool(self.name)),
                    ("coordinates", self.x is not None and self.y is not None),
                    ("element", getattr(self, "element", None) is not None),
                )
                if present
            ]
            raise ValueError(
                f"Ambiguous target: you set {' + '.join(provided)} together. Provide exactly "
                "ONE of ref / selector / name / coordinates (a `ref` from get_interactive_elements "
                "is unique if you want to avoid name/selector ambiguity)."
            )

    @model_validator(mode="after")
    def _require_target(self):
        self._validate_targeting()
        return self


class ClickAction(_CoordinateTargetMixin):
    type: Literal["click"] = "click"
    element: int | None = None

    def _targeting_groups(self) -> int:
        return super()._targeting_groups() + (self.element is not None)

    @model_validator(mode="after")
    def _require_target(self):
        if self.ref and self.ref.startswith("e") and self.ref[1:].isdigit():
            self.element = int(self.ref[1:])
            self.ref = None
        self._validate_targeting()
        return self

    async def execute(self, page: Page):
        if self.ref:
            await self._locator(page).click()
        elif self.selector:
            await _click_selector(page, self.selector)
        else:
            await page.mouse.click(self.x, self.y)


class HoverAction(_CoordinateTargetMixin):
    type: Literal["hover"] = "hover"
    mutates: ClassVar[bool] = False

    async def execute(self, page: Page):
        if self.ref:
            await self._locator(page).hover()
        elif self.selector:
            await page.hover(self.selector)
        else:
            await page.mouse.move(self.x, self.y)


class TypeTextAction(Action):
    type: Literal["type_text"] = "type_text"
    ref: str | None = None
    selector: str | None = None
    name: str | None = None
    role: str | None = None
    text: str
    clear_first: bool = True

    @model_validator(mode="after")
    def _validate_targeting(self):
        if self.role and not self.name:
            raise ValueError("role requires name")
        return self

    def _locator(self, page: Page):
        return (
            page.locator(ref_locator(self.ref))
            if self.ref
            else page.locator(self.selector)
        )

    async def execute(self, page: Page):
        if not self.ref and not self.selector:
            raise ValueError("Browser type_text requires ref or selector")
        target = self._locator(page)
        if self.clear_first:
            await target.fill(self.text)
        else:
            await target.type(self.text)


class ScrollAction(Action):
    DELTA: ClassVar[dict[str, tuple[int, int]]] = {
        "down": (0, 300),
        "up": (0, -300),
        "right": (300, 0),
        "left": (-300, 0),
    }
    type: Literal["scroll"] = "scroll"
    direction: Literal["down", "up", "left", "right"] = "down"
    amount: int = 3

    @field_validator("amount")
    @classmethod
    def _positive_amount(cls, v: int):
        if v <= 0:
            raise ValueError("amount must be > 0")
        return v

    async def execute(self, page: Page):
        dx, dy = self.DELTA[self.direction]
        for _ in range(self.amount):
            await page.mouse.wheel(dx, dy)


async def _click_selector(page: Page, selector: str) -> None:
    """Click a CSS selector, preferring the first VISIBLE match when several match. Duplicated link
    text (a breadcrumb mirroring the sidebar) or a generic button label (`:has-text('Annuler')`)
    makes a selector resolve to many nodes; `page.click` would target whatever is first in DOM
    order — often a hidden/off-screen one, so the click silently lands wrong or times out (#29).
    A single match clicks directly; none-visible falls back to the first so a hidden-but-actionable
    target still works."""
    loc = page.locator(selector)
    if await loc.count() <= 1:
        await loc.click()
        return
    for i in range(await loc.count()):
        item = loc.nth(i)
        if await item.is_visible():
            await item.click()
            return
    await loc.first.click()


async def _ref_center(page: Page, ref: str) -> tuple[float, float]:
    box = await page.locator(ref_locator(ref)).bounding_box()
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


class DragAction(Action):
    type: Literal["drag"] = "drag"
    name: str | None = None
    role: str | None = None
    from_x: int | None = None
    from_y: int | None = None
    to_x: int | None = None
    to_y: int | None = None
    from_ref: str | None = None
    to_ref: str | None = None
    steps: int = Field(1, ge=1)

    @model_validator(mode="after")
    def _require_targets(self):
        if self.role and not self.name:
            raise ValueError("role requires name")
        has_from = self.from_ref or (
            self.from_x is not None and self.from_y is not None
        )
        has_to = self.to_ref or (self.to_x is not None and self.to_y is not None)
        if not has_from or not has_to:
            raise ValueError(
                "Provide from_ref or from_x+from_y, and to_ref or to_x+to_y"
            )
        return self

    async def execute(self, page: Page):
        if self.from_ref:
            fx, fy = await _ref_center(page, self.from_ref)
        else:
            fx, fy = self.from_x, self.from_y

        if self.to_ref:
            tx, ty = await _ref_center(page, self.to_ref)
        else:
            tx, ty = self.to_x, self.to_y

        await page.mouse.move(fx, fy)
        await page.mouse.down()
        await page.mouse.move(tx, ty, steps=self.steps)
        await page.mouse.up()

        await page.evaluate(
            _DND_DISPATCH_JS, [float(fx), float(fy), float(tx), float(ty)]
        )


class NavigateAction(Action):
    type: Literal["navigate"] = "navigate"
    url: str

    async def execute(self, page: Page):
        await page.goto(self.url)


class EvaluateJsAction(Action):
    type: Literal["evaluate_js"] = "evaluate_js"
    script: str
    # Optional JSON-serialisable value passed to the script as `args` (Playwright serialises it
    # across to the page). Lets a script be parameterised by data instead of string-building it
    # into the source — e.g. {"type":"evaluate_js","script":"return args.ids.length","args":{...}}.
    args: object | None = None

    async def execute(self, page: Page):
        if self.args is not None:
            return await page.evaluate(_wrap_js(self.script, has_args=True), self.args)
        return await page.evaluate(_wrap_js(self.script))


class ScreenshotAction(ObservationAction):
    type: Literal["screenshot"] = "screenshot"
    scope: str | None = None
    query: str | None = None
    selector: str | None = None
    element: int | None = None
    # Absolute path to also write the captured PNG to — same as the standalone screenshot tool, so
    # an inline screenshot in a run_actions sequence can keep a frame without a follow-up tool call
    # that would re-capture a now-changed page (#27).
    path: str | None = None


class WaitForAction(ObservationAction):
    type: Literal["wait_for"] = "wait_for"
    selector: str | None = None
    text: str | None = None  # wait until this substring appears in the page's visible text
    state: Literal["visible", "hidden", "attached", "detached"] = "visible"
    timeout: int = 10000

    @field_validator("timeout")
    @classmethod
    def _positive_timeout(cls, v: int):
        if v <= 0:
            raise ValueError("timeout must be > 0")
        return v

    @model_validator(mode="after")
    def _require_condition(self):
        if (self.selector is None) == (self.text is None):
            raise ValueError("Provide exactly one of `selector` or `text` to wait for")
        return self

    async def execute(self, page: Page):
        # Deterministic alternative to a guessed `sleep`: block until a concrete condition holds
        # (an element reaches a state, or text appears), then continue — no fixed duration to tune.
        if self.text is not None:
            await page.wait_for_function(
                "t => !!document.body && document.body.innerText.includes(t)",
                arg=self.text,
                timeout=self.timeout,
            )
            return f"text {self.text!r} appeared"
        await page.wait_for_selector(
            self.selector, state=self.state, timeout=self.timeout
        )
        return f"'{self.selector}' is {self.state}"


class UploadFileAction(TargetedAction):
    type: Literal["upload_file"] = "upload_file"
    path: str

    async def execute(self, page: Page):
        target = self._locator(page)
        await target.set_input_files(self.path)


class KeyPressAction(Action):
    type: Literal["key_press"] = "key_press"
    key: str

    async def execute(self, page: Page):
        await page.keyboard.press(self.key)


class AnnotateAction(ObservationAction):
    type: Literal["annotate"] = "annotate"
    scope: str | None = None
    query: str | None = None
    limit: int = DEFAULT_LIMIT


class SleepAction(ObservationAction):
    type: Literal["sleep"] = "sleep"
    # A FIXED pause. For waiting on content/navigation prefer wait_for (selector/text) or a
    # `wait` on the preceding action — they block exactly until ready instead of guessing a duration.
    duration: float = Field(1.0, gt=0, le=30)

    async def execute(self, page: Page):
        await asyncio.sleep(self.duration)
        return f"waited {self.duration}s"


class CompareAction(ObservationAction):
    type: Literal["compare"] = "compare"
    steps: list[int]
    query: str


class ClickElementAction(Action):
    type: Literal["click_element"] = "click_element"
    element: int

    async def execute(self, page: Page):
        raise NotImplementedError(
            "server resolves click_element using stored element map"
        )


class NewTabAction(ObservationAction):
    type: Literal["new_tab"] = "new_tab"
    url: str | None = None


class SwitchTabAction(ObservationAction):
    type: Literal["switch_tab"] = "switch_tab"
    index: int = 0


class CloseTabAction(ObservationAction):
    type: Literal["close_tab"] = "close_tab"
    index: int | None = None


class HttpRequestAction(ObservationAction):
    type: Literal["http_request"] = "http_request"
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None

    async def execute(self, page: Page):
        headers = {"User-Agent": "interact/0.1", **self.headers}
        async with httpx.AsyncClient() as client:
            response = await client.request(
                self.method,
                self.url,
                headers=headers,
                content=self.body,
                timeout=30.0,
            )
            return f"{response.status_code} {response.reason_phrase}\n{response.text[:2000]}"


class EmulateDeviceAction(ObservationAction):
    """Reconfigure the browser session's viewport / device profile for the rest of the session —
    to verify responsive & mobile layouts at true device metrics (CSS size, DPR, touch). Give a
    Playwright ``device`` name (``"iPhone 13"``, ``"Pixel 7"``, ``"iPad Mini"``) OR an explicit
    ``width``+``height`` (plus optional ``device_scale_factor`` / ``is_mobile`` / ``has_touch`` /
    ``user_agent``); ``reset=true`` restores the configured default viewport.

    Viewport/DPR/mobile/touch are fixed when a browser context is created, so this rebuilds the
    session context — cookies are preserved and the current URL is re-opened. Run it before
    navigating (or it reloads the current page at the new size). At ``device_scale_factor`` ≠ 1 a
    screenshot is DPR-scaled, so VLM/annotator ref boxes can be offset; layout/visual checks are
    unaffected. ``is_mobile`` is Chromium-only (ignored on Firefox/WebKit)."""

    type: Literal["emulate_device"] = "emulate_device"
    device: str | None = None
    width: int | None = None
    height: int | None = None
    device_scale_factor: float | None = None
    is_mobile: bool | None = None
    has_touch: bool | None = None
    user_agent: str | None = None
    reset: bool = False

    @model_validator(mode="after")
    def _require_profile(self):
        if (self.width is None) != (self.height is None):
            raise ValueError("Provide both width and height, or neither.")
        if not self.reset and not self.device and self.width is None:
            raise ValueError(
                "Provide a `device` name (e.g. 'iPhone 13'), or both `width` and `height`, "
                "or `reset=true`."
            )
        return self


AnyAction = Annotated[
    ClickAction
    | HoverAction
    | TypeTextAction
    | ScrollAction
    | DragAction
    | NavigateAction
    | EvaluateJsAction
    | KeyPressAction
    | ScreenshotAction
    | WaitForAction
    | UploadFileAction
    | NewTabAction
    | SwitchTabAction
    | CloseTabAction
    | HttpRequestAction
    | AnnotateAction
    | ClickElementAction
    | EmulateDeviceAction
    | SleepAction
    | CompareAction,
    Field(discriminator="type"),
]

BROWSER_ONLY_ACTIONS = frozenset(
    {
        "navigate",
        "evaluate_js",
        "wait_for",
        "upload_file",
        "new_tab",
        "switch_tab",
        "close_tab",
        "emulate_device",
    }
)
