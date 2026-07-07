"""launch_app on a BROWSER command must isolate it from the user's real browser instance.

Without an isolated profile, `google-chrome <url>` in the sandbox just signals the user's running
Chrome via its profile singleton lock: the URL opens on the REAL desktop, the sandboxed process
exits, and the user is left staring at an empty Xephyr window ("when the browser opens, a xephyr
page also opens"). Firefox has the same remote-singleton behavior. So launch_app injects a
sandbox-local profile dir + first-run silencers for known browsers — unless the caller already
chose a profile."""

import pytest

from interact.server import _browser_isolate


def _flags(argv: list[str]) -> str:
    return " ".join(argv)


@pytest.mark.parametrize("exe", ["google-chrome", "chromium", "brave-browser", "microsoft-edge",
                                 "/usr/bin/google-chrome-stable"])
def test_chromium_family_gets_an_isolated_profile(exe):
    argv, note = _browser_isolate([exe, "--new-window", "http://localhost:3000"], ":99")
    joined = _flags(argv)
    assert "--user-data-dir=" in joined and ":99" not in joined.split("--user-data-dir=")[0]
    assert "--no-first-run" in argv and "--no-default-browser-check" in argv
    assert "isolated profile" in note
    assert argv[-1] == "http://localhost:3000"  # the URL stays last (chrome treats it as the arg)


def test_firefox_gets_no_remote_and_a_profile():
    argv, note = _browser_isolate(["firefox", "http://localhost:3000"], ":99")
    assert "--no-remote" in argv
    assert "--profile" in argv
    assert "isolated profile" in note


def test_caller_chosen_profile_is_respected():
    argv, note = _browser_isolate(
        ["google-chrome", "--user-data-dir=/tmp/mine", "http://x"], ":99"
    )
    assert argv == ["google-chrome", "--user-data-dir=/tmp/mine", "http://x"] and note == ""


def test_non_browser_commands_are_untouched():
    argv, note = _browser_isolate(["xterm", "-e", "top"], ":99")
    assert argv == ["xterm", "-e", "top"] and note == ""


def test_env_prefix_is_seen_through():
    argv, note = _browser_isolate(["env", "LANG=C", "google-chrome", "http://x"], ":99")
    assert any(a.startswith("--user-data-dir=") for a in argv)


def test_profile_dir_is_stable_per_display_and_browser():
    """Same display + browser → same profile dir, so a relaunch joins ITS OWN sandbox instance
    (an in-sandbox singleton is fine); a different display never shares a profile."""
    a1, _ = _browser_isolate(["google-chrome", "http://x"], ":99")
    a2, _ = _browser_isolate(["google-chrome", "http://x"], ":99")
    b, _ = _browser_isolate(["google-chrome", "http://x"], ":100")
    dir_of = lambda argv: next(x for x in argv if x.startswith("--user-data-dir="))
    assert dir_of(a1) == dir_of(a2)
    assert dir_of(a1) != dir_of(b)
