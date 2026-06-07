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


def report(title: str, body: str, kind: str = "bug") -> str:
    """File the report. Returns a human-readable status (issue URL, or local path + how to submit).
    Never raises — reporting a bug must not itself blow up."""
    kind = kind if kind in KINDS else "feedback"
    title = f"[{kind}] {title.strip()}" if not title.lower().startswith(f"[{kind}]") else title.strip()
    full = body.strip() + _footer()

    if shutil.which("gh"):
        try:
            done = subprocess.run(
                ["gh", "issue", "create", "--repo", REPO, "--title", title, "--body", full],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if done.returncode == 0 and done.stdout.strip():
                return f"Reported to interact — {done.stdout.strip().splitlines()[-1]}"
        except (subprocess.SubprocessError, OSError):
            pass  # fall through to the local save

    # Fallback: persist locally so the report isn't lost; the user can submit it.
    try:
        FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        path = FEEDBACK_DIR / f"{stamp}-{_slug(title)}.md"
        path.write_text(f"# {title}\n\n{full}\n")
        return (
            f"Saved feedback locally to {path} (couldn't file to GitHub directly — submit it at "
            f"https://github.com/{REPO}/issues)."
        )
    except OSError as e:
        return f"Could not record feedback ({e}); please open an issue at https://github.com/{REPO}/issues"
