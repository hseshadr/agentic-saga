from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import SecretStr

from agentic_saga.agents import OpenRouterSettings
from agentic_saga.agents.deepagents import AgentFailureCategory, AgentPlanningError
from agentic_saga.contracts.actions import AgentProposal, Finish, ToolCall
from agentic_saga.contracts.common import JsonObject
from agentic_saga.contracts.runtime import AgentDriver, SagaObservation, ToolDescriptor
from agentic_saga.manifest import SagaContext
from examples.ecommerce.eval import main
from examples.ecommerce.evaluation import EvalSuite
from examples.ecommerce.live_eval import (
    LiveEvalConfigurationError,
    LiveEvalOptions,
    run_live_corpus,
)

ROOT = Path(__file__).parents[2]
CORPUS = ROOT / "examples/ecommerce/eval-corpus-v1.json"
FAKE_KEY = "fake-openrouter-key"


class ExpectedDriver:
    async def next_action(
        self, observation: SagaObservation, available_tools: Sequence[ToolDescriptor]
    ) -> AgentProposal:
        if not available_tools:
            return _finish(observation)
        name = available_tools[0].name
        return _tool_call(observation, name, _arguments(observation, name))


class FailingDriver:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def next_action(
        self, observation: SagaObservation, available_tools: Sequence[ToolDescriptor]
    ) -> AgentProposal:
        del observation, available_tools
        raise self.error


def _finish(observation: SagaObservation) -> Finish:
    return Finish(
        proposal_id=f"proposal_{observation.saga_seq:020d}",
        based_on_saga_seq=observation.saga_seq,
        rationale="The workflow supplied fresh terminal proof.",
        target_status="succeeded_verified",
    )


def _tool_call(observation: SagaObservation, name: str, arguments: JsonObject) -> ToolCall:
    return ToolCall(
        proposal_id=f"proposal_{observation.saga_seq:020d}",
        based_on_saga_seq=observation.saga_seq,
        tool_name=name,
        arguments=arguments,
        rationale="Select the one currently eligible business capability.",
    )


def _arguments(observation: SagaObservation, name: str) -> JsonObject:
    context = observation.goal.context
    if name == "reserve_inventory":
        keys = ("order_id", "sku", "quantity", "version")
        values = {key: context[key] for key in keys}
        values["expected_version"] = values.pop("version")
        return values
    if name == "charge_payment":
        keys = ("order_id", "customer_id", "amount_minor", "currency")
        return {key: context[key] for key in keys}
    return {"order_id": context["order_id"]}


def _factory(context: SagaContext, settings: OpenRouterSettings) -> AgentDriver:
    assert settings.primary_model == "openai/gpt-oss-120b"
    assert "Temporal workflow" in context.manifest.instructions[0]
    return ExpectedDriver()


def _options(suite: EvalSuite = EvalSuite.SMOKE) -> LiveEvalOptions:
    return LiveEvalOptions(
        suite=suite,
        settings=OpenRouterSettings(api_key=SecretStr(FAKE_KEY)),
        driver_factory=_factory,
    )


def _failing_options(error: Exception) -> LiveEvalOptions:
    def factory(context: SagaContext, settings: OpenRouterSettings) -> AgentDriver:
        del context, settings
        return FailingDriver(error)

    return LiveEvalOptions(
        settings=OpenRouterSettings(api_key=SecretStr(FAKE_KEY)), driver_factory=factory
    )


@pytest.mark.asyncio
async def test_smoke_exercises_temporal_observation_and_native_forward_tool(tmp_path: Path) -> None:
    report = await run_live_corpus(CORPUS, 1, tmp_path, _options())

    assert report.identity.configured_model == "openai/gpt-oss-120b"
    assert report.identity.provider == "openrouter"
    assert report.score.model_sample_count == 1
    assert report.score.decision_accuracy == 1
    assert report.samples[0].selected_tool == "reserve_inventory"
    assert report.samples[0].exposed_tools == ("reserve_inventory",)


@pytest.mark.asyncio
async def test_release_excludes_workflow_owned_compensation_and_human_controls(
    tmp_path: Path,
) -> None:
    report = await run_live_corpus(CORPUS, 1, tmp_path, _options(EvalSuite.RELEASE))

    exposed = {tool for sample in report.samples for tool in sample.exposed_tools}
    assert "begin_compensation" not in exposed
    assert "escalate_to_human" not in exposed
    assert report.score.model_sample_count == 3


@pytest.mark.asyncio
async def test_artifacts_never_persist_provider_credentials(tmp_path: Path) -> None:
    await run_live_corpus(CORPUS, 1, tmp_path, _options())

    text = _artifact_text(tmp_path)
    assert FAKE_KEY not in text
    assert "expected_arguments" not in text


@pytest.mark.asyncio
async def test_provider_failure_is_separate_from_model_quality(tmp_path: Path) -> None:
    error = AgentPlanningError(AgentFailureCategory.TRANSPORT_EXHAUSTED)

    report = await run_live_corpus(CORPUS, 1, tmp_path, _failing_options(error))

    assert report.score.model_sample_count == 0
    assert report.score.provider_failure_count == 1


@pytest.mark.asyncio
async def test_invalid_model_response_counts_as_model_quality_failure(tmp_path: Path) -> None:
    error = AgentPlanningError(AgentFailureCategory.INVALID_RESPONSE)

    report = await run_live_corpus(CORPUS, 1, tmp_path, _failing_options(error))

    assert report.score.model_sample_count == 1
    assert report.samples[0].structured_valid is False


@pytest.mark.asyncio
async def test_unknown_driver_failure_is_sanitized(tmp_path: Path) -> None:
    with pytest.raises(LiveEvalConfigurationError, match="failed safely"):
        await run_live_corpus(CORPUS, 1, tmp_path, _failing_options(RuntimeError("private")))


@pytest.mark.asyncio
async def test_sample_count_is_bounded_before_model_construction(tmp_path: Path) -> None:
    with pytest.raises(LiveEvalConfigurationError, match="between 1 and 10"):
        await run_live_corpus(CORPUS, 0, tmp_path, _options())


@pytest.mark.asyncio
async def test_missing_environment_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(LiveEvalConfigurationError, match="configuration is unavailable"):
        await run_live_corpus(CORPUS, 1, tmp_path)


def _artifact_text(root: Path) -> str:
    return "".join(path.read_text() for path in root.rglob("*.json"))


def test_cli_requires_explicit_live_consent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RUN_LIVE_MODEL_EVALS", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)

    assert main(["--live", "--suite", "smoke"]) == 2


def test_cli_offline_validation_makes_no_live_call(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--corpus", str(CORPUS)]) == 0
    assert "24" in capsys.readouterr().out


@pytest.mark.live_model
@pytest.mark.asyncio
async def test_real_openrouter_smoke_is_explicit_and_bounded(tmp_path: Path) -> None:
    if os.environ.get("RUN_LIVE_MODEL_EVALS") != "1" or not os.environ.get("OPENROUTER_API_KEY"):
        pytest.skip("live OpenRouter smoke requires explicit consent and credentials")

    report = await run_live_corpus(CORPUS, 1, tmp_path, LiveEvalOptions(suite=EvalSuite.SMOKE))

    sample = report.samples[0]
    assert report.identity.configured_model == "openai/gpt-oss-120b"
    assert sample.selected_tool in sample.exposed_tools
    assert sample.latency_ms >= 0
