"""VS Code extension — manifest, staleness detection, delivery, `doctor` status.

Formerly `test_extension_manifest`, `test_extension_staleness`, `test_extension_delivery`
and `test_extension_status`. One subject (the extension `interact` ships in `clients/vscode`),
four life-cycle facets: what its manifest declares, how the running install can drift, how a
fresh `.vsix` gets delivered without killing the session that asked, and what `doctor` does with
the machine-readable status.

Delivery must never shell out to `code --install-extension`: that reload kills the agent session
issuing it — observed 2026-08-19, day of work sat undelivered because the delivery gate was the
one thing killing the delivery session.
"""

from __future__ import annotations

import json
import re
import time
import zipfile
from pathlib import Path

import pytest

from interact import extension_status as es
from interact import server_registry as reg
from interact.cli import app_commands


# ── Manifest ────────────────────────────────────────────────────────────────────────────────
# package.json is hand-maintained. Nothing generates it, so it drifts from `config/schema.py`
# silently — an unregistered key just never appears in the VS Code settings UI.

EXT_ROOT = Path(__file__).resolve().parents[1] / "clients" / "vscode"

# Settings the extension owns that have no Python counterpart (UI-only, never sent to the server).
_EXTENSION_ONLY = {"interact.projectPath", "interact.display.currency"}
_BENCH_RE = re.compile(r"bench|grounding|eval", re.I)


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((EXT_ROOT / "package.json").read_text())


@pytest.fixture(scope="module")
def extension_ts() -> str:
    return (EXT_ROOT / "src" / "extension.ts").read_text()


#: Any user home: this file asserts on a cmdline SHAPE, never on whose machine wrote it.
HOME = str(Path.home()).encode()


def test_commands_use_interact_namespace(manifest):
    cmds = {c["command"] for c in manifest["contributes"]["commands"]}
    assert "interact.openDashboard" in cmds
    assert "interact.selectModel" in cmds
    assert "interact.manageApiKeys" in cmds
    assert not any(c.startswith("interactMcp.") for c in cmds), cmds


def test_configuration_keys_use_interact_namespace(manifest):
    props = manifest["contributes"]["configuration"]["properties"]
    assert props, "expected configuration properties to be declared"
    for key in props:
        assert key.startswith("interact."), key
        assert not key.startswith("interactMcp."), key


def test_every_python_setting_is_registered(manifest):
    from interact.config import SETTINGS

    declared = set(manifest["contributes"]["configuration"]["properties"])
    expected = {f"interact.{s.key}" for s in SETTINGS}
    assert not (expected - declared), f"in schema.py but not package.json: {sorted(expected - declared)}"
    assert not (declared - expected - _EXTENSION_ONLY), (
        f"in package.json but not schema.py: {sorted(declared - expected - _EXTENSION_ONLY)}"
    )


def test_debug_dir_default_matches_python(manifest, monkeypatch):
    """The manifest default is what the dashboard resolves the usage log against when the user
    never touched the setting — it must be Python's ``Config.debug_dir``, or the panel reads a
    file nothing writes (see tests/test_paths.py)."""
    from interact.config import Config

    monkeypatch.delenv("INTERACT_DEBUG_DIR", raising=False)
    prop = manifest["contributes"]["configuration"]["properties"]["interact.debug.dir"]
    default = Config().debug_dir
    assert prop["default"] == "~/" + default.relative_to(Path.home()).as_posix()


def test_activates_on_startup(manifest):
    assert "onStartupFinished" in manifest.get("activationEvents", [])


@pytest.mark.parametrize(
    "needle",
    [
        "$(eye) Interact",
        "statusBar.show()",
        "interact.openDashboard",
    ],
)
def test_extension_ts_has_status_bar_wiring(extension_ts, needle):
    assert needle in extension_ts, f"missing {needle!r} in extension.ts"


def test_no_benchmark_run_commands(manifest):
    """Benchmark/grounding runs are developer-only CLIs — never user-facing commands."""
    cmds = [c["command"] for c in manifest["contributes"]["commands"]]
    offenders = [c for c in cmds if _BENCH_RE.search(c)]
    assert not offenders, f"commands trigger paid evals: {offenders}"


