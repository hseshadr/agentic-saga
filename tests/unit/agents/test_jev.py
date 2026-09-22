from __future__ import annotations

import json
from collections.abc import Callable, Coroutine, Mapping
from types import SimpleNamespace
from typing import cast

import httpx2
import pytest
from pydantic import SecretStr, ValidationError
from typesafe_sdk import AsyncTypeSafeClient, Choice, RetryPolicy

from agentic_saga.agents import jev as adapter_module
from agentic_saga.agents.choice import DecisionSelection
from agentic_saga.agents.jev import JevSettings, build_jev_driver
from agentic_saga.agents.pydanticai import AgentFailureCategory, AgentPlanningError
from tests.unit.agents.test_choice import _candidate, _factory
from tests.unit.agents.test_pydanticai import _context, _descriptor, _observation

_MODEL = "jev-1.13.0"


class _RetryRecorder:
    def __init__(self, captured: dict[str, object]) -> None:
        self.captured = captured

    def __call__(self, *, max_retries: int) -> object:
        self.captured["max_retries"] = max_retries
        return SimpleNamespace(max_retries=max_retries)


class _ChoiceRecorder:
    def __init__(self, captured: dict[str, object]) -> None:
        self.captured = captured

    def __call__(self, *, instructions: str, criteria: Mapping[str, object]) -> object:
        self.captured.update(instructions=instructions, criteria=criteria)
        return SimpleNamespace(instructions=instructions, criteria=criteria)


class _RecordedClient:
    def __init__(self, captured: dict[str, object], values: dict[str, object]) -> None:
        self.captured = captured
        captured["client"] = values

    async def __aenter__(self) -> _RecordedClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def system_one(self, *, state: object, questions: Mapping[str, object]) -> object:
        self.captured.update(state=state, questions=questions)
        answer = SimpleNamespace(
            choice="choice_00000002",
            confidence=0.9,
            probabilities={"choice_00000001": 0.1, "choice_00000002": 0.9},
        )
        return SimpleNamespace(model=_MODEL, choices={"next_action": answer})


class _ClientRecorder:
    def __init__(self, captured: dict[str, object]) -> None:
        self.captured = captured

    def __call__(self, **values: object) -> _RecordedClient:
        return _RecordedClient(self.captured, values)


def _recording_dependencies(
    captured: dict[str, object],
) -> adapter_module._JevDependencies:
    return adapter_module._JevDependencies(
        client_factory=_ClientRecorder(captured),
        choice_factory=_ChoiceRecorder(captured),
        retry_factory=_RetryRecorder(captured),
    )


def test_should_use_pinned_bounded_secret_safe_defaults() -> None:
    settings = JevSettings(api_key=SecretStr("test-key"))

    assert settings.model == _MODEL
    assert settings.timeout_ms == 10_000
    assert settings.sdk_retries == 0
    assert settings.min_confidence == 0.8
    assert "test-key" not in repr(settings)


@pytest.mark.parametrize("model", ["jev-latest", "jev-preview", "latest", "jev-1"])
def test_should_reject_moving_or_unversioned_model_aliases(model: str) -> None:
    with pytest.raises(ValidationError):
        JevSettings(api_key=SecretStr("test-key"), model=model)


def test_should_load_secret_and_pinned_model_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "private-" + "provider-value"
    monkeypatch.setenv("TYPESAFE_API_KEY", secret)
    monkeypatch.setenv("TYPESAFE_DEFAULT_MODEL", _MODEL)

    settings = JevSettings.from_environment()

    assert settings.api_key.get_secret_value() == secret
    assert settings.model == _MODEL
    assert secret not in repr(settings)


