from __future__ import annotations

import json
import logging
from collections.abc import Callable, Coroutine, Mapping
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from openrouter import OpenRouter
from pydantic import SecretStr, ValidationError

from agentic_saga.agents import openrouter_decisions as adapter_module
from agentic_saga.agents.deepagents import AgentFailureCategory, AgentPlanningError
from agentic_saga.agents.openrouter_decisions import (
    OpenRouterDecisionsSettings,
    build_openrouter_decisions_driver,
)
from tests.unit.agents.test_choice import _candidate, _factory
from tests.unit.agents.test_deepagents import _context, _descriptor, _observation

_MODEL = "typesafe/jev-1.13"
_RESPONSE_MODEL = "typesafe/jev-1.13-20260917"


class _DecisionsRecorder:
    def __init__(self, captured: dict[str, object], response: object) -> None:
        self.captured = captured
        self.response = response

    async def create_async(self, **values: object) -> object:
        self.captured["request"] = values
        return self.response


class _Client:
    def __init__(self, captured: dict[str, object], response: object, values: object) -> None:
        captured["client"] = values
        self.alpha = SimpleNamespace(decisions=_DecisionsRecorder(captured, response))

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args


class _ClientRecorder:
    def __init__(self, captured: dict[str, object], response: object) -> None:
        self.captured = captured
        self.response = response

    def __call__(self, **values: object) -> _Client:
        return _Client(self.captured, self.response, values)


def _response(
    *,
    model: str = _RESPONSE_MODEL,
    choice: object = "choice_00000002",
    confidence: object = 0.9,
    probabilities: object = None,
) -> object:
    distribution = probabilities or {"choice_00000001": 0.1, "choice_00000002": 0.9}
    answer = SimpleNamespace(
        choice=choice,
        confidence=confidence,
        probabilities=distribution,
    )
    return SimpleNamespace(model=model, answers={"next_action": answer})


def _dependencies(
    captured: dict[str, object], response: object | None = None
) -> adapter_module._OpenRouterDependencies:
    factory = _ClientRecorder(captured, response or _response())
    return adapter_module._OpenRouterDependencies(
        client_factory=cast(adapter_module._ClientFactory, factory),
        sync_http_client_factory=cast(adapter_module._SyncHttpClientFactory, httpx.Client),
        async_http_client_factory=cast(adapter_module._AsyncHttpClientFactory, httpx.AsyncClient),
    )


def test_should_use_pinned_private_bounded_defaults() -> None:
    settings = OpenRouterDecisionsSettings(api_key=SecretStr("test-secret"))

    assert settings.model == _MODEL
    assert settings.timeout_ms == 10_000
    assert settings.sdk_retries == 0
    assert settings.zdr is True
    assert settings.data_collection == "deny"
    assert settings.min_confidence == 0.8
    assert "test-secret" not in repr(settings)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model", "typesafe/jev-latest"),
        ("model", "typesafe/jev-1.12"),
        ("sdk_retries", 1),
        ("zdr", False),
        ("data_collection", "allow"),
    ],
)
def test_should_reject_route_or_privacy_weakening(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        OpenRouterDecisionsSettings.model_validate(
            {"api_key": "test-secret", field: value}, strict=True
        )


def test_should_load_only_secret_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "private-provider-value")
    monkeypatch.setenv("OPENROUTER_MODEL", "attacker/model")

    settings = OpenRouterDecisionsSettings.from_environment()

    assert settings.api_key.get_secret_value() == "private-provider-value"
    assert settings.model == _MODEL


