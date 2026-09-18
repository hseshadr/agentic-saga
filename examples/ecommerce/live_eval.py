from __future__ import annotations

import os
import shutil
import subprocess
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from time import monotonic_ns
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, TypeAdapter, ValidationError

from agentic_saga.agents import (
    OpenRouterSettings,
    build_openrouter_driver,
    native_proposal_tool_names,
)
from agentic_saga.agents.deepagents import AgentFailureCategory, AgentPlanningError
from agentic_saga.contracts.actions import (
    AgentProposal,
    BeginCompensation,
    Escalate,
    Finish,
    ToolCall,
)
from agentic_saga.contracts.clock import FakeClock
from agentic_saga.contracts.common import JsonObject, canonical_json, sha256_json
from agentic_saga.contracts.runtime import AgentDriver, SagaObservation, ToolDescriptor
from agentic_saga.contracts.trace import RunTrace
from agentic_saga.manifest import SagaContext
from examples.ecommerce import demo
from examples.ecommerce import evaluation as evals
from examples.ecommerce.domain import StrictModel

type _Digest = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-f0-9]{64}$")]
type _SourceRevision = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-f0-9]{40}$")]
type _Name = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]
type DriverFactory = Callable[[SagaContext, OpenRouterSettings], AgentDriver]
_JSON: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
_NOW = datetime(2026, 9, 8, tzinfo=UTC)
_PACKAGES = ("agentic-saga", "pydantic-deep", "pydantic-ai-slim", "openai")
_MAX_SAMPLES = 10
_MAX_ARTIFACT_BYTES = 4_194_304
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_GIT_ENVIRONMENT_KEYS = ("HOME", "PATH", "SYSTEMROOT", "TMPDIR")
_GIT_TIMEOUT_SECONDS = 2
_SOURCE_REVISION_LENGTH = 40
_PROVIDER_FAILURES = {
    AgentFailureCategory.REQUEST_REJECTED: evals.ProviderFailureReason.REQUEST_REJECTED,
    AgentFailureCategory.RATE_LIMIT_EXHAUSTED: evals.ProviderFailureReason.RATE_LIMIT_EXHAUSTED,
    AgentFailureCategory.SERVER_ERROR_EXHAUSTED: evals.ProviderFailureReason.SERVER_ERROR_EXHAUSTED,
    AgentFailureCategory.TRANSPORT_EXHAUSTED: evals.ProviderFailureReason.TRANSPORT_EXHAUSTED,
}


class LiveEvalConfigurationError(RuntimeError):
    """Raised before a paid call or when durable evidence cannot be trusted."""


class RequestPolicyIdentity(StrictModel):
    """Versioned, non-secret identity for the live model request contract."""

    schema_version: Literal["openrouter-native-tools-v1"] = "openrouter-native-tools-v1"
    proposal_identity_contract: Literal["host-owned:saga_id+saga_seq"] = (
        "host-owned:saga_id+saga_seq"
    )
    model_proposal_contract: Literal["action-only:no-identity-or-freshness"] = (
        "action-only:no-identity-or-freshness"
    )
    provider_parameters_required: Literal[True] = True
    tool_calling: Literal["pydantic-ai:deferred-native-tool"] = "pydantic-ai:deferred-native-tool"
    business_tool_authority: Literal["eligible-proposals:kernel-executed"] = (
        "eligible-proposals:kernel-executed"
    )
    tool_allowlist_contract: Literal["recorded-exactly-per-turn"] = "recorded-exactly-per-turn"
    control_tool_authority: Literal["kernel-validated-proposals"] = "kernel-validated-proposals"
    model_calls_per_agent_turn: Literal[1] = 1
    multiple_call_policy: Literal["reject-before-execution"] = "reject-before-execution"
    routing_strategy: Literal["pinned-model:openrouter-provider-routing"] = (
        "pinned-model:openrouter-provider-routing"
    )
    model: _Name
    temperature: Literal[0] = 0
    reasoning_effort: Literal["low"] = "low"
    max_output_tokens: int = Field(default=512, strict=True, ge=1, le=4_096)
    timeout_ms: int = Field(default=30_000, strict=True, ge=100, le=60_000)
    sdk_retries: Literal[0] = 0


class EvalIdentity(StrictModel):
    corpus_sha256: _Digest
    prompt_sha256: _Digest
    manifest_sha256: _Digest
    tool_catalog_sha256: _Digest
    configured_provider: Literal["openrouter"] = "openrouter"
    configured_model: _Name
    suite: evals.EvalSuite
    request_policy: RequestPolicyIdentity
    dependency_versions: JsonObject
    source_revision: _SourceRevision | None = None
    source_dirty: bool | None = None


