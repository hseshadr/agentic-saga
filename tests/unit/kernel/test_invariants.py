from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from agentic_saga.contracts.actions import Finish
from agentic_saga.contracts.common import Direction, JsonObject, sha256_json
from agentic_saga.contracts.events import (
    CompensationIntentRecorded,
    CompensationStarted,
    DispatchStarted,
    EffectIntentRecorded,
    EffectOutcomeRecorded,
    HumanRequired,
    HumanResolutionRecorded,
    InvariantEvaluated,
    LedgerEvent,
    SagaCreated,
    SagaStarted,
)
from agentic_saga.contracts.outcomes import EffectConfirmed, safe_outcome_json
from agentic_saga.contracts.runtime import SagaStatus, TerminalRequirement
from agentic_saga.kernel.invariants import (
    AcceptedResidual,
    AuthoritativeInvariantInput,
    InvariantEvidence,
    InvariantResult,
    InvariantRule,
    TerminalGate,
    TerminalStateDenied,
    VerifiedHumanResolution,
    build_invariant_event_fields,
    invariant_evidence_digest,
)
from agentic_saga.kernel.reducer import rebuild_projection, reduce_event
from agentic_saga.kernel.state import (
    CompensationObligation,
    ObligationStatus,
    OperationRecord,
    OperationStatus,
    SagaSnapshot,
)

SAGA_ID = "saga_0123456789abcdef"
OTHER_SAGA_ID = "saga_fedcba9876543210"
OPERATION_ID = f"op_{'a' * 64}"
COMPENSATION_OPERATION_ID = f"op_{'d' * 64}"
COMMAND_HASH = "b" * 64


def requirement(rule_id: str) -> TerminalRequirement:
    return TerminalRequirement(invariant_version="rules-v1", required_rule_ids=(rule_id,))


def gate() -> TerminalGate:
    return TerminalGate(
        {
            SagaStatus.SUCCEEDED_VERIFIED: requirement("success"),
            SagaStatus.COMPENSATED_VERIFIED: requirement("compensated"),
            SagaStatus.ABORTED_CLEAN: requirement("clean_abort"),
            SagaStatus.RESOLVED_WITH_EXCEPTION: requirement("human_exception"),
        }
    )


def test_should_require_configuration_for_every_terminal_status() -> None:
    # Given / When / Then
    with pytest.raises(ValueError, match="every terminal status"):
        TerminalGate({SagaStatus.SUCCEEDED_VERIFIED: requirement("success")})


def test_should_reject_duplicate_required_rule_configuration() -> None:
    # Given / When / Then
    with pytest.raises(ValidationError, match="unique"):
        TerminalRequirement(
            invariant_version="rules-v1",
            required_rule_ids=("success", "success"),
        )


def operation(status: OperationStatus) -> OperationRecord:
    return OperationRecord(
        operation_id=OPERATION_ID,
        step_instance_id="step_aaaaaaaa",
        direction=Direction.FORWARD,
        semantic_generation=0,
        delivery_attempt=1,
        tool_name="generic_effect",
        status=status,
        redacted_command={},
        command_hash=COMMAND_HASH,
    )


def obligation(status: ObligationStatus) -> CompensationObligation:
    return CompensationObligation(
        forward_operation_id=OPERATION_ID,
        compensation_tool_name="generic_repair",
        status=status,
    )


def snapshot(
    *,
    seq: int = 9,
    status: SagaStatus = SagaStatus.RUNNING,
    operation_status: OperationStatus | None = None,
    obligation_status: ObligationStatus | None = None,
    proof: InvariantEvidence | None = None,
) -> SagaSnapshot:
    operations = {} if operation_status is None else {OPERATION_ID: operation(operation_status)}
    obligations = {} if obligation_status is None else {OPERATION_ID: obligation(obligation_status)}
    bound_proof = proof or evidence(SagaStatus.SUCCEEDED_VERIFIED, seq=seq - 1)
    return SagaSnapshot(
        saga_id=SAGA_ID,
        seq=seq,
        status=status,
        definition_version="generic-v1",
        operations=operations,
        obligations=obligations,
        last_invariant_seq=seq,
        last_invariant_passed=True,
        last_invariant_target=bound_proof.target_status,
        last_invariant_version=bound_proof.invariant_version,
        last_invariant_evidence_digest=invariant_evidence_digest(bound_proof),
        pending_approval=False,
    )


def reducer_metadata(seq: int) -> dict[str, object]:
    return {
        "event_id": f"evt_{seq:016d}",
        "saga_id": SAGA_ID,
        "saga_seq": seq,
        "definition_version": "generic-v1",
        "fence_token": 1,
        "actor": "kernel",
        "trace_id": "trace_0123456789abcdef",
        "recorded_at": datetime(2026, 9, 6, 12, 0, seq, tzinfo=UTC),
    }


def effect_metadata(operation_id: str, direction: Direction, tool_name: str) -> dict[str, object]:
    return {
        "operation_id": operation_id,
        "step_instance_id": "step_aaaaaaaa",
        "direction": direction,
        "semantic_generation": 0,
        "delivery_attempt": 1,
        "tool_name": tool_name,
        "redacted_command": {},
        "command_hash": COMMAND_HASH,
    }