@pytest.mark.parametrize("value", [None, ""])
def test_should_require_environment_key(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    else:
        monkeypatch.setenv("OPENROUTER_API_KEY", value)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY is required") as captured:
        OpenRouterDecisionsSettings.from_environment()
    assert captured.value.__cause__ is None


@pytest.mark.asyncio
async def test_should_call_only_decisions_with_private_routing_and_no_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(adapter_module, "_load_dependencies", lambda: _dependencies(captured))
    driver = build_openrouter_decisions_driver(
        _context("inspect"),
        _factory(_candidate(), _candidate("choice_00000002")),
        OpenRouterDecisionsSettings(api_key=SecretStr("private-provider-value"), timeout_ms=2_500),
    )

    proposal = await driver.next_action(_observation(), (_descriptor("inspect"),))

    assert proposal.based_on_saga_seq == 3
    assert driver.provider_id == "openrouter-decisions"
    assert driver.model_route == (_MODEL,)
    client = cast(dict[str, object], captured["client"])
    assert client["api_key"] == "private-provider-value"
    assert client["server_url"] == "https://openrouter.ai"
    assert client["timeout_ms"] == 2_500
    assert client["http_referer"] == ""
    assert client["x_open_router_title"] == ""
    assert client["x_open_router_categories"] == ""
    assert cast(httpx.Client, client["client"]).is_closed
    assert cast(httpx.AsyncClient, client["async_client"]).is_closed
    request = cast(dict[str, object], captured["request"])
    assert request["model"] == _MODEL
    assert request["provider"] == {"zdr": True, "data_collection": "deny"}
    assert request["retries"] is None
    assert request["server_url"] == "https://openrouter.ai"
    assert request["timeout_ms"] == 2_500
    assert set(cast(Mapping[str, object], request["questions"])) == {"next_action"}
    assert "private-provider-value" not in repr(driver)


@pytest.mark.asyncio
async def test_should_cap_timeout_to_remaining_per_turn_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(adapter_module, "_load_dependencies", lambda: _dependencies(captured))
    base = _context("inspect")
    budget = base.budget.model_copy(update={"turn_limit": 4, "elapsed_ms_limit": 2_000})
    context = base.model_copy(update={"budget": budget})
    driver = build_openrouter_decisions_driver(
        context,
        _factory(_candidate(), _candidate("choice_00000002")),
        OpenRouterDecisionsSettings(api_key=SecretStr("test-secret")),
    )

    await driver.next_action(_observation(), (_descriptor("inspect"),))

    request = cast(dict[str, object], captured["request"])
    assert request["timeout_ms"] == 500


@pytest.mark.asyncio
async def test_should_ignore_ambient_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://attacker.invalid")
    monkeypatch.setattr(adapter_module, "_load_dependencies", lambda: _dependencies(captured))
    driver = build_openrouter_decisions_driver(
        _context("inspect"),
        _factory(_candidate(), _candidate("choice_00000002")),
        OpenRouterDecisionsSettings(api_key=SecretStr("test-secret")),
    )

    await driver.next_action(_observation(), (_descriptor("inspect"),))

    request = cast(dict[str, object], captured["request"])
    assert request["server_url"] == "https://openrouter.ai"


@pytest.mark.asyncio
async def test_should_match_real_openrouter_sdk_http_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    transport = httpx.MockTransport(_successful_sdk_response(captured))
    sync_clients: list[httpx.Client] = []
    async_clients: list[httpx.AsyncClient] = []

    def sync_factory(**values: object) -> httpx.Client:
        client = httpx.Client(follow_redirects=cast(bool, values["follow_redirects"]))
        sync_clients.append(client)
        return client

    def async_factory(**values: object) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=cast(bool, values["follow_redirects"]),
        )
        async_clients.append(client)
        return client

    monkeypatch.setattr(
        adapter_module,
        "_load_dependencies",
        lambda: _real_dependencies(sync_factory, async_factory),
    )
    driver = build_openrouter_decisions_driver(
        _context("inspect"),
        _factory(_candidate(), _candidate("choice_00000002")),
        OpenRouterDecisionsSettings(api_key=SecretStr("test-secret")),
    )

    proposal = await driver.next_action(_observation(), (_descriptor("inspect"),))

    assert captured["url"] == "https://openrouter.ai/api/alpha/decisions"
    assert captured["authorization"] == "Bearer test-secret"
    body = cast(Mapping[str, object], captured["body"])
    assert body["model"] == _MODEL
    assert body["provider"] == {"data_collection": "deny", "zdr": True}
    questions = cast(Mapping[str, Mapping[str, object]], body["questions"])
    assert questions["next_action"]["type"] == "choice"
    assert proposal.based_on_saga_seq == 3
    assert len(sync_clients) == 1 and sync_clients[0].is_closed
    assert len(async_clients) == 1 and async_clients[0].is_closed


