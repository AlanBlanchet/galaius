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


def _wrap_js(script: str, has_args: bool = False) -> str:
    """Prepare a script for ``page.evaluate`` so agents can write natural JS.

    Without args: a multi-statement script's top-level ``return``/``await`` is otherwise illegal
    (``page.evaluate`` only runs a string as a function body when it IS a function), so wrap it in
    an async IIFE. A single expression (``document.title``) has neither keyword and passes through
    untouched, so its value is still returned.

    With args: emit an ``async (args) => {{ ... }}`` function expression — Playwright invokes it
    with the serialised ``args`` value, which the script reads as ``args``. The body should
    ``return`` the value it wants back."""
    src = script.strip()
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
                "ONE of ref / selector / name / coordinates. Prefer `ref` from "
                "get_interactive_elements (unique & stable); raw coordinates are a last resort."
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
            await page.click(self.selector)
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
    }
)