def test_dashboard_has_no_run_grounding_bench():
    text = (EXT_ROOT / "src" / "dashboard.ts").read_text()
    assert "runGroundingBench" not in text


# ── Staleness: two ways the installed extension goes out of date ───────────────────────────
# The installed extension can be an older VERSION than the tree, or the same version with a
# newer compiled bundle than the running editor started before.


def test_an_older_installed_version_is_reported(tmp_path, monkeypatch):
    (tmp_path / "alanblanchet.interact-0.27.0").mkdir()
    monkeypatch.setattr("interact.extension_status._extensions_dir", lambda: tmp_path)
    monkeypatch.setattr("interact.extension_status._tree_version", lambda: "0.28.0")

    st = es.extension_status()

    assert st is not None
    assert st["installed"] == "0.27.0"
    assert st["tree"] == "0.28.0"
    assert st["reason"] == "version"


def test_a_matching_version_with_a_newer_build_than_the_editor_is_still_stale(tmp_path, monkeypatch):
    ext = tmp_path / "alanblanchet.interact-0.28.0"
    ext.mkdir()
    (ext / "out").mkdir()
    (ext / "out" / "extension.js").write_text("// rebuilt just now\n")
    tree = tmp_path / "tree"
    (tree / "out").mkdir(parents=True)
    (tree / "out" / "extension.js").write_text("// rebuilt just now\n")
    monkeypatch.setattr("interact.extension_status._extensions_dir", lambda: tmp_path)
    monkeypatch.setattr("interact.extension_status._extension_dir", lambda: tree)
    monkeypatch.setattr("interact.extension_status._tree_version", lambda: "0.28.0")
    monkeypatch.setattr("interact.extension_status._editor_starts", lambda: [time.time() - 3600])

    st = es.extension_status()

    assert st is not None and (st["reason"], st["remedy"]) == ("code", "restart"), st


@pytest.mark.parametrize(
    ("installed_bundle", "expected_status"),
    [
        pytest.param(b"exports.activate = 'current';\n", None, id="matching-bundle"),
        pytest.param(
            b"exports.activate = 'stale';\n",
            ("code", "install"),
            id="different-bundle",
        ),
    ],
)
def test_matching_version_with_an_editor_started_after_the_build_uses_compiled_bundle(
    tmp_path, monkeypatch, installed_bundle, expected_status
):
    extensions = tmp_path / "extensions"
    ext = extensions / "alanblanchet.interact-0.28.0"
    installed_compiled = ext / "out" / "extension.js"
    installed_compiled.parent.mkdir(parents=True)
    installed_compiled.write_bytes(installed_bundle)

    tree = tmp_path / "tree"
    tree_compiled = tree / "out" / "extension.js"
    tree_compiled.parent.mkdir(parents=True)
    tree_compiled.write_bytes(b"exports.activate = 'current';\n")

    monkeypatch.setattr("interact.extension_status._extensions_dir", lambda: extensions)
    monkeypatch.setattr("interact.extension_status._extension_dir", lambda: tree)
    monkeypatch.setattr("interact.extension_status._tree_version", lambda: "0.28.0")
    monkeypatch.setattr(
        "interact.extension_status._editor_starts",
        lambda: [installed_compiled.stat().st_mtime + 60],
    )

    status = es.extension_status()
    actual_status = None if status is None else (status["reason"], status["remedy"])

    assert actual_status == expected_status, status


def test_no_installed_extension_is_not_a_complaint(tmp_path, monkeypatch):
    """Plenty of people use interact purely as an MCP server; that is not a problem to report."""
    monkeypatch.setattr("interact.extension_status._extensions_dir", lambda: tmp_path)
    assert es.extension_status() is None


def test_it_reuses_the_process_start_helper():
    """Same question as the MCP half, so it must not grow a second answer to it."""
    assert es._process_start is reg._process_start


