"""Decoding a child's exit status must not need signals that only exist on POSIX.

Regression: `_SIGNAL_CAUSE` was keyed by `signal.SIGKILL` / `signal.SIGHUP` objects, evaluated at
MODULE level. Neither exists on Windows, so importing `interact.desktop.backend` raised
`AttributeError: module 'signal' has no attribute 'SIGKILL'` — and because nearly every module
imports it, the ENTIRE Windows suite failed at collection. Linux and macOS were green, so a local
run could not see it; only the Windows CI leg could.

Keying by NAME keeps the table platform-independent: a signal absent here simply never matches.
"""

import signal

import pytest

from interact.desktop.backend import _SIGNAL_CAUSE, _exit_reason


def test_signal_table_is_keyed_by_name_not_by_platform_specific_objects():
    """The structural guard. A `signal.SIGKILL` key would re-break Windows collection at import,
    and no Linux run would notice."""
    assert _SIGNAL_CAUSE, "the table should not be empty"
    assert all(isinstance(k, str) for k in _SIGNAL_CAUSE), (
        f"keys must be signal NAMES: {[k for k in _SIGNAL_CAUSE if not isinstance(k, str)]}"
    )


@pytest.mark.parametrize(
    "returncode, expected",
    [
        (None, "no exit status recorded"),
        (0, "exited rc=0"),
        (1, "exited rc=1"),
        (-9, "SIGKILL"),      # the OOM-killer case the sandbox diagnostics exist for
        (-11, "SIGSEGV"),
        (-15, "SIGTERM"),
    ],
)
def test_exit_reason_reads_the_cause(returncode, expected):
    assert expected in _exit_reason(returncode)


def test_the_oom_case_names_the_outside_killer():
    """A sandbox death by SIGKILL must point away from interact — that distinction was the whole
    point of decoding the status (#84)."""
    assert "OOM" in _exit_reason(-9)


def test_an_unknown_signal_number_degrades_instead_of_raising():
    assert "signal" in _exit_reason(-99).lower()


def test_a_signal_missing_on_this_platform_is_simply_not_matched(monkeypatch):
    """Windows has no SIGKILL. The lookup must miss quietly, never raise."""
    monkeypatch.delattr(signal, "SIGKILL", raising=False)
    assert _exit_reason(0) == "exited rc=0"
    assert isinstance(_exit_reason(-9), str)