@pytest.mark.asyncio
async def test_should_supply_and_close_both_sdk_clients_without_hidden_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    sync_client = httpx.Client(follow_redirects=False)
    async_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_successful_sdk_response(captured)),
        follow_redirects=False,
    )

    def reject_hidden_client(**values: object) -> None:
        del values
        raise AssertionError("SDK created a hidden client")

    def sync_factory(**values: object) -> httpx.Client:
        assert values == {"follow_redirects": False}
        return sync_client

    def async_factory(**values: object) -> httpx.AsyncClient:
        assert values == {"follow_redirects": False}
        return async_client

    dependencies = adapter_module._OpenRouterDependencies(
        client_factory=cast(adapter_module._ClientFactory, OpenRouter),
        sync_http_client_factory=cast(adapter_module._SyncHttpClientFactory, sync_factory),
        async_http_client_factory=cast(adapter_module._AsyncHttpClientFactory, async_factory),
        transport_errors=(httpx.RequestError, httpx.TimeoutException),
    )
    monkeypatch.setattr("openrouter.sdk.httpx.Client", reject_hidden_client)
    monkeypatch.setattr("openrouter.sdk.httpx.AsyncClient", reject_hidden_client)
    monkeypatch.setattr(adapter_module, "_load_dependencies", lambda: dependencies)
    driver = build_openrouter_decisions_driver(
        _context("inspect"),
        _factory(_candidate(), _candidate("choice_00000002")),
        OpenRouterDecisionsSettings(api_key=SecretStr("test-secret")),
    )

    await driver.next_action(_observation(), (_descriptor("inspect"),))

    assert sync_client.is_closed
    assert async_client.is_closed


@pytest.mark.asyncio
async def test_should_refuse_and_not_follow_cross_origin_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visited: list[str] = []
    sync_clients: list[httpx.Client] = []
    async_clients: list[httpx.AsyncClient] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        visited.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://attacker.invalid/collect"})

    def sync_factory(**values: object) -> httpx.Client:
        assert values == {"follow_redirects": False}
        client = httpx.Client(follow_redirects=False)
        sync_clients.append(client)
        return client

    def async_factory(**values: object) -> httpx.AsyncClient:
        assert values == {"follow_redirects": False}
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=cast(bool, values["follow_redirects"]),
        )
        async_clients.append(client)
        return client

    monkeypatch.setattr(
        adapter_module,
        "_load_dependencies",
        lambda: _real_dependencies(sync_factory, async_factory),
    )
    driver = build_openrouter_decisions_driver(
        _context("inspect"),
        _factory(_candidate(), _candidate("choice_00000002")),
        OpenRouterDecisionsSettings(api_key=SecretStr("test-secret")),
    )

    with pytest.raises(AgentPlanningError) as captured:
        await driver.next_action(_observation(), (_descriptor("inspect"),))

    assert captured.value.category is AgentFailureCategory.INVALID_RESPONSE
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert visited == ["https://openrouter.ai/api/alpha/decisions"]
    assert len(sync_clients) == 1 and sync_clients[0].is_closed
    assert len(async_clients) == 1 and async_clients[0].is_closed


