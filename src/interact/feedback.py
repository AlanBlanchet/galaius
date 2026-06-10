"""Let an agent (or user) report a problem / missing capability / feedback about interact ITSELF
back to the maintainers, so issues hit in the wild actually surface.

Deliberately AGENT-INITIATED, not automatic telemetry: the caller composes the report, so there's
no surprise data collection and no secret leakage by default. Files a GitHub issue via `gh` when
it's available + authed; otherwise saves a local report the user can submit by hand. interact's
version + platform are appended automatically (safe, useful triage context).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = "AlanBlanchet/interact"
KINDS = ("bug", "limitation", "feedback")
FEEDBACK_DIR = Path.home() / ".interact" / "feedback"


def _footer() -> str:
    import platform

    from interact import __version__

    return (
        f"\n\n---\n_interact {__version__} · Python {sys.version.split()[0]} · "
        f"{platform.platform()} · reported via report_issue_"
    )


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


def report(title: str, body: str, kind: str = "bug") -> str:
    """File the report. Returns a human-readable status (issue URL, or local path + a
    prefilled submit link). Never raises — reporting a bug must not itself blow up."""
    kind = kind if kind in KINDS else "feedback"
    title = f"[{kind}] {title.strip()}" if not title.lower().startswith(f"[{kind}]") else title.strip()
    full = body.strip() + _footer()

    url, reason = _gh_create(title, full)
    if url:
        return f"Reported to interact — {url}"

    # Fallback: persist locally so the report isn't lost, say WHY gh failed, and hand back
    # a prefilled link so delivery is still one click.
    submit = _prefilled_url(title, full)
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
