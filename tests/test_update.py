"""GitHub update check (TUI banner + `interact update`)."""

import io
import json

from interact import update


def test_available_update_when_remote_newer(monkeypatch):
    monkeypatch.setattr(update, "is_editable_install", lambda: False)
    monkeypatch.setattr(update, "latest_remote_version", lambda *a, **k: "9.9.9")
    monkeypatch.setattr(update, "installed_version", lambda: "0.2.0")
    assert update.available_update() == "9.9.9"


def test_editable_install_is_never_offered_an_update(monkeypatch):
    monkeypatch.setattr(update, "is_editable_install", lambda: True)
    monkeypatch.setattr(update, "latest_remote_version", lambda *a, **k: "9.9.9")
    monkeypatch.setattr(update, "installed_version", lambda: "0.2.0")
    assert update.available_update() is None  # dev checkout — don't nag


def test_no_update_when_current_or_offline(monkeypatch):
    monkeypatch.setattr(update, "is_editable_install", lambda: False)
    monkeypatch.setattr(update, "installed_version", lambda: "0.2.0")
    monkeypatch.setattr(update, "latest_remote_version", lambda *a, **k: "0.2.0")
    assert update.available_update() is None
    monkeypatch.setattr(update, "latest_remote_version", lambda *a, **k: None)  # offline
    assert update.available_update() is None


def test_latest_remote_version_parses_release(monkeypatch):
    def fake_urlopen(request, timeout=0):
        return io.BytesIO(json.dumps({"tag_name": "v1.4.2"}).encode())

    monkeypatch.setattr(update.urllib.request, "urlopen", fake_urlopen)
    assert update.latest_remote_version() == "1.4.2"


def test_latest_remote_version_soft_fails(monkeypatch):
    def boom(request, timeout=0):
        raise OSError("network down")

    monkeypatch.setattr(update.urllib.request, "urlopen", boom)
    assert update.latest_remote_version() is None  # never raises
