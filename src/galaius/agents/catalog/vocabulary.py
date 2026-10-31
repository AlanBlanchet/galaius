"""Provider-independent agent intents and the translations each harness can make.

The caller speaks these terms.  Provider adapters consume the translations; they must never
compare a caller's term with another vendor's spelling.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


class TouchScope(StrEnum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    FULL_ACCESS = "full_access"


class ApprovalIntent(StrEnum):
    ASK = "ask"
    AUTO_SAFE = "auto_safe"
    AUTO_EDITS = "auto_edits"
    NEVER = "never"


class ThinkingLevel(StrEnum):
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"
    ULTRA = "ultra"


class ModelSource(StrEnum):
    PROVIDER_LOGIN = "provider_login"
    OPERATOR_ENDPOINT = "operator_endpoint"
    OLLAMA_LOCAL = "ollama_local"
    OLLAMA_CLOUD = "ollama_cloud"
    CURSOR_CLOUD = "cursor_cloud"


@dataclass(frozen=True)
class Translation:
    """One harness spelling, or a safe refusal when it has no honest spelling."""

    native: str | None
    exact: bool
    detail: str


@dataclass(frozen=True)
class HarnessVocabulary:
    """Capabilities of one execution harness, expressed in standard terms."""

    name: str
    touch: Mapping[TouchScope, Translation]
    approval: Mapping[ApprovalIntent, Translation]
    thinking: Mapping[ThinkingLevel, Translation]
    model_source: Mapping[ModelSource, Translation]
    tools: Translation

    def translate(self, axis: str, intent: StrEnum) -> Translation:
        values = getattr(self, axis)
        result = values[intent]
        if result.native is None:
            raise UnsupportedIntent(f"{self.name} cannot express {axis} intent {intent.value}: {result.detail}")
        return result


class UnsupportedIntent(ValueError):
    """A harness cannot safely express a standard caller intent."""


def _translations(values: dict[StrEnum, Translation]) -> Mapping[StrEnum, Translation]:
    return MappingProxyType(values)


_SAFE_NO_EQUIVALENT = "no exact equivalent; safely narrowed"


VOCABULARY: Mapping[str, HarnessVocabulary] = MappingProxyType({
    "codex": HarnessVocabulary(
        name="codex",
        touch=_translations({
            TouchScope.READ_ONLY: Translation("read-only", True, "Codex sandbox scope"),
            TouchScope.WORKSPACE_WRITE: Translation("workspace-write", True, "Codex sandbox scope"),
            TouchScope.FULL_ACCESS: Translation("workspace-write", False, _SAFE_NO_EQUIVALENT),
        }),
        approval=_translations({
            ApprovalIntent.ASK: Translation(None, True, "Codex uses its documented default approval policy"),
            ApprovalIntent.AUTO_SAFE: Translation("approve-for-me", True, "Codex automatic review"),
            ApprovalIntent.AUTO_EDITS: Translation("approve-for-me", False, "Codex has no separate edit-only approval mode"),
            ApprovalIntent.NEVER: Translation(None, False, "Codex has no safe no-prompt equivalent; default approval prompts remain enabled"),
        }),
        thinking=_translations({level: Translation(level.value, True, "Codex model_reasoning_effort") for level in ThinkingLevel}),
        model_source=_translations({
            ModelSource.PROVIDER_LOGIN: Translation("openai", True, "Codex native provider"),
            ModelSource.OPERATOR_ENDPOINT: Translation("model_providers", True, "named OpenAI-compatible endpoint"),
            ModelSource.OLLAMA_LOCAL: Translation("model_providers", True, "OpenAI-compatible Ollama endpoint"),
            ModelSource.OLLAMA_CLOUD: Translation("model_providers", True, "OpenAI-compatible Ollama cloud endpoint"),
            ModelSource.CURSOR_CLOUD: Translation(None, False, "Codex cannot use Cursor's model account"),
        }),
        tools=Translation("sandbox + features.shell_tool + MCP tool lists", True, "Codex tool policy"),
    ),
    "claude": HarnessVocabulary(
        name="claude",
        touch=_translations({
            TouchScope.READ_ONLY: Translation("plan", True, "Claude Code permission mode"),
            TouchScope.WORKSPACE_WRITE: Translation("acceptEdits", False, "Claude's permission mode also controls approval"),
            TouchScope.FULL_ACCESS: Translation("bypassPermissions", True, "Claude Code unrestricted mode"),
        }),
        approval=_translations({
            ApprovalIntent.ASK: Translation("manual", True, "Claude Code permission mode"),
            ApprovalIntent.AUTO_SAFE: Translation("auto", True, "Claude Code permission mode"),
            ApprovalIntent.AUTO_EDITS: Translation("acceptEdits", True, "Claude Code permission mode"),
            ApprovalIntent.NEVER: Translation("dontAsk", True, "Claude Code permission mode"),
        }),
        thinking=_translations({
            ThinkingLevel.MINIMAL: Translation("low", False, "Claude Code has no minimal effort level"),
            ThinkingLevel.LOW: Translation("low", True, "Claude Code --effort"),
            ThinkingLevel.MEDIUM: Translation("medium", True, "Claude Code --effort"),
            ThinkingLevel.HIGH: Translation("high", True, "Claude Code --effort"),
            ThinkingLevel.XHIGH: Translation("xhigh", True, "Claude Code --effort"),
            ThinkingLevel.MAX: Translation("max", True, "Claude Code --effort"),
            ThinkingLevel.ULTRA: Translation("max", False, "Claude Code has no ultra effort level"),
        }),
        model_source=_translations({
            ModelSource.PROVIDER_LOGIN: Translation("anthropic", True, "Claude native provider"),
            ModelSource.OPERATOR_ENDPOINT: Translation("ANTHROPIC_BASE_URL", True, "Anthropic-compatible endpoint"),
            ModelSource.OLLAMA_LOCAL: Translation("ANTHROPIC_BASE_URL", True, "Ollama local Anthropic-compatible endpoint"),
            ModelSource.OLLAMA_CLOUD: Translation("ANTHROPIC_BASE_URL", True, "Ollama cloud Anthropic-compatible endpoint"),
            ModelSource.CURSOR_CLOUD: Translation(None, False, "Claude Code cannot use Cursor's model account"),
        }),
        tools=Translation("--tools/--allowedTools/--disallowedTools", True, "Claude Code tool policy"),
    ),
    "ollama": HarnessVocabulary(
        name="ollama",
        touch=_translations({scope: Translation(None, False, "Ollama is a model/API server; use Claude Code, Codex, or Cursor as the file-touch harness") for scope in TouchScope}),
        approval=_translations({intent: Translation(None, False, "Ollama is a model/API server; choose approval in the calling harness") for intent in ApprovalIntent}),
        thinking=_translations({
            ThinkingLevel.MINIMAL: Translation("low", False, "Ollama --think supports low but not minimal"),
            ThinkingLevel.LOW: Translation("low", True, "Ollama --think"),
            ThinkingLevel.MEDIUM: Translation("medium", True, "Ollama --think"),
            ThinkingLevel.HIGH: Translation("high", True, "Ollama --think"),
            ThinkingLevel.XHIGH: Translation("high", False, "Ollama has no xhigh effort level"),
            ThinkingLevel.MAX: Translation("high", False, "Ollama has no max effort level"),
            ThinkingLevel.ULTRA: Translation("high", False, "Ollama has no ultra effort level"),
        }),
        model_source=_translations({
            ModelSource.PROVIDER_LOGIN: Translation(None, False, "Ollama does not provide a provider-login agent harness"),
            ModelSource.OPERATOR_ENDPOINT: Translation("OLLAMA_API_BASE", True, "operator-selected Ollama endpoint"),
            ModelSource.OLLAMA_LOCAL: Translation("http://localhost:11434", True, "local Ollama API"),
            ModelSource.OLLAMA_CLOUD: Translation("https://ollama.com", True, "Ollama cloud API"),
            ModelSource.CURSOR_CLOUD: Translation(None, False, "Ollama cannot use Cursor's model account"),
        }),
        tools=Translation(None, False, "Ollama API exposes model generation, not agent tools; choose a calling harness"),
    ),
    "cursor": HarnessVocabulary(
        name="cursor",
        touch=_translations({
            TouchScope.READ_ONLY: Translation("ask", True, "Cursor CLI read-only mode"),
            TouchScope.WORKSPACE_WRITE: Translation("agent + sandbox", True, "Cursor CLI workspace sandbox"),
            TouchScope.FULL_ACCESS: Translation("run-everything", True, "Cursor unrestricted run mode"),
        }),
        approval=_translations({
            ApprovalIntent.ASK: Translation("allowlist", True, "Cursor approval mode"),
            ApprovalIntent.AUTO_SAFE: Translation("auto-review", True, "Cursor approval mode"),
            ApprovalIntent.AUTO_EDITS: Translation("auto-review", False, "Cursor has no separate edit-only approval mode"),
            ApprovalIntent.NEVER: Translation("unrestricted", False, "Cursor unrestricted mode also changes touch scope"),
        }),
        thinking=_translations({level: Translation(None, False, "Cursor CLI documents no reasoning-effort control; omit this intent or set it in Cursor's own model controls") for level in ThinkingLevel}),
        model_source=_translations({
            ModelSource.PROVIDER_LOGIN: Translation("--model", True, "Cursor-selected model"),
            ModelSource.OPERATOR_ENDPOINT: Translation(None, False, "Cursor CLI does not document an arbitrary model endpoint"),
            ModelSource.OLLAMA_LOCAL: Translation(None, False, "Cursor CLI does not document an Ollama endpoint mapping"),
            ModelSource.OLLAMA_CLOUD: Translation(None, False, "Cursor CLI does not document an Ollama endpoint mapping"),
            ModelSource.CURSOR_CLOUD: Translation("--model", True, "Cursor cloud model"),
        }),
        tools=Translation("permissions allow/deny + MCP", True, "Cursor tool policy"),
    ),
})


def vocabulary_for(harness: str) -> HarnessVocabulary:
    try:
        return VOCABULARY[harness]
    except KeyError as exc:
        raise ValueError(f"unknown harness {harness!r}") from exc