def _event[EventT: BaseModel](model: type[EventT], seq: int, payload: dict[str, object]) -> EventT:
    return model.model_validate(reducer_metadata(seq) | payload)


def _confirmed_event(
    seq: int, operation_id: str, direction: Direction, tool_name: str
) -> EffectOutcomeRecorded:
    effect = effect_metadata(operation_id, direction, tool_name)
    outcome = EffectConfirmed(receipt={"provider_id": operation_id})
    safe_result = safe_outcome_json(outcome)
    result = {
        "outcome": outcome,
        "redacted_result": safe_result,
        "result_hash": sha256_json(safe_result),
    }
    return _event(EffectOutcomeRecorded, seq, effect | result)


def forward_events(compensate_with: str | None) -> tuple[LedgerEvent, ...]:
    effect = effect_metadata(OPERATION_ID, Direction.FORWARD, "generic_effect")
    return (
        _event(
            SagaCreated,
            1,
            {"definition_name": "generic", "definition_fingerprint": "f" * 64, "redacted_goal": {}},
        ),
        _event(SagaStarted, 2, {}),
        _event(EffectIntentRecorded, 3, effect | {"compensate_with": compensate_with}),
        _event(DispatchStarted, 4, effect),
        _confirmed_event(5, OPERATION_ID, Direction.FORWARD, "generic_effect"),
    )


def compensation_events() -> tuple[LedgerEvent, ...]:
    compensation = effect_metadata(
        COMPENSATION_OPERATION_ID, Direction.COMPENSATION, "generic_repair"
    )
    return (
        _event(CompensationStarted, 6, {}),
        _event(
            CompensationIntentRecorded,
            7,
            compensation
            | {
                "compensates_operation_id": OPERATION_ID,
                "forward_receipts": ({"provider_id": OPERATION_ID},),
            },
        ),
        _event(DispatchStarted, 8, compensation),
        _confirmed_event(9, COMPENSATION_OPERATION_ID, Direction.COMPENSATION, "generic_repair"),
    )


def reducer_backed_snapshot(*, repaired: bool, compensate_with: str | None) -> SagaSnapshot:
    events = forward_events(compensate_with)
    if repaired:
        events += compensation_events()
    return _human_resolved_snapshot(rebuild_projection(events))


def _human_resolved_snapshot(current: SagaSnapshot) -> SagaSnapshot:
    required = _event(HumanRequired, current.seq + 1, {"reason_code": "human_exception"})
    paused = reduce_event(current, required)
    payload = {
        "decision_id": "decision_1",
        "proposal_hash": "c" * 64,
        "verification_result": True,
    }
    resolution = _event(HumanResolutionRecorded, paused.seq + 1, payload)
    return reduce_event(paused, resolution)


def residual_proof(
    current: SagaSnapshot, operation_id: str
) -> tuple[SagaSnapshot, InvariantEvidence]:
    resolution = human_resolution(
        accepted_residuals=(accepted_residual(operation_id=operation_id),),
        resolved_at_seq=current.seq,
    )
    proof = evidence(
        SagaStatus.RESOLVED_WITH_EXCEPTION,
        seq=current.seq,
        resolution=resolution,
    )
    return reduce_event(current, _proof_event(proof)), proof


def _proof_event(proof: InvariantEvidence) -> InvariantEvaluated:
    fields = build_invariant_event_fields(proof)
    return _event(InvariantEvaluated, proof.evaluated_at_seq + 1, fields.model_dump())


def accepted_residual(**changes: object) -> AcceptedResidual:
    values: dict[str, object] = {
        "operation_id": OPERATION_ID,
        "reason": "The named provider effect is accepted.",
        "evidence": {"provider_state": "confirmed"},
    }
    return AcceptedResidual.model_validate(values | changes)


def human_resolution(**changes: object) -> VerifiedHumanResolution:
    values: dict[str, object] = {
        "decision_id": "decision_1",
        "actor": "authorized-operator",
        "proposal_hash": "c" * 64,
        "reason": "Named residual effects were accepted.",
        "accepted_residuals": (accepted_residual(),),
        "saga_id": SAGA_ID,
        "resolved_at_seq": 8,
        "verification_result": True,
    }
    return VerifiedHumanResolution.model_validate(values | changes)


def evidence(
    target: SagaStatus,
    *,
    seq: int = 8,
    version: str = "rules-v1",
    results: tuple[InvariantResult, ...] | None = None,
    resolution: VerifiedHumanResolution | None = None,
) -> InvariantEvidence:
    required_id = {
        SagaStatus.SUCCEEDED_VERIFIED: "success",
        SagaStatus.COMPENSATED_VERIFIED: "compensated",
        SagaStatus.ABORTED_CLEAN: "clean_abort",
        SagaStatus.RESOLVED_WITH_EXCEPTION: "human_exception",
    }[target]
    actual = results or (
        InvariantResult(
            rule_id=required_id,
            passed=True,
            inputs={"authoritative": True},
            explanation="Authoritative evidence was evaluated.",
        ),
    )
    return InvariantEvidence(
        saga_id=SAGA_ID,
        definition_version="generic-v1",
        evaluated_at_seq=seq,
        target_status=target,
        invariant_version=version,
        results=actual,
        human_resolution=resolution,
    )


