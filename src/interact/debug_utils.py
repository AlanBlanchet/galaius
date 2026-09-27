"""Debug-output session helpers.

Every method lives on :class:`Debug` — they share state (the global
session timestamp) but otherwise take an ``invocation_id`` arg that
identifies the per-tool output directory.
"""

import logging
import re as _re
from contextvars import ContextVar
from datetime import datetime as _dt
from pathlib import Path

from interact.runtime import config
from interact.state import _SLUG_MAX

_log = logging.getLogger("interact")

# Dump dir of the tool call currently running. @instrumented (interact.server.core) sets it per
# call, so a tool body reaches its invocation dir via Debug.inv() instead of threading an `inv`
# arg through every helper. ContextVar keeps concurrent calls isolated.
_CURRENT_INV: ContextVar[str | None] = ContextVar("interact_current_inv", default=None)


def resolve_output_path(path: str) -> Path:
    """Where a caller-supplied output ``path`` lands — ONE rule for every tool taking one: ``~``
    expands, an absolute path is kept, a RELATIVE path anchors under ``config.debug_dir``
    (interact's own output dir, ``~/.interact/out``, where every other artifact lives) — never the
    server process's cwd, whatever the editor started it with and invisible to the calling agent
    (#120). Always absolute, so a tool can name the file the caller will actually find."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(config.debug_dir).expanduser() / p
    return p.absolute()


class Debug:
    """Namespace for debug-dump helpers (all staticmethod / classmethod)."""

    SESSION_TS: str = _dt.now().strftime("%Y%m%d_%H%M%S")

    @classmethod
    def inv(cls) -> str | None:
        """The dump dir of the tool call currently running (set by @instrumented), or None."""
        return _CURRENT_INV.get()

    @staticmethod
    def dump_dir(debug_dir: str | None) -> Path | None:
        # per-call arg wins, then explicit screenshot override, then debug_dir base. per-call
        # STRING follows the one output-path rule (#120): `~` expands, RELATIVE dir lands under
        # the base, never beside the server's cwd (Config's fields already expanded).
        if debug_dir:
            return resolve_output_path(debug_dir)
        return config.screenshot_dump_dir or config.debug_dir

    @classmethod
    def new_invocation_dir(cls, debug_dir: str | None, tool: str) -> str | None:
        d = cls.dump_dir(debug_dir)
        if not d:
            return None
        if d.resolve() in {Path("out").resolve(), resolve_output_path("out").resolve()}:
            raise ValueError(
                "debug_dir must be a subdirectory of out/, not out/ itself; "
                "use 'out/vscode' or 'out/tests'"
            )
        # Default (no override) → organize under sessions/<session>/<date>, artifacts land in
        # ~/.interact/out, separated by session and dated. Explicit debug_dir/screenshot_dump_dir
        # keeps the flat <base>/<session_ts> layout (out/vscode, dump dirs, tests).
        if debug_dir is None and config.screenshot_dump_dir is None:
            session_dir = config.session_log_dir()
        else:
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
    def dump_output(cls, invocation_id: str | None, result) -> None:
        """Record the EXACT value handed back to the agent in ``output.txt`` — including
        ``ERROR:``/``No window matching…`` strings, so a failed call (and any retry after it) is
        fully reconstructable from the logs, not just its inputs. ``result`` is whatever the tool
        returns: a plain string, or the ``[text, Image]`` pair from a ``return_image`` capture
        (only the text is written; the PNG is already dumped)."""
        if not invocation_id:
            return
        if isinstance(result, (list, tuple)):  # [text, Image(...)] from return_image=True
            result = next((p for p in result if isinstance(p, str)), "")
        cls.save("output", result if isinstance(result, str) else str(result),
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
