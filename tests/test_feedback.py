"""report_issue must reliably get a problem to the maintainers (GitHub issue when gh is there,
local file otherwise) and must NEVER raise — reporting a bug can't itself blow up."""

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
