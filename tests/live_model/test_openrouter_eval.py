from __future__ import annotations

import json
import os
from collections.abc import Callable, Coroutine, Sequence
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import SecretStr, TypeAdapter

from agentic_saga.agents import OpenRouterSettings
from agentic_saga.agents.deepagents import AgentFailureCategory, AgentPlanningError
from agentic_saga.contracts.actions import AgentProposal, Escalate
from agentic_saga.contracts.common import JsonObject, sha256_json
from agentic_saga.contracts.runtime import (
    AgentDriver,
    SagaObservation,
    ToolDescriptor,
)
from agentic_saga.manifest import SagaContext
from examples.ecommerce.demo import ScriptedProposalDriver
from examples.ecommerce.eval import main as eval_main
from examples.ecommerce.evaluation import EvalReport, ProviderFailureReason
from examples.ecommerce.live_eval import (
    EvalIdentity,
    LiveEvalConfigurationError,
    LiveEvalReportArtifact,
    LiveSampleArtifact,
    run_live_corpus,
)

ROOT = Path(__file__).parents[2]
CORPUS = ROOT / "examples/ecommerce/eval-corpus-v1.json"
SECRET = "test-openrouter-secret"  # noqa: S105 - deliberately fake regression sentinel
_JSON: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
_IDENTITY = EvalIdentity(
    corpus_sha256="a" * 64,
    prompt_sha256="b" * 64,
    manifest_sha256="c" * 64,
    tool_catalog_sha256="d" * 64,
    configured_model="openai/gpt-oss-20b",
    configured_fallback_model="qwen/qwen3-30b-a3b-instruct-2507",
    dependency_versions=_JSON.validate_python({"agentic-saga": "0.1.0"}),
)
_SCORE = EvalReport(
    model_sample_count=24,
    provider_failure_count=0,
    structured_validity=Decimal(1),
    recoverable_success=Decimal(1),
    critical_escalation_recall=Decimal(1),
    kernel_rejection_rate=Decimal(1),
    turn_budget_compliance=Decimal(1),
    forbidden_effect_count=0,
    leakage_count=0,
    total_latency_ms=24,
    total_input_tokens=None,
    total_output_tokens=None,
    total_cost_usd=None,
    failed_thresholds=(),
    thresholds_met=True,
)
_DENOMINATORS = _JSON.validate_python(
    {
        "structured_validity": 24,
        "recoverable_success": 6,
        "critical_escalation": 6,
        "kernel_rejection": 6,
        "forbidden_effects": 24,
        "leakage": 24,
        "budget_compliance": 24,
    }
)
_REPORT = LiveEvalReportArtifact(
    identity=_IDENTITY,
    denominators=_DENOMINATORS,
    provider_failures=_JSON.validate_python({"transport_exhausted": 0}),
    samples=_JSON.validate_python({}),
    score=_SCORE,
)
_FAILED_REPORT = _REPORT.model_copy(
    update={
        "score": _SCORE.model_copy(
            update={
                "recoverable_success": Decimal("0.83"),
                "failed_thresholds": ("recoverable_success",),
                "thresholds_met": False,
            }
        )
    }
)
_PROVIDER_REPORT = _REPORT.model_copy(
    update={
        "denominators": _JSON.validate_python({key: 0 for key in _DENOMINATORS}),
        "provider_failures": _JSON.validate_python({"transport_exhausted": 24}),
        "score": _SCORE.model_copy(
            update={
                "model_sample_count": 0,
                "provider_failure_count": 24,
                "failed_thresholds": ("structured_validity",),
                "thresholds_met": False,
            }
        ),
    }
)


def _enable_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUN_LIVE_MODEL_EVALS", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)


def _settings() -> OpenRouterSettings:
    return OpenRouterSettings(api_key=SecretStr(SECRET))


def _scripted_factory(context: SagaContext, settings: OpenRouterSettings) -> AgentDriver:
    del context, settings
    return ScriptedProposalDriver()


def _json_artifacts(root: Path) -> tuple[Path, ...]:
    return tuple(root.rglob("*.json"))


def _artifact_text(root: Path) -> str:
    return "".join(path.read_text() for path in _json_artifacts(root))


def _sample_records(root: Path) -> tuple[Path, ...]:
    return tuple(sorted((root / "samples").glob("*.json")))


def _temporary_artifacts(root: Path) -> tuple[Path, ...]:
    return tuple(root.rglob("*.tmp"))


