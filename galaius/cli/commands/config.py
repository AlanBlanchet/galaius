"""The `config` commands: portable settings on the server, machine settings here."""

import sys
from collections.abc import Callable
from typing import TypeVar
from uuid import UUID
from cyclopts import App
from galaius.agents.catalog.connection import CatalogConnectionError
from galaius.cli.command_bootstrap import UserConfig
from galaius.catalog.criteria import Criteria, CriteriaError
from galaius.models.registry import Model
from galaius.runtime import config
from galaius.account.tool_settings import PORTABLE_ENV, MachineNotice, ServerToolSettings, ToolSettingsConflict
from galaius.cli.workspace import WorkspaceCLI


config_app = App(name="config", help="Personal portable settings on the server; credentials and machine settings stay local.")


def _settings_status(*, allow_stale: bool = True) -> dict:
    server = ServerToolSettings.configured()
    if server is None:
        machine = MachineNotice.connection()
        return {"configured": False, "source": "local", "stale": False, "revision": None,
                "values": {key: value for key, value in UserConfig.read_local().items() if key in PORTABLE_ENV},
                "portable_keys": list(PORTABLE_ENV)} | ({"notice": str(MachineNotice(machine))} if machine else {})
    return server.read(allow_stale=allow_stale).status()


@config_app.command(name="status")
def config_status(*, json_out: bool = False) -> None:
    """Report portable settings source, account, revision and explicit cache staleness."""
    WorkspaceCLI.emit(_settings_status)


#: The tools that pick their model by rule, as `ToolRoleModels.role` names them; "sovereign" is the
#: quick (low / medium) review tier.
_TOOL_MODEL_ROLES = ("image", "component", "video", "audio", "sovereign")


def _tool_models() -> dict:
    """Each tool's rule and what it resolves to HERE, best first: the rule is the account's, the
    keys that decide what clears it are this computer's."""
    from galaius.config.settings import _TIER_SOVEREIGN_DEFAULT_CRITERIA

    config.refresh()
    Model.load_registry()
    roles = []
    for role in _TOOL_MODEL_ROLES:
        written = (config.tier_sovereign_criteria if role == "sovereign" else getattr(config, f"{role}_criteria")) or ""
        criterion = written.strip() or (_TIER_SOVEREIGN_DEFAULT_CRITERIA if role == "sovereign" else config.criteria_for(role))
        try:
            rule = Criteria.parse(criterion)
            rule.validate_weights(config.criteria_weights)
            ranked = rule.ranked(available_only=True, weights=config.criteria_weights)
            reason = "" if ranked else rule.explain(available_only=True)
        except CriteriaError as error:
            ranked, reason = [], str(error)
        roles.append({"role": role, "criterion": criterion, "configured": bool(written.strip()),
                      "models": [{"provider": model.provider, "model": model.id} for model in ranked[:5]],
                      "reason": reason[:600]})
    return {"roles": roles}


@config_app.command(name="models")
def config_models(*, json_out: bool = False) -> None:
    """What each tool's model rule (image, component, video, audio, and the quick review tier)
    resolves to on this computer, best first. Change a rule with `galaius config set
    image.criteria "…"`; it applies on every computer of your account."""
    if json_out:
        WorkspaceCLI.emit(_tool_models)
        return
    for role in _tool_models()["roles"]:
        chosen = ", ".join(item["model"] for item in role["models"]) or f"— {role['reason']}"
        print(f"{role['role']:<10} {role['criterion']}{'' if role['configured'] else ' (default)'}\n           → {chosen}")


@config_app.command(name="sync")
def config_sync(*, json_out: bool = False) -> None:
    """Refresh the replaceable personal settings cache online; never upload local entries."""
    WorkspaceCLI.emit(lambda: _settings_status(allow_stale=False))


@config_app.command(name="import-preview")
def config_import_preview(*, json_out: bool = False) -> None:
    """Show only allowlisted local portable overrides for review; never save or print secrets."""
    def preview():
        status = _settings_status(allow_stale=False)
        local = {key: value for key, value in UserConfig.read_local().items() if key in PORTABLE_ENV}
        return {**status, "changes": {key: {"current": status["values"].get(key), "proposed": value}
                for key, value in local.items() if status["values"].get(key) != value}}

    WorkspaceCLI.emit(preview)


def _mask(name: str, value: str) -> str:
    return f"{value[:4]}…{value[-4:]}" if name.endswith("_API_KEY") and len(value) > 8 else value


