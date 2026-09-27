"""Shared test scaffolding.

These helpers were copy-pasted across the suite — `_mgr` in ten files, `_home` in nine, `_ready`
in six — with bodies that had drifted apart character by character while meaning the same thing.
They live here once so a change to how a test reaches a browser or a fake capture happens in one
place. HOME isolation itself is not one of these: `conftest.py::_isolate_unit_configuration` is
autouse and already relocates it for every non-integration test.
"""

from .agents import ScriptedProvider, install_provider, register_run, use_policy
from .browser import browser_config, browser_manager, interactive_element, ready_or_skip
from .capture import async_capture
from .desktop import RecordingBackend, bare_nested_backend, desktop_window
from .git import commit_all, git_out, init_repo, run_git
from .media import solid_png, varied_png
from .models import catalog_dict, catalog_json, catalog_of, model

__all__ = [
    "register_run",
    "ScriptedProvider",
    "install_provider",
    "use_policy",
    "browser_config",
    "browser_manager",
    "interactive_element",
    "ready_or_skip",
    "solid_png",
    "varied_png",
    "async_capture",
    "run_git",
    "git_out",
    "init_repo",
    "commit_all",
    "catalog_of",
    "catalog_dict",
    "catalog_json",
    "model",
    "RecordingBackend",
    "bare_nested_backend",
    "desktop_window",
]