@pytest.mark.parametrize(
    ("source", "target", "obligation_status", "requires_human"),
    [
        (SagaStatus.RUNNING, SagaStatus.SUCCEEDED_VERIFIED, ObligationStatus.ELIGIBLE, False),
        (SagaStatus.RUNNING, SagaStatus.ABORTED_CLEAN, ObligationStatus.ARMED, False),
        (
            SagaStatus.COMPENSATING,
            SagaStatus.COMPENSATED_VERIFIED,
            ObligationStatus.SATISFIED,
            False,
        ),
        (
            SagaStatus.HUMAN_REQUIRED,
            SagaStatus.RESOLVED_WITH_EXCEPTION,
            ObligationStatus.ELIGIBLE,
            True,
        ),
    ],
)
def test_should_authorize_each_terminal_status_with_target_specific_proof(
    source: SagaStatus,
    target: SagaStatus,
    obligation_status: ObligationStatus,
    requires_human: bool,
) -> None:
    # Given
    resolution = human_resolution() if requires_human else None
    proof = evidence(target, resolution=resolution)
    current = snapshot(
        status=source,
        operation_status=(OperationStatus.EFFECT_CONFIRMED if requires_human else None),
        obligation_status=obligation_status,
        proof=proof,
    )

    # When
    authorized = gate().evaluate(target, current, proof, runnable_commands=0)

    # Then
    assert authorized is target
    assert current.status is source


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (SagaStatus.CREATED, SagaStatus.SUCCEEDED_VERIFIED),
        (SagaStatus.RUNNING, SagaStatus.COMPENSATED_VERIFIED),
        (SagaStatus.RECOVERY_PLAN_REQUIRED, SagaStatus.ABORTED_CLEAN),
        (SagaStatus.RETRY_WAIT, SagaStatus.SUCCEEDED_VERIFIED),
        (SagaStatus.RECONCILING_UNKNOWN, SagaStatus.RESOLVED_WITH_EXCEPTION),
        (SagaStatus.COMPENSATING, SagaStatus.SUCCEEDED_VERIFIED),
        (SagaStatus.HUMAN_REQUIRED, SagaStatus.ABORTED_CLEAN),
        (SagaStatus.SUCCEEDED_VERIFIED, SagaStatus.SUCCEEDED_VERIFIED),
    ],
)
def test_should_deny_target_incompatible_with_current_snapshot(
    source: SagaStatus, target: SagaStatus
) -> None:
    # Given / When / Then
    with pytest.raises(TerminalStateDenied, match="compatible"):
        gate().evaluate(target, snapshot(status=source), evidence(target), 0)


@pytest.mark.parametrize(
    ("operation_status", "message"),
    [
        (OperationStatus.OUTCOME_UNKNOWN, "unknown operation blocks terminal state"),
        (OperationStatus.PLANNED, "in-flight operation blocks terminal state"),
        (OperationStatus.INTENT_DURABLE, "in-flight operation blocks terminal state"),
        (OperationStatus.DISPATCHED, "in-flight operation blocks terminal state"),
    ],
)
def test_should_deny_terminal_state_with_unknown_or_inflight_operation(
    operation_status: OperationStatus, message: str
) -> None:
    # Given
    current = snapshot(operation_status=operation_status)

    # When / Then
    with pytest.raises(TerminalStateDenied) as caught:
        gate().evaluate(
            SagaStatus.SUCCEEDED_VERIFIED,
            current,
            evidence(SagaStatus.SUCCEEDED_VERIFIED),
            0,
        )
    assert str(caught.value) == message


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (SagaStatus.RUNNING, SagaStatus.SUCCEEDED_VERIFIED),
        (SagaStatus.RUNNING, SagaStatus.ABORTED_CLEAN),
        (SagaStatus.COMPENSATING, SagaStatus.COMPENSATED_VERIFIED),
        (SagaStatus.HUMAN_REQUIRED, SagaStatus.RESOLVED_WITH_EXCEPTION),
    ],
)
def test_should_block_every_terminal_target_when_an_operation_is_unknown(
    source: SagaStatus, target: SagaStatus
) -> None:
    # Given
    current = snapshot(status=source, operation_status=OperationStatus.OUTCOME_UNKNOWN)

    # When / Then
    with pytest.raises(TerminalStateDenied, match="unknown operation"):
        gate().evaluate(target, current, evidence(target), 0)


def test_should_deny_terminal_state_with_runnable_command() -> None:
    # Given / When / Then
    with pytest.raises(TerminalStateDenied, match="runnable command"):
        gate().evaluate(
            SagaStatus.SUCCEEDED_VERIFIED,
            snapshot(),
            evidence(SagaStatus.SUCCEEDED_VERIFIED),
            runnable_commands=1,
        )


