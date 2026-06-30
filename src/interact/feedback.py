"""Let an agent (or user) report a problem / missing capability / feedback about interact ITSELF
back to the maintainers, so issues hit in the wild actually surface.

Deliberately AGENT-INITIATED, not automatic telemetry: the caller composes the report, so there's
no surprise data collection and no secret leakage by default. Delivery ladder: a GitHub issue via
`gh` when it's available + authed; else the prefilled new-issue page opens in the user's browser
(submitting = one click); else a local report + submit link. interact's version + platform are
appended automatically (safe, useful triage context).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = "AlanBlanchet/interact"
KINDS = ("bug", "limitation", "feedback")
FEEDBACK_DIR = Path.home() / ".interact" / "out" / "feedback"


def _footer() -> str:
    import platform

    from interact import __version__

    # platform.platform() can shell out internally (e.g. macOS/Windows code paths), so it breaks
    # under a mocked subprocess and on the odd host. The footer is cosmetic and must never crash a
    # bug report, so fall back to the pieces that read straight from os.uname/sys.
    try:
        plat = platform.platform()
    except Exception:
        try:
            plat = f"{platform.system()}-{platform.release()}-{platform.machine()}"
        except Exception:
            plat = sys.platform
    return (
        f"\n\n---\n_interact {__version__} · Python {sys.version.split()[0]} · "
        f"{plat} · reported via report_issue_"
    )


def _version_lt(a: str, b: str) -> bool:
    """True if version string ``a`` is numerically older than ``b`` — by component, so 0.19.9 < 0.19.10
    (a plain string compare gets that backwards). Unparseable input → False, so a non-semver never
    triggers a warning."""

    def parts(v: str) -> tuple[int, ...]:
        try:
            return tuple(int(x) for x in v.split("."))
        except ValueError:
            return ()

    pa, pb = parts(a), parts(b)
    return bool(pa) and bool(pb) and pa < pb


def _stale_warning() -> str:
    """A banner prepended to a report when the REPORTING process is running an interact OLDER than
    what's installed on this machine right now. A long-lived MCP server keeps the code it imported at
    startup (``__version__`` is frozen then), so a bug filed from a stale one is the single biggest
    source of already-fixed reports — flag it up front, for the filer and the maintainer, with the
    one-line fix. Compares the frozen startup version against the live installed metadata (what a
    restart would load), so it never false-fires on a fresh process. Best-effort: any trouble → no
    banner (a report must never be blocked by its own staleness check)."""
    try:
        from interact import __version__, installed_version

        current = installed_version()
        if _version_lt(__version__, current):
            return (
                f"> ⚠️ **Filed from interact {__version__}, but {current} is installed here** — this "
                f"may already be fixed. A running interact MCP server keeps the code it loaded at "
                f"startup; reconnect/restart it (or `uv tool install --force --editable .`) to load "
                f"current code, then re-check before filing.\n\n"
            )
    except Exception:
        pass
    return ""


def _slug(text: str) -> str:
    keep = "".join(c if c.isalnum() else "-" for c in text.lower())
    return "-".join(p for p in keep.split("-") if p)[:50] or "report"


def _gh_create(title: str, body: str) -> tuple[str | None, str]:
    """Try ``gh issue create``; return ``(issue_url, "")`` or ``(None, why_it_failed)``."""
    gh = shutil.which("gh")
    if not gh:
        return None, "gh CLI not installed"
    try:
        done = subprocess.run(
            [gh, "issue", "create", "--repo", REPO, "--title", title, "--body", body],
            capture_output=True,
            text=True,
            timeout=30,
            # Inside an MCP server our stdin is the protocol pipe — gh must never read it
            # (a stdin read would block to the timeout, or eat protocol bytes).
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.SubprocessError, OSError) as e:
        return None, f"gh didn't complete: {e}"
    if done.returncode == 0 and done.stdout.strip():
        return done.stdout.strip().splitlines()[-1], ""
    reason = done.stderr.strip().splitlines()[-1] if done.stderr.strip() else f"gh exited {done.returncode}"
    return None, reason


def _prefilled_url(title: str, body: str) -> str:
    """A one-click new-issue link, capped to GitHub's URL limit. The cap is applied to the
    ENCODED body, then any severed %XX escape at the cut is trimmed so the URL stays valid."""
    import re
    from urllib.parse import quote

    encoded = quote(body)
    if len(encoded) > 7000:
        encoded = re.sub(r"%[0-9A-Fa-f]?$", "", encoded[:7000])
    return f"https://github.com/{REPO}/issues/new?title={quote(title)}&body={encoded}"


def _open_browser(url: str) -> bool:
    """Open ``url`` in the user's browser, detached. Never touches our stdin/stdout/stderr —
    inside an MCP stdio server those ARE the protocol pipes (same hygiene as the gh call).
    Returns False when there's no opener (headless box, SSH session)."""
    if sys.platform == "darwin":
        cmd = ["open", url]
    elif sys.platform.startswith("win"):
        try:
            import os

            os.startfile(url)  # ShellExecute — detached by construction, no pipes
            return True
        except OSError:
            return False
    else:
        cmd = ["xdg-open", url]
    if not shutil.which(cmd[0]):
        return False
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except OSError:
        return False


def report(title: str, body: str, kind: str = "bug") -> str:
    """File the report and say what happened. Delivery ladder: an authed ``gh`` files the
    issue outright; otherwise the user's browser opens on the prefilled new-issue page
    (submitting = pressing the button); only with no browser either (headless/SSH) is the
    report saved locally with the submit link. Never raises — reporting a bug must not
    itself blow up."""
    kind = kind if kind in KINDS else "feedback"
    title = f"[{kind}] {title.strip()}" if not title.lower().startswith(f"[{kind}]") else title.strip()
    full = _stale_warning() + body.strip() + _footer()

    url, reason = _gh_create(title, full)
    if url:
        return f"Reported to interact — {url}"

    submit = _prefilled_url(title, full)
    if _open_browser(submit):
        return (
            f"Couldn't file via gh ({reason}), so the prefilled issue page was opened in the "
            f"user's browser — ask them to press Submit there. (Link, in case the tab was "
            f"lost: {submit})"
        )

    # Last resort (no gh, no browser): persist locally so the report isn't lost, say WHY,
    # and hand back the prefilled link so delivery is still one click.
    try:
        FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        path = FEEDBACK_DIR / f"{stamp}-{_slug(title)}.md"
        path.write_text(f"# {title}\n\n{full}\n")
        return (
            f"Saved feedback locally to {path} — couldn't file to GitHub ({reason}). "
            f"Submit it in one click: {submit}"
        )
    except OSError as e:
        return f"Could not record feedback ({e}); please open it yourself: {submit}"
