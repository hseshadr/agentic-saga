"""OpenRouter Decisions transport for pinned TypeSafe Jev selection."""

from __future__ import annotations

import os
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from importlib import import_module
from types import ModuleType
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from agentic_saga.agents.choice import CandidateFactory, ChoiceAgentDriver, DecisionSelection
from agentic_saga.agents.pydanticai import AgentFailureCategory, AgentPlanningError
from agentic_saga.manifest import SagaContext

_PINNED_MODEL: Literal["typesafe/jev-1.13"] = "typesafe/jev-1.13"
_EXPECTED_RESPONSE_MODEL = "typesafe/jev-1.13-20260917"
_OFFICIAL_BASE_URL = "https://openrouter.ai"
_QUESTION_NAME = "next_action"
_INSTRUCTIONS = (
    "Select the safest eligible next Saga proposal from current public evidence. "
    "Do not invent actions or arguments."
)
_TOO_MANY_REQUESTS = 429
_INVALID_RESPONSE_RANGE = range(200, 400)
_CLIENT_ERROR_RANGE = range(400, 500)
_SERVER_ERROR_RANGE = range(500, 600)


class _Decisions(Protocol):
    async def create_async(self, **values: object) -> object: ...


class _Alpha(Protocol):
    decisions: _Decisions


class _AsyncClient(Protocol):
    alpha: _Alpha

    async def __aenter__(self) -> _AsyncClient: ...

    async def __aexit__(self, *args: object) -> None: ...


class _ClientFactory(Protocol):
    def __call__(self, **values: object) -> _AsyncClient: ...


class _OwnedSyncHttpClient(Protocol):
    def __enter__(self) -> _OwnedSyncHttpClient: ...

    def __exit__(self, *args: object) -> None: ...


class _SyncHttpClientFactory(Protocol):
    def __call__(self, **values: object) -> _OwnedSyncHttpClient: ...


class _OwnedAsyncHttpClient(Protocol):
    async def __aenter__(self) -> _OwnedAsyncHttpClient: ...

    async def __aexit__(self, *args: object) -> None: ...


class _AsyncHttpClientFactory(Protocol):
    def __call__(self, **values: object) -> _OwnedAsyncHttpClient: ...


class _InertLogger:
    def debug(self, message: str, *args: object, **kwargs: object) -> None:
        return None


class _ChoiceAnswer(Protocol):
    choice: object
    confidence: object
    probabilities: object


class _DecisionsResponse(Protocol):
    model: str
    answers: Mapping[str, _ChoiceAnswer]


@dataclass(frozen=True)
class _OpenRouterDependencies:
    client_factory: _ClientFactory
    sync_http_client_factory: _SyncHttpClientFactory
    async_http_client_factory: _AsyncHttpClientFactory
    transport_errors: tuple[type[BaseException], ...] = (
        ConnectionError,
        OSError,
        TimeoutError,
    )


class OpenRouterDecisionsSettings(BaseModel):
    """Secret-safe settings for one pinned OpenRouter Decisions route."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    api_key: SecretStr = Field(min_length=1, repr=False)
    model: Literal["typesafe/jev-1.13"] = _PINNED_MODEL
    timeout_ms: int = Field(default=10_000, strict=True, ge=100, le=60_000)
    min_confidence: float = Field(default=0.8, strict=True, ge=0, le=1, allow_inf_nan=False)
    sdk_retries: Literal[0] = 0
    zdr: Literal[True] = True
    data_collection: Literal["deny"] = "deny"

    @property
    def model_route(self) -> tuple[str]:
        return (self.model,)

    @classmethod
    def from_environment(cls) -> OpenRouterDecisionsSettings:
        value = os.environ.get("OPENROUTER_API_KEY")
        if not value:
            raise ValueError("OPENROUTER_API_KEY is required") from None
        return cls(api_key=SecretStr(value))


@dataclass(frozen=True)
class _OpenRouterDecisionCall:
    settings: OpenRouterDecisionsSettings
    dependencies: _OpenRouterDependencies

    async def __call__(
        self, state: dict[str, object], criteria: dict[str, object]
    ) -> DecisionSelection:
        try:
            response = await self._request(state, criteria)
            return _selection_from_response(response)
        except AgentPlanningError as error:
            raise error from None
        except Exception as error:
            category = _failure_category(error, self.dependencies.transport_errors)
        raise AgentPlanningError(category) from None

    async def _request(self, state: dict[str, object], criteria: dict[str, object]) -> object:
        question: dict[str, object] = {
            "type": "choice",
            "instructions": _INSTRUCTIONS,
            "criteria": criteria,
        }
        sync_client = self.dependencies.sync_http_client_factory(follow_redirects=False)
        with sync_client:
            return await self._with_async_client(sync_client, state, question)

    async def _with_async_client(
        self,
        sync_client: _OwnedSyncHttpClient,
        state: dict[str, object],
        question: dict[str, object],
    ) -> object:
        factory = self.dependencies.async_http_client_factory
        async_client = factory(follow_redirects=False)
        async with async_client:
            return await self._send(sync_client, async_client, state, question)

    async def _send(
        self,
        sync_client: _OwnedSyncHttpClient,
        async_client: _OwnedAsyncHttpClient,
        state: dict[str, object],
        question: dict[str, object],
    ) -> object:
        async with self._client(sync_client, async_client) as client:
            values = self._request_values(state, question)
            return await client.alpha.decisions.create_async(**values)

    def _client(
        self,
        sync_client: _OwnedSyncHttpClient,
        async_client: _OwnedAsyncHttpClient,
    ) -> _AsyncClient:
        return self.dependencies.client_factory(
            api_key=self.settings.api_key.get_secret_value(),
            server_url=_OFFICIAL_BASE_URL,
            timeout_ms=self.settings.timeout_ms,
            client=sync_client,
            async_client=async_client,
            debug_logger=_InertLogger(),
            http_referer="",
            x_open_router_title="",
            x_open_router_categories="",
        )

    def _request_values(
        self, state: dict[str, object], question: dict[str, object]
    ) -> dict[str, object]:
        return {
            "model": self.settings.model,
            "state": state,
            "questions": {_QUESTION_NAME: question},
            "provider": _provider_preferences(self.settings),
            "retries": None,
            "server_url": _OFFICIAL_BASE_URL,
            "timeout_ms": self.settings.timeout_ms,
        }


def build_openrouter_decisions_driver(
    context: SagaContext,
    candidate_factory: CandidateFactory,
    settings: OpenRouterDecisionsSettings | None = None,
) -> ChoiceAgentDriver:
    """Build a pinned Jev driver over OpenRouter's native Decisions endpoint."""

    selected = settings or OpenRouterDecisionsSettings.from_environment()
    bounded = selected.model_copy(update={"timeout_ms": _effective_timeout_ms(context, selected)})
    decision = _OpenRouterDecisionCall(bounded, _load_dependencies())
    return _driver(context, candidate_factory, selected, decision)


