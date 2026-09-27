"""Background (headless) default + explicit reveal."""


def test_nested_automation_is_background_by_default_and_reveal_is_explicit(monkeypatch, tmp_path):
    from interact.config.settings import Config
    from interact.desktop import nested
    from interact.desktop.backend import nested_server_command

    reaped: list[bool] = []
    started: list[tuple[str, bool]] = []
    monkeypatch.setattr(nested.shutil, "which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(nested.orphans, "reap_orphaned_displays", lambda: reaped.append(True))
    monkeypatch.setattr(nested.NestedBackend, "_free_displays", staticmethod(lambda preferred: [preferred]))
    monkeypatch.setattr(nested.NestedBackend, "_url_shim_dir", lambda self: str(tmp_path))
    monkeypatch.setattr(nested.NestedBackend, "_start_server",
                        lambda self, timeout: started.append((self.server_name, self.headless)))
    background = nested.NestedBackend()
    revealed = nested.NestedBackend(headless=False)
    assert Config().nested_headless is True
    assert started == [("Xvfb", True), ("Xephyr", False)]
    assert nested_server_command(":99", "800x600", True)[0] == "Xvfb"
    assert nested_server_command(":99", "800x600", False)[0] == "Xephyr"
    assert reaped == []
    assert background.display != "" and revealed.display != ""
