"""Check GitHub for a newer interact release (used by the TUI banner and `interact update`).

Stdlib-only HTTP so it adds no dependency and fails soft — a network hiccup never breaks
the UI; it just means "no update info this time".
"""

import json
import urllib.request
from importlib.metadata import distribution

from interact import DIST_NAME, installed_version
from interact.versioning import is_newer

REPO = "AlanBlanchet/interact"


def is_editable_install() -> bool:
    """True for a local editable/dev checkout (``uv tool install --editable .`` / ``pip -e``).
    Such installs track local source, so we never nag them to update from GitHub."""
    try:
        raw = distribution(DIST_NAME).read_text("direct_url.json")
        return bool(raw) and json.loads(raw).get("dir_info", {}).get("editable", False)
    except Exception:
        return False


def latest_remote_version(repo: str = REPO, timeout: float = 3.0) -> str | None:
    """Newest released version on GitHub (the latest release, else the newest tag), or
    None if it can't be determined. Never raises."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "interact"}
    for url, pick in (
        (f"https://api.github.com/repos/{repo}/releases/latest", lambda d: d.get("tag_name")),
        (f"https://api.github.com/repos/{repo}/tags", lambda d: d[0]["name"] if d else None),
    ):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 — fixed https host
                tag = pick(json.loads(response.read()))
        except Exception:
            continue
        if tag:
            return tag.lstrip("v")
    return None


def available_update(repo: str = REPO, timeout: float = 3.0) -> str | None:
    """The newer version available, or None if up to date / offline / a dev checkout."""
    if is_editable_install():
        return None  # local editable checkout — don't offer to overwrite it from GitHub
    remote = latest_remote_version(repo, timeout)
    if remote and is_newer(remote, installed_version()):
        return remote
    return None