def _driver(
    context: SagaContext,
    candidate_factory: CandidateFactory,
    settings: OpenRouterDecisionsSettings,
    decision: _OpenRouterDecisionCall,
) -> ChoiceAgentDriver:
    return ChoiceAgentDriver(
        context=context,
        candidate_factory=candidate_factory,
        decision_call=decision,
        min_confidence=settings.min_confidence,
        provider_id="openrouter-decisions",
        model_route=settings.model_route,
    )


def _effective_timeout_ms(context: SagaContext, settings: OpenRouterDecisionsSettings) -> int:
    turns = context.budget.turn_limit
    if turns <= 0 or context.budget.elapsed_ms_limit <= 0:
        raise ValueError("Saga budget does not permit an OpenRouter decision call")
    per_turn = context.budget.elapsed_ms_limit // turns
    if per_turn <= 0:
        raise ValueError("Saga budget does not permit an OpenRouter decision call")
    return min(settings.timeout_ms, per_turn)


def _provider_preferences(settings: OpenRouterDecisionsSettings) -> dict[str, object]:
    return {"zdr": settings.zdr, "data_collection": settings.data_collection}


def _selection_from_response(response: object) -> DecisionSelection:
    selection: DecisionSelection | None = None
    with suppress(AttributeError, KeyError, TypeError, ValidationError, ValueError):
        selection = _parse_selection(response)
    if selection is None:
        raise AgentPlanningError(AgentFailureCategory.INVALID_RESPONSE) from None
    return selection


def _parse_selection(response: object) -> DecisionSelection:
    typed = cast(_DecisionsResponse, response)
    if typed.model != _EXPECTED_RESPONSE_MODEL:
        raise ValueError("unexpected OpenRouter model")
    answer = typed.answers[_QUESTION_NAME]
    payload = {
        "choice_id": answer.choice,
        "confidence": answer.confidence,
        "probabilities": answer.probabilities,
    }
    return DecisionSelection.model_validate(payload, strict=True)


def _load_dependencies() -> _OpenRouterDependencies:
    try:
        sdk = import_module("openrouter")
        httpx = import_module("httpx")
        return _dependencies_from_modules(sdk, httpx)
    except (AttributeError, ModuleNotFoundError):
        message = "install agentic-saga[jev-openrouter] to use the OpenRouter Decisions adapter"
        raise RuntimeError(message) from None


def _dependencies_from_modules(sdk: ModuleType, httpx: ModuleType) -> _OpenRouterDependencies:
    errors = (httpx.RequestError, httpx.TimeoutException)
    return _OpenRouterDependencies(
        client_factory=cast(_ClientFactory, sdk.OpenRouter),
        sync_http_client_factory=cast(_SyncHttpClientFactory, httpx.Client),
        async_http_client_factory=cast(_AsyncHttpClientFactory, httpx.AsyncClient),
        transport_errors=cast(tuple[type[BaseException], ...], errors),
    )


def _failure_category(
    error: Exception, transport_errors: tuple[type[BaseException], ...]
) -> AgentFailureCategory:
    http = _http_failure_category(_status_code(error))
    if http is not None:
        return http
    if isinstance(error, transport_errors):
        return AgentFailureCategory.TRANSPORT_EXHAUSTED
    if isinstance(error, ValueError):
        return AgentFailureCategory.INVALID_RESPONSE
    return AgentFailureCategory.INTERNAL


def _http_failure_category(status: int | None) -> AgentFailureCategory | None:
    if status in _INVALID_RESPONSE_RANGE:
        return AgentFailureCategory.INVALID_RESPONSE
    if status == _TOO_MANY_REQUESTS:
        return AgentFailureCategory.RATE_LIMIT_EXHAUSTED
    if status in _CLIENT_ERROR_RANGE:
        return AgentFailureCategory.REQUEST_REJECTED
    if status in _SERVER_ERROR_RANGE:
        return AgentFailureCategory.SERVER_ERROR_EXHAUSTED
    return None


def _status_code(error: Exception) -> int | None:
    try:
        value = getattr(error, "status_code", None)
    except Exception:
        return None
    return value if type(value) is int else None


__all__ = ["OpenRouterDecisionsSettings", "build_openrouter_decisions_driver"]
