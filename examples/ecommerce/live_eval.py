from __future__ import annotations

import os
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic_ns
from typing import Annotated, Literal

from pydantic import StringConstraints, TypeAdapter, ValidationError

from agentic_saga.agents import OpenRouterSettings, build_openrouter_driver
from agentic_saga.agents.deepagents import AgentFailureCategory, AgentPlanningError
from agentic_saga.contracts.actions import AgentProposal
from agentic_saga.contracts.clock import FakeClock
from agentic_saga.contracts.common import JsonObject, canonical_json, sha256_json
from agentic_saga.contracts.runtime import AgentDriver, SagaObservation, ToolDescriptor
from agentic_saga.manifest import SagaContext
from examples.ecommerce import demo
from examples.ecommerce import evaluation as evals
from examples.ecommerce.domain import StrictModel

type _Digest = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-f0-9]{64}$")]
type _Name = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]
type DriverFactory = Callable[[SagaContext, OpenRouterSettings], AgentDriver]
_JSON: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
_NOW = datetime(2026, 9, 8, tzinfo=UTC)
_PACKAGES = ("agentic-saga", "deepagents", "langchain-openrouter")
_MAX_SAMPLES = 10
_MAX_ARTIFACT_BYTES = 4_194_304
_PROVIDER_FAILURES = {
    AgentFailureCategory.RATE_LIMIT_EXHAUSTED: evals.ProviderFailureReason.RATE_LIMIT_EXHAUSTED,
    AgentFailureCategory.SERVER_ERROR_EXHAUSTED: evals.ProviderFailureReason.SERVER_ERROR_EXHAUSTED,
    AgentFailureCategory.TRANSPORT_EXHAUSTED: evals.ProviderFailureReason.TRANSPORT_EXHAUSTED,
}


class LiveEvalConfigurationError(RuntimeError):
    """Raised before a paid call or when durable evidence cannot be trusted."""


class EvalIdentity(StrictModel):
    corpus_sha256: _Digest
    prompt_sha256: _Digest
    manifest_sha256: _Digest
    tool_catalog_sha256: _Digest
    configured_provider: Literal["openrouter"] = "openrouter"
    configured_model: _Name
    configured_fallback_model: _Name
    dependency_versions: JsonObject