@pytest.mark.asyncio
async def test_should_ignore_ambient_debug_and_attribution_without_leaking_payload(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "private-state-sentinel"
    captured: dict[str, object] = {}
    sync_clients: list[httpx.Client] = []
    async_clients: list[httpx.AsyncClient] = []
    monkeypatch.setenv("OPENROUTER_DEBUG", "1")
    monkeypatch.setenv("OPENROUTER_HTTP_REFERER", "https://attacker.invalid/referer")
    monkeypatch.setenv("OPENROUTER_X_OPEN_ROUTER_TITLE", "hostile-title")
    monkeypatch.setenv("OPENROUTER_X_OPEN_ROUTER_CATEGORIES", "hostile-category")
    caplog.set_level(logging.DEBUG)

    def sync_factory(**values: object) -> httpx.Client:
        client = httpx.Client(follow_redirects=cast(bool, values["follow_redirects"]))
        sync_clients.append(client)
        return client

    def async_factory(**values: object) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(_successful_sdk_response(captured)),
            follow_redirects=cast(bool, values["follow_redirects"]),
        )
        async_clients.append(client)
        return client

    monkeypatch.setattr(
        adapter_module,
        "_load_dependencies",
        lambda: _real_dependencies(sync_factory, async_factory),
    )
    driver = build_openrouter_decisions_driver(
        _context("inspect"),
        _factory(_candidate(criteria=sentinel), _candidate("choice_00000002")),
        OpenRouterDecisionsSettings(api_key=SecretStr("test-secret")),
    )

    await driver.next_action(_observation(), (_descriptor("inspect"),))

    headers = cast(Mapping[str, str], captured["headers"])
    assert all("attacker.invalid" not in value for value in headers.values())
    assert "hostile-title" not in headers.values()
    assert "hostile-category" not in headers.values()
    assert sentinel not in caplog.text
    assert len(sync_clients) == 1 and sync_clients[0].is_closed
    assert len(async_clients) == 1 and async_clients[0].is_closed


