from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import monotonic_ns
from typing import Annotated, Literal

from pydantic import StringConstraints

from agentic_saga.agents import OpenRouterSettings, build_openrouter_driver
from agentic_saga.agents.deepagents import AgentFailureCategory, AgentPlanningError
from agentic_saga.contracts.common import canonical_json
from agentic_saga.contracts.runtime import AgentDriver, ExecutionBudget, ToolDescriptor
from agentic_saga.manifest import SagaContext, SagaManifest
from examples.ecommerce.domain import StrictModel
from examples.ecommerce.evaluation import (
    DecisionCheckpoint,
    EvalCase,
    EvalMetadata,
    EvalReport,
    EvalSample,
    EvalSuite,
    ProviderExecutionError,
    ProviderFailureReason,
    aggregate,
    decision_checkpoint,
    invalid_model_sample,
    load_corpus,
    provider_failure_sample,
    score_proposal,
    select_cases,
)

type _Digest = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-f0-9]{64}$")]
type _Name = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]
type DriverFactory = Callable[[SagaContext, OpenRouterSettings], AgentDriver]
_MAX_SAMPLES = 10
_MILLISECONDS_PER_NANOSECOND = 1_000_000
_PROVIDER_FAILURES = {
    AgentFailureCategory.REQUEST_REJECTED: ProviderFailureReason.REQUEST_REJECTED,
    AgentFailureCategory.RATE_LIMIT_EXHAUSTED: ProviderFailureReason.RATE_LIMIT_EXHAUSTED,
    AgentFailureCategory.SERVER_ERROR_EXHAUSTED: ProviderFailureReason.SERVER_ERROR_EXHAUSTED,
    AgentFailureCategory.TRANSPORT_EXHAUSTED: ProviderFailureReason.TRANSPORT_EXHAUSTED,
}


class LiveEvalConfigurationError(RuntimeError):
    """Safe live-evaluation configuration failure."""


class RequestPolicyIdentity(StrictModel):
    schema_version: Literal["temporal-native-tools-v1"] = "temporal-native-tools-v1"
    provider: Literal["openrouter"] = "openrouter"
    model: _Name
    temperature: Literal[0] = 0
    reasoning_effort: Literal["low"] = "low"
    sdk_retries: Literal[0] = 0
    workflow_authority: Literal["temporal-owned-recovery"] = "temporal-owned-recovery"
    model_authority: Literal["eligible-business-or-verified-finish"] = (
        "eligible-business-or-verified-finish"
    )


class RunIdentity(StrictModel):
    corpus_sha256: _Digest
    provider: Literal["openrouter"] = "openrouter"
    configured_model: _Name
    suite: EvalSuite
    request_policy: RequestPolicyIdentity


