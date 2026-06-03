"""Debug-output session helpers.

Every method lives on :class:`Debug` — they share state (the global
session timestamp) but otherwise take an ``invocation_id`` arg that
identifies the per-tool output directory.
"""

import logging
import re as _re
from datetime import datetime as _dt
from pathlib import Path

from interact.runtime import config
from interact.state import _SLUG_MAX

_log = logging.getLogger("interact")


class Debug:
    """Namespace for debug-dump helpers (all staticmethod / classmethod)."""

    SESSION_TS: str = _dt.now().strftime("%Y%m%d_%H%M%S")

    @staticmethod
    def dump_dir(debug_dir: str | None) -> Path | None:
        # per-call arg wins, then the explicit screenshot override, then the debug_dir base.
        if debug_dir:
            return Path(debug_dir)
        return config.screenshot_dump_dir or config.debug_dir

    @classmethod
    def new_invocation_dir(cls, debug_dir: str | None, tool: str) -> str | None:
        d = cls.dump_dir(debug_dir)
        if not d:
            return None
        if d.resolve() == Path("out").resolve():
            raise ValueError(
                "debug_dir must be a subdirectory of out/, not out/ itself; "
                "use 'out/vscode' or 'out/tests'"
            )
        session_dir = d / cls.SESSION_TS
        ts = _dt.now().strftime("%H%M%S")
        base = session_dir / f"{ts}_{tool}"
        candidate = base
        suffix = 2
        while candidate.exists():
            candidate = session_dir / f"{base.name}_{suffix}"
            suffix += 1
        candidate.mkdir(parents=True, exist_ok=True)
        return str(candidate)

    @staticmethod
    def path(
        label: str,
        ext: str,
        invocation_id: str | None = None,
    ) -> Path | None:
        if not invocation_id:
            return None
        slug = _re.sub(r"[^a-zA-Z0-9]", "_", label)[:_SLUG_MAX]
        inv_dir = Path(invocation_id)
        inv_dir.mkdir(parents=True, exist_ok=True)
        return inv_dir / f"{slug}.{ext}"

    @classmethod
    def dump_input(cls, invocation_id: str | None, tool_input: dict, resolved: dict | None = None) -> None:
        """Record what the agent actually passed (``tool_input.json``) and the full effective
        config with defaults applied (``tool_input_resolved.json``) — so a run can be replayed
        and audited: which params the agent chose vs which came from defaults."""
        if not invocation_id:
            return
        import json

        cls.save("tool_input", json.dumps(tool_input, indent=2, default=str), ext="json",
                 invocation_id=invocation_id)
        if resolved is not None:
            cls.save("tool_input_resolved", json.dumps(resolved, indent=2, default=str), ext="json",
                     invocation_id=invocation_id)

    @classmethod
    def save(
        cls,
        label: str,
        data: str | bytes,
        ext: str = "txt",
        invocation_id: str | None = None,
    ) -> None:
        try:
            p = cls.path(label, ext, invocation_id=invocation_id)
            if p:
                if isinstance(data, str):
                    p.write_text(data)
                else:
                    p.write_bytes(data)
                _log.debug("debug_save: %s", p)
        except Exception:
            _log.warning("debug_save failed for %s", label, exc_info=True)

    @classmethod
    def step_save(
        cls,
        invocation_id: str | None,
        step_idx: int,
        action_type: str,
        label: str,
        data: str | bytes,
        ext: str = "txt",
    ) -> None:
        if not invocation_id:
            return
        step_dir = str(Path(invocation_id) / f"{step_idx:03d}_{action_type}")
        cls.save(label, data, ext=ext, invocation_id=step_dir)