def test_should_deny_terminal_state_with_pending_approval() -> None:
    # Given / When / Then
    current = snapshot(status=SagaStatus.HUMAN_REQUIRED).model_copy(
        update={"pending_approval": True}
    )
    with pytest.raises(TerminalStateDenied, match="pending approval"):
        gate().evaluate(
            SagaStatus.RESOLVED_WITH_EXCEPTION,
            current,
            evidence(SagaStatus.RESOLVED_WITH_EXCEPTION, resolution=human_resolution()),
            0,
        )


@pytest.mark.parametrize(
    ("evaluated_at_seq", "message"),
    [(7, "stale invariant evidence"), (9, "future invariant evidence")],
)
def test_should_deny_stale_or_future_invariant_sequence(
    evaluated_at_seq: int, message: str
) -> None:
    # Given / When / Then
    with pytest.raises(TerminalStateDenied) as caught:
        gate().evaluate(
            SagaStatus.SUCCEEDED_VERIFIED,
            snapshot(),
            evidence(SagaStatus.SUCCEEDED_VERIFIED, seq=evaluated_at_seq),
            0,
        )
    assert str(caught.value) == message


def test_should_deny_evidence_for_different_terminal_target() -> None:
    # Given / When / Then
    with pytest.raises(TerminalStateDenied) as caught:
        gate().evaluate(
            SagaStatus.SUCCEEDED_VERIFIED,
            snapshot(),
            evidence(SagaStatus.ABORTED_CLEAN),
            0,
        )
    assert str(caught.value) == "invariant target does not match requested terminal state"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"saga_id": OTHER_SAGA_ID}, "invariant evidence belongs to a different Saga"),
        ({"definition_version": "generic-v2"}, "invariant evidence uses a different definition"),
    ],
)
def test_should_deny_cross_boundary_evidence_reuse(
    changes: dict[str, object], message: str
) -> None:
    # Given
    proof = evidence(SagaStatus.SUCCEEDED_VERIFIED).model_copy(update=changes)

    # When / Then
    with pytest.raises(TerminalStateDenied) as caught:
        gate().evaluate(SagaStatus.SUCCEEDED_VERIFIED, snapshot(), proof, 0)
    assert str(caught.value) == message


def test_should_deny_wrong_invariant_version() -> None:
    # Given / When / Then
    with pytest.raises(TerminalStateDenied, match="version"):
        gate().evaluate(
            SagaStatus.SUCCEEDED_VERIFIED,
            snapshot(),
            evidence(SagaStatus.SUCCEEDED_VERIFIED, version="rules-v2"),
            0,
        )


def test_should_require_post_proof_snapshot() -> None:
    # Given
    pre_proof = snapshot(seq=8).model_copy(
        update={
            "last_invariant_seq": None,
            "last_invariant_passed": None,
            "last_invariant_target": None,
            "last_invariant_version": None,
            "last_invariant_evidence_digest": None,
        }
    )

    # When / Then
    with pytest.raises(TerminalStateDenied, match="post-proof"):
        gate().evaluate(
            SagaStatus.SUCCEEDED_VERIFIED,
            pre_proof,
            evidence(SagaStatus.SUCCEEDED_VERIFIED),
            0,
        )


def test_should_accept_task7_post_proof_reducer_flow() -> None:
    # Given
    before = snapshot(seq=8).model_copy(
        update={
            "last_invariant_seq": None,
            "last_invariant_passed": None,
            "last_invariant_target": None,
            "last_invariant_version": None,
            "last_invariant_evidence_digest": None,
        }
    )
    proof = evidence(SagaStatus.SUCCEEDED_VERIFIED)
    fields = build_invariant_event_fields(proof)
    event = InvariantEvaluated(
        event_id="evt_0123456789abcdef",
        saga_id=SAGA_ID,
        saga_seq=9,
        definition_version="generic-v1",
        fence_token=1,
        actor="kernel",
        trace_id="trace_0123456789abcdef",
        recorded_at=datetime(2026, 9, 6, 12, tzinfo=UTC),
        **fields.model_dump(),
    )

    # When
    post_proof = reduce_event(before, event)
    target = gate().evaluate(SagaStatus.SUCCEEDED_VERIFIED, post_proof, proof, 0)

    # Then
    assert post_proof.seq == 9
    assert post_proof.last_invariant_seq == 9
    assert post_proof.last_invariant_target is SagaStatus.SUCCEEDED_VERIFIED
    assert post_proof.last_invariant_evidence_digest == invariant_evidence_digest(proof)
    assert target is SagaStatus.SUCCEEDED_VERIFIED


def test_should_build_deterministic_complete_invariant_event_fields() -> None:
    # Given
    proof = evidence(SagaStatus.SUCCEEDED_VERIFIED)

    # When
    first = build_invariant_event_fields(proof)
    second = build_invariant_event_fields(proof)

    # Then
    assert first == second
    assert first.evidence_digest == invariant_evidence_digest(proof)
    assert first.results == {"success": True}
    assert first.all_passed is True