def test_it_does_not_crash_where_proc_does_not_exist(monkeypatch, tmp_path):
    """`/proc` is Linux-only. Reading it unguarded means `interact doctor` — a DIAGNOSTIC, the
    command someone runs precisely when things are wrong — tracebacks for every macOS and Windows
    user with the extension installed. The server side already carries a platform guard; this one
    did not, which is the same class the project has been bitten by in CI before."""
    ext = tmp_path / "alanblanchet.interact-0.28.0"
    (ext / "out").mkdir(parents=True)
    (ext / "out" / "extension.js").write_text("x")
    tree = tmp_path / "tree"
    (tree / "out").mkdir(parents=True)
    (tree / "out" / "extension.js").write_text("x")
    monkeypatch.setattr(es, "_extensions_dir", lambda: tmp_path)
    monkeypatch.setattr(es, "_extension_dir", lambda: tree)
    monkeypatch.setattr(es, "_tree_version", lambda: "0.28.0")

    def no_proc():
        raise FileNotFoundError("/proc")

    monkeypatch.setattr(es, "_iter_proc", no_proc)

    assert es.extension_status() is None  # unknown, not a crash


def test_a_language_server_is_not_an_editor(tmp_path, monkeypatch):
    """VS Code spawns pylance/tsserver/copilot as `code <server.js>` with ELECTRON_RUN_AS_NODE and
    no `--type=`, so a filter of "basename is code, no --type=" counted 14 editors for one open
    window. The verdict was still right — a server is a child, so it cannot predate its parent —
    but a count nobody can reconcile with their screen is not a diagnostic."""
    assert es._is_editor_cmdline(b"/usr/share/code/code\x00") is True
    assert es._is_editor_cmdline(b"/usr/share/code/code\x00--ozone-platform-hint=auto\x00") is True
    assert es._is_editor_cmdline(b"/usr/share/code/code\x00--type=renderer\x00") is False
    assert es._is_editor_cmdline(
        b"/usr/share/code/code\x00" + HOME + b"/.vscode/extensions/ms-python/server.js\x00--stdio\x00"
    ) is False, "a language server run through the code binary is not an editor window"


def test_other_vs_code_flavours_are_recognised():
    """Insiders, VSCodium and code-server are the same product for this purpose."""
    for exe in (b"code-insiders", b"codium", b"code-server", b"electron"):
        assert es._is_editor_cmdline(b"/usr/bin/" + exe + b"\x00"), exe


# ── Delivery: unpack the .vsix, never `code --install-extension` ──────────────────────────


