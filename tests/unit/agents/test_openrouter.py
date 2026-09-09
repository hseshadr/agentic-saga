from __future__ import annotations

from typing import cast

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import SecretStr, ValidationError

from agentic_saga.agents import OpenRouterSettings, build_openrouter_driver
from agentic_saga.agents import openrouter as adapter_module
from tests.unit.agents.test_deepagents import _context

_PRIMARY = "openai/gpt-oss-20b"
_FALLBACK = "qwen/qwen3-30b-a3b-instruct-2507"


def test_should_pin_cheap_capable_models_and_bounded_deterministic_defaults() -> None:
    # Given / When
    settings = OpenRouterSettings(api_key=SecretStr("test-key"))

    # Then
    assert settings.model_route == (_PRIMARY, _FALLBACK)
    assert settings.temperature == 0
    assert settings.reasoning_effort == "low"
    assert settings.max_output_tokens == 512
    assert settings.timeout_ms == 10_000
    assert settings.sdk_retries == 0


@pytest.mark.parametrize(
    "model_id",
    [
        "openrouter/auto",
        "openrouter/free",
        "vendor/latest",
        "vendor/model:free",
        "vendor/model:pinned-route",
    ],
)
def test_should_reject_moving_or_random_model_routes(model_id: str) -> None:
    # Given / When / Then
    with pytest.raises(ValidationError):
        OpenRouterSettings(api_key=SecretStr("test-key"), primary_model=model_id)


def test_should_accept_a_concrete_custom_model_route() -> None:
    settings = OpenRouterSettings(
        api_key=SecretStr("test-key"),
        primary_model="openai/custom-pinned-model",
    )
    assert settings.primary_model == "openai/custom-pinned-model"


def test_should_reject_same_primary_and_fallback() -> None:
    with pytest.raises(ValidationError, match="fallback model must differ"):
        OpenRouterSettings(
            api_key=SecretStr("test-key"),
            primary_model=_PRIMARY,
            fallback_model=_PRIMARY,
        )


def test_should_load_key_from_environment_without_exposing_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    secret = "private-" + "provider-value"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)

    # When
    settings = OpenRouterSettings.from_environment()

    # Then
    assert settings.api_key.get_secret_value() == secret
    assert secret not in repr(settings)


def test_should_fail_closed_when_environment_key_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    # When / Then
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY is required") as captured:
        OpenRouterSettings.from_environment()
    assert captured.value.__cause__ is None


def test_should_fail_closed_when_environment_key_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY is required"):
        OpenRouterSettings.from_environment()


def test_should_build_deep_agent_with_ordered_model_route() -> None:
    # Given
    settings = OpenRouterSettings(api_key=SecretStr("test-key"))

    # When
    driver = build_openrouter_driver(_context("inspect"), settings)

    # Then
    assert driver.provider_id == "openrouter"
    assert driver.model_route == (_PRIMARY, _FALLBACK)


def test_should_apply_budget_caps_and_one_ordered_server_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    captured: dict[str, object] = {}

    def factory(**kwargs: object) -> BaseChatModel:
        captured.update(kwargs)
        return cast(BaseChatModel, object())

    monkeypatch.setattr(adapter_module, "_load_model_factory", lambda: factory)

    # When
    private_value = "private-" + "provider-value"
    settings = OpenRouterSettings(api_key=SecretStr(private_value))
    adapter_module._build_model(_context("inspect"), settings)

    # Then
    assert captured["model"] == _PRIMARY
    assert captured["model_kwargs"] == {"models": [_FALLBACK], "parallel_tool_calls": False}
    assert captured["max_tokens"] == 500
    assert captured["timeout"] == 5_000
    assert captured["max_retries"] == 0
    assert captured["temperature"] == 0
    assert captured["seed"] == 0
    assert captured["openrouter_provider"] == {"require_parameters": True}
    assert private_value not in repr(captured)


def test_should_cap_non_divisible_model_budget_at_fixed_durable_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    captured: dict[str, object] = {}

    def factory(**kwargs: object) -> BaseChatModel:
        captured.update(kwargs)
        return cast(BaseChatModel, object())

    monkeypatch.setattr(adapter_module, "_load_model_factory", lambda: factory)
    context = _context("inspect")
    budget = context.budget.model_copy(
        update={"turn_limit": 3, "token_limit": 10, "elapsed_ms_limit": 10}
    )

    # When
    adapter_module._build_model(
        context.model_copy(update={"budget": budget}),
        OpenRouterSettings(api_key=SecretStr("test-key")),
    )

    # Then
    assert captured["max_tokens"] == 3
    assert captured["timeout"] == 3


def test_should_fail_safely_when_openrouter_extra_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    def missing(name: str) -> object:
        del name
        raise ModuleNotFoundError("raw dependency internals")

    monkeypatch.setattr(adapter_module, "import_module", missing)

    # When / Then
    with pytest.raises(RuntimeError, match=r"install agentic-saga\[agent\]") as captured:
        adapter_module._load_model_factory()
    assert "raw dependency internals" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_should_reject_a_budget_without_model_capacity() -> None:
    with pytest.raises(ValueError, match="budget does not permit"):
        adapter_module._turn_cap(0, 1)