def _first_sample(root: Path) -> LiveSampleArtifact:
    return LiveSampleArtifact.model_validate_json(
        _sample_records(root)[0].read_bytes(), strict=True
    )


def _tamper_model(root: Path) -> None:
    path = _sample_records(root)[0]
    payload = json.loads(path.read_bytes())
    payload["identity"]["configured_model"] = "tampered/model"
    unsigned = {key: value for key, value in payload.items() if key != "artifact_sha256"}
    payload["artifact_sha256"] = sha256_json(unsigned)
    path.write_text(json.dumps(payload))


def _tamper_sample_metric(root: Path) -> None:
    path = _sample_records(root)[0]
    payload = json.loads(path.read_bytes())
    payload["sample"]["leakage_count"] = 1
    path.write_text(json.dumps(payload))


def _write_invalid_sample(root: Path) -> None:
    path = root / "samples/00-000.json"
    path.parent.mkdir(parents=True)
    path.write_text(SECRET)


def _runner(
    report: LiveEvalReportArtifact, calls: list[Path]
) -> Callable[[Path, int, Path], Coroutine[None, None, LiveEvalReportArtifact]]:
    async def run(path: Path, samples: int, output: Path) -> LiveEvalReportArtifact:
        del path, samples
        calls.append(output)
        return report

    return run


async def _unexpected_runner(path: Path, samples: int, output: Path) -> LiveEvalReportArtifact:
    del path, samples, output
    raise AssertionError("offline validation must not construct a live driver")