def _vsix(path, version="9.9.9", body="console.log(1)"):
    """A minimal but REAL .vsix: a zip whose payload lives under `extension/`."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("extension/package.json", f'{{"name":"interact","version":"{version}"}}')
        z.writestr("extension/out/extension.js", body)
        z.writestr("[Content_Types].xml", "<Types/>")
    return path


def test_it_unpacks_the_payload_into_a_version_directory(tmp_path, monkeypatch):
    ext = tmp_path / "extensions"
    ext.mkdir()
    monkeypatch.setattr(es, "_extensions_dir", lambda: ext)
    v = _vsix(tmp_path / "interact-9.9.9.vsix")

    assert es._install_vsix(v, "9.9.9") is True
    target = ext / "alanblanchet.interact-9.9.9"
    assert (target / "package.json").is_file(), "the payload never landed"
    assert (target / "out" / "extension.js").read_text() == "console.log(1)"
    assert not (target / "extension").exists(), "the `extension/` prefix must be stripped"


def test_it_never_shells_out_to_the_editor(tmp_path, monkeypatch):
    """The whole point. Any `code --install-extension` here reloads the hosts and kills us."""
    ext = tmp_path / "extensions"
    ext.mkdir()
    monkeypatch.setattr(es, "_extensions_dir", lambda: ext)
    called = []
    monkeypatch.setattr(es, "_run", lambda argv, cwd=None: called.append(argv) or (0, ""))

    es._install_vsix(_vsix(tmp_path / "interact-9.9.9.vsix"), "9.9.9")
    assert not called, f"delivery shelled out to {called} — that reload is what kills the session"


def test_a_reinstall_replaces_rather_than_merges(tmp_path, monkeypatch):
    """A stale file left behind from a previous version is a file the host will happily load."""
    ext = tmp_path / "extensions"
    (ext / "alanblanchet.interact-9.9.9" / "out").mkdir(parents=True)
    (ext / "alanblanchet.interact-9.9.9" / "out" / "gone.js").write_text("stale")
    monkeypatch.setattr(es, "_extensions_dir", lambda: ext)

    es._install_vsix(_vsix(tmp_path / "interact-9.9.9.vsix"), "9.9.9")
    assert not (ext / "alanblanchet.interact-9.9.9" / "out" / "gone.js").exists()


def test_a_corrupt_package_fails_loudly_rather_than_half_installing(tmp_path, monkeypatch):
    ext = tmp_path / "extensions"
    ext.mkdir()
    monkeypatch.setattr(es, "_extensions_dir", lambda: ext)
    bad = tmp_path / "broken.vsix"
    bad.write_bytes(b"not a zip")

    assert es._install_vsix(bad, "9.9.9") is False
    assert not list(ext.iterdir()), "a failed install must leave nothing behind"


# ── Doctor: pair the detector with the remedy, and apply it ───────────────────────────────


def test_a_stale_extension_can_be_rebuilt_and_installed(monkeypatch, tmp_path):
    ran: list[list[str]] = []
    monkeypatch.setattr(es, "extension_status",
                        lambda: {"installed": "0.28.0", "tree": "0.29.0", "reason": "version"})
    monkeypatch.setattr(es, "_run", lambda argv, cwd=None: ran.append(argv) or (0, ""))
    monkeypatch.setattr(es, "_extension_dir", lambda: tmp_path)
    monkeypatch.setattr(es, "_tree_version", lambda: "0.29.0")
    ext = tmp_path / "extensions"
    ext.mkdir()
    monkeypatch.setattr(es, "_extensions_dir", lambda: ext)
    with zipfile.ZipFile(tmp_path / "interact-0.29.0.vsix", "w") as z:
        z.writestr("extension/package.json", '{"version":"0.29.0"}')

    assert es.deliver_extension() is True
    joined = [" ".join(a) for a in ran]
    assert any("package" in j for j in joined), f"never packaged: {joined}"
    assert (ext / "alanblanchet.interact-0.29.0" / "package.json").is_file(), "never installed"
    assert not any("--install-extension" in j for j in joined), (
        f"still shelling out to the editor: {joined}"
    )


def test_nothing_is_installed_when_the_extension_is_already_current(monkeypatch):
    """Reinstalling for no reason costs the user every editor window's extension host."""
    ran: list[list[str]] = []
    monkeypatch.setattr(es, "extension_status", lambda: None)
    monkeypatch.setattr(es, "_run", lambda argv, cwd=None: ran.append(argv) or (0, ""))
    assert es.deliver_extension() is False
    assert ran == []


def test_a_failed_package_is_reported_rather_than_claimed(monkeypatch, tmp_path):
    """The failure this whole feature exists to prevent is a delivery that was never checked."""
    monkeypatch.setattr(es, "extension_status",
                        lambda: {"installed": "0.28.0", "tree": "0.29.0", "reason": "version"})
    monkeypatch.setattr(es, "_run", lambda argv, cwd=None: (1, "vsce exploded"))
    monkeypatch.setattr(es, "_extension_dir", lambda: tmp_path)
    assert es.deliver_extension() is False


@pytest.mark.parametrize(
    ("status", "message", "expected_deliveries"),
    [
        pytest.param(
            {
                "installed": "0.28.0",
                "tree": "0.28.0",
                "reason": "code",
                "remedy": "install",
            },
            "compiled bundle differs",
            1,
            id="bundle-mismatch-installs",
        ),
        pytest.param(
            {
                "installed": "0.28.0",
                "tree": "0.28.0",
                "reason": "code",
                "remedy": "restart",
                "behind": 1,
                "running": 1,
            },
            "fully restart",
            0,
            id="loaded-host-restarts",
        ),
    ],
)
def test_doctor_applies_the_machine_readable_extension_remedy(
    monkeypatch, capsys, status, message, expected_deliveries
):
    deliveries = []
    monkeypatch.setattr(app_commands, "extension_status", lambda: status)
    monkeypatch.setattr(
        app_commands,
        "deliver_extension",
        lambda: deliveries.append(status["remedy"]) or True,
    )

    app_commands._print_extension_status(fix=True)

    assert message in capsys.readouterr().out
    assert deliveries == ["install"] * expected_deliveries
