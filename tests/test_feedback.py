"""report_issue must reliably get a problem to the maintainers: GitHub issue when gh is
authed, else a prefilled issue page opened in the user's browser, else a local file + link.
And it must NEVER raise — reporting a bug can't itself blow up."""

import pytest

import interact.feedback as fb


@pytest.fixture(autouse=True)
def no_real_browser(monkeypatch):
    """Tests must never open an actual browser tab; pretend no browser is available unless
    a test installs its own capture. Returns the real opener for tests that exercise it."""
    real = getattr(fb, "_open_browser", None)
    monkeypatch.setattr(fb, "_open_browser", lambda url: False, raising=False)
    return real


class _Ok:
    returncode = 0
    stdout = "https://github.com/AlanBlanchet/interact/issues/42\n"


def test_files_github_issue_when_gh_available(monkeypatch):
    monkeypatch.setattr(fb.shutil, "which", lambda c: "/usr/bin/gh")
    monkeypatch.setattr(fb.subprocess, "run", lambda *a, **k: _Ok())
    out = fb.report("click times out on a canvas", "steps to reproduce…", "bug")
    assert "issues/42" in out


def test_kind_prefixes_title_and_appends_env_footer(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, **k):
        captured["cmd"] = cmd
        return _Ok()

    monkeypatch.setattr(fb.shutil, "which", lambda c: "/usr/bin/gh")
    monkeypatch.setattr(fb.subprocess, "run", fake_run)
    fb.report("missing select_option", "would help", "limitation")
    cmd = captured["cmd"]
    assert cmd[cmd.index("--title") + 1].startswith("[limitation] ")
    body = cmd[cmd.index("--body") + 1]
    assert "interact" in body and "Python" in body  # auto footer, no caller effort


def test_invalid_kind_becomes_feedback(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, **k):
        captured["cmd"] = cmd
        return _Ok()

    monkeypatch.setattr(fb.shutil, "which", lambda c: "/usr/bin/gh")
    monkeypatch.setattr(fb.subprocess, "run", fake_run)
    fb.report("note", "body", "not-a-kind")
    assert captured["cmd"][captured["cmd"].index("--title") + 1].startswith("[feedback] ")


def test_falls_back_to_local_file_without_gh(monkeypatch, tmp_path):
    monkeypatch.setattr(fb.shutil, "which", lambda c: None)  # no gh
    monkeypatch.setattr(fb, "FEEDBACK_DIR", tmp_path / "feedback")
    out = fb.report("offline bug", "body text")
    files = list((tmp_path / "feedback").glob("*.md"))
    assert "Saved feedback locally" in out and len(files) == 1
    assert "offline bug" in files[0].read_text()