class LiveEvalReportArtifact(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    identity: RunIdentity
    samples: tuple[EvalSample, ...]
    score: EvalReport


@dataclass(frozen=True)
class LiveEvalOptions:
    suite: EvalSuite = EvalSuite.SMOKE
    settings: OpenRouterSettings | None = None
    driver_factory: DriverFactory = build_openrouter_driver


@dataclass(frozen=True)
class _SampleInput:
    driver: AgentDriver
    checkpoint: DecisionCheckpoint
    index: int
    settings: OpenRouterSettings


async def run_live_corpus(
    corpus: Path, samples_per_case: int, output_dir: Path, options: LiveEvalOptions | None = None
) -> LiveEvalReportArtifact:
    """Run bounded model decisions; transaction correctness remains a separate proof."""
    selected = options or LiveEvalOptions()
    _require_sample_count(samples_per_case)
    cases = select_cases(load_corpus(corpus), selected.suite)
    checkpoints = tuple(_required_checkpoint(case) for case in cases)
    settings = selected.settings or _settings_from_environment()
    driver = selected.driver_factory(_context(checkpoints), settings)
    samples = await _run_samples(driver, checkpoints, samples_per_case, settings)
    artifact = _artifact(corpus, selected.suite, settings, samples)
    _write_report(output_dir, artifact)
    return artifact


def _require_sample_count(value: int) -> None:
    if type(value) is not int or not 1 <= value <= _MAX_SAMPLES:
        raise LiveEvalConfigurationError("samples per case must be between 1 and 10")


def _settings_from_environment() -> OpenRouterSettings:
    try:
        return OpenRouterSettings.from_environment()
    except ValueError:
        raise LiveEvalConfigurationError("OpenRouter configuration is unavailable") from None


def _required_checkpoint(case: EvalCase) -> DecisionCheckpoint:
    checkpoint = decision_checkpoint(case)
    if checkpoint is None:
        raise LiveEvalConfigurationError("selected case has no model decision")
    return checkpoint


async def _run_samples(
    driver: AgentDriver,
    checkpoints: tuple[DecisionCheckpoint, ...],
    count: int,
    settings: OpenRouterSettings,
) -> tuple[EvalSample, ...]:
    samples: list[EvalSample] = []
    for checkpoint in checkpoints:
        for index in range(count):
            sample = _SampleInput(driver, checkpoint, index, settings)
            samples.append(await _run_sample(sample))
    return tuple(samples)


async def _run_sample(sample: _SampleInput) -> EvalSample:
    started = monotonic_ns()
    try:
        proposal = await sample.driver.next_action(
            sample.checkpoint.observation, sample.checkpoint.available_tools
        )
    except AgentPlanningError as error:
        metadata = _metadata(sample.index, sample.settings, started)
        return _planning_failure(sample.checkpoint, metadata, error)
    except Exception:
        raise LiveEvalConfigurationError("live model evaluation failed safely") from None
    metadata = _metadata(sample.index, sample.settings, started)
    return score_proposal(sample.checkpoint, proposal, metadata=metadata)


def _planning_failure(
    checkpoint: DecisionCheckpoint,
    metadata: EvalMetadata,
    error: AgentPlanningError,
) -> EvalSample:
    reason = _PROVIDER_FAILURES.get(error.category)
    if reason is None:
        return invalid_model_sample(checkpoint, metadata)
    return provider_failure_sample(checkpoint, metadata, ProviderExecutionError(reason))


def _metadata(index: int, settings: OpenRouterSettings, started: int) -> EvalMetadata:
    latency = (monotonic_ns() - started) // _MILLISECONDS_PER_NANOSECOND
    return EvalMetadata(
        sample_index=index,
        model=settings.primary_model,
        provider="openrouter",
        latency_ms=latency,
    )


def _context(checkpoints: tuple[DecisionCheckpoint, ...]) -> SagaContext:
    descriptors = _unique_descriptors(checkpoints)
    manifest = _manifest(tuple(item.name for item in descriptors))
    payload = {
        "manifest": manifest.model_dump(mode="json"),
        "tools": [item.model_dump(mode="json") for item in descriptors],
    }
    return SagaContext(
        manifest=manifest,
        agent_context=canonical_json(payload).decode(),
        tool_descriptors=descriptors,
        budget=_budget(),
    )


def _unique_descriptors(
    checkpoints: tuple[DecisionCheckpoint, ...],
) -> tuple[ToolDescriptor, ...]:
    by_name = {
        descriptor.name: descriptor
        for checkpoint in checkpoints
        for descriptor in checkpoint.available_tools
    }
    return tuple(by_name[name] for name in sorted(by_name))


def _manifest(names: tuple[str, ...]) -> SagaManifest:
    return SagaManifest.model_validate(_manifest_values(names), strict=True)


def _manifest_values(names: tuple[str, ...]) -> dict[str, object]:
    return _manifest_identity() | {
        "objective": "Choose the next eligible checkout capability from public evidence.",
        "instructions": [
            "The deterministic Temporal workflow owns execution and every recovery path."
        ],
        "success_criteria": ["Fresh proof permits succeeded_verified."],
        "autonomy": _autonomy_values(),
        "budgets": _budget().model_dump(mode="json"),
        "tools": {"catalog_sha256": "0" * 64, "allowed": names},
        "checks": _check_values(),
        "escalation": _escalation_values(),
    }


def _manifest_identity() -> dict[str, object]:
    return {"schema_version": "1.0", "name": "temporal_ecommerce_eval", "version": "1.0"}


def _autonomy_values() -> dict[str, object]:
    return {"mode": "guarded", "instructions": ["Choose only an advertised capability."]}


def _check_values() -> dict[str, object]:
    return {
        "policy": [],
        "success": ["verified_order"],
        "compensation": ["workflow_owned_compensation"],
        "clean_abort": ["workflow_owned_abort"],
    }


def _escalation_values() -> dict[str, object]:
    return {
        "conditions": ["The workflow cannot resolve public evidence."],
        "instructions": ["Use verified human authorization."],
    }


def _budget() -> ExecutionBudget:
    return ExecutionBudget(
        turn_limit=8,
        tool_call_limit=8,
        elapsed_ms_limit=240_000,
        token_limit=8_000,
    )


def _artifact(
    corpus: Path,
    suite: EvalSuite,
    settings: OpenRouterSettings,
    samples: tuple[EvalSample, ...],
) -> LiveEvalReportArtifact:
    policy = RequestPolicyIdentity(model=settings.primary_model)
    identity = RunIdentity(
        corpus_sha256=sha256(corpus.read_bytes()).hexdigest(),
        configured_model=settings.primary_model,
        suite=suite,
        request_policy=policy,
    )
    return LiveEvalReportArtifact(identity=identity, samples=samples, score=aggregate(samples))


def _write_report(output_dir: Path, artifact: LiveEvalReportArtifact) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = canonical_json(artifact.model_dump(mode="json")) + b"\n"
    with NamedTemporaryFile(dir=output_dir, prefix=".report-", delete=False) as stream:
        stream.write(payload)
        temporary = Path(stream.name)
    temporary.replace(output_dir / "report.json")


__all__ = [
    "LiveEvalConfigurationError",
    "LiveEvalOptions",
    "LiveEvalReportArtifact",
    "RequestPolicyIdentity",
    "RunIdentity",
    "run_live_corpus",
]