def test_should_bind_invariant_digest_to_rule_result_order() -> None:
    # Given
    first = evidence(
        SagaStatus.SUCCEEDED_VERIFIED,
        results=(result("first"), result("second")),
    )
    reversed_results = first.model_copy(update={"results": tuple(reversed(first.results))})

    # When / Then
    assert invariant_evidence_digest(first) != invariant_evidence_digest(reversed_results)


def post_proof_snapshot(proof: InvariantEvidence, source: SagaStatus) -> SagaSnapshot:
    before = snapshot(seq=8, status=source, proof=proof).model_copy(
        update={
            "last_invariant_seq": None,
            "last_invariant_passed": None,
            "last_invariant_target": None,
            "last_invariant_version": None,
            "last_invariant_evidence_digest": None,
        }
    )
    fields = build_invariant_event_fields(proof)
    event = InvariantEvaluated(
        event_id="evt_0123456789abcdef",
        saga_id=SAGA_ID,
        saga_seq=9,
        definition_version="generic-v1",
        fence_token=1,
        actor="kernel",
        trace_id="trace_0123456789abcdef",
        recorded_at=datetime(2026, 9, 6, 12, tzinfo=UTC),
        **fields.model_dump(),
    )
    return reduce_event(before, event)


def test_should_reject_terminal_target_substitution_after_proof_event() -> None:
    # Given
    original = evidence(SagaStatus.SUCCEEDED_VERIFIED)
    post_proof = post_proof_snapshot(original, SagaStatus.RUNNING)
    substitute = evidence(SagaStatus.ABORTED_CLEAN)

    # When / Then
    with pytest.raises(TerminalStateDenied, match="proof binding"):
        gate().evaluate(SagaStatus.ABORTED_CLEAN, post_proof, substitute, 0)


def test_should_reject_rule_input_tampering_after_proof_event() -> None:
    # Given
    original = evidence(SagaStatus.SUCCEEDED_VERIFIED)
    post_proof = post_proof_snapshot(original, SagaStatus.RUNNING)
    changed = result("success").model_copy(update={"inputs": {"authoritative": False}})
    tampered = original.model_copy(update={"results": (changed,)})

    # When / Then
    with pytest.raises(TerminalStateDenied, match="proof binding"):
        gate().evaluate(SagaStatus.SUCCEEDED_VERIFIED, post_proof, tampered, 0)


def test_should_reject_human_residual_tampering_after_proof_event() -> None:
    # Given
    original_resolution = human_resolution()
    original = evidence(
        SagaStatus.RESOLVED_WITH_EXCEPTION,
        resolution=original_resolution,
    )
    post_proof = post_proof_snapshot(original, SagaStatus.HUMAN_REQUIRED).model_copy(
        update={
            "operations": {OPERATION_ID: operation(OperationStatus.EFFECT_CONFIRMED)},
            "obligations": {OPERATION_ID: obligation(ObligationStatus.ELIGIBLE)},
        }
    )
    changed_residual = accepted_residual(reason="A different residual was accepted.")
    changed_resolution = original_resolution.model_copy(
        update={"accepted_residuals": (changed_residual,)}
    )
    tampered = original.model_copy(update={"human_resolution": changed_resolution})

    # When / Then
    with pytest.raises(TerminalStateDenied, match="proof binding"):
        gate().evaluate(SagaStatus.RESOLVED_WITH_EXCEPTION, post_proof, tampered, 0)


@pytest.mark.parametrize(
    ("last_seq", "last_passed", "message"),
    [(8, True, "current invariant event"), (9, False, "passing invariant event")],
)
def test_should_require_current_passing_invariant_event(
    last_seq: int, last_passed: bool, message: str
) -> None:
    # Given
    current = snapshot().model_copy(
        update={"last_invariant_seq": last_seq, "last_invariant_passed": last_passed}
    )

    # When / Then
    with pytest.raises(TerminalStateDenied, match=message):
        gate().evaluate(
            SagaStatus.SUCCEEDED_VERIFIED,
            current,
            evidence(SagaStatus.SUCCEEDED_VERIFIED),
            0,
        )


def test_should_reject_evidence_after_any_intervening_event() -> None:
    # Given
    current = snapshot(seq=10).model_copy(update={"last_invariant_seq": 9})

    # When / Then
    with pytest.raises(TerminalStateDenied, match="stale invariant evidence"):
        gate().evaluate(
            SagaStatus.SUCCEEDED_VERIFIED,
            current,
            evidence(SagaStatus.SUCCEEDED_VERIFIED),
            0,
        )


def test_should_deny_nonterminal_target() -> None:
    # Given
    proof = InvariantEvidence(
        saga_id=SAGA_ID,
        definition_version="generic-v1",
        evaluated_at_seq=8,
        target_status=SagaStatus.RUNNING,
        invariant_version="rules-v1",
        results=(result("success"),),
    )

    # When / Then
    with pytest.raises(TerminalStateDenied, match="terminal target"):
        gate().evaluate(SagaStatus.RUNNING, snapshot(), proof, 0)


def result(rule_id: str, passed: bool = True) -> InvariantResult:
    return InvariantResult(
        rule_id=rule_id,
        passed=passed,
        inputs={"source": "authoritative"},
        explanation="The named rule was evaluated.",
    )