@pytest.mark.asyncio
async def test_live_runner_requires_opt_in_and_environment_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("RUN_LIVE_MODEL_EVALS", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(LiveEvalConfigurationError, match="RUN_LIVE_MODEL_EVALS=1"):
        await run_live_corpus(CORPUS, 1, tmp_path, settings=_settings())
    monkeypatch.setenv("RUN_LIVE_MODEL_EVALS", "1")
    with pytest.raises(LiveEvalConfigurationError, match="OPENROUTER_API_KEY"):
        await run_live_corpus(CORPUS, 1, tmp_path, settings=_settings())


@pytest.mark.asyncio
async def test_fake_driver_executes_and_persists_all_24_cases_without_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live(monkeypatch)

    report = await run_live_corpus(
        CORPUS, 1, tmp_path, settings=_settings(), driver_factory=_scripted_factory
    )

    records = _sample_records(tmp_path)
    payload = _artifact_text(tmp_path)
    assert report.score.model_sample_count == 24
    assert len(records) == 24
    assert SECRET not in payload
    assert not _temporary_artifacts(tmp_path)
    first = _first_sample(tmp_path)
    assert report.samples[first.sample_ref] == sha256(records[0].read_bytes()).hexdigest()
    assert first.returned_model is None
    assert first.returned_provider is None
    assert first.sample.input_tokens is None
    assert first.sample.output_tokens is None
    assert first.sample.cost_usd is None


@pytest.mark.asyncio
async def test_completed_samples_resume_without_invoking_driver(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live(monkeypatch)
    first = await run_live_corpus(
        CORPUS, 1, tmp_path, settings=_settings(), driver_factory=_scripted_factory
    )

    def fail_factory(context: SagaContext, settings: OpenRouterSettings) -> AgentDriver:
        del context, settings
        raise AssertionError("completed samples must not execute again")

    second = await run_live_corpus(
        CORPUS, 1, tmp_path, settings=_settings(), driver_factory=fail_factory
    )

    assert second == first


@pytest.mark.asyncio
async def test_tampered_completed_sample_is_rejected_before_driver_execution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live(monkeypatch)
    await run_live_corpus(
        CORPUS, 1, tmp_path, settings=_settings(), driver_factory=_scripted_factory
    )
    _tamper_model(tmp_path)

    with pytest.raises(LiveEvalConfigurationError, match="does not match"):
        await run_live_corpus(
            CORPUS, 1, tmp_path, settings=_settings(), driver_factory=_scripted_factory
        )


@pytest.mark.asyncio
async def test_tampered_sample_score_is_rejected_before_driver_execution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live(monkeypatch)
    await run_live_corpus(
        CORPUS, 1, tmp_path, settings=_settings(), driver_factory=_scripted_factory
    )
    _tamper_sample_metric(tmp_path)
    calls: list[int] = []
    with pytest.raises(LiveEvalConfigurationError, match="digest"):
        await _run_failure(tmp_path, AssertionError("must not run"), calls)
    assert calls == []


@pytest.mark.asyncio
async def test_invalid_artifact_never_echoes_secret_in_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live(monkeypatch)
    _write_invalid_sample(tmp_path)

    with pytest.raises(LiveEvalConfigurationError) as captured:
        await run_live_corpus(
            CORPUS, 1, tmp_path, settings=_settings(), driver_factory=_scripted_factory
        )

    assert SECRET not in str(captured.value)
    assert captured.value.__cause__ is None


class _FailingDriver:
    def __init__(self, error: Exception, calls: list[int]) -> None:
        self._error = error
        self._calls = calls

    async def next_action(
        self, observation: SagaObservation, available_tools: Sequence[ToolDescriptor]
    ) -> AgentProposal:
        del observation, available_tools
        self._calls.append(1)
        raise self._error


def _failing_factory(
    error: Exception, calls: list[int]
) -> Callable[[SagaContext, OpenRouterSettings], AgentDriver]:
    def factory(context: SagaContext, settings: OpenRouterSettings) -> AgentDriver:
        del context, settings
        return _FailingDriver(error, calls)

    return factory


async def _run_failure(root: Path, error: Exception, calls: list[int]) -> LiveEvalReportArtifact:
    return await run_live_corpus(
        CORPUS, 1, root, settings=_settings(), driver_factory=_failing_factory(error, calls)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (
            AgentPlanningError(AgentFailureCategory.TRANSPORT_EXHAUSTED),
            ProviderFailureReason.TRANSPORT_EXHAUSTED,
        ),
        (
            AgentPlanningError(AgentFailureCategory.RATE_LIMIT_EXHAUSTED),
            ProviderFailureReason.RATE_LIMIT_EXHAUSTED,
        ),
    ],
)
async def test_provider_transport_failure_is_separate_and_never_retried(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error: Exception,
    reason: ProviderFailureReason,
) -> None:
    _enable_live(monkeypatch)
    calls: list[int] = []
    report = await _run_failure(tmp_path, error, calls)
    sample = _first_sample(tmp_path)
    assert report.score.provider_failure_count == 24
    assert report.score.model_sample_count == 0
    assert sample.sample.status == "provider_failure"
    assert sample.sample.provider_failure_reason is reason
    assert len(calls) == 24


class _LeakyDriver:
    async def next_action(
        self, observation: SagaObservation, available_tools: Sequence[ToolDescriptor]
    ) -> AgentProposal:
        del available_tools
        return Escalate(
            proposal_id=f"proposal_{'1' * 20}",
            based_on_saga_seq=observation.saga_seq,
            reason_code="model_pause",
            rationale=f"Bearer {SECRET}",
        )


def _leaky_factory(context: SagaContext, settings: OpenRouterSettings) -> AgentDriver:
    del context, settings
    return _LeakyDriver()


@pytest.mark.asyncio
async def test_leaky_proposal_is_scored_but_never_persisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live(monkeypatch)

    report = await run_live_corpus(
        CORPUS, 1, tmp_path, settings=_settings(), driver_factory=_leaky_factory
    )

    assert report.score.leakage_count == 24
    assert SECRET not in _artifact_text(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ValueError(f"invalid {SECRET}"),
        RuntimeError("agent planning failed"),
        ConnectionError("untrusted transport label"),
    ],
)
async def test_invalid_model_result_is_not_a_provider_failure_or_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception
) -> None:
    _enable_live(monkeypatch)
    calls: list[int] = []
    report = await _run_failure(tmp_path, error, calls)
    assert report.score.provider_failure_count == 0
    assert report.score.model_sample_count == 24
    assert len(calls) == 24
    assert SECRET not in _artifact_text(tmp_path)
    assert SECRET not in str(report)


def test_artifact_contract_exposes_unknown_provider_telemetry_as_null(tmp_path: Path) -> None:
    sample_path = tmp_path / "sample.json"
    sample_path.write_text("{}")

    with pytest.raises(ValueError):
        LiveSampleArtifact.model_validate_json(sample_path.read_bytes(), strict=True)
    assert "returned_model" in LiveSampleArtifact.model_json_schema()["properties"]
    assert "denominators" in LiveEvalReportArtifact.model_json_schema()["properties"]


