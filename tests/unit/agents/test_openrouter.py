from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import SecretStr, ValidationError
from pydantic_ai.models.openrouter import OpenRouterModel

from agentic_saga.agents import OpenRouterSettings, build_openrouter_driver
from agentic_saga.agents import openrouter as adapter_module
from tests.unit.agents.test_deepagents import _context

_PRIMARY = "openai/gpt-oss-120b"
_COMPARATOR = "openai/gpt-oss-20b"
_ALTERNATE = "qwen/qwen3-30b-a3b-instruct-2507"


def test_should_pin_cheap_capable_models_and_bounded_deterministic_defaults() -> None:
    # Given / When
    settings = OpenRouterSettings(api_key=SecretStr("test-key"))

    # Then
    assert settings.model_route == (_PRIMARY,)
    assert settings.temperature == 0
    assert settings.reasoning_effort == "low"
    assert settings.max_output_tokens == 512
    assert settings.timeout_ms == 30_000
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
        primary_model=_COMPARATOR,
    )
    assert settings.primary_model == _COMPARATOR


def test_should_load_key_from_environment_without_exposing_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    secret = "private-" + "provider-value"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)

    # When
    settings = OpenRouterSettings.from_environment()

    # Then
    assert settings.api_key.get_secret_value() == secret
    assert secret not in repr(settings)


def test_should_load_a_validated_pinned_model_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", _ALTERNATE)

    settings = OpenRouterSettings.from_environment()

    assert settings.primary_model == _ALTERNATE


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
    assert driver.model_route == (_PRIMARY,)


def test_should_not_retain_openrouter_secret_in_driver_representation() -> None:
    private_value = "private-" + "provider-value"

    driver = build_openrouter_driver(
        _context("inspect"),
        OpenRouterSettings(api_key=SecretStr(private_value)),
    )

    assert private_value not in repr(driver)


def test_should_apply_only_supported_parameters_to_one_pinned_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    captured: dict[str, dict[str, object]] = {}
    model = object()
    provider = SimpleNamespace(client=SimpleNamespace(max_retries=2))

    def provider_factory(**kwargs: object) -> adapter_module._Provider:
        captured["provider"] = kwargs
        return cast(adapter_module._Provider, provider)

    def model_factory(model_name: str, **kwargs: object) -> object:
        captured["model"] = {"model_name": model_name, **kwargs}
        return model

    dependencies = adapter_module._OpenRouterDependencies(provider_factory, model_factory)
    monkeypatch.setattr(adapter_module, "_load_model_dependencies", lambda: dependencies)

    # When
    private_value = "private-" + "provider-value"
    settings = OpenRouterSettings(api_key=SecretStr(private_value))
    built = adapter_module._build_model(_context("inspect"), settings)

    # Then
    assert built is model
    assert captured["provider"]["api_key"] == private_value
    assert provider.client.max_retries == 0
    assert captured["model"]["provider"] is provider
    assert captured["model"]["model_name"] == _PRIMARY
    assert captured["model"]["settings"] == {
        "max_tokens": 250,
        "timeout": 2.5,
        "temperature": 0,
        "openrouter_reasoning": {"effort": "low"},
        "openrouter_provider": {"require_parameters": True},
    }


def test_should_cap_non_divisible_model_budget_at_fixed_durable_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    captured: dict[str, dict[str, object]] = {}
    provider = SimpleNamespace(client=SimpleNamespace(max_retries=2))

    def provider_factory(**kwargs: object) -> adapter_module._Provider:
        captured["provider"] = kwargs
        return cast(adapter_module._Provider, provider)

    def model_factory(model_name: str, **kwargs: object) -> object:
        captured["model"] = {"model_name": model_name, **kwargs}
        return object()

    dependencies = adapter_module._OpenRouterDependencies(provider_factory, model_factory)
    monkeypatch.setattr(adapter_module, "_load_model_dependencies", lambda: dependencies)
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
    settings = captured["model"]["settings"]
    assert isinstance(settings, dict)
    assert settings["max_tokens"] == 1
    assert settings["timeout"] == 0.001
    assert provider.client.max_retries == 0


@pytest.mark.asyncio
async def test_should_close_and_reopen_provider_owned_client() -> None:
    model = cast(
        OpenRouterModel,
        adapter_module._build_model(
            _context("inspect"), OpenRouterSettings(api_key=SecretStr("test-key"))
        ),
    )

    assert model.client.max_retries == 0
    with pytest.raises(RuntimeError, match="lifecycle probe"):
        async with model:
            assert not model.client.is_closed()
            raise RuntimeError("lifecycle probe")
    assert model.client.is_closed()
    async with model:
        assert not model.client.is_closed()
    assert model.client.is_closed()


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
        adapter_module._load_model_dependencies()
    assert "raw dependency internals" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_should_reject_a_budget_without_model_capacity() -> None:
    with pytest.raises(ValueError, match="budget does not permit"):
        adapter_module._turn_cap(0, 1)


def test_should_reject_a_per_turn_budget_too_small_for_bounded_correction() -> None:
    context = _context("inspect")
    budget = context.budget.model_copy(update={"turn_limit": 4, "token_limit": 4})

    with pytest.raises(ValueError, match="budget does not permit"):
        adapter_module._model_caps(context.model_copy(update={"budget": budget}))
