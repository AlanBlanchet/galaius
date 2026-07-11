"""Input actions, grouped into a package.

``models`` holds the typed action definitions (the ``AnyAction`` union + each ``*Action`` pydantic
model, plus ``BROWSER_ONLY_ACTIONS`` and the JS-wrapping helpers); ``dispatch`` executes a sequence
of them against a browser session or a desktop window. This ``__init__`` re-exports the action
models (so ``from interact.actions import AnyAction`` resolves) and the two dispatch entry points.
"""

from interact.actions.models import (  # noqa: F401
    BROWSER_ONLY_ACTIONS,
    AnnotateAction,
    AnyAction,
    ClickAction,
    ClickElementAction,
    CloseTabAction,
    CompareAction,
    DoubleClickAction,
    DragAction,
    EmulateDeviceAction,
    EvaluateJsAction,
    HoverAction,
    HttpRequestAction,
    KeyPressAction,
    NavigateAction,
    NewTabAction,
    ScreenshotAction,
    ScrollAction,
    SelectTextAction,
    SleepAction,
    SwitchTabAction,
    TypeTextAction,
    UploadFileAction,
    WaitForAction,
    _wrap_js,
    settle_animations,
)
from interact.actions.dispatch import _run_actions_browser, _run_actions_desktop  # noqa: F401