class LiveSampleArtifact(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    artifact_sha256: _Digest
    identity: EvalIdentity
    returned_provider: _Name | None = None
    returned_model: _Name | None = None
    sample_ref: _Name
    trace_ref: _Name
    trace_sha256: _Digest
    sample: evals.EvalSample


class LiveEvalReportArtifact(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    identity: EvalIdentity
    denominators: JsonObject
    provider_failures: JsonObject
    samples: JsonObject
    score: evals.EvalReport


@dataclass(frozen=True)
class _RunContext:
    corpus_sha256: str
    output_dir: Path
    settings: OpenRouterSettings
    versions: JsonObject
    driver_factory: DriverFactory


@dataclass(frozen=True)
class _PendingSample:
    case: evals.EvalCase
    sample_index: int
    key: str
    sample_path: Path
    assembly: demo._Assembly
    identity: EvalIdentity


@dataclass
class _ObservedDriver:
    delegate: AgentDriver
    provider_failure: evals.ProviderExecutionError | None = None
    model_invalid: bool = False
    candidates: list[JsonObject] = field(default_factory=list)

    async def next_action(
        self, observation: SagaObservation, available_tools: Sequence[ToolDescriptor]
    ) -> AgentProposal:
        try:
            proposal = await self.delegate.next_action(observation, available_tools)
        except Exception as error:
            self._record_failure(error)
            raise
        self.candidates.append(_JSON.validate_python(proposal.model_dump(mode="json")))
        return proposal

    def _record_failure(self, error: Exception) -> None:
        reason = _provider_reason(error)
        self.provider_failure = None if reason is None else evals.ProviderExecutionError(reason)
        self.model_invalid = reason is None


async def run_live_corpus(
    path: Path,
    samples_per_case: int,
    output_dir: Path,
    *,
    settings: OpenRouterSettings | None = None,
    driver_factory: DriverFactory = build_openrouter_driver,
) -> LiveEvalReportArtifact:
    """Run or safely resume the versioned corpus with explicit live consent."""
    selected = _configuration(samples_per_case, settings)
    digest = sha256(_read_bounded(path, "corpus")).hexdigest()
    context = _RunContext(digest, output_dir, selected, _versions(), driver_factory)
    artifacts = await _case_samples(evals.load_corpus(path), samples_per_case, context)
    return _persist_report(output_dir, artifacts)


def _persist_report(
    output_dir: Path, artifacts: tuple[LiveSampleArtifact, ...]
) -> LiveEvalReportArtifact:
    report = _report(artifacts)
    _write_model(output_dir / "report.json", report)
    return report


def _configuration(
    samples_per_case: int, settings: OpenRouterSettings | None
) -> OpenRouterSettings:
    if os.environ.get("RUN_LIVE_MODEL_EVALS") != "1":
        raise LiveEvalConfigurationError("set RUN_LIVE_MODEL_EVALS=1 to authorize live evaluation")
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise LiveEvalConfigurationError("OPENROUTER_API_KEY is required for live evaluation")
    if not 1 <= samples_per_case <= _MAX_SAMPLES:
        raise LiveEvalConfigurationError("samples must be between 1 and 10")
    return settings or OpenRouterSettings.from_environment()


async def _case_samples(
    cases: tuple[evals.EvalCase, ...], samples: int, context: _RunContext
) -> tuple[LiveSampleArtifact, ...]:
    results: list[LiveSampleArtifact] = []
    for case_number, case in enumerate(cases):
        for sample_index in range(samples):
            results.append(await _load_or_run(case_number, case, sample_index, context))
    return tuple(results)


async def _load_or_run(
    case_number: int, case: evals.EvalCase, sample_index: int, context: _RunContext
) -> LiveSampleArtifact:
    key = f"{case_number:02d}-{sample_index:03d}"
    sample_path = context.output_dir / "samples" / f"{key}.json"
    with TemporaryDirectory(prefix="agentic-saga-live-eval-") as directory:
        assembly = demo.prepare_eval_case(case, Path(directory), FakeClock(_NOW))
        identity = _identity(assembly.context, context)
        pending = _PendingSample(case, sample_index, key, sample_path, assembly, identity)
        if sample_path.exists():
            return _load_completed(pending, context.output_dir)
        return await _execute(pending, context)


async def _execute(pending: _PendingSample, context: _RunContext) -> LiveSampleArtifact:
    agent = _ObservedDriver(context.driver_factory(pending.assembly.context, context.settings))
    started = monotonic_ns()
    evidence = await demo.run_with_agent(pending.assembly, pending.case, agent)
    latency_ms = max(0, (monotonic_ns() - started) // 1_000_000)
    sample = _score(pending, evidence, agent, latency_ms, context.settings)
    trace = _write_trace(context.output_dir, pending.key, evidence)
    artifact = _sample_artifact(pending, trace, sample)
    _write_model(pending.sample_path, artifact)
    return artifact


def _sample_artifact(
    pending: _PendingSample, trace: dict[str, object], sample: evals.EvalSample
) -> LiveSampleArtifact:
    values = {"identity": pending.identity, "sample_ref": f"samples/{pending.key}.json"}
    unsigned = LiveSampleArtifact.model_validate(
        values | trace | {"sample": sample, "artifact_sha256": "0" * 64}
    )
    payload = unsigned.model_dump(mode="json", exclude={"artifact_sha256"})
    return unsigned.model_copy(update={"artifact_sha256": sha256_json(payload)})


def _score(
    pending: _PendingSample,
    evidence: demo.EvalEvidence,
    agent: _ObservedDriver,
    latency_ms: int,
    settings: OpenRouterSettings,
) -> evals.EvalSample:
    metadata = _metadata(pending, agent, latency_ms, settings)
    if agent.provider_failure is not None:
        return evals.provider_failure_sample(pending.case, metadata, agent.provider_failure)
    sample = evals.score_sample(pending.case, evidence.result, evidence.trace, metadata=metadata)
    return sample.model_copy(update=_INVALID_MODEL) if agent.model_invalid else sample


def _metadata(
    pending: _PendingSample,
    agent: _ObservedDriver,
    latency_ms: int,
    settings: OpenRouterSettings,
) -> evals.EvalMetadata:
    return evals.EvalMetadata(
        sample_index=pending.sample_index,
        model=settings.primary_model,
        provider="openrouter",
        latency_ms=latency_ms,
        redaction_candidates=tuple(agent.candidates),
    )


_INVALID_MODEL = {"structured_valid": False, "allowed_outcome": False}


def _identity(saga: SagaContext, context: _RunContext) -> EvalIdentity:
    return EvalIdentity(
        corpus_sha256=context.corpus_sha256,
        prompt_sha256=sha256(saga.agent_context.encode()).hexdigest(),
        manifest_sha256=sha256_json(saga.manifest.model_dump(mode="json")),
        tool_catalog_sha256=saga.manifest.tools.catalog_sha256,
        configured_model=context.settings.primary_model,
        configured_fallback_model=context.settings.fallback_model,
        dependency_versions=context.versions,
    )


def _write_trace(output: Path, key: str, evidence: demo.EvalEvidence) -> dict[str, object]:
    trace_ref = f"traces/{key}.json"
    path = output / trace_ref
    payload = _model_bytes(evidence.trace.model_dump(mode="json"))
    _atomic_write(path, payload)
    return {"trace_ref": trace_ref, "trace_sha256": sha256(payload).hexdigest()}


def _load_completed(pending: _PendingSample, output_dir: Path) -> LiveSampleArtifact:
    try:
        payload = _read_bounded(pending.sample_path, "sample")
        artifact = LiveSampleArtifact.model_validate_json(payload, strict=True)
    except ValidationError:
        raise LiveEvalConfigurationError("completed sample is invalid") from None
    _verify_artifact_digest(artifact)
    _verify_completed(artifact, pending)
    _verify_trace(artifact, output_dir)
    return artifact


def _verify_artifact_digest(artifact: LiveSampleArtifact) -> None:
    values = artifact.model_dump(mode="json", exclude={"artifact_sha256"})
    if sha256_json(values) != artifact.artifact_sha256:
        raise LiveEvalConfigurationError("completed sample artifact digest does not match")


def _verify_completed(artifact: LiveSampleArtifact, pending: _PendingSample) -> None:
    matches = all(
        (
            artifact.identity == pending.identity,
            artifact.sample.case_id == pending.case.case_id,
            artifact.sample.sample_index == pending.sample_index,
            artifact.sample_ref == f"samples/{pending.key}.json",
            artifact.trace_ref == f"traces/{pending.key}.json",
        )
    )
    if not matches:
        raise LiveEvalConfigurationError("completed sample does not match this evaluation run")


def _verify_trace(artifact: LiveSampleArtifact, output_dir: Path) -> None:
    payload = _read_bounded(output_dir / artifact.trace_ref, "sample trace")
    if sha256(payload).hexdigest() != artifact.trace_sha256:
        raise LiveEvalConfigurationError("completed sample trace digest does not match")


def _report(artifacts: tuple[LiveSampleArtifact, ...]) -> LiveEvalReportArtifact:
    samples = tuple(item.sample for item in artifacts)
    values = {
        "identity": artifacts[0].identity,
        "denominators": _denominators(samples),
        "provider_failures": _failure_counts(samples),
        "samples": _references(artifacts),
        "score": evals.aggregate(samples),
    }
    return LiveEvalReportArtifact.model_validate(values)


def _denominators(samples: tuple[evals.EvalSample, ...]) -> JsonObject:
    model = tuple(item for item in samples if item.status == "model_result")
    category = Counter(item.category for item in model)
    count = len(model)
    values = {
        "structured_validity": count,
        "recoverable_success": category[evals.EvalCategory.RECOVERABLE],
        "critical_escalation": category[evals.EvalCategory.ESCALATION],
        "kernel_rejection": category[evals.EvalCategory.ADVERSARIAL],
        "forbidden_effects": count,
        "leakage": count,
        "budget_compliance": count,
    }
    return _JSON.validate_python(values)


def _failure_counts(samples: tuple[evals.EvalSample, ...]) -> JsonObject:
    failures = Counter(
        item.provider_failure_reason.value
        for item in samples
        if item.provider_failure_reason is not None
    )
    values = {reason.value: failures[reason.value] for reason in evals.ProviderFailureReason}
    return _JSON.validate_python(values)


def _references(artifacts: tuple[LiveSampleArtifact, ...]) -> JsonObject:
    values = {
        item.sample_ref: sha256(_model_bytes(item.model_dump(mode="json"))).hexdigest()
        for item in artifacts
    }
    return _JSON.validate_python(values)


def _provider_reason(error: Exception) -> evals.ProviderFailureReason | None:
    if not isinstance(error, AgentPlanningError):
        return None
    return _PROVIDER_FAILURES.get(error.category)


def _versions() -> JsonObject:
    values = {name: _package_version(name) for name in _PACKAGES}
    return _JSON.validate_python(values)


def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unavailable"


def _read_bounded(path: Path, role: str) -> bytes:
    try:
        with path.open("rb") as stream:
            payload = stream.read(_MAX_ARTIFACT_BYTES + 1)
    except OSError:
        raise LiveEvalConfigurationError(f"{role} is unavailable") from None
    if len(payload) > _MAX_ARTIFACT_BYTES:
        raise LiveEvalConfigurationError(f"{role} exceeds the size limit")
    return payload


def _write_model(path: Path, model: StrictModel) -> None:
    _atomic_write(path, _model_bytes(model.model_dump(mode="json")))


def _model_bytes(value: object) -> bytes:
    return canonical_json(value) + b"\n"


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