def test_should_fail_with_safe_missing_extra_message(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(name: str) -> object:
        del name
        raise ModuleNotFoundError

    monkeypatch.setattr(adapter_module, "import_module", missing)

    with pytest.raises(RuntimeError, match=r"agentic-saga\[jev-openrouter\]") as captured:
        build_openrouter_decisions_driver(
            _context("inspect"),
            _factory(_candidate()),
            OpenRouterDecisionsSettings(api_key=SecretStr("test-secret")),
        )
    assert captured.value.__cause__ is None


def test_should_load_the_official_sdk_dynamically() -> None:
    dependencies = adapter_module._load_dependencies()

    assert cast(object, dependencies.client_factory) is OpenRouter
    assert cast(object, dependencies.sync_http_client_factory) is httpx.Client
    assert cast(object, dependencies.async_http_client_factory) is httpx.AsyncClient
    assert httpx.RequestError in dependencies.transport_errors


@pytest.mark.asyncio
async def test_should_sanitize_a_malformed_response_from_the_call_boundary() -> None:
    decision = adapter_module._OpenRouterDecisionCall(
        OpenRouterDecisionsSettings(api_key=SecretStr("test-secret")),
        _dependencies({}, SimpleNamespace(answers={})),
    )

    with pytest.raises(AgentPlanningError) as captured:
        await decision({}, {"choice_00000001": {}})
    assert captured.value.category is AgentFailureCategory.INVALID_RESPONSE
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.asyncio
async def test_should_reject_incomplete_probabilities_through_choice_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    incomplete = _response(probabilities={"choice_00000002": 1.0})
    monkeypatch.setattr(
        adapter_module, "_load_dependencies", lambda: _dependencies(captured, incomplete)
    )
    driver = build_openrouter_decisions_driver(
        _context("inspect"),
        _factory(_candidate(), _candidate("choice_00000002")),
        OpenRouterDecisionsSettings(api_key=SecretStr("test-secret")),
    )

    with pytest.raises(AgentPlanningError) as captured_error:
        await driver.next_action(_observation(), (_descriptor("inspect"),))
    assert captured_error.value.category is AgentFailureCategory.INVALID_RESPONSE


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (httpx.ConnectError("private transport"), AgentFailureCategory.TRANSPORT_EXHAUSTED),
        (TimeoutError("private timeout"), AgentFailureCategory.TRANSPORT_EXHAUSTED),
        (ValueError("private invalid"), AgentFailureCategory.INVALID_RESPONSE),
    ],
)
@pytest.mark.asyncio
async def test_should_map_sdk_failures_without_private_details(
    error: Exception, category: AgentFailureCategory
) -> None:
    decision = adapter_module._OpenRouterDecisionCall(
        OpenRouterDecisionsSettings(api_key=SecretStr("test-secret")),
        _failing_dependencies(error),
    )

    with pytest.raises(AgentPlanningError) as captured:
        await decision({}, {"choice_00000001": {}})
    assert captured.value.category is category
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "private" not in _exception_tree(captured.value)


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
async def test_should_map_openrouter_status_code(
    status: int, category: AgentFailureCategory
) -> None:
    class ProviderError(Exception):
        def __init__(self) -> None:
            self.status_code = status
            super().__init__("private provider body")

    decision = adapter_module._OpenRouterDecisionCall(
        OpenRouterDecisionsSettings(api_key=SecretStr("test-secret")),
        _failing_dependencies(ProviderError()),
    )

    with pytest.raises(AgentPlanningError) as captured:
        await decision({}, {"choice_00000001": {}})
    assert captured.value.category is category


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(model=_RESPONSE_MODEL, answers={}),
        _response(model="typesafe/jev-1.13-unknown"),
        _response(confidence=None),
        _response(probabilities="not-a-distribution"),
    ],
)
def test_should_fail_closed_on_malformed_or_wrong_model_response(response: object) -> None:
    with pytest.raises(AgentPlanningError) as captured:
        adapter_module._selection_from_response(response)
    assert captured.value.category is AgentFailureCategory.INVALID_RESPONSE
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def _successful_sdk_response(
    captured: dict[str, object],
) -> Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]:
    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["authorization"]
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_sdk_response_payload())

    return handler


def _sdk_response_payload() -> dict[str, object]:
    return {
        "model": _RESPONSE_MODEL,
        "provider": "TypeSafe",
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


def _failing_dependencies(error: Exception) -> adapter_module._OpenRouterDependencies:
    class Decisions:
        async def create_async(self, **values: object) -> object:
            del values
            raise error

    class Client:
        def __init__(self, **values: object) -> None:
            del values
            self.alpha = SimpleNamespace(decisions=Decisions())

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

    return adapter_module._OpenRouterDependencies(
        client_factory=cast(adapter_module._ClientFactory, Client),
        sync_http_client_factory=cast(adapter_module._SyncHttpClientFactory, httpx.Client),
        async_http_client_factory=cast(adapter_module._AsyncHttpClientFactory, httpx.AsyncClient),
        transport_errors=(httpx.RequestError, TimeoutError),
    )


def _real_dependencies(
    sync_factory: Callable[..., httpx.Client],
    async_factory: Callable[..., httpx.AsyncClient],
) -> adapter_module._OpenRouterDependencies:
    return adapter_module._OpenRouterDependencies(
        client_factory=cast(adapter_module._ClientFactory, OpenRouter),
        sync_http_client_factory=cast(adapter_module._SyncHttpClientFactory, sync_factory),
        async_http_client_factory=cast(adapter_module._AsyncHttpClientFactory, async_factory),
        transport_errors=(httpx.RequestError, httpx.TimeoutException),
    )


def _exception_tree(error: BaseException) -> str:
    values: list[str] = []
    current: BaseException | None = error
    while current is not None:
        values.extend((str(current), repr(current)))
        current = current.__cause__ or current.__context__
    return "".join(values)