class NativeToolTurnEvidence(StrictModel):
    """One model turn's exact native proposal-tool surface and public selection."""

    saga_seq: int = Field(strict=True, ge=1)
    exposed_tool_allowlist: tuple[_Name, ...] = Field(min_length=1, max_length=100)
    selected_tool: _Name | None = None
    arguments_sha256: _Digest


class LiveSampleArtifact(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    artifact_sha256: _Digest
    identity: EvalIdentity
    returned_provider: _Name | None = None
    returned_model: _Name | None = None
    sample_ref: _Name
    trace_ref: _Name
    trace_sha256: _Digest
    native_tool_turns: tuple[NativeToolTurnEvidence, ...] = Field(max_length=100)
    sample: evals.EvalSample


class LiveEvalReportArtifact(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    identity: EvalIdentity
    denominators: JsonObject
    provider_failures: JsonObject
    samples: JsonObject
    score: evals.EvalReport


@dataclass(frozen=True)
class LiveEvalOptions:
    """Non-secret execution choices for one live evaluation run."""

    suite: evals.EvalSuite = evals.EvalSuite.RELEASE
    settings: OpenRouterSettings | None = None
    driver_factory: DriverFactory = build_openrouter_driver


@dataclass(frozen=True)
class _RunContext:
    corpus_sha256: str
    output_dir: Path
    settings: OpenRouterSettings
    suite: evals.EvalSuite
    versions: JsonObject
    driver_factory: DriverFactory
    source_revision: str | None
    source_dirty: bool | None


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
    native_tool_turns: list[NativeToolTurnEvidence] = field(default_factory=list)

    async def next_action(
        self, observation: SagaObservation, available_tools: Sequence[ToolDescriptor]
    ) -> AgentProposal:
        try:
            proposal = await self.delegate.next_action(observation, available_tools)
            candidate = _JSON.validate_python(proposal.model_dump(mode="json"))
            tool_turn = _tool_turn(observation, available_tools, proposal)
        except Exception as error:
            self.native_tool_turns.append(_tool_turn(observation, available_tools, None))
            self._record_failure(error)
            raise
        self.candidates.append(candidate)
        self.native_tool_turns.append(tool_turn)
        return proposal

    def _record_failure(self, error: Exception) -> None:
        reason = _provider_reason(error)
        self.provider_failure = None if reason is None else evals.ProviderExecutionError(reason)
        self.model_invalid = reason is None


def _tool_turn(
    observation: SagaObservation,
    available_tools: Sequence[ToolDescriptor],
    proposal: AgentProposal | None,
) -> NativeToolTurnEvidence:
    selected, arguments = _proposal_selection(proposal)
    allowlist = native_proposal_tool_names(observation, available_tools)
    return NativeToolTurnEvidence(
        saga_seq=observation.saga_seq,
        exposed_tool_allowlist=allowlist,
        selected_tool=selected if selected in allowlist else None,
        arguments_sha256=sha256_json(arguments),
    )


def _proposal_selection(proposal: AgentProposal | None) -> tuple[str | None, JsonObject]:
    if isinstance(proposal, ToolCall):
        return proposal.tool_name, proposal.arguments
    if isinstance(proposal, Finish):
        return "finish_saga", _JSON.validate_python({"target_status": proposal.target_status})
    if isinstance(proposal, BeginCompensation):
        return "begin_compensation", _JSON.validate_python({"reason_code": proposal.reason_code})
    if isinstance(proposal, Escalate):
        return "escalate_to_human", _JSON.validate_python({"reason_code": proposal.reason_code})
    return None, _JSON.validate_python({})


async def run_live_corpus(
    path: Path,
    samples_per_case: int,
    output_dir: Path,
    options: LiveEvalOptions | None = None,
) -> LiveEvalReportArtifact:
    """Run or safely resume the versioned corpus with explicit live consent."""
    selected_options = options or LiveEvalOptions()
    selected = _configuration(samples_per_case, selected_options.settings)
    context = _run_context(path, output_dir, selected_options, selected)
    cases = evals.select_cases(evals.load_corpus(path), selected_options.suite)
    artifacts = await _case_samples(cases, samples_per_case, context)
    return _persist_report(output_dir, artifacts)


def _run_context(
    path: Path,
    output_dir: Path,
    options: LiveEvalOptions,
    settings: OpenRouterSettings,
) -> _RunContext:
    digest = sha256(_read_bounded(path, "corpus")).hexdigest()
    source_revision, source_dirty = _source_state()
    return _RunContext(
        corpus_sha256=digest,
        output_dir=output_dir,
        settings=settings,
        suite=options.suite,
        versions=_versions(),
        driver_factory=options.driver_factory,
        source_revision=source_revision,
        source_dirty=source_dirty,
    )


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
    for case in cases:
        for sample_index in range(samples):
            results.append(await _load_or_run(case, sample_index, context))
    return tuple(results)


async def _load_or_run(
    case: evals.EvalCase, sample_index: int, context: _RunContext
) -> LiveSampleArtifact:
    key = f"{case.case_id}-{sample_index:03d}"
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
    artifact = _sample_artifact(pending, trace, sample, tuple(agent.native_tool_turns))
    _write_model(pending.sample_path, artifact)
    return artifact


def _sample_artifact(
    pending: _PendingSample,
    trace: dict[str, object],
    sample: evals.EvalSample,
    native_tool_turns: tuple[NativeToolTurnEvidence, ...],
) -> LiveSampleArtifact:
    values = {"identity": pending.identity, "sample_ref": f"samples/{pending.key}.json"}
    unsigned = LiveSampleArtifact.model_validate(
        values
        | trace
        | {
            "native_tool_turns": native_tool_turns,
            "sample": sample,
            "artifact_sha256": "0" * 64,
        }
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
    invalid = agent.model_invalid or _has_failed_agent_turn(evidence.trace)
    return sample.model_copy(update=_INVALID_MODEL) if invalid else sample


def _has_failed_agent_turn(trace: RunTrace) -> bool:
    return any(event.event_type == "agent_turn_failed" for event in trace.events)


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
        suite=context.suite,
        request_policy=_request_policy(context.settings),
        dependency_versions=context.versions,
        source_revision=context.source_revision,
        source_dirty=context.source_dirty,
    )


def _source_state() -> tuple[str | None, bool | None]:
    revision = _source_revision()
    if revision is None:
        return None, None
    status = _git_output(("status", "--porcelain=v1", "--untracked-files=normal"))
    return (revision, bool(status.strip())) if status is not None else (None, None)


def _source_revision() -> str | None:
    output = _git_output(("rev-parse", "--verify", "HEAD"))
    return _parse_revision(output) if output is not None else None


def _parse_revision(output: bytes) -> str | None:
    candidate = output.decode("ascii", errors="ignore").strip()
    valid_length = len(candidate) == _SOURCE_REVISION_LENGTH
    valid_characters = all(character in "0123456789abcdef" for character in candidate)
    return candidate if valid_length and valid_characters else None


def _git_output(arguments: tuple[str, ...]) -> bytes | None:
    executable = shutil.which("git")
    if executable is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603 - resolved executable; fixed internal arguments
            (executable, *arguments),
            cwd=_PROJECT_ROOT,
            env=_git_environment(),
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _git_environment() -> dict[str, str]:
    environment = {
        key: value for key in _GIT_ENVIRONMENT_KEYS if (value := os.environ.get(key)) is not None
    }
    return environment | {"LC_ALL": "C"}


def _request_policy(settings: OpenRouterSettings) -> RequestPolicyIdentity:
    return RequestPolicyIdentity(
        model=settings.primary_model,
        temperature=settings.temperature,
        reasoning_effort=settings.reasoning_effort,
        max_output_tokens=settings.max_output_tokens,
        timeout_ms=settings.timeout_ms,
        sdk_retries=settings.sdk_retries,
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
    values = _proof_denominators(model) | _quality_denominators(model)
    return _JSON.validate_python(values)


def _proof_denominators(samples: tuple[evals.EvalSample, ...]) -> dict[str, int]:
    return {
        "happy_path": _proof_count(samples, evals.ReleaseProof.HAPPY_PATH),
        "compensation": _proof_count(samples, evals.ReleaseProof.COMPENSATION),
        "unknown_reconciliation": _proof_count(samples, evals.ReleaseProof.UNKNOWN_RECONCILIATION),
        "human_escalation": _proof_count(samples, evals.ReleaseProof.HUMAN_ESCALATION),
    }


def _quality_denominators(samples: tuple[evals.EvalSample, ...]) -> dict[str, int]:
    category = Counter(item.category for item in samples)
    count = len(samples)
    return {
        "structured_validity": count,
        "straightforward_success": category[evals.EvalCategory.STRAIGHTFORWARD],
        "recoverable_success": category[evals.EvalCategory.RECOVERABLE],
        "critical_escalation": sum(item.escalation_required for item in samples),
        "adversarial_safety": category[evals.EvalCategory.ADVERSARIAL],
        "forbidden_effects": count,
        "leakage": count,
        "budget_compliance": count,
    }


def _proof_count(samples: tuple[evals.EvalSample, ...], proof: evals.ReleaseProof) -> int:
    return sum(item.release_proof is proof for item in samples)


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
    temporary = _write_temporary(path, payload)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_temporary(path: Path, payload: bytes) -> Path:
    with NamedTemporaryFile(
        mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    ) as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
        return Path(stream.name)