def test_report_json_is_machine_readable(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"not": "a report"}))

    with pytest.raises(ValueError):
        LiveEvalReportArtifact.model_validate_json(path.read_bytes(), strict=True)


def test_cli_defaults_to_offline_corpus_validation(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    output = tmp_path / "unused"
    code = eval_main(["--corpus", str(CORPUS), "--output", str(output)], runner=_unexpected_runner)

    assert code == 0
    assert "24 cases" in capsys.readouterr().out
    assert not output.exists()


def test_cli_live_mode_requires_both_environment_guards(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("RUN_LIVE_MODEL_EVALS", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    assert eval_main(["--live"], runner=_unexpected_runner) == 2
    assert "RUN_LIVE_MODEL_EVALS=1" in capsys.readouterr().err
    monkeypatch.setenv("RUN_LIVE_MODEL_EVALS", "1")
    assert eval_main(["--live"], runner=_unexpected_runner) == 2
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err


def test_cli_json_summary_and_resume_directory_are_stable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _enable_live(monkeypatch)
    calls: list[Path] = []
    arguments = ["--live", "--json", "--output", str(tmp_path)]

    assert eval_main(arguments, runner=_runner(_REPORT, calls)) == 0
    first = capsys.readouterr().out
    assert eval_main(arguments, runner=_runner(_REPORT, calls)) == 0
    second = capsys.readouterr().out

    assert json.loads(first)["denominators"]["structured_validity"] == 24
    assert first == second
    assert calls == [tmp_path, tmp_path]


def test_cli_threshold_failure_and_provider_failure_return_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _enable_live(monkeypatch)

    assert eval_main(["--live"], runner=_runner(_FAILED_REPORT, [])) == 1
    assert "recoverable_success" in capsys.readouterr().out
    assert eval_main(["--live"], runner=_runner(_PROVIDER_REPORT, [])) == 1
    output = capsys.readouterr().out
    assert "Provider failures: 24" in output
    assert "transport_exhausted=24" in output
    assert "Model-quality samples: 0" in output


def test_cli_never_echoes_credential_or_runner_exception(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _enable_live(monkeypatch)

    async def fail(path: Path, samples: int, output: Path) -> LiveEvalReportArtifact:
        del path, samples, output
        raise LiveEvalConfigurationError(f"provider rejected {SECRET}")

    assert eval_main(["--live"], runner=fail) == 2
    assert SECRET not in capsys.readouterr().err


def test_cli_help_is_clear_about_live_cost_and_consent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit, match="0"):
        eval_main(["--help"])

    help_text = capsys.readouterr().out
    assert "costs money" in help_text
    assert "RUN_LIVE_MODEL_EVALS=1" in help_text
    assert "OPENROUTER_API_KEY" in help_text


def test_live_cli_loads_local_dotenv_without_echoing_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "local-dotenv-openrouter-secret"  # noqa: S105 - deliberately fake sentinel
    (tmp_path / ".env").write_text(f"OPENROUTER_API_KEY={secret}\nRUN_LIVE_MODEL_EVALS=1\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("RUN_LIVE_MODEL_EVALS", raising=False)

    try:
        assert eval_main(["--live"], runner=_runner(_REPORT, [])) == 0
        output = capsys.readouterr().out
        assert secret not in output
        assert os.environ["OPENROUTER_API_KEY"] == secret
    finally:
        os.environ.pop("OPENROUTER_API_KEY", None)
        os.environ.pop("RUN_LIVE_MODEL_EVALS", None)


def test_offline_cli_does_not_load_local_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text(
        "OPENROUTER_API_KEY=unused-offline-secret\nRUN_LIVE_MODEL_EVALS=1\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("RUN_LIVE_MODEL_EVALS", raising=False)

    assert eval_main([]) == 0
    assert "OPENROUTER_API_KEY" not in os.environ
    assert "RUN_LIVE_MODEL_EVALS" not in os.environ


@pytest.mark.live_model
@pytest.mark.network
@pytest.mark.enable_socket
def test_explicit_openrouter_corpus_quality_gate(tmp_path: Path) -> None:
    if os.environ.get("RUN_LIVE_MODEL_EVALS") != "1" or not os.environ.get("OPENROUTER_API_KEY"):
        pytest.skip("requires RUN_LIVE_MODEL_EVALS=1 and OPENROUTER_API_KEY")

    assert eval_main(["--live", "--samples", "1", "--output", str(tmp_path)]) == 0