@pytest.mark.parametrize("value", [None, ""])
def test_should_require_environment_key(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    else:
        monkeypatch.setenv("TYPESAFE_API_KEY", value)
    with pytest.raises(ValueError, match="TYPESAFE_API_KEY is required") as captured:
        JevSettings.from_environment()
    assert captured.value.__cause__ is None


@pytest.mark.asyncio
async def test_should_call_official_async_sdk_with_retry_disabled_and_bounded_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    dependencies = _recording_dependencies(captured)
    monkeypatch.setattr(adapter_module, "_load_dependencies", lambda: dependencies)
    driver = build_jev_driver(
        _context("inspect"),
        _factory(_candidate(), _candidate("choice_00000002")),
        JevSettings(api_key=SecretStr("private-provider-value"), timeout_ms=2_500),
    )

    proposal = await driver.next_action(_observation(), (_descriptor("inspect"),))

    assert proposal.based_on_saga_seq == 3
    assert driver.provider_id == "typesafe"
    assert driver.model_route == (_MODEL,)
    client_values = cast(dict[str, object], captured["client"])
    assert client_values == {
        "api_key": "private-provider-value",
        "base_url": "https://api.typesafe.ai",
        "model": _MODEL,
        "retry": client_values["retry"],
        "timeout": 2.5,
    }
    assert captured["max_retries"] == 0
    questions = cast(Mapping[str, object], captured["questions"])
    assert set(questions) == {"next_action"}
    assert "private-provider-value" not in repr(driver)


@pytest.mark.asyncio
async def test_should_ignore_ambient_typesafe_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://attacker.invalid")
    monkeypatch.setattr(
        adapter_module, "_load_dependencies", lambda: _recording_dependencies(captured)
    )
    driver = build_jev_driver(
        _context("inspect"),
        _factory(_candidate(), _candidate("choice_00000002")),
        JevSettings(api_key=SecretStr("private-provider-value")),
    )

    await driver.next_action(_observation(), (_descriptor("inspect"),))

    client_values = cast(dict[str, object], captured["client"])
    assert client_values["base_url"] == "https://api.typesafe.ai"


@pytest.mark.asyncio
async def test_should_match_real_typesafe_sdk_http_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    transport = httpx2.MockTransport(_successful_sdk_response(captured))

    def client_factory(**values: object) -> AsyncTypeSafeClient:
        return AsyncTypeSafeClient(
            api_key=cast(str, values["api_key"]),
            base_url=cast(str, values["base_url"]),
            model=cast(str, values["model"]),
            retry=cast(RetryPolicy, values["retry"]),
            timeout=cast(float, values["timeout"]),
            transport=transport,
        )

    dependencies = adapter_module._JevDependencies(
        client_factory=cast(adapter_module._ClientFactory, client_factory),
        choice_factory=cast(adapter_module._ChoiceFactory, Choice),
        retry_factory=cast(adapter_module._RetryFactory, RetryPolicy),
    )
    monkeypatch.setattr(adapter_module, "_load_dependencies", lambda: dependencies)
    driver = build_jev_driver(
        _context("inspect"),
        _factory(_candidate(), _candidate("choice_00000002")),
        JevSettings(api_key=SecretStr("test-secret")),
    )

    proposal = await driver.next_action(_observation(), (_descriptor("inspect"),))

    assert captured["url"] == "https://api.typesafe.ai/v1/systemone"
    body = cast(Mapping[str, object], captured["body"])
    assert body["model"] == _MODEL
    assert set(cast(Mapping[str, object], body["questions"])) == {"next_action"}
    assert "test-secret" not in repr(body)
    assert proposal.based_on_saga_seq == 3


def test_should_fail_with_safe_missing_extra_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(name: str) -> object:
        del name
        raise ModuleNotFoundError

    monkeypatch.setattr(adapter_module, "import_module", missing)

    with pytest.raises(RuntimeError, match=r"install agentic-saga\[jev\]") as captured:
        build_jev_driver(
            _context("inspect"),
            _factory(_candidate()),
            JevSettings(api_key=SecretStr("test-key")),
        )
    assert captured.value.__cause__ is None


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (ConnectionError("private transport"), AgentFailureCategory.TRANSPORT_EXHAUSTED),
        (TimeoutError("private timeout"), AgentFailureCategory.TRANSPORT_EXHAUSTED),
        (ValueError("private invalid"), AgentFailureCategory.INVALID_RESPONSE),
    ],
)
@pytest.mark.asyncio
async def test_should_map_sdk_failures_without_retaining_private_details(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    category: AgentFailureCategory,
) -> None:
    decision = adapter_module._JevDecisionCall(
        settings=JevSettings(api_key=SecretStr("test-key")),
        dependencies=_failing_dependencies(error),
    )

    with pytest.raises(AgentPlanningError) as captured:
        await decision({}, {"choice_00000001": {}})
    assert captured.value.category is category
    assert "private" not in f"{captured.value!s}{captured.value!r}"


