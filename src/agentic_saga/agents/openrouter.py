from __future__ import annotations

import os
from dataclasses import dataclass
from importlib import import_module
from typing import Annotated, Literal, Protocol, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
)

from agentic_saga.agents.deepagents import DeepAgentsDriver, native_model_request_limit
from agentic_saga.manifest import SagaContext

type _ModelId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[a-z0-9][a-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:-]*$"),
]

_PRIMARY_MODEL = "openai/gpt-oss-120b"
_MILLISECONDS_PER_SECOND = 1_000


class _ModelFactory(Protocol):
    def __call__(self, model_name: str, **values: object) -> object: ...


class _ProviderClient(Protocol):
    max_retries: int


class _Provider(Protocol):
    client: _ProviderClient


class _ProviderFactory(Protocol):
    def __call__(self, **values: object) -> _Provider: ...


@dataclass(frozen=True)
class _OpenRouterDependencies:
    provider_factory: _ProviderFactory
    model_factory: _ModelFactory


class OpenRouterSettings(BaseModel):
    """Injected, secret-safe configuration for one bounded model route."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    api_key: SecretStr = Field(min_length=1, repr=False)
    primary_model: _ModelId = _PRIMARY_MODEL
    temperature: Literal[0] = 0
    reasoning_effort: Literal["low"] = "low"
    max_output_tokens: int = Field(default=512, strict=True, ge=1, le=4_096)
    timeout_ms: int = Field(default=30_000, strict=True, ge=100, le=60_000)
    sdk_retries: Literal[0] = 0

    @field_validator("primary_model")
    @classmethod
    def require_pinned_model(cls, value: str) -> str:
        moving = value in {"openrouter/auto", "openrouter/free"}
        if moving or ":" in value or value.endswith("/latest"):
            raise ValueError("model route must be pinned and omit OpenRouter variant suffixes")
        return value

    @property
    def model_route(self) -> tuple[str]:
        return (self.primary_model,)

    @classmethod
    def from_environment(cls) -> OpenRouterSettings:
        try:
            value = os.environ["OPENROUTER_API_KEY"]
        except KeyError:
            raise ValueError("OPENROUTER_API_KEY is required") from None
        if not value:
            raise ValueError("OPENROUTER_API_KEY is required")
        model = os.environ.get("OPENROUTER_MODEL", _PRIMARY_MODEL)
        return cls(api_key=SecretStr(value), primary_model=model)


def build_openrouter_driver(
    context: SagaContext, settings: OpenRouterSettings | None = None
) -> DeepAgentsDriver:
    """Build a proposal-only Pydantic Deep driver with bounded OpenRouter limits."""

    selected = settings or OpenRouterSettings.from_environment()
    model = _build_model(context, selected)
    return DeepAgentsDriver._from_model(
        context, model, provider_id="openrouter", model_route=selected.model_route
    )


def _build_model(context: SagaContext, settings: OpenRouterSettings) -> object:
    dependencies = _load_model_dependencies()
    output_tokens, timeout_ms = effective_model_request_caps(context, settings)
    timeout = timeout_ms / _MILLISECONDS_PER_SECOND
    provider = dependencies.provider_factory(api_key=settings.api_key.get_secret_value())
    provider.client.max_retries = settings.sdk_retries
    model_settings = _model_settings(settings, output_tokens, timeout)
    return dependencies.model_factory(
        settings.primary_model,
        provider=provider,
        settings=model_settings,
    )


def _model_settings(
    settings: OpenRouterSettings,
    output_tokens: int,
    timeout: float,
) -> dict[str, object]:
    return {
        "max_tokens": min(settings.max_output_tokens, output_tokens),
        "timeout": timeout,
        "temperature": 0,
        "openrouter_reasoning": {"effort": settings.reasoning_effort},
        "openrouter_provider": {"require_parameters": True},
    }


def _model_caps(context: SagaContext) -> tuple[int, int]:
    turns = context.budget.turn_limit
    return _request_cap(context.budget.token_limit, turns), _request_cap(
        context.budget.elapsed_ms_limit, turns
    )


def effective_model_request_caps(
    context: SagaContext, settings: OpenRouterSettings
) -> tuple[int, int]:
    """Return per-request output-token and timeout caps within one reserved turn."""

    output_tokens, timeout_ms = _model_caps(context)
    return min(settings.max_output_tokens, output_tokens), min(settings.timeout_ms, timeout_ms)


def _request_cap(limit: int, turns: int) -> int:
    cap = _turn_cap(limit, turns) // native_model_request_limit()
    if cap <= 0:
        raise ValueError("Saga budget does not permit an agent model call")
    return cap


def _turn_cap(limit: int, turns: int) -> int:
    if limit <= 0 or turns <= 0:
        raise ValueError("Saga budget does not permit an agent model call")
    cap = limit // turns
    if cap <= 0:
        raise ValueError("Saga budget does not permit an agent model call")
    return cap


def _load_model_dependencies() -> _OpenRouterDependencies:
    try:
        models = import_module("pydantic_ai.models.openrouter")
        providers = import_module("pydantic_ai.providers.openrouter")
    except ModuleNotFoundError:
        raise RuntimeError("install agentic-saga[agent] to use the agent adapter") from None
    return _OpenRouterDependencies(
        cast(_ProviderFactory, providers.OpenRouterProvider),
        cast(_ModelFactory, models.OpenRouterModel),
    )


__all__ = ["OpenRouterSettings", "build_openrouter_driver", "effective_model_request_caps"]
