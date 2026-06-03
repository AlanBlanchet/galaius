"""Version single-source-of-truth helpers — the spine of the release automation."""

import json

import pytest

from interact import versioning


@pytest.mark.parametrize("text,expected", [("1.2.3", (1, 2, 3)), ("v0.10.4", (0, 10, 4)), ("2.0.0-rc1", (2, 0, 0))])
def test_parse(text, expected):
    assert versioning.parse(text) == expected


@pytest.mark.parametrize("bad", ["", "1.2", "x.y.z", "v1"])
def test_parse_rejects_non_semver(bad):
    with pytest.raises(ValueError):
        versioning.parse(bad)


@pytest.mark.parametrize("candidate,baseline,newer", [
    ("0.2.0", "0.1.0", True), ("1.0.0", "0.9.9", True), ("0.2.0", "0.2.0", False),
    ("0.1.9", "0.2.0", False), ("garbage", "0.1.0", False),
])
def test_is_newer(candidate, baseline, newer):
    assert versioning.is_newer(candidate, baseline) is newer


@pytest.mark.parametrize("version,part,expected", [
    ("1.2.3", "major", "2.0.0"), ("1.2.3", "minor", "1.3.0"), ("1.2.3", "patch", "1.2.4"),
])
def test_bump(version, part, expected):
    assert versioning.bump(version, part) == expected


def _project(tmp_path, py="0.1.0", pkg="0.1.0"):
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "interact"\nversion = "{py}"\n')
    ext = tmp_path / "vscode-extension"
    ext.mkdir()
    if pkg is not None:
        (ext / "package.json").write_text(json.dumps({"name": "interact", "version": pkg}))
    return tmp_path


def test_check_in_sync_ok(tmp_path):
    assert versioning.check_in_sync(_project(tmp_path, "0.2.0", "0.2.0")) == []


def test_check_in_sync_detects_mismatch(tmp_path):
    errors = versioning.check_in_sync(_project(tmp_path, "0.2.0", "0.1.0"))
    assert errors and "mismatch" in errors[0]


def test_set_version_updates_both_files(tmp_path):
    root = _project(tmp_path, "0.1.0", "0.1.0")
    versioning.set_version(root, "0.3.1")
    assert versioning.pyproject_version(root) == "0.3.1"
    assert versioning.package_json_version(root) == "0.3.1"
    assert versioning.check_in_sync(root) == []


def test_repo_root_finds_pyproject(tmp_path):
    root = _project(tmp_path)
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    assert versioning.repo_root(nested) == root


def test_main_check_returns_nonzero_on_mismatch(tmp_path, monkeypatch):
    root = _project(tmp_path, "0.2.0", "0.1.0")
    monkeypatch.setattr(versioning, "repo_root", lambda *a, **k: root)
    assert versioning.main(["check"]) == 1
    assert versioning.main(["current"]) == 0