def test_should_deny_duplicate_invariant_result_ids() -> None:
    # Given
    duplicate = evidence(
        SagaStatus.SUCCEEDED_VERIFIED,
        results=(result("success"), result("success")),
    )

    # When / Then
    with pytest.raises(TerminalStateDenied, match="duplicate invariant"):
        gate().evaluate(SagaStatus.SUCCEEDED_VERIFIED, snapshot(), duplicate, 0)


def test_should_not_build_event_fields_from_duplicate_rule_results() -> None:
    # Given
    duplicate = evidence(
        SagaStatus.SUCCEEDED_VERIFIED,
        results=(result("success"), result("success")),
    )

    # When / Then
    with pytest.raises(TerminalStateDenied, match="duplicate invariant"):
        build_invariant_event_fields(duplicate)


def test_should_deny_missing_required_invariant_result() -> None:
    # Given
    missing = evidence(SagaStatus.SUCCEEDED_VERIFIED, results=(result("unrelated"),))

    # When / Then
    with pytest.raises(TerminalStateDenied) as caught:
        gate().evaluate(SagaStatus.SUCCEEDED_VERIFIED, snapshot(), missing, 0)
    assert str(caught.value) == "missing required invariant rule result"


def test_should_deny_unexpected_invariant_rule_set() -> None:
    # Given
    extra = evidence(
        SagaStatus.SUCCEEDED_VERIFIED,
        results=(result("success"), result("unrelated")),
    )

    # When / Then
    with pytest.raises(TerminalStateDenied) as caught:
        gate().evaluate(SagaStatus.SUCCEEDED_VERIFIED, snapshot(), extra, 0)
    assert str(caught.value) == "invariant rule set does not match terminal requirement"


def test_should_deny_failed_required_invariant() -> None:
    # Given / When / Then
    with pytest.raises(TerminalStateDenied, match="invariant rule failed"):
        gate().evaluate(
            SagaStatus.SUCCEEDED_VERIFIED,
            snapshot(),
            evidence(
                SagaStatus.SUCCEEDED_VERIFIED,
                results=(result("success", passed=False),),
            ),
            0,
        )


@pytest.mark.parametrize(
    "obligation_status",
    [ObligationStatus.ARMED, ObligationStatus.ELIGIBLE, ObligationStatus.IN_PROGRESS],
)
def test_should_deny_compensated_terminal_with_unresolved_obligation(
    obligation_status: ObligationStatus,
) -> None:
    # Given
    current = snapshot(status=SagaStatus.COMPENSATING, obligation_status=obligation_status)

    # When / Then
    with pytest.raises(TerminalStateDenied) as caught:
        gate().evaluate(
            SagaStatus.COMPENSATED_VERIFIED,
            current,
            evidence(SagaStatus.COMPENSATED_VERIFIED),
            0,
        )
    assert str(caught.value) == "unresolved compensation obligation"


def test_should_deny_exception_terminal_without_verified_human_evidence() -> None:
    # Given
    current = snapshot(status=SagaStatus.HUMAN_REQUIRED)

    # When / Then
    with pytest.raises(TerminalStateDenied, match="human-exception"):
        gate().evaluate(
            SagaStatus.RESOLVED_WITH_EXCEPTION,
            current,
            evidence(SagaStatus.RESOLVED_WITH_EXCEPTION),
            0,
        )


@pytest.mark.parametrize(
    "residuals",
    [(), (accepted_residual(), accepted_residual())],
)
def test_should_reject_empty_or_duplicate_accepted_residuals(
    residuals: tuple[AcceptedResidual, ...],
) -> None:
    # Given / When / Then
    with pytest.raises(ValidationError):
        human_resolution(accepted_residuals=residuals)


def test_should_deny_unknown_accepted_residual_operation() -> None:
    # Given
    resolution = human_resolution(
        accepted_residuals=(accepted_residual(operation_id=f"op_{'f' * 64}"),)
    )
    proof = evidence(SagaStatus.RESOLVED_WITH_EXCEPTION, resolution=resolution)
    current = snapshot(status=SagaStatus.HUMAN_REQUIRED, proof=proof)

    # When / Then
    with pytest.raises(TerminalStateDenied, match="residual operation"):
        gate().evaluate(SagaStatus.RESOLVED_WITH_EXCEPTION, current, proof, 0)


def test_should_accept_residual_bound_to_confirmed_operation() -> None:
    # Given
    proof = evidence(
        SagaStatus.RESOLVED_WITH_EXCEPTION,
        resolution=human_resolution(),
    )
    current = snapshot(
        status=SagaStatus.HUMAN_REQUIRED,
        operation_status=OperationStatus.EFFECT_CONFIRMED,
        proof=proof,
    )

    # When
    target = gate().evaluate(SagaStatus.RESOLVED_WITH_EXCEPTION, current, proof, 0)

    # Then
    assert target is SagaStatus.RESOLVED_WITH_EXCEPTION