@config_app.command(name="list")
def config_list() -> None:
    """Show persisted settings (secrets masked)."""
    server = UserConfig.server()
    data = UserConfig.read_local()
    if server:
        snapshot = server.read()
        data = {key: value for key, value in data.items() if key not in PORTABLE_ENV} | snapshot.env
        print(f"Personal server settings · revision {snapshot.settings.revision} · {'STALE cache' if snapshot.stale else 'current'}; other settings local")
    if not data:
        print(f"No settings yet. Set one with: galaius config set image.model <id>\n({UserConfig.PATH})")
        return
    for name, value in sorted(data.items()):
        print(f"  {name} = {_mask(name, value)}")


@config_app.command(name="get")
def config_get(key: str) -> None:
    """Print one persisted value."""
    env = UserConfig.normalize_key(key)
    server = UserConfig.server() if env in PORTABLE_ENV else None
    if server:
        snapshot = _refused_plainly(server.read)
        value = snapshot.env.get(env)
        print(f"Personal server settings · revision {snapshot.settings.revision} · {'STALE cache' if snapshot.stale else 'current'}")
    else:
        value = UserConfig.get(key)
    print(value if value is not None else f"{UserConfig.normalize_key(key)} is unset")


@config_app.command(name="set")
def config_set(key: str, value: str, *, expected_revision: int | None = None, account_id: UUID | None = None, json_out: bool = False) -> None:
    """Persist a setting. e.g. `galaius config set image.model gpt-4o` or `... OPENAI_API_KEY sk-...`.

    Connected portable preferences save on the server without a model call.
    Local API keys and standalone model pins use the existing configuration probe.
    """
    if json_out:
        WorkspaceCLI.emit(lambda: _settings_change(key, value, expected_revision, account_id))
        return
    env = _refused_plainly(lambda: UserConfig.set(key, value))
    source = "personal server settings" if env in PORTABLE_ENV and UserConfig.server() else str(UserConfig.PATH)
    print(f"✓ {env} = {_mask(env, value)}  →  {source}")
    if env in PORTABLE_ENV and UserConfig.server():
        return
    _maybe_probe(env, key, value)


def _maybe_probe(env: str, key: str, value: str) -> None:
    """Verify a just-set model key or pin with one cheap vision call."""
    from galaius.cli import config_check

    if env.endswith("_API_KEY"):
        if config_check.ConfigCheck._criterion_for(env) is None:
            return
        verdict = config_check.ConfigCheck.run(config_check.ConfigCheck.key(env))
    elif key in ("image.model", "image-model", "component.model", "video.model"):
        if config.media_billing != "api_allowed":
            return  # probe would run through a session backend, not this pin
        verdict = config_check.ConfigCheck.run(config_check.ConfigCheck.model(value))
    else:
        return
    print(config_check.ConfigCheck.line(verdict))


@config_app.command(name="unset")
def config_unset(key: str, *, expected_revision: int | None = None, account_id: UUID | None = None, json_out: bool = False) -> None:
    """Remove a persisted setting."""
    if json_out:
        WorkspaceCLI.emit(lambda: _settings_change(key, None, expected_revision, account_id))
        return
    env = UserConfig.normalize_key(key)
    print(f"✓ removed {env}" if _refused_plainly(lambda: UserConfig.unset(key)) else f"{env} was not set")


_T = TypeVar("_T")


def _refused_plainly(write: Callable[[], _T]) -> _T:
    """A refused settings write (machine connection, conflict, signed out) is its sentence and exit 1."""
    try:
        return write()
    except CatalogConnectionError as refused:
        if not (isinstance(refused, MachineNotice) and MachineNotice.said):
            print(refused, file=sys.stderr)
        raise SystemExit(1) from None


def _settings_change(key: str, value: str | None, revision: int | None, account_id: UUID | None) -> dict:
    env = UserConfig.normalize_key(key)
    if env not in PORTABLE_ENV:
        raise ValueError("JSON settings bridge accepts portable settings only")
    server = ServerToolSettings.configured()
    if server is None:
        if revision is not None or account_id is not None:
            raise ToolSettingsConflict("Server connection removed. Reload before saving; draft retained.")
        UserConfig.update({env: value})
        return _settings_status()
    base = server.read(allow_stale=False)
    if revision is None or account_id is None or base.settings.revision != revision or base.account_id != account_id:
        raise ToolSettingsConflict("Settings account or revision changed. Reload; draft retained.")
    return server.update({env: value}, base=base).status()


@config_app.command(name="path")
def config_path() -> None:
    """Print the config file path."""
    print(UserConfig.PATH)
