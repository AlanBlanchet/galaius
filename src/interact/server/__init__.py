"""The ``interact`` MCP server, split by cohesion into a package.

``core`` owns the shared ``FastMCP`` instance, lifespan, instructions and the browser session
registry; ``vlm`` / ``sandbox`` / ``targets`` / ``capture`` hold the private helpers; the
``tools_*`` modules hold the ``@mcp.tool`` surfaces (importing them registers the tools). This
``__init__`` imports them in dependency order and RE-EXPORTS the whole public + test-patched
surface, so ``import interact.server as srv; srv._vlm`` and ``from interact.server import
_scan_elements`` keep working exactly as when this was one module.

Monkeypatch note: a helper is patched on the module that DEFINES it (``srv.vlm._vlm``,
``srv.targets._resolve_target``, ``srv.sandbox._get_sandbox``) — cross-module call sites are
module-qualified so the patch is seen; the re-exports below are for direct import/read access.
"""

import asyncio  # noqa: F401 — re-exported: some tests patch interact.server.asyncio

# Submodules — importing the tools_* modules runs their @mcp.tool decorators (registration).
from interact.server import capture, core, sandbox, targets, vlm  # noqa: F401
from interact.server import tools_desktop, tools_meta, tools_vision, tools_web  # noqa: F401

# --- Shared instances / entrypoint (core) ---
from interact.server.core import (  # noqa: F401
    _ANNOTATE_JS,
    _AUDIO_MIME,
    _DBG_ACTIONS,
    _DBG_ELEMENTS,
    _DEFAULT_SESSION,
    _MAX_FALLBACKS,
    _NO_WINDOWS_MSG,
    _audio_mime,
    _desktop_label,
    _instructions,
    _lifespan,
    _not_found,
    _parse_int_tuple,
    _save_to_path,
    _session_response,
    _sessions,
    breaker,
    config,
    instrumented,
    main,
    mcp,
)

# --- Sandbox / portable backend lifecycle ---
from interact.server.sandbox import (  # noqa: F401
    _close_sandbox,
    _get_portable,
    _get_sandbox,
    _idle_session_reaper,
    _reap_sandbox,
    _resolve_portable_screen,
)

# --- Target resolution ---
from interact.server.targets import (  # noqa: F401
    _EDITOR_TITLE_MARKERS,
    _desktop_unsupported,
    _find_desktop_window,
    _looks_like_editor,
    _name_not_found_msg,
    _resolve_desktop_el,
    _resolve_image_source,
    _resolve_nested_target,
    _resolve_target,
)

# --- VLM boundary ---
from interact.server.vlm import (  # noqa: F401
    _analyze,
    _analyze_interaction_frames,
    _effective_model,
    _fmt_timing,
    _media_response,
    _resolved_config,
    _run_compare,
    _run_observe,
    _vlm,
)

# --- Capture + UI-critique ---
from interact.server.capture import (  # noqa: F401
    _annotate_and_describe,
    _annotate_desktop,
    _annotate_page,
    _capture,
    _capture_desktop,
    _capture_target_png,
    _element_screenshot,
    _quality_plan,
    _resolve_capture,
    _review_drop_phantom_findings,
    _run_ui_critique,
    _scan_elements,
    _verify_downgrade_phantom_pass,
    _wait,
)

# --- Tools ---
from interact.server.tools_web import (  # noqa: F401
    download_asset,
    get_logs,
    get_page_state,
    navigate,
    run_actions,
    session,
)
from interact.server.tools_vision import (  # noqa: F401
    get_interactive_elements,
    measure_ui,
    review_ui,
    screenshot,
    transcribe,
    verify_ui,
)
from interact.server.tools_desktop import (  # noqa: F401
    _record_browser,
    _record_desktop,
    launch_app,
    list_desktop_windows,
    record,
    reset_sandbox,
)
from interact.server.tools_meta import list_providers, report_issue  # noqa: F401

# --- Names other modules define but tests reach through interact.server (back-compat) ---
from interact.debug_utils import Debug  # noqa: F401
from interact.desktop import DesktopElement, DesktopWindow  # noqa: F401
from interact.vision.detect import _crop_image, _desktop_context, _detect_desktop_elements  # noqa: F401
from interact.actions.dispatch import _run_actions_browser, _run_actions_desktop  # noqa: F401
from interact.launch import (  # noqa: F401
    _browser_isolate,
    _flutter_software_render,
    _resolve_nested_size,
    apply_launch_rewrites,
)
from interact.vision import (  # noqa: F401
    _UNSET,
    MediaItem,
    VLMResult,
    _Unset,
    analyze_media,
    analyze_screenshot,
    transcribe_audio,
)