def test_should_deny_repaired_forward_effect_as_human_residual() -> None:
    # Given
    current = reducer_backed_snapshot(repaired=True, compensate_with="generic_repair")
    post_proof, proof = residual_proof(current, OPERATION_ID)
    assert current.obligations[OPERATION_ID].status is ObligationStatus.SATISFIED

    # When / Then
    with pytest.raises(TerminalStateDenied, match="residual operation"):
        gate().evaluate(SagaStatus.RESOLVED_WITH_EXCEPTION, post_proof, proof, 0)


def test_should_deny_successful_compensation_operation_as_human_residual() -> None:
    # Given
    current = reducer_backed_snapshot(repaired=True, compensate_with="generic_repair")
    post_proof, proof = residual_proof(current, COMPENSATION_OPERATION_ID)
    compensation = current.operations[COMPENSATION_OPERATION_ID]
    assert compensation.status is OperationStatus.EFFECT_CONFIRMED

    # When / Then
    with pytest.raises(TerminalStateDenied, match="residual operation"):
        gate().evaluate(SagaStatus.RESOLVED_WITH_EXCEPTION, post_proof, proof, 0)


def test_should_accept_unresolved_forward_effect_with_obligation_as_human_residual() -> None:
    # Given
    current = reducer_backed_snapshot(repaired=False, compensate_with="generic_repair")
    post_proof, proof = residual_proof(current, OPERATION_ID)
    assert current.obligations[OPERATION_ID].status is ObligationStatus.ELIGIBLE

    # When
    target = gate().evaluate(SagaStatus.RESOLVED_WITH_EXCEPTION, post_proof, proof, 0)

    # Then
    assert target is SagaStatus.RESOLVED_WITH_EXCEPTION


def test_should_accept_uncompensated_forward_effect_as_human_residual() -> None:
    # Given
    current = reducer_backed_snapshot(repaired=False, compensate_with=None)
    post_proof, proof = residual_proof(current, OPERATION_ID)
    assert current.obligations == {}

    # When
    target = gate().evaluate(SagaStatus.RESOLVED_WITH_EXCEPTION, post_proof, proof, 0)

    # Then
    assert target is SagaStatus.RESOLVED_WITH_EXCEPTION


@pytest.mark.parametrize(
    ("operation_status", "obligation_status"),
    [
        (OperationStatus.NO_EFFECT_CONFIRMED, None),
        (None, ObligationStatus.NOT_REQUIRED),
        (None, ObligationStatus.SATISFIED),
    ],
)
def test_should_deny_residual_without_unresolved_effect(
    operation_status: OperationStatus | None,
    obligation_status: ObligationStatus | None,
) -> None:
    # Given
    proof = evidence(
        SagaStatus.RESOLVED_WITH_EXCEPTION,
        resolution=human_resolution(),
    )
    current = snapshot(
        status=SagaStatus.HUMAN_REQUIRED,
        operation_status=operation_status,
        obligation_status=obligation_status,
        proof=proof,
    )

    # When / Then
    with pytest.raises(TerminalStateDenied, match="residual operation"):
        gate().evaluate(SagaStatus.RESOLVED_WITH_EXCEPTION, current, proof, 0)


def test_should_deny_misbound_accepted_residual_operation() -> None:
    # Given
    proof = evidence(
        SagaStatus.RESOLVED_WITH_EXCEPTION,
        resolution=human_resolution(),
    )
    record = operation(OperationStatus.EFFECT_CONFIRMED).model_copy(
        update={"operation_id": f"op_{'f' * 64}"}
    )
    current = snapshot(status=SagaStatus.HUMAN_REQUIRED, proof=proof).model_copy(
        update={"operations": {OPERATION_ID: record}}
    )

    # When / Then
    with pytest.raises(TerminalStateDenied, match="residual operation"):
        gate().evaluate(SagaStatus.RESOLVED_WITH_EXCEPTION, current, proof, 0)


def test_should_deny_accepted_residual_with_misbound_obligation() -> None:
    # Given
    proof = evidence(
        SagaStatus.RESOLVED_WITH_EXCEPTION,
        resolution=human_resolution(),
    )
    base = snapshot(
        status=SagaStatus.HUMAN_REQUIRED,
        operation_status=OperationStatus.EFFECT_CONFIRMED,
        proof=proof,
    )
    misbound = CompensationObligation(
        forward_operation_id=f"op_{'f' * 64}",
        compensation_tool_name="generic_repair",
        status=ObligationStatus.ELIGIBLE,
    )
    current = SagaSnapshot.model_validate(
        base.model_dump(mode="python") | {"obligations": {OPERATION_ID: misbound}}
    )

    # When / Then
    with pytest.raises(TerminalStateDenied, match="residual operation"):
        gate().evaluate(SagaStatus.RESOLVED_WITH_EXCEPTION, current, proof, 0)