def test_never_raises_even_when_everything_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(fb.shutil, "which", lambda c: "/usr/bin/gh")
    monkeypatch.setattr(fb.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    monkeypatch.setattr(fb, "FEEDBACK_DIR", tmp_path / "feedback")
    out = fb.report("x", "y")  # gh raises → local fallback still works
    assert "Saved feedback locally" in out


def test_gh_failure_reason_reaches_the_caller(monkeypatch, tmp_path):
    """A report that falls back must say WHY gh failed — a bare 'couldn't file to GitHub'
    leaves the agent (and the maintainer reading logs) nothing to act on. Seen in the wild:
    a real report fell back with no reason recorded."""

    class _Denied:
        returncode = 1
        stdout = ""
        stderr = "HTTP 403: Resource not accessible by integration"

    monkeypatch.setattr(fb.shutil, "which", lambda c: "/usr/bin/gh")
    monkeypatch.setattr(fb.subprocess, "run", lambda *a, **k: _Denied())
    monkeypatch.setattr(fb, "FEEDBACK_DIR", tmp_path / "feedback")
    out = fb.report("t", "b")
    assert "403" in out


def test_gh_never_inherits_stdin(monkeypatch, tmp_path):
    """In an MCP server, the parent's stdin IS the protocol pipe. gh must get DEVNULL —
    a child that reads inherited stdin can block until the timeout (then silently fall
    back) or, worse, swallow protocol bytes."""
    captured: dict = {}

    def fake_run(cmd, **k):
        captured.update(k)
        return _Ok()

    monkeypatch.setattr(fb.shutil, "which", lambda c: "/usr/bin/gh")
    monkeypatch.setattr(fb.subprocess, "run", fake_run)
    fb.report("t", "b")
    assert captured.get("stdin") == fb.subprocess.DEVNULL


def test_no_gh_opens_the_prefilled_issue_page_in_the_browser(monkeypatch, tmp_path):
    """Default UX without gh: the user's browser opens straight on the prefilled new-issue
    page so submitting is just pressing the button — no file hunting."""
    opened: list[str] = []
    monkeypatch.setattr(fb.shutil, "which", lambda c: None)
    monkeypatch.setattr(fb, "_open_browser", lambda url: opened.append(url) or True)
    monkeypatch.setattr(fb, "FEEDBACK_DIR", tmp_path / "feedback")
    out = fb.report("crash on launch", "details here")
    assert opened and f"https://github.com/{fb.REPO}/issues/new?" in opened[0]
    assert "crash%20on%20launch" in opened[0]
    assert "browser" in out.lower() and "submit" in out.lower()
    assert not (tmp_path / "feedback").exists()  # delivered to the browser, not squirreled away


def test_gh_failure_also_opens_the_browser(monkeypatch, tmp_path):
    """gh present but failing (not authed, network, scopes) gets the same browser hand-off."""

    class _Denied:
        returncode = 1
        stdout = ""
        stderr = "gh: To get started with GitHub CLI, please run: gh auth login"

    opened: list[str] = []
    monkeypatch.setattr(fb.shutil, "which", lambda c: "/usr/bin/gh")
    monkeypatch.setattr(fb.subprocess, "run", lambda *a, **k: _Denied())
    monkeypatch.setattr(fb, "_open_browser", lambda url: opened.append(url) or True)
    monkeypatch.setattr(fb, "FEEDBACK_DIR", tmp_path / "feedback")
    out = fb.report("t", "b")
    assert opened
    assert "gh auth login" in out  # the reason still reaches the caller


def test_fallback_offers_a_prefilled_issue_url(monkeypatch, tmp_path):
    """No gh AND no browser (headless/SSH): the report is saved locally and the message
    still carries the prefilled new-issue URL, so nothing is ever lost."""
    monkeypatch.setattr(fb.shutil, "which", lambda c: None)
    monkeypatch.setattr(fb, "FEEDBACK_DIR", tmp_path / "feedback")
    out = fb.report("crash on launch", "details here")
    assert f"https://github.com/{fb.REPO}/issues/new?" in out
    assert "crash%20on%20launch" in out
    assert "Saved feedback locally" in out


def test_browser_spawn_never_touches_our_stdio(monkeypatch, no_real_browser):
    """The browser opener runs inside an MCP stdio server: the spawned process must get
    DEVNULL for stdin/stdout/stderr (an inherited pipe = corrupted protocol), detached."""
    captured: dict = {}

    def fake_popen(cmd, **k):
        captured.update(k, cmd=cmd)
        return object()

    monkeypatch.setattr(fb.sys, "platform", "linux")  # pin the xdg-open branch everywhere
    monkeypatch.setattr(fb.shutil, "which", lambda c: f"/usr/bin/{c}")
    monkeypatch.setattr(fb.subprocess, "Popen", fake_popen)
    assert no_real_browser("https://example.com/x") is True
    assert captured["stdin"] == fb.subprocess.DEVNULL
    assert captured["stdout"] == fb.subprocess.DEVNULL
    assert captured["stderr"] == fb.subprocess.DEVNULL
    assert captured.get("start_new_session") is True


@pytest.mark.parametrize("huge", ["x" * 50_000, "\n " * 25_000], ids=["plain", "all-escaped"])
def test_prefilled_url_stays_valid_for_huge_bodies(monkeypatch, tmp_path, huge):
    """GitHub rejects multi-kB URLs; the ENCODED body is capped, and truncation never
    leaves a severed %XX escape — even when every char encodes to three."""
    monkeypatch.setattr(fb.shutil, "which", lambda c: None)
    monkeypatch.setattr(fb, "FEEDBACK_DIR", tmp_path / "feedback")
    out = fb.report("t", huge)
    url = next(w for w in out.split() if w.startswith("https://") and "/issues/new?" in w)
    assert len(url) < 8000
    assert "%" not in url[-3:] or len(url[url.rindex("%"):]) == 3  # any trailing escape is whole


def test_cli_report_command_sends_through_feedback(monkeypatch, capsys):
    """`interact report` is the shell-accessible twin of the MCP report_issue tool, so any
    agent with a terminal can file feedback without an MCP connection."""
    import interact.cli as cli

    sent: dict = {}

    def fake_report(title, body, kind="bug"):
        sent.update(title=title, body=body, kind=kind)
        return "Reported to interact — https://github.com/AlanBlanchet/interact/issues/7"

    monkeypatch.setattr(fb, "report", fake_report)
    cli.report("emulator black", "frames are black", kind="limitation")
    assert sent == {"title": "emulator black", "body": "frames are black", "kind": "limitation"}
    assert "issues/7" in capsys.readouterr().out


def test_footer_survives_platform_platform_raising(monkeypatch):
    """report()'s env footer must never crash the report: platform.platform() can shell out
    internally and raise (the macOS-under-mocked-subprocess case that reddened CI), so _footer
    falls back to os.uname-backed pieces. Regression for the cross-OS CI failure."""
    import platform as _pl

    import interact.feedback as fb

    monkeypatch.setattr(_pl, "platform", lambda *a, **k: (_ for _ in ()).throw(AttributeError("boom")))
    footer = fb._footer()
    assert "reported via report_issue" in footer and "interact" in footer


@pytest.mark.parametrize(
    "a, b, expected",
    [
        ("0.19.2", "0.19.3", True),
        ("0.19.3", "0.19.3", False),
        ("0.19.3", "0.19.2", False),
        ("0.19.9", "0.19.10", True),   # numeric, not lexicographic ('9' > '1' as strings)
        ("0.2.5", "0.19.0", True),
        ("nonsense", "0.19.0", False),  # unparseable → never warns
    ],
)
def test_version_lt_compares_numerically(a, b, expected):
    assert fb._version_lt(a, b) is expected


def test_stale_warning_fires_when_the_reporting_process_is_behind(monkeypatch):
    # A long-lived MCP server froze __version__ at an older startup; the install has since advanced.
    monkeypatch.setattr("interact.__version__", "0.19.0")
    monkeypatch.setattr("interact.installed_version", lambda: "0.19.3")
    banner = fb._stale_warning()
    assert "0.19.0" in banner and "0.19.3" in banner and "may already be fixed" in banner


def test_stale_warning_silent_on_a_current_process(monkeypatch):
    # A fresh process: __version__ == the live installed metadata → no false banner.
    monkeypatch.setattr("interact.__version__", "0.19.3")
    monkeypatch.setattr("interact.installed_version", lambda: "0.19.3")
    assert fb._stale_warning() == ""


def test_report_prepends_the_stale_banner(monkeypatch):
    monkeypatch.setattr("interact.__version__", "0.19.0")
    monkeypatch.setattr("interact.installed_version", lambda: "0.19.3")
    captured: dict = {}

    def fake_run(cmd, **k):
        captured["cmd"] = cmd
        return _Ok()

    monkeypatch.setattr(fb.shutil, "which", lambda c: "/usr/bin/gh")
    monkeypatch.setattr(fb.subprocess, "run", fake_run)
    fb.report("clicks dropped after N actions", "repro steps", "bug")
    body = captured["cmd"][captured["cmd"].index("--body") + 1]
    assert body.lstrip().startswith(">") and "may already be fixed" in body  # banner leads the body
