"""Direct TypeSafe Jev adapter for host-materialized Saga proposals."""

from __future__ import annotations

import os
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from importlib import import_module
from types import ModuleType
from typing import Annotated, Literal, Protocol, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    ValidationError,
    field_validator,
)

from agentic_saga.agents.choice import (
    CandidateFactory,
    ChoiceAgentDriver,
    DecisionSelection,
)
from agentic_saga.agents.deepagents import AgentFailureCategory, AgentPlanningError
from agentic_saga.manifest import SagaContext

type _ModelId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^jev-[0-9]+\.[0-9]+\.[0-9]+$"),
]

_PINNED_MODEL = "jev-1.13.0"
_OFFICIAL_BASE_URL = "https://api.typesafe.ai"
_QUESTION_NAME = "next_action"
_INSTRUCTIONS = (
    "Select the safest eligible next Saga proposal from current public evidence. "
    "Do not invent actions or arguments."
)
_MILLISECONDS_PER_SECOND = 1_000
_TOO_MANY_REQUESTS = 429
_SUCCESS_RANGE = range(200, 300)
_CLIENT_ERROR_RANGE = range(400, 500)
_SERVER_ERROR_RANGE = range(500, 600)


class _ChoiceFactory(Protocol):
    def __call__(self, *, instructions: str, criteria: Mapping[str, object]) -> object: ...


class _RetryFactory(Protocol):
    def __call__(self, *, max_retries: int) -> object: ...


class _AsyncClient(Protocol):
    async def __aenter__(self) -> _AsyncClient: ...

    async def __aexit__(self, *args: object) -> None: ...

    async def system_one(self, *, state: object, questions: Mapping[str, object]) -> object: ...


class _ClientFactory(Protocol):
    def __call__(self, **values: object) -> _AsyncClient: ...


class _ChoiceAnswer(Protocol):
    choice: object
    confidence: object
    probabilities: object


class _SystemOneResponse(Protocol):
    model: str
    choices: Mapping[str, _ChoiceAnswer]


@dataclass(frozen=True)
class _JevDependencies:
    client_factory: _ClientFactory
    choice_factory: _ChoiceFactory
    retry_factory: _RetryFactory