@pytest.mark.parametrize(
    "resolution",
    [
        human_resolution(saga_id=OTHER_SAGA_ID),
        human_resolution(resolved_at_seq=7),
        human_resolution(verification_result=False),
    ],
)
def test_should_deny_misbound_human_resolution(
    resolution: VerifiedHumanResolution,
) -> None:
    # Given
    current = snapshot(status=SagaStatus.HUMAN_REQUIRED)

    # When / Then
    with pytest.raises(TerminalStateDenied, match="human-exception"):
        gate().evaluate(
            SagaStatus.RESOLVED_WITH_EXCEPTION,
            current,
            evidence(SagaStatus.RESOLVED_WITH_EXCEPTION, resolution=resolution),
            0,
        )


def test_should_reject_human_resolution_for_non_exception_terminal() -> None:
    # Given / When / Then
    with pytest.raises(TerminalStateDenied, match="irrelevant human"):
        gate().evaluate(
            SagaStatus.SUCCEEDED_VERIFIED,
            snapshot(),
            evidence(SagaStatus.SUCCEEDED_VERIFIED, resolution=human_resolution()),
            0,
        )


def test_should_never_store_authentication_proof_in_human_resolution_evidence() -> None:
    # Given / When
    dumped = human_resolution().model_dump(mode="json")

    # Then
    assert "auth_proof" not in dumped


def test_should_keep_accepted_residual_strict_frozen_and_deeply_immutable() -> None:
    # Given
    source = {"provider": {"state": "confirmed"}}
    residual = accepted_residual(evidence=source)
    source["provider"]["state"] = "changed"

    # When / Then
    provider = residual.evidence["provider"]
    assert isinstance(provider, Mapping)
    assert provider["state"] == "confirmed"
    with pytest.raises(ValidationError):
        residual.reason = "changed"
    with pytest.raises(ValidationError):
        accepted_residual(operation_id="not-an-operation")


def finish_proposal(seq: int) -> Finish:
    return Finish(
        proposal_id="proposal_00000001",
        based_on_saga_seq=seq,
        rationale="Everything appears complete.",
        target_status="succeeded_verified",
    )


def assert_false_finish_denied(current: SagaSnapshot) -> None:
    with pytest.raises(TerminalStateDenied, match="invariant rule failed"):
        gate().evaluate(
            SagaStatus.SUCCEEDED_VERIFIED,
            current,
            evidence(
                SagaStatus.SUCCEEDED_VERIFIED,
                results=(result("success", passed=False),),
            ),
            0,
        )


def test_should_not_treat_false_finish_as_terminal_authority() -> None:
    # Given
    current = snapshot()
    proposed = finish_proposal(current.seq)

    # When / Then
    assert proposed.kind == "finish"
    assert_false_finish_denied(current)
    assert current.status is SagaStatus.RUNNING


@dataclass(frozen=True)
class AuthoritativeRule:
    rule_id: str

    def __call__(self, evidence: AuthoritativeInvariantInput) -> InvariantResult:
        return InvariantResult(
            rule_id=self.rule_id,
            passed=evidence.values["provider_state"] == "settled",
            inputs=evidence.values,
            explanation="The authoritative provider state was evaluated.",
        )


def evaluate_rule(rule: InvariantRule, item: AuthoritativeInvariantInput) -> InvariantResult:
    return rule(item)


def test_should_evaluate_rule_against_typed_authoritative_input() -> None:
    # Given
    item = AuthoritativeInvariantInput(
        saga_id=SAGA_ID,
        definition_version="generic-v1",
        evaluated_at_seq=8,
        target_status=SagaStatus.SUCCEEDED_VERIFIED,
        values={"provider_state": "settled"},
    )

    # When
    actual = evaluate_rule(AuthoritativeRule("success"), item)

    # Then
    assert actual.passed is True
    assert actual.inputs == {"provider_state": "settled"}


def test_should_keep_invariant_inputs_and_evidence_strict_and_deeply_immutable() -> None:
    # Given
    source = {"items": [{"state": "settled"}]}
    adapter: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
    immutable = adapter.validate_python(source)
    item = InvariantResult(
        rule_id="success",
        passed=True,
        inputs=immutable,
        explanation="The named rule was evaluated.",
    )
    proof = evidence(SagaStatus.SUCCEEDED_VERIFIED, results=(item,))
    source["items"][0]["state"] = "changed"

    # When / Then
    items = proof.results[0].inputs["items"]
    assert isinstance(items, tuple)
    entry = items[0]
    assert isinstance(entry, Mapping)
    assert entry["state"] == "settled"
    with pytest.raises(TypeError):
        proof.results[0].inputs["new"] = True  # type: ignore[index]
    with pytest.raises(ValidationError):
        InvariantEvidence.model_validate(
            {
                "saga_id": SAGA_ID,
                "definition_version": "generic-v1",
                "evaluated_at_seq": "8",
                "target_status": SagaStatus.SUCCEEDED_VERIFIED,
                "invariant_version": "rules-v1",
                "results": (),
                "human_resolution": None,
            }
        )


def test_should_reject_invalid_runnable_count_as_programmer_error() -> None:
    # Given / When / Then
    with pytest.raises(ValueError, match="runnable_commands"):
        gate().evaluate(
            SagaStatus.SUCCEEDED_VERIFIED,
            snapshot(),
            evidence(SagaStatus.SUCCEEDED_VERIFIED),
            -1,
        )
