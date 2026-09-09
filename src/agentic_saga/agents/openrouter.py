from __future__ import annotations

import os
from importlib import import_module
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

from agentic_saga.agents.deepagents import DeepAgentsDriver
from agentic_saga.manifest import SagaContext

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

type _ModelId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[a-z0-9][a-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:-]*$"),
]

_PRIMARY_MODEL = "openai/gpt-oss-20b"
_FALLBACK_MODEL = "qwen/qwen3-30b-a3b-instruct-2507"


class _ChatModelFactory(Protocol):
    def __call__(  # noqa: PLR0913 - mirrors the maintained third-party model
        self,
        *,
        model: str,
        api_key: SecretStr,
        timeout: int,
        max_tokens: int,
        max_retries: Literal[0],
        temperature: Literal[0],
        seed: Literal[0],
        reasoning: dict[str, str],
        model_kwargs: dict[str, object],
        openrouter_provider: dict[str, bool],
        metadata: dict[str, str],
    ) -> BaseChatModel: ...


class OpenRouterSettings(BaseModel):
    """Injected, secret-safe configuration for one bounded model route."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    api_key: SecretStr = Field(min_length=1, repr=False)
    primary_model: _ModelId = _PRIMARY_MODEL
    fallback_model: _ModelId = _FALLBACK_MODEL
    temperature: Literal[0] = 0
    reasoning_effort: Literal["low"] = "low"
    max_output_tokens: int = Field(default=512, strict=True, ge=1, le=4_096)
    timeout_ms: int = Field(default=10_000, strict=True, ge=100, le=60_000)
    sdk_retries: Literal[0] = 0

    @field_validator("primary_model", "fallback_model")
    @classmethod
    def require_pinned_model(cls, value: str) -> str:
        moving = value in {"openrouter/auto", "openrouter/free"}
        if moving or ":" in value or value.endswith("/latest"):
            raise ValueError("model route must be pinned and omit OpenRouter variant suffixes")
        return value

    @model_validator(mode="after")
    def require_distinct_fallback(self) -> Self:
        if self.primary_model == self.fallback_model:
            raise ValueError("fallback model must differ from primary model")
        return self

    @property
    def model_route(self) -> tuple[str, str]:
        return self.primary_model, self.fallback_model

    @classmethod
    def from_environment(cls) -> OpenRouterSettings:
        try:
            value = os.environ["OPENROUTER_API_KEY"]
        except KeyError:
            raise ValueError("OPENROUTER_API_KEY is required") from None
        if not value:
            raise ValueError("OPENROUTER_API_KEY is required")
        return cls(api_key=SecretStr(value))


def build_openrouter_driver(
    context: SagaContext, settings: OpenRouterSettings | None = None
) -> DeepAgentsDriver:
    """Build a proposal-only Deep Agents driver with deterministic OpenRouter limits."""

    selected = settings or OpenRouterSettings.from_environment()
    model = _build_model(context, selected)
    return DeepAgentsDriver._from_model(
        context, model, provider_id="openrouter", model_route=selected.model_route
    )


def _build_model(context: SagaContext, settings: OpenRouterSettings) -> BaseChatModel:
    output_tokens, timeout_ms = _model_caps(context)
    return _load_model_factory()(
        model=settings.primary_model,
        api_key=settings.api_key,
        timeout=min(settings.timeout_ms, timeout_ms),
        max_tokens=min(settings.max_output_tokens, output_tokens),
        max_retries=0,
        temperature=0,
        seed=0,
        reasoning={"effort": settings.reasoning_effort},
        model_kwargs={"models": [settings.fallback_model], "parallel_tool_calls": False},
        openrouter_provider={"require_parameters": True},
        metadata={"provider": "openrouter", "model_route": ",".join(settings.model_route)},
    )


def _model_caps(context: SagaContext) -> tuple[int, int]:
    turns = context.budget.turn_limit
    return _turn_cap(context.budget.token_limit, turns), _turn_cap(
        context.budget.elapsed_ms_limit, turns
    )


def _turn_cap(limit: int, turns: int) -> int:
    if limit <= 0 or turns <= 0:
        raise ValueError("Saga budget does not permit an agent model call")
    return limit // turns


def _load_model_factory() -> _ChatModelFactory:
    try:
        module = import_module("langchain_openrouter")
    except ModuleNotFoundError:
        raise RuntimeError("install agentic-saga[agent] to use the agent adapter") from None
    return cast(_ChatModelFactory, module.ChatOpenRouter)


__all__ = ["OpenRouterSettings", "build_openrouter_driver"]
