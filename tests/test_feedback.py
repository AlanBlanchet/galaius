"""report_issue must reliably get a problem to the maintainers (GitHub issue when gh is there,
local file otherwise) and must NEVER raise — reporting a bug can't itself blow up."""

import pytest

import interact.feedback as fb


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


def test_fallback_offers_a_prefilled_issue_url(monkeypatch, tmp_path):
    """Without gh, the report must still be one click from delivered: the message carries a
    prefilled new-issue URL (title + body encoded), not just a bare repo link."""
    monkeypatch.setattr(fb.shutil, "which", lambda c: None)
    monkeypatch.setattr(fb, "FEEDBACK_DIR", tmp_path / "feedback")
    out = fb.report("crash on launch", "details here")
    assert f"https://github.com/{fb.REPO}/issues/new?" in out
    assert "crash%20on%20launch" in out


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