@pytest.mark.parametrize(
    ("status", "category"),
    [
        (200, AgentFailureCategory.INVALID_RESPONSE),
        (400, AgentFailureCategory.REQUEST_REJECTED),
        (429, AgentFailureCategory.RATE_LIMIT_EXHAUSTED),
        (503, AgentFailureCategory.SERVER_ERROR_EXHAUSTED),
    ],
)
@pytest.mark.asyncio
async def test_should_map_typesafe_status_attribute(
    status: int, category: AgentFailureCategory
) -> None:
    class ProviderError(Exception):
        def __init__(self) -> None:
            self.status = status
            super().__init__("private body")

    decision = adapter_module._JevDecisionCall(
        settings=JevSettings(api_key=SecretStr("test-key")),
        dependencies=_failing_dependencies(ProviderError()),
    )

    with pytest.raises(AgentPlanningError) as captured:
        await decision({}, {"choice_00000001": {}})
    assert captured.value.category is category


@pytest.mark.asyncio
async def test_should_reject_malformed_sdk_response_as_invalid() -> None:
    class Client:
        def __init__(self, **values: object) -> None:
            del values

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def system_one(self, **values: object) -> object:
            del values
            return SimpleNamespace(choices={})

    dependencies = _dependencies(Client)
    decision = adapter_module._JevDecisionCall(
        settings=JevSettings(api_key=SecretStr("test-key")),
        dependencies=dependencies,
    )

    with pytest.raises(AgentPlanningError) as captured:
        await decision({}, {"choice_00000001": {}})
    assert captured.value.category is AgentFailureCategory.INVALID_RESPONSE


def test_should_not_retain_malformed_provider_values_in_exception_context() -> None:
    sentinel = "private-provider-response-value"
    answer = SimpleNamespace(
        choice=sentinel,
        confidence=0.9,
        probabilities={"choice_00000001": 1.0},
    )
    response = SimpleNamespace(model=_MODEL, choices={"next_action": answer})

    with pytest.raises(AgentPlanningError) as captured:
        adapter_module._selection_from_response(response, _MODEL)

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert sentinel not in _exception_tree(captured.value)


def test_should_reject_response_from_unconfigured_model() -> None:
    answer = SimpleNamespace(
        choice="choice_00000001",
        confidence=1.0,
        probabilities={"choice_00000001": 1.0},
    )
    response = SimpleNamespace(model="jev-9.99.0", choices={"next_action": answer})

    with pytest.raises(AgentPlanningError) as captured:
        adapter_module._selection_from_response(response, _MODEL)

    assert captured.value.category is AgentFailureCategory.INVALID_RESPONSE
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def _dependencies(client: type[object]) -> adapter_module._JevDependencies:
    class Retry:
        def __init__(self, *, max_retries: int) -> None:
            self.max_retries = max_retries

    class Choice:
        def __init__(self, *, instructions: str, criteria: Mapping[str, object]) -> None:
            self.instructions = instructions
            self.criteria = criteria

    return adapter_module._JevDependencies(
        client_factory=cast(adapter_module._ClientFactory, client),
        choice_factory=cast(adapter_module._ChoiceFactory, Choice),
        retry_factory=cast(adapter_module._RetryFactory, Retry),
    )


def _successful_sdk_response(
    captured: dict[str, object],
) -> Callable[[httpx2.Request], Coroutine[None, None, httpx2.Response]]:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx2.Response(200, json=_sdk_response_payload())

    return handler


def _sdk_response_payload() -> dict[str, object]:
    return {
        "model": _MODEL,
        "usage": {"input_tokens": 10, "output_tokens": 2},
        "answers": {
            "next_action": {
                "type": "choice",
                "choice": "choice_00000002",
                "confidence": 0.92,
                "probabilities": {
                    "choice_00000001": 0.08,
                    "choice_00000002": 0.92,
                },
            }
        },
    }


def _failing_dependencies(error: Exception) -> adapter_module._JevDependencies:
    class Client:
        def __init__(self, **values: object) -> None:
            del values

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def system_one(self, **values: object) -> object:
            del values
            raise error

    return _dependencies(Client)


def _exception_tree(error: BaseException) -> str:
    values: list[str] = []
    current: BaseException | None = error
    while current is not None:
        values.extend((str(current), repr(current)))
        current = current.__cause__ or current.__context__
    return "".join(values)


def test_decision_selection_is_the_sdk_boundary_contract() -> None:
    selection = DecisionSelection(
        choice_id="choice_00000001",
        confidence=1.0,
        probabilities={"choice_00000001": 1.0},
    )
    assert selection.choice_id == "choice_00000001"