class JevSettings(BaseModel):
    """Secret-safe configuration for one pinned direct TypeSafe Jev route."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    api_key: SecretStr = Field(min_length=1, repr=False)
    model: _ModelId = _PINNED_MODEL
    timeout_ms: int = Field(default=10_000, strict=True, ge=100, le=60_000)
    min_confidence: float = Field(default=0.8, strict=True, ge=0, le=1, allow_inf_nan=False)
    sdk_retries: Literal[0] = 0

    @field_validator("model", mode="before")
    @classmethod
    def reject_moving_aliases(cls, value: object) -> object:
        if value in {"jev-latest", "jev-preview", "latest", "preview"}:
            raise ValueError("Jev model must be a pinned semantic version")
        return value

    @property
    def model_route(self) -> tuple[str]:
        return (self.model,)

    @classmethod
    def from_environment(cls) -> JevSettings:
        value = os.environ.get("TYPESAFE_API_KEY")
        if not value:
            raise ValueError("TYPESAFE_API_KEY is required") from None
        model = os.environ.get("TYPESAFE_DEFAULT_MODEL", _PINNED_MODEL)
        return cls(api_key=SecretStr(value), model=model)


@dataclass(frozen=True)
class _JevDecisionCall:
    settings: JevSettings
    dependencies: _JevDependencies

    async def __call__(
        self, state: dict[str, object], criteria: dict[str, object]
    ) -> DecisionSelection:
        try:
            response = await self._request(state, criteria)
            return _selection_from_response(response, self.settings.model)
        except AgentPlanningError as error:
            raise error from None
        except Exception as error:
            category = _failure_category(error)
        raise AgentPlanningError(category) from None

    async def _request(self, state: dict[str, object], criteria: dict[str, object]) -> object:
        client = self._client()
        question = self.dependencies.choice_factory(instructions=_INSTRUCTIONS, criteria=criteria)
        async with client:
            return await client.system_one(state=state, questions={_QUESTION_NAME: question})

    def _client(self) -> _AsyncClient:
        settings = self.settings
        retry = self.dependencies.retry_factory(max_retries=settings.sdk_retries)
        return self.dependencies.client_factory(
            api_key=settings.api_key.get_secret_value(),
            base_url=_OFFICIAL_BASE_URL,
            model=settings.model,
            retry=retry,
            timeout=settings.timeout_ms / _MILLISECONDS_PER_SECOND,
        )


def build_jev_driver(
    context: SagaContext,
    candidate_factory: CandidateFactory,
    settings: JevSettings | None = None,
) -> ChoiceAgentDriver:
    """Build a direct TypeSafe Jev driver; no OpenRouter or Node sidecar is used."""

    selected = settings or JevSettings.from_environment()
    bounded = selected.model_copy(update={"timeout_ms": _effective_timeout_ms(context, selected)})
    decision = _JevDecisionCall(bounded, _load_dependencies())
    return _driver(context, candidate_factory, selected, decision)


def _driver(
    context: SagaContext,
    candidate_factory: CandidateFactory,
    settings: JevSettings,
    decision: _JevDecisionCall,
) -> ChoiceAgentDriver:
    return ChoiceAgentDriver(
        context=context,
        candidate_factory=candidate_factory,
        decision_call=decision,
        min_confidence=settings.min_confidence,
        provider_id="typesafe",
        model_route=settings.model_route,
    )


def _effective_timeout_ms(context: SagaContext, settings: JevSettings) -> int:
    turns = context.budget.turn_limit
    if turns <= 0 or context.budget.elapsed_ms_limit <= 0:
        raise ValueError("Saga budget does not permit a Jev decision call")
    per_turn = context.budget.elapsed_ms_limit // turns
    if per_turn <= 0:
        raise ValueError("Saga budget does not permit a Jev decision call")
    return min(settings.timeout_ms, per_turn)


def _selection_from_response(response: object, expected_model: str) -> DecisionSelection:
    selection: DecisionSelection | None = None
    with suppress(AttributeError, KeyError, TypeError, ValidationError, ValueError):
        selection = _parse_selection(response, expected_model)
    if selection is None:
        raise AgentPlanningError(AgentFailureCategory.INVALID_RESPONSE) from None
    return selection


def _parse_selection(response: object, expected_model: str) -> DecisionSelection:
    typed = cast(_SystemOneResponse, response)
    if typed.model != expected_model:
        raise ValueError("unexpected Jev model")
    answer = typed.choices[_QUESTION_NAME]
    payload = {
        "choice_id": answer.choice,
        "confidence": answer.confidence,
        "probabilities": answer.probabilities,
    }
    return DecisionSelection.model_validate(payload, strict=True)


def _load_dependencies() -> _JevDependencies:
    try:
        sdk = import_module("typesafe_sdk")
        return _dependencies_from_module(sdk)
    except (AttributeError, ModuleNotFoundError):
        raise RuntimeError("install agentic-saga[jev] to use the Jev adapter") from None


def _dependencies_from_module(sdk: ModuleType) -> _JevDependencies:
    return _JevDependencies(
        client_factory=cast(_ClientFactory, sdk.AsyncTypeSafeClient),
        choice_factory=cast(_ChoiceFactory, sdk.Choice),
        retry_factory=cast(_RetryFactory, sdk.RetryPolicy),
    )


def _failure_category(error: Exception) -> AgentFailureCategory:
    http = _http_failure_category(_status(error))
    if http is not None:
        return http
    if isinstance(error, (ConnectionError, OSError, TimeoutError)):
        return AgentFailureCategory.TRANSPORT_EXHAUSTED
    if isinstance(error, ValueError):
        return AgentFailureCategory.INVALID_RESPONSE
    return AgentFailureCategory.INTERNAL


def _http_failure_category(status: int | None) -> AgentFailureCategory | None:
    if status in _SUCCESS_RANGE:
        return AgentFailureCategory.INVALID_RESPONSE
    if status == _TOO_MANY_REQUESTS:
        return AgentFailureCategory.RATE_LIMIT_EXHAUSTED
    if status in _CLIENT_ERROR_RANGE:
        return AgentFailureCategory.REQUEST_REJECTED
    if status in _SERVER_ERROR_RANGE:
        return AgentFailureCategory.SERVER_ERROR_EXHAUSTED
    return None


def _status(error: Exception) -> int | None:
    try:
        value = getattr(error, "status", None)
    except Exception:
        return None
    return value if type(value) is int else None


__all__ = ["JevSettings", "build_jev_driver"]
