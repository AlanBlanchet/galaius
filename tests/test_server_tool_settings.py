"""Portable preferences share an account authority and never export local secrets."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from textual.widgets import Button, Input, Static, TabbedContent
from interact_core import PortableToolSettings, PortableToolSettingsUpdate
from interact_core.accounts import Account, Bootstrap, Workspace

from interact.agents.catalog_connection import CatalogAuthenticationError, CatalogConnection, CatalogConnectionError
from interact.config import SETTINGS, UserConfig
from interact.cli.tui import InteractTUI, WorkspacePane, _build_widget
from interact.server_tool_settings import ServerToolSettings, ToolSettingsConflict


def test_delayed_revision_cannot_replace_newer_cache(settings_server):
    server = ServerToolSettings.configured()
    base = server.read()
    newer = PortableToolSettings(revision=5, values={"video_fps": 15})
    older = PortableToolSettings(revision=4, values={"video_fps": 14})
    server.publish(newer, settings_server["bootstrap"], base.generation, base.session_digest)
    before = server.cache_path.read_bytes()
    result = server.publish(older, settings_server["bootstrap"], base.generation, base.session_digest)
    assert result.settings == newer
    assert server.cache_path.read_bytes() == before


@pytest.mark.parametrize("action,failure", [("save", "disconnect"), ("reset", "disconnect"), ("reset", "offline"), ("reset", 409), ("reset", None)])
async def test_tui_actions_preserve_server_draft_until_save_succeeds(settings_server, monkeypatch, action, failure):
    UserConfig.PATH.write_bytes(b"# recovery\r\nINTERACT_VIDEO_FPS=7\r\n")
    before = UserConfig.PATH.read_bytes()
    app = InteractTUI()
    base = ServerToolSettings.configured().read()
    app._settings_snapshot = base
    app._settings_data = dict(base.env)
    # Keep the real persistence call; avoid executor teardown in the sandboxed runner.
    async def in_process(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr(asyncio, "to_thread", in_process)
    widgets = [_build_widget(setting, base.env) for setting in SETTINGS]
    widgets.extend([Static(id="save-status"), Static(id="status-body")])
    monkeypatch.setattr(InteractTUI, "compose", lambda self: iter(widgets))
    monkeypatch.setattr(InteractTUI, "on_mount", lambda self: None)
    monkeypatch.setattr(app, "_status_text", lambda: "fixture status")
    async with app.run_test(size=(120, 48)) as pilot:
        field = app.query_one("#set-video-fps", Input)
        field.value = "17"
        if failure == "disconnect":
            monkeypatch.setattr(CatalogConnection, "load", lambda: None)
        else:
            settings_server["failure"] = failure
        app.on_button_pressed(Button.Pressed(Button(id=f"btn-{action}-config")))
        assert field.value == "17", "reset must await a confirmed save before changing the draft"
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert field.value == ("17" if failure else "")
        assert UserConfig.PATH.read_bytes() == before
        if failure:
            assert app._settings_snapshot is base
            assert "Draft retained" in str(app.query_one("#save-status", Static).render())


@pytest.fixture
def settings_server(tmp_path, monkeypatch):
    connection = CatalogConnection(endpoint="http://127.0.0.1:8767", auth_mode="preview", workspace_id=uuid4())
    connection.save()
    bootstrap = Bootstrap(account=Account(account_id=uuid4(), email="fixture@example.invalid", locale="en", verified=True),
        workspaces=(Workspace(workspace_id=connection.workspace_id, name="Fixture", role="owner"),),
        current_workspace_id=connection.workspace_id, csrf_token="fixture-csrf", session_expires_at=datetime.now(UTC)+timedelta(hours=1))
    state = {"value": PortableToolSettings(revision=0, values={}), "bootstrap": bootstrap,
             "failure": None, "requests": [], "connection": connection}
    def respond(request):
        state["requests"].append(request)
        if state["failure"] == "offline":
            raise httpx.ConnectError("fixture offline", request=request)
        if request.url.path == "/v1/auth/local-preview":
            return httpx.Response(200, headers={"Set-Cookie": "session=fixture; Path=/"}, json={})
        if isinstance(state["failure"], int):
            return httpx.Response(state["failure"])
        if request.url.path == "/v1/bootstrap":
            return httpx.Response(200, content=state["bootstrap"].model_dump_json())
        assert request.url.path == "/v1/account/tool-settings"
        if state["failure"] == "invalid":
            return httpx.Response(200, json={"revision": -1, "values": {"OPENAI_API_KEY": "no"}})
        if request.method == "PUT":
            if "save_response" in state:
                return httpx.Response(200, json=state["save_response"])
            assert request.headers["x-csrf-token"] == bootstrap.csrf_token
            update = PortableToolSettingsUpdate.model_validate_json(request.content)
            if update.expected_revision != state["value"].revision:
                return httpx.Response(409, json={"code": "tool_settings_conflict", "current": state["value"].model_dump(mode="json")})
            state["value"] = PortableToolSettings(revision=update.expected_revision+1, values=update.values)
        elif "read_response" in state:
            return httpx.Response(200, json=state["read_response"])
        return httpx.Response(200, content=state["value"].model_dump_json())
    original = CatalogConnection.connect
    monkeypatch.setattr(CatalogConnection, "connect", lambda self, **kwargs: original(self, transport=httpx.MockTransport(respond)))
    return state


def test_two_consumers_conflict_and_local_bytes_unchanged(settings_server):
    UserConfig.PATH.write_text("# recovery\nINTERACT_IMAGE_CRITERIA=old\nOPENAI_API_KEY=fixture-secret\n")
    original = UserConfig.PATH.read_bytes()
    first, second = ServerToolSettings.configured(), ServerToolSettings.configured()
    a, b = first.read(), second.read()
    first.update({"INTERACT_IMAGE_CRITERIA": "fixture/new"}, base=a)
    with pytest.raises(ToolSettingsConflict):
        second.update({"INTERACT_IMAGE_CRITERIA": "fixture/other"}, base=b)
    assert UserConfig.read()["INTERACT_IMAGE_CRITERIA"] == "fixture/new"
    assert UserConfig.PATH.read_bytes() == original
    assert all(b"fixture-secret" not in request.content for request in settings_server["requests"])


@pytest.mark.parametrize("failure", [401, 403, "invalid"])
def test_denial_or_invalid_response_disables_transport_fallback(settings_server, failure):
    server = ServerToolSettings.configured()
    server.read()
    settings_server["failure"] = failure
    with pytest.raises(CatalogConnectionError):
        server.read()
    settings_server["failure"] = "offline"
    with pytest.raises(CatalogConnectionError):
        server.read()


def test_transport_cache_is_explicit_and_never_writable(settings_server):
    server = ServerToolSettings.configured()
    fresh = server.read()
    settings_server["failure"] = "offline"
    assert server.read().stale
    with pytest.raises(CatalogConnectionError):
        server.update({"INTERACT_VIDEO_FPS": "20"}, base=fresh)


def test_runtime_server_removal_clears_process_and_recovery_pins(settings_server, monkeypatch):
    from interact.runtime import _LiveConfig
    monkeypatch.setenv("INTERACT_IMAGE_CRITERIA", "process-old")
    UserConfig.PATH.write_text("INTERACT_IMAGE_CRITERIA=recovery-old\n")
    live = _LiveConfig()
    assert live.refresh().image_criteria == ""
    server = ServerToolSettings.configured()
    server.update({"INTERACT_IMAGE_CRITERIA": "fixture/new"}, base=server.read())
    assert live.refresh().image_criteria == "fixture/new"
    server.update({"INTERACT_IMAGE_CRITERIA": None}, base=server.read())
    assert live.refresh().image_criteria == ""


def test_local_unset_keeps_recovery_bytes_and_cached_portable_values(settings_server):
    server = ServerToolSettings.configured()
    server.update({"INTERACT_IMAGE_CRITERIA": "fixture/new"}, base=server.read())
    UserConfig.PATH.write_bytes(b"# preserved\r\nINTERACT_IMAGE_CRITERIA='recovery old'\r\nOPENAI_API_KEY=fixture\r\n")
    before = server.cache_path.read_bytes()
    settings_server["failure"] = "offline"
    assert UserConfig.unset("OPENAI_API_KEY")
    assert UserConfig.PATH.read_bytes() == b"# preserved\r\nINTERACT_IMAGE_CRITERIA='recovery old'\r\n"
    assert server.cache_path.read_bytes() == before


@pytest.mark.parametrize("changes", [
    {"OPENAI_API_KEY": "fixture"}, {"INTERACT_DEBUG_DIR": "fixture"},
    {"INTERACT_MEDIA_PROVIDER_ORDER": "invented-provider"}, {"INTERACT_VLM_MIN_DIM": "9999"},
])
def test_actual_config_boundary_rejects_nonportable_or_unusable_values(settings_server, changes):
    server = ServerToolSettings.configured()
    base = server.read()
    before = len(settings_server["requests"])
    with pytest.raises(ValueError):
        server.update(changes, base=base)
    assert len(settings_server["requests"]) == before


def test_account_switch_refuses_retained_editor_base(settings_server):
    server = ServerToolSettings.configured()
    base = server.read()
    bootstrap = settings_server["bootstrap"]
    settings_server["bootstrap"] = bootstrap.model_copy(update={"account": bootstrap.account.model_copy(update={"account_id": uuid4()})})
    with pytest.raises(ToolSettingsConflict, match="account changed"):
        server.update({"INTERACT_IMAGE_CRITERIA": "fixture/new"}, base=base)
    assert not server.cache_path.exists()


def test_changed_session_cannot_reuse_account_cache(settings_server):
    server = ServerToolSettings.configured()
    server.read()
    cookies = server.connection.session_path()
    cookies.write_text(cookies.read_text().replace('fixture', 'different-session'))
    settings_server["failure"] = "offline"
    with pytest.raises(CatalogConnectionError):
        server.read()


def test_workspace_token_refused_before_credential_file_access(tmp_path):
    connection = CatalogConnection(endpoint="https://example.invalid", auth_mode="token", workspace_id=uuid4(), token_file=tmp_path / "absent-token")
    with pytest.raises(CatalogAuthenticationError, match="work machine"):
        ServerToolSettings(connection=connection).read()


def test_cli_status_and_revision_guard_never_project_secrets(settings_server, capsys):
    from interact.cli.app_commands import config_status, config_set
    UserConfig.PATH.write_text("OPENAI_API_KEY=fixture-secret\nINTERACT_IMAGE_CRITERIA=recovery\n")
    config_status(json_out=True)
    view = json.loads(capsys.readouterr().out)
    assert view["ok"] and view["configured"] and view["revision"] == 0 and not view["stale"]
    assert "fixture-secret" not in json.dumps(view) and "recovery" not in json.dumps(view)
    config_set("image.criteria", "fixture/new", expected_revision=0, account_id=settings_server["bootstrap"].account.account_id, json_out=True)
    assert json.loads(capsys.readouterr().out)["revision"] == 1
    with pytest.raises(SystemExit):
        config_set("image.criteria", "fixture/stale", expected_revision=0, account_id=settings_server["bootstrap"].account.account_id, json_out=True)
    assert not json.loads(capsys.readouterr().out)["ok"]


async def test_tui_keyboard_save_conflict_retains_draft(settings_server, monkeypatch):
    from interact.cli.tui import InteractTUI, WorkspacePane
    from textual.widgets import Button, Input, Static, TabbedContent
    monkeypatch.setattr(InteractTUI, "_load_registry_info", lambda self: None)
    monkeypatch.setattr(WorkspacePane, "on_mount", lambda self: None)
    app = InteractTUI()
    async with app.run_test(size=(120, 48)) as pilot:
        app.query_one(TabbedContent).active = "tab-config"
        field = app.query_one("#set-video-fps", Input)
        field.value = "12"
        field.focus()
        await pilot.press("ctrl+s")
        await pilot.pause(0.3)
        assert settings_server["value"].values.video_fps == 12
        settings_server["value"] = settings_server["value"].model_copy(update={"revision": 2})
        field.value = "17"
        await pilot.press("ctrl+s")
        await pilot.pause(0.3)
        assert field.value == "17"
        assert "Draft retained" in str(app.query_one("#save-status", Static).render())


@pytest.mark.parametrize("client", ["tui", "cli"])
def test_removed_connection_never_turns_server_draft_into_local_write(settings_server, monkeypatch, client):
    from interact.cli.app_commands import _settings_change
    base = ServerToolSettings.configured().read()
    UserConfig.PATH.write_text("# recovery\nINTERACT_VIDEO_FPS=7\n")
    before = UserConfig.PATH.read_bytes()
    monkeypatch.setattr(CatalogConnection, "load", lambda: None)
    with pytest.raises(ToolSettingsConflict, match="connection removed"):
        if client == "tui":
            UserConfig.update({"INTERACT_VIDEO_FPS": "12"}, base=base)
        else:
            _settings_change("video.fps", "12", base.settings.revision, base.account_id)
    assert UserConfig.PATH.read_bytes() == before


def test_import_preview_is_allowlisted_diff_without_upload(settings_server, capsys):
    from interact.cli.app_commands import config_import_preview
    UserConfig.PATH.write_text("INTERACT_VIDEO_FPS=12\nOPENAI_API_KEY=fixture-secret\nINTERACT_MEDIA_BILLING=api_allowed\n")
    config_import_preview(json_out=True)
    output = capsys.readouterr().out
    assert json.loads(output)["changes"] == {"INTERACT_VIDEO_FPS": {"current": None, "proposed": "12"}}
    assert "fixture-secret" not in output and "api_allowed" not in output
    assert all(request.method != "PUT" for request in settings_server["requests"])


def test_cache_without_timezone_cannot_be_used_offline(settings_server):
    server = ServerToolSettings.configured()
    snapshot = server.read()
    server.cache_path.write_text(snapshot.model_copy(update={"expires_at": datetime.now() + timedelta(hours=1)}).model_dump_json())
    settings_server["failure"] = "offline"
    with pytest.raises(CatalogConnectionError, match="no verified account cache"):
        server.read()


@pytest.mark.parametrize("response", [
    {"revision": 0, "values": {"video_fps": 12}},
    {"revision": 1, "values": {"video_fps": 9}},
    {"revision": 1, "values": {"unknown": "ignored?"}},
])
def test_malformed_save_never_publishes_cache_or_claims_success(settings_server, response):
    server = ServerToolSettings.configured()
    base = server.read()
    settings_server["save_response"] = response
    with pytest.raises(CatalogConnectionError, match="Invalid settings save response"):
        server.update({"INTERACT_VIDEO_FPS": "12"}, base=base)
    assert not server.cache_path.exists()
    assert base.settings.revision == 0


def test_cli_structured_argv_reaches_typed_cas(settings_server, capsys):
    from interact.cli.app_commands import config_app
    with pytest.raises(SystemExit) as exited:
        config_app(["set", "video.fps", "12", "--expected-revision", "0", "--account-id",
            str(settings_server["bootstrap"].account.account_id), "--json-out"])
    assert exited.value.code == 0
    assert json.loads(capsys.readouterr().out)["revision"] == 1
    assert settings_server["value"].values.video_fps == 12


@pytest.mark.parametrize("values,compatible", [
    ({"vlm_max_dim": 1500}, True),
    ({"vlm_min_dim": 1500}, False),
    ({"vlm_max_dim": 600}, False),
    ({"vlm_min_dim": 1400, "vlm_max_dim": 1500}, True),
])
def test_sparse_dimensions_use_client_defaults_not_host_environment(settings_server, monkeypatch, values, compatible):
    monkeypatch.setenv("INTERACT_VLM_MIN_DIM", "1600")
    monkeypatch.setenv("INTERACT_VLM_MAX_DIM", "2000")
    server = ServerToolSettings.configured()
    server.read()
    assert server.cache_path.exists()
    settings_server["read_response"] = {"revision": 1, "values": values}
    if compatible:
        snapshot = server.read()
        assert snapshot.settings.values.model_dump(exclude_none=True) == values
        assert not snapshot.stale
    else:
        with pytest.raises(CatalogConnectionError, match="Invalid personal settings response") as error:
            server.read()
        assert "minimum image dimension cannot exceed maximum" in str(error.value.__cause__)
        assert not server.cache_path.exists()


@pytest.fixture
def machine_connection(tmp_path, monkeypatch):
    """`interact login` / `machine connect` leave a workspace-token connection: a machine, no person."""
    for name in ("LC_ALL", "LC_MESSAGES"):
        monkeypatch.delenv(name, raising=False)
    connection = CatalogConnection(endpoint="https://interact.example.invalid", auth_mode="token", workspace_id=uuid4(), token_file=tmp_path / "machine.key")
    connection.save()
    UserConfig.PATH.parent.mkdir(parents=True, exist_ok=True)
    UserConfig.PATH.write_text("INTERACT_VIDEO_FPS=9\nOPENAI_API_KEY=fixture-secret\n")
    return connection


async def test_machine_connection_runs_on_local_settings(machine_connection):
    from interact.runtime import _LiveConfig
    assert UserConfig.read()["INTERACT_VIDEO_FPS"] == "9"
    assert _LiveConfig().refresh().video_fps == 9
    app = InteractTUI()  # bare `interact` on a terminal
    async with app.run_test(size=(120, 48)) as pilot:
        await pilot.pause()
        assert "work machine" in str(app.query_one("#save-status", Static).render())
        assert any("work machine" in str(toast.message) for toast in app._notifications)


@pytest.mark.parametrize("lang,sentence", [("fr_FR.UTF-8", "Ce PC est connecté comme machine de travail"), ("en_US.UTF-8", "This PC is connected as a work machine")])
def test_machine_connection_refuses_personal_write_in_plain_words(machine_connection, monkeypatch, capsys, lang, sentence):
    from interact.cli.app_commands import config_set, config_unset
    monkeypatch.setenv("LANG", lang)
    before = UserConfig.PATH.read_bytes()
    for write in (lambda: config_set("video.fps", "12"), lambda: config_unset("video.fps")):
        with pytest.raises(SystemExit) as refused:
            write()
        assert refused.value.code == 1
        error = capsys.readouterr().err
        assert sentence in error and machine_connection.endpoint in error and "Traceback" not in error
    with pytest.raises(SystemExit):
        config_set("video.fps", "12", json_out=True)
    view = json.loads(capsys.readouterr().out)
    assert view["code"] == "authorization" and sentence in view["message"]
    assert UserConfig.PATH.read_bytes() == before
    UserConfig.set("desktop.target", "local")  # machine settings stay writable here
    assert UserConfig.read_local()["INTERACT_VIDEO_FPS"] == "9"


def test_machine_connection_notice_is_said_once(machine_connection, monkeypatch, capsys):
    import io
    from interact.cli.app_commands import config_set
    from interact.server_tool_settings import MachineNotice
    class Terminal(io.StringIO):
        def isatty(self):
            return True
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    first, second, piped = Terminal(), Terminal(), io.StringIO()
    monkeypatch.setattr(MachineNotice, "said", False)
    MachineNotice.say(piped)
    MachineNotice.say(first)
    MachineNotice.say(second)
    assert piped.getvalue() == "" and "work machine" in first.getvalue() and second.getvalue() == ""
    with machine_connection.access_guard():  # the remembered notice keeps the catalog session directory private
        pass
    with pytest.raises(SystemExit):  # the refusal right after the notice does not repeat it
        config_set("video.fps", "12")
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("auth_mode", ["token", "preview"])
def test_session_directory_left_open_by_the_notice_is_made_private(tmp_path, auth_mode):
    import io
    from interact.server_tool_settings import MachineNotice
    connection = CatalogConnection(endpoint="http://127.0.0.1:8767", auth_mode=auth_mode, workspace_id=uuid4(),
                                   token_file=tmp_path / "machine.key" if auth_mode == "token" else None)
    connection.save()
    sessions = connection.session_path().parent
    sessions.mkdir(mode=0o775, parents=True)
    sessions.chmod(0o775)
    connection.session_path().with_suffix(".machine-notice").touch()
    MachineNotice.say(io.StringIO())
    with connection.access_guard():
        pass
