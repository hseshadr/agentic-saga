from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Coroutine, Sequence
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import cast

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
from examples.ecommerce import live_eval as live_eval_module
from examples.ecommerce.demo import ScriptedProposalDriver
from examples.ecommerce.eval import main as eval_main
from examples.ecommerce.evaluation import EvalReport, EvalSuite, ProviderFailureReason
from examples.ecommerce.live_eval import (
    EvalIdentity,
    LiveEvalConfigurationError,
    LiveEvalOptions,
    LiveEvalReportArtifact,
    LiveSampleArtifact,
    NativeToolTurnEvidence,
    RequestPolicyIdentity,
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
    suite=EvalSuite.RELEASE,
    request_policy=RequestPolicyIdentity(
        model="openai/gpt-oss-20b",
        per_call_max_output_tokens=256,
        per_call_timeout_ms=15_000,
    ),
    dependency_versions=_JSON.validate_python({"agentic-saga": "0.1.0"}),
)
_SCORE = EvalReport(
    model_sample_count=4,
    provider_failure_count=0,
    structured_validity=Decimal(1),
    straightforward_success=Decimal(1),
    recoverable_success=Decimal(1),
    critical_escalation_recall=Decimal(1),
    adversarial_safety=None,
    happy_path_success=Decimal(1),
    compensation_success=Decimal(1),
    unknown_reconciliation_success=Decimal(1),
    human_escalation_success=Decimal(1),
    kernel_rejection_rate=None,
    turn_budget_compliance=Decimal(1),
    forbidden_effect_count=0,
    leakage_count=0,
    total_latency_ms=4,
    total_input_tokens=None,
    total_output_tokens=None,
    total_cost_usd=None,
    failed_thresholds=(),
    thresholds_met=True,
)
_DENOMINATORS = _JSON.validate_python(
    {
        "structured_validity": 4,
        "happy_path": 1,
        "compensation": 1,
        "unknown_reconciliation": 1,
        "human_escalation": 1,
        "straightforward_success": 1,
        "recoverable_success": 2,
        "critical_escalation": 1,
        "adversarial_safety": 0,
        "forbidden_effects": 4,
        "leakage": 4,
        "budget_compliance": 4,
    }
)
_REPORT = LiveEvalReportArtifact(
    identity=live_eval_module.RunIdentity.model_validate(
        _IDENTITY.model_dump(exclude={"prompt_sha256", "manifest_sha256"})
    ),
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
        "provider_failures": _JSON.validate_python({"transport_exhausted": 4}),
        "score": _SCORE.model_copy(
            update={
                "model_sample_count": 0,
                "provider_failure_count": 4,
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


def _options(
    *,
    suite: EvalSuite = EvalSuite.RELEASE,
    driver_factory: Callable[[SagaContext, OpenRouterSettings], AgentDriver] = _scripted_factory,
) -> LiveEvalOptions:
    return LiveEvalOptions(suite=suite, settings=_settings(), driver_factory=driver_factory)


def _json_artifacts(root: Path) -> tuple[Path, ...]:
    return tuple(root.rglob("*.json"))


def _artifact_text(root: Path) -> str:
    return "".join(path.read_text() for path in _json_artifacts(root))


def _sample_records(root: Path) -> tuple[Path, ...]:
    return tuple(sorted((root / "samples").glob("*.json")))


def _sample_artifacts(root: Path) -> tuple[LiveSampleArtifact, ...]:
    return tuple(
        LiveSampleArtifact.model_validate_json(path.read_bytes(), strict=True)
        for path in _sample_records(root)
    )


def _temporary_artifacts(root: Path) -> tuple[Path, ...]:
    return tuple(root.rglob("*.tmp"))


def test_atomic_artifact_write_never_follows_predictable_temp_symlink(tmp_path: Path) -> None:
    destination = tmp_path / "report.json"
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"private-local-content")
    destination.with_name(".report.json.tmp").symlink_to(victim)

    live_eval_module._atomic_write(destination, b"safe-artifact\n")

    assert destination.read_bytes() == b"safe-artifact\n"
    assert victim.read_bytes() == b"private-local-content"


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


def _tamper_identity_without_digest(root: Path) -> None:
    path = _sample_records(root)[0]
    payload = json.loads(path.read_bytes())
    payload["identity"]["prompt_sha256"] = "9" * 64
    path.write_text(json.dumps(payload))


def _resign(artifact: LiveSampleArtifact, **updates: object) -> LiveSampleArtifact:
    changed = artifact.model_copy(update=updates)
    values = changed.model_dump(mode="json", exclude={"artifact_sha256"})
    return changed.model_copy(update={"artifact_sha256": sha256_json(values)})


def _tamper_request_policy(root: Path) -> None:
    path = _sample_records(root)[0]
    payload = json.loads(path.read_bytes())
    payload["identity"]["request_policy"]["max_output_tokens"] += 1
    unsigned = {key: value for key, value in payload.items() if key != "artifact_sha256"}
    payload["artifact_sha256"] = sha256_json(unsigned)
    path.write_text(json.dumps(payload))


def _tamper_sample_metric(root: Path) -> None:
    path = _sample_records(root)[0]
    payload = json.loads(path.read_bytes())
    payload["sample"]["leakage_count"] = 1
    path.write_text(json.dumps(payload))


def _write_invalid_sample(root: Path) -> None:
    path = root / "samples/s01-basic-order-000.json"
    path.parent.mkdir(parents=True)
    path.write_text(SECRET)


def _runner(
    report: LiveEvalReportArtifact, calls: list[Path]
) -> Callable[[Path, int, Path, EvalSuite], Coroutine[None, None, LiveEvalReportArtifact]]:
    async def run(
        path: Path, samples: int, output: Path, suite: EvalSuite
    ) -> LiveEvalReportArtifact:
        del path, samples, suite
        calls.append(output)
        return report

    return run


async def _unexpected_runner(
    path: Path, samples: int, output: Path, suite: EvalSuite
) -> LiveEvalReportArtifact:
    del path, samples, output, suite
    raise AssertionError("offline validation must not construct a live driver")


@pytest.mark.asyncio
async def test_live_runner_requires_opt_in_and_environment_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("RUN_LIVE_MODEL_EVALS", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(LiveEvalConfigurationError, match="RUN_LIVE_MODEL_EVALS=1"):
        await run_live_corpus(CORPUS, 1, tmp_path, LiveEvalOptions(settings=_settings()))
    monkeypatch.setenv("RUN_LIVE_MODEL_EVALS", "1")
    with pytest.raises(LiveEvalConfigurationError, match="OPENROUTER_API_KEY"):
        await run_live_corpus(CORPUS, 1, tmp_path, LiveEvalOptions(settings=_settings()))


@pytest.mark.asyncio
async def test_fake_driver_executes_four_canonical_release_proofs_without_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live(monkeypatch)

    report = await run_live_corpus(CORPUS, 1, tmp_path, _options())

    records = _sample_records(tmp_path)
    payload = _artifact_text(tmp_path)
    assert report.identity.suite is EvalSuite.RELEASE
    assert report.score.model_sample_count == 4
    assert len(records) == 4
    artifacts = _sample_artifacts(tmp_path)
    assert len({item.identity.prompt_sha256 for item in artifacts}) == 4
    assert len({item.identity.manifest_sha256 for item in artifacts}) == 4
    assert report.samples == _JSON.validate_python(
        {
            item.sample_ref: sha256(path.read_bytes()).hexdigest()
            for item, path in zip(artifacts, records, strict=True)
        }
    )
    assert SECRET not in payload
    assert not _temporary_artifacts(tmp_path)
    first = _first_sample(tmp_path)
    assert isinstance(first.native_tool_turns[0], NativeToolTurnEvidence)
    assert report.samples[first.sample_ref] == sha256(records[0].read_bytes()).hexdigest()
    assert first.returned_model is None
    assert first.returned_provider is None
    assert first.sample.input_tokens is None
    assert first.sample.output_tokens is None
    assert first.sample.cost_usd is None
    assert first.native_tool_turns
    assert all(
        turn.selected_tool in turn.exposed_tool_allowlist
        for turn in first.native_tool_turns
        if turn.selected_tool is not None
    )
    assert all(len(turn.arguments_sha256) == 64 for turn in first.native_tool_turns)
    assert first.native_tool_turns[0].exposed_tool_allowlist == (
        "charge_payment",
        "check_inventory",
        "inspect_order",
        "reserve_inventory",
        "schedule_fulfillment",
        "finish_saga",
        "escalate_to_human",
    )


@pytest.mark.asyncio
async def test_source_identity_is_computed_once_and_persisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live(monkeypatch)
    revision = "1" * 40
    calls: list[int] = []

    def source_state() -> tuple[str, bool]:
        calls.append(1)
        return revision, True

    monkeypatch.setattr(live_eval_module, "_source_state", source_state)

    report = await run_live_corpus(CORPUS, 1, tmp_path, _options())

    assert calls == [1]
    assert report.identity.source_revision == revision
    assert report.identity.source_dirty is True
    identities = tuple(
        LiveSampleArtifact.model_validate_json(path.read_bytes(), strict=True).identity
        for path in _sample_records(tmp_path)
    )
    assert all(identity.source_revision == revision for identity in identities)
    assert all(identity.source_dirty is True for identity in identities)


def test_source_state_uses_fixed_sanitized_bounded_git_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = "2" * 40
    invocations: list[tuple[tuple[str, ...], dict[str, object]]] = []
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "another-secret")

    def run(command: Sequence[str], **options: object) -> subprocess.CompletedProcess[bytes]:
        invocations.append((tuple(command), options))
        output = f"{revision}\n".encode() if "rev-parse" in command else b" M tracked\n?? new\n"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr=b"")

    monkeypatch.setattr(subprocess, "run", run)

    assert live_eval_module._source_state() == (revision, True)
    assert [item[0][1:] for item in invocations] == [
        ("rev-parse", "--verify", "HEAD"),
        ("status", "--porcelain=v1", "--untracked-files=normal"),
    ]
    assert all(Path(item[0][0]).name == "git" for item in invocations)
    assert all(item[1]["timeout"] == 2 for item in invocations)
    assert all(item[1]["capture_output"] is True for item in invocations)
    assert all(
        "OPENROUTER_API_KEY" not in cast(dict[str, str], item[1]["env"]) for item in invocations
    )
    assert all(
        "ANTHROPIC_API_KEY" not in cast(dict[str, str], item[1]["env"]) for item in invocations
    )


def test_source_state_is_null_without_a_valid_git_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(command: Sequence[str], **options: object) -> subprocess.CompletedProcess[bytes]:
        del options
        return subprocess.CompletedProcess(command, 128, stdout=b"", stderr=SECRET.encode())

    monkeypatch.setattr(subprocess, "run", run)

    assert live_eval_module._source_state() == (None, None)


@pytest.mark.asyncio
async def test_extended_suite_retains_all_24_research_cases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live(monkeypatch)

    report = await run_live_corpus(
        CORPUS,
        1,
        tmp_path,
        _options(suite=EvalSuite.EXTENDED),
    )

    assert report.identity.suite is EvalSuite.EXTENDED
    assert report.score.model_sample_count == 24
    assert len(_sample_records(tmp_path)) == 24


@pytest.mark.asyncio
async def test_completed_samples_resume_without_invoking_driver(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live(monkeypatch)
    first = await run_live_corpus(CORPUS, 1, tmp_path, _options())

    def fail_factory(context: SagaContext, settings: OpenRouterSettings) -> AgentDriver:
        del context, settings
        raise AssertionError("completed samples must not execute again")

    second = await run_live_corpus(CORPUS, 1, tmp_path, _options(driver_factory=fail_factory))

    assert second == first


@pytest.mark.asyncio
async def test_report_rejects_mismatched_shared_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live(monkeypatch)
    await run_live_corpus(CORPUS, 1, tmp_path, _options())
    artifacts = _sample_artifacts(tmp_path)
    changed = _resign(
        artifacts[1],
        identity=artifacts[1].identity.model_copy(update={"configured_model": "tampered/model"}),
    )

    with pytest.raises(LiveEvalConfigurationError, match="shared run identity"):
        live_eval_module._report((artifacts[0], changed, *artifacts[2:]))


@pytest.mark.asyncio
async def test_report_rejects_mismatched_identity_within_one_case(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live(monkeypatch)
    await run_live_corpus(CORPUS, 1, tmp_path, _options())
    first = _sample_artifacts(tmp_path)[0]
    changed = _resign(
        first,
        identity=first.identity.model_copy(update={"prompt_sha256": "9" * 64}),
        sample_ref="samples/duplicate-case-001.json",
    )

    with pytest.raises(LiveEvalConfigurationError, match="case identity"):
        live_eval_module._report((first, changed))


@pytest.mark.asyncio
async def test_report_rejects_duplicate_sample_reference(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live(monkeypatch)
    await run_live_corpus(CORPUS, 1, tmp_path, _options())
    first = _sample_artifacts(tmp_path)[0]

    with pytest.raises(LiveEvalConfigurationError, match="sample reference"):
        live_eval_module._report((first, first))


def test_report_rejects_empty_artifact_set() -> None:
    with pytest.raises(LiveEvalConfigurationError, match="nonempty"):
        live_eval_module._report(())


@pytest.mark.asyncio
async def test_tampered_sample_identity_breaks_digest_before_resume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live(monkeypatch)
    await run_live_corpus(CORPUS, 1, tmp_path, _options())
    _tamper_identity_without_digest(tmp_path)

    with pytest.raises(LiveEvalConfigurationError, match="digest"):
        await run_live_corpus(CORPUS, 1, tmp_path, _options())


@pytest.mark.asyncio
async def test_tampered_completed_sample_is_rejected_before_driver_execution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live(monkeypatch)
    await run_live_corpus(CORPUS, 1, tmp_path, _options())
    _tamper_model(tmp_path)

    with pytest.raises(LiveEvalConfigurationError, match="does not match"):
        await run_live_corpus(CORPUS, 1, tmp_path, _options())


@pytest.mark.asyncio
async def test_changed_request_policy_invalidates_resumable_sample(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live(monkeypatch)
    await run_live_corpus(CORPUS, 1, tmp_path, _options())
    _tamper_request_policy(tmp_path)

    with pytest.raises(LiveEvalConfigurationError, match="does not match"):
        await run_live_corpus(CORPUS, 1, tmp_path, _options())


@pytest.mark.asyncio
async def test_request_policy_identity_is_explicit_complete_and_secret_free(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live(monkeypatch)

    await run_live_corpus(CORPUS, 1, tmp_path, _options())

    policy = _first_sample(tmp_path).identity.request_policy
    assert policy.schema_version == "openrouter-native-tools-v2"
    assert policy.proposal_identity_contract == "host-owned:saga_id+saga_seq"
    assert policy.model_proposal_contract == "action-only:no-identity-or-freshness"
    assert policy.provider_parameters_required is True
    assert policy.tool_calling == "pydantic-ai:deferred-native-tool"
    assert policy.business_tool_authority == "eligible-proposals:kernel-executed"
    assert policy.tool_allowlist_contract == "recorded-exactly-per-turn"
    assert policy.control_tool_authority == "kernel-validated-proposals"
    assert policy.adapter_retries == 1
    assert policy.model_calls_per_agent_turn == 2
    assert policy.multiple_call_policy == "reject-before-execution"
    assert policy.routing_strategy == "pinned-model:openrouter-provider-routing"
    assert policy.model == _settings().primary_model
    assert policy.timeout_ms == 30_000
    assert policy.per_call_max_output_tokens == 500
    assert policy.per_call_timeout_ms == 15_000
    assert SECRET not in policy.model_dump_json()


def test_legacy_json_schema_request_policy_identity_is_rejected() -> None:
    payload = _IDENTITY.model_dump(mode="json")
    policy = payload["request_policy"]
    assert isinstance(policy, dict)
    policy["schema_version"] = "openrouter-proposal-v2"
    policy.pop("proposal_identity_contract")
    policy.pop("model_proposal_contract")

    with pytest.raises(ValueError):
        EvalIdentity.model_validate(payload, strict=True)


@pytest.mark.asyncio
async def test_tampered_sample_score_is_rejected_before_driver_execution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live(monkeypatch)
    await run_live_corpus(CORPUS, 1, tmp_path, _options())
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
        await run_live_corpus(CORPUS, 1, tmp_path, _options())

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
        CORPUS, 1, root, _options(driver_factory=_failing_factory(error, calls))
    )


def _request_rejection_with_untrusted_detail() -> AgentPlanningError:
    error = AgentPlanningError(AgentFailureCategory.REQUEST_REJECTED)
    error.add_note(f"untrusted provider detail: {SECRET}")
    return error


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
        (
            AgentPlanningError(AgentFailureCategory.SERVER_ERROR_EXHAUSTED),
            ProviderFailureReason.SERVER_ERROR_EXHAUSTED,
        ),
        (
            _request_rejection_with_untrusted_detail(),
            ProviderFailureReason.REQUEST_REJECTED,
        ),
    ],
)
async def test_provider_failure_is_separate_and_never_retried(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error: Exception,
    reason: ProviderFailureReason,
) -> None:
    _enable_live(monkeypatch)
    calls: list[int] = []
    report = await _run_failure(tmp_path, error, calls)
    sample = _first_sample(tmp_path)
    assert report.score.provider_failure_count == 4
    assert report.score.model_sample_count == 0
    assert sample.sample.status == "provider_failure"
    assert sample.sample.provider_failure_reason is reason
    assert report.provider_failures[reason.value] == 4
    assert all(value == 0 for value in report.denominators.values())
    assert SECRET not in _artifact_text(tmp_path)
    assert len(calls) == 4


class _MalformedDriver:
    async def next_action(
        self, observation: SagaObservation, available_tools: Sequence[ToolDescriptor]
    ) -> AgentProposal:
        del observation, available_tools
        return cast(AgentProposal, {"authorization": f"Bearer {SECRET}"})


def _malformed_factory(context: SagaContext, settings: OpenRouterSettings) -> AgentDriver:
    del context, settings
    return _MalformedDriver()


@pytest.mark.asyncio
async def test_failed_agent_turn_is_structurally_invalid_even_when_observer_cannot_classify_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live(monkeypatch)

    report = await run_live_corpus(CORPUS, 1, tmp_path, _options(driver_factory=_malformed_factory))

    sample = _first_sample(tmp_path).sample
    assert report.score.model_sample_count == 4
    assert report.score.provider_failure_count == 0
    assert report.score.structured_validity == Decimal(0)
    assert sample.status == "model_result"
    assert sample.structured_valid is False
    assert sample.allowed_outcome is False
    assert SECRET not in _artifact_text(tmp_path)


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

    report = await run_live_corpus(CORPUS, 1, tmp_path, _options(driver_factory=_leaky_factory))

    assert report.score.leakage_count == 4
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
    assert report.score.model_sample_count == 4
    assert len(calls) == 4
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
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
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

    assert json.loads(first)["denominators"]["structured_validity"] == 4
    assert json.loads(first)["denominators"]["straightforward_success"] == 1
    assert first == second
    assert calls == [tmp_path, tmp_path]


def test_cli_extended_suite_is_explicit_and_forwarded_to_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_live(monkeypatch)
    selected: list[EvalSuite] = []

    async def capture(
        path: Path, samples: int, output: Path, suite: EvalSuite
    ) -> LiveEvalReportArtifact:
        del path, samples, output
        selected.append(suite)
        return _REPORT.model_copy(
            update={"identity": _IDENTITY.model_copy(update={"suite": suite})}
        )

    assert eval_main(["--live", "--suite", "extended"], runner=capture) == 0
    assert selected == [EvalSuite.EXTENDED]


def test_cli_threshold_failure_and_provider_failure_return_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _enable_live(monkeypatch)

    assert eval_main(["--live"], runner=_runner(_FAILED_REPORT, [])) == 1
    output = capsys.readouterr().out
    assert "straightforward_success" in output
    assert "recoverable_success" in output
    assert eval_main(["--live"], runner=_runner(_PROVIDER_REPORT, [])) == 1
    output = capsys.readouterr().out
    assert "Provider failures: 4" in output
    assert "transport_exhausted=4" in output
    assert "Model-quality samples: 0" in output


def test_cli_never_echoes_credential_or_runner_exception(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _enable_live(monkeypatch)

    async def fail(
        path: Path, samples: int, output: Path, suite: EvalSuite
    ) -> LiveEvalReportArtifact:
        del path, samples, output, suite
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
