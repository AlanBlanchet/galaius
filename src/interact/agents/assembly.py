"""Composition root for private conversation transports and route discovery."""

import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, PrivateAttr, ValidationError

from interact.agents.codex_transport import _CodexTransport
from interact.agents.completion_transport import _CompletionTransport
from interact.agents.policy import Policy, PolicyError
from interact.agents.protocol import ConversationRoute, RouteAvailability
from interact.agents.providers import CodexProvider, provider_for
from interact.agents.registry import AgentRun
from interact.agents.transport import _ConversationTransport
from interact.models import Model, ModelCapability


def _codex_failure_reason(error: BaseException, *, phase: str) -> str:
    detail = " ".join(str(error).split())[:400]
    if not detail:
        detail = error.__class__.__name__
    return (
        f"Codex app-server {phase} failed: {detail}. Check the installed Codex CLI and sign-in, "
        "then retry; use an API route while the local session is unavailable."
    )


class _TransportRegistry(BaseModel):
    """Injected route-to-transport assembly; concrete adapters end at this boundary."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    workspace_root: Path
    _session: _CodexTransport | None = PrivateAttr(default=None)
    _session_authenticated: bool = PrivateAttr(default=False)
    _session_availability: RouteAvailability = PrivateAttr(default="unavailable")
    _session_reason: str = PrivateAttr(
        default=(
            "Codex local-session intent cannot run because the Codex CLI is not installed; "
            "install Codex and sign in, or choose an API route."
        )
    )
    _route_transports: dict[str, _ConversationTransport] = PrivateAttr(default_factory=dict)
    _run_transports: dict[str, _ConversationTransport] = PrivateAttr(default_factory=dict)

    @property
    def session(self) -> _ConversationTransport | None:
        return self._session

    async def close(self) -> None:
        transports = [*self._route_transports.values(), *self._run_transports.values()]
        if self._session is not None:
            transports.insert(0, self._session)
        closed: set[int] = set()
        failures: list[BaseException] = []
        for transport in transports:
            identity = id(transport)
            if identity in closed:
                continue
            closed.add(identity)
            try:
                await transport.close()
            except BaseException as error:
                failures.append(error)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("conversation transports failed to close", failures)

    def require(self, route: ConversationRoute) -> _ConversationTransport:
        if route.connection == "local_session":
            if self._session is None:
                raise ConnectionError("selected session route is unavailable")
            transport: _ConversationTransport = self._session
        else:
            transport = self._route_transports.get(route.id) or self.completion(
                provider=route.provider
            )
        self._route_transports[route.id] = transport
        return transport

    def bind(self, route: ConversationRoute, run: AgentRun) -> _ConversationTransport:
        transport = self.require(route)
        transport.bind_conversation(run)
        self._run_transports[run.run_id] = transport
        return transport

    async def for_run(self, run: AgentRun) -> _ConversationTransport:
        transport = self._run_transports.get(run.run_id)
        if transport is not None:
            return transport
        if run.connection == "local_session":
            if self._session is None:
                await self.session_route(time.time())
            if self._session is None:
                raise ConnectionError("stored session route is unavailable")
            transport = self._session
        else:
            transport = self.completion(provider=run.provider)
        self._run_transports[run.run_id] = transport
        return transport

    async def session_route(self, now: float) -> ConversationRoute:
        try:
            active = Policy.load().provider_active("codex")
        except PolicyError:
            active = False
        if not active:
            self._session_availability = "policy_blocked"
            self._session_reason = "Codex sessions are disabled by agent policy."
        elif self._session is None:
            await self._open_session()
        model_ids: list[str] = []
        default: str | None = None
        if self._session is not None and self._session_authenticated:
            try:
                model_ids, default = await self._session.models()
            except (ConnectionError, ValidationError, ValueError) as error:
                self._session_availability = "incompatible"
                self._session_reason = _codex_failure_reason(error, phase="model catalog")
            if self._session_availability == "available" and not model_ids:
                self._session_availability = "incompatible"
                self._session_reason = (
                    "Codex local-session model intent cannot run because the app-server returned "
                    "no usable models; check Codex model access and retry, or choose an API route."
                )
        models = [self._catalog_model(model_id) for model_id in model_ids]
        return ConversationRoute(
            id="codex:local_session", provider="codex", connection="local_session",
            label="Codex local session", availability=self._session_availability,
            reason=self._session_reason, charge_path="unknown", cost_certainty="unknown",
            billing_note="Uses the signed-in Codex session; plan quota or usage credits may still apply.",
            authenticated=self._session_authenticated,
            capabilities=["streaming", "resume", "cancel", "approvals", "collaboration"]
            if model_ids else [],
            models=models,
            default_model=default if default in model_ids else (model_ids[0] if model_ids else None),
            cataloged_at=now,
        )

    async def _open_session(self) -> None:
        provider = provider_for("codex")
        if not isinstance(provider, CodexProvider):
            self._session_availability = "incompatible"
            self._session_reason = (
                "Codex local-session intent cannot run because configured Codex provider has no "
                "app-server support; select a supported Codex CLI or choose an API route."
            )
            return
        try:
            connection = await _CodexTransport.open(provider, self.workspace_root)
        except (FileNotFoundError, OSError):
            self._session_availability = "unavailable"
            self._session_reason = (
                "Codex local-session intent cannot run because the Codex CLI is not installed; "
                "install Codex and sign in, or choose an API route."
            )
            return
        try:
            self._session_authenticated = await connection.initialize()
        except (ConnectionError, ValidationError, ValueError) as error:
            await connection.close()
            self._session_availability = "incompatible"
            self._session_reason = _codex_failure_reason(error, phase="handshake")
            return
        self._session = connection
        if self._session_authenticated:
            self._session_availability = "available"
            self._session_reason = (
                "Codex app-server is experimental and unsupported for production workloads."
            )
        else:
            self._session_availability = "unauthenticated"
            self._session_reason = (
                "Codex local-session login intent cannot run because Codex is not signed in with "
                "a ChatGPT account; sign in with Codex, then retry, or choose an API route."
            )

    def completion_routes(self, now: float) -> list[ConversationRoute]:
        grouped: dict[str, list[Model]] = {}
        for model in Model.catalog():
            if model.is_available() and model.can(ModelCapability.LLM):
                grouped.setdefault(model.provider, []).append(model)
        routes: list[ConversationRoute] = []
        for provider in sorted(grouped):
            models = sorted(grouped[provider], key=lambda model: model.id)
            local = provider == "ollama"
            priced = all(
                model.input_cost_per_million is not None
                and model.output_cost_per_million is not None
                for model in models
            )
            routes.append(ConversationRoute(
                id=f"{provider}:api", provider=provider, connection="api",
                label=f"{provider} API", availability="available", reason="",
                charge_path="local_compute" if local else "metered_api",
                cost_certainty="known" if priced and not local else "unknown",
                billing_note=(
                    "Explicit local route; local compute and hosting costs may apply."
                    if local else "Explicit API route; provider billing applies."
                ),
                authenticated=True, capabilities=["cancel"], models=models,
                default_model=models[0].id, cataloged_at=now,
            ))
        return routes

    @staticmethod
    def completion(*, provider: str) -> _CompletionTransport:
        return _CompletionTransport(provider=provider)

    @staticmethod
    def _catalog_model(model_id: str) -> Model:
        known = next((model for model in Model.catalog() if model.id == model_id), None)
        if known is not None:
            return known
        provider = model_id.split("/", 1)[0] if "/" in model_id else "openai"
        return Model(id=model_id, provider=provider, capabilities={ModelCapability.LLM})


def build_transport_registry(workspace_root: Path) -> _TransportRegistry:
    return _TransportRegistry(workspace_root=workspace_root)


__all__: list[str] = []
