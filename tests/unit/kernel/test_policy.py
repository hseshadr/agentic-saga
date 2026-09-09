from collections.abc import Callable, Mapping
from dataclasses import fields, replace
from datetime import UTC, datetime
from hashlib import sha256
from json import dumps
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from agentic_saga.contracts.actions import (
    BeginCompensation,
    Escalate,
    Finish,
    HumanDecision,
    ToolCall,
)
from agentic_saga.contracts.common import Direction, JsonObject, Reversibility
from agentic_saga.contracts.outcomes import (
    EffectConfirmed,
    EffectOutcome,
    ReconcileEffectConfirmed,
    ReconciliationOutcome,
)
from agentic_saga.contracts.runtime import ExecutionBudget, SagaStatus
from agentic_saga.contracts.tools import (
    EffectContext,
    EffectToolDefinition,
    ReadToolDefinition,
    ReconcileContext,
    ToolCapabilities,
    ToolRegistry,
)
from agentic_saga.kernel.identity import OperationIdentityFactory
from agentic_saga.kernel.policy import (
    PolicyContext,
    PolicyEngine,
    PolicyRules,
    ProposedEffectIdentity,
)
from agentic_saga.kernel.state import (
    CompensationObligation,
    ObligationStatus,
    OperationRecord,
    OperationStatus,
    SagaSnapshot,
)

SAGA_ID = "saga_0123456789abcdef"
STEP_ID = "step_01234567"
OPERATION_ID = f"op_{'d' * 64}"
OTHER_OPERATION_ID = f"op_{'f' * 64}"
COMMAND_HASH = "c" * 64


class ChargeCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    tenant_id: str
    resource_id: str
    amount_minor: int = Field(gt=0)
    currency: Literal["USD"]


class ReadCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    resource_id: str


class ReadResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    found: bool


class NestedCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    resource_id: str
    items: list[str]


class EffectAdapter:
    async def execute(self, command: ChargeCommand, context: EffectContext) -> EffectOutcome:
        return EffectConfirmed(receipt={"operation_id": context.operation_id})

    async def reconcile(
        self, command: ChargeCommand, context: ReconcileContext
    ) -> ReconciliationOutcome:
        return ReconcileEffectConfirmed(receipt={"operation_id": context.operation_id})


class ReadAdapter:
    async def read(self, command: ReadCommand) -> ReadResult:
        return ReadResult(found=bool(command.resource_id))


class GenericEffectAdapter:
    async def execute(self, command: BaseModel, context: EffectContext) -> EffectOutcome:
        return EffectConfirmed(receipt={"operation_id": context.operation_id})

    async def reconcile(
        self, command: BaseModel, context: ReconcileContext
    ) -> ReconciliationOutcome:
        return ReconcileEffectConfirmed(receipt={"operation_id": context.operation_id})


type Rule = Callable[[BaseModel, SagaSnapshot, PolicyContext], bool]
type DuplicateRule = Callable[[ProposedEffectIdentity, SagaSnapshot, PolicyContext], bool]


def allow_rule(command: BaseModel, snapshot: SagaSnapshot, context: PolicyContext) -> bool:
    del command, snapshot, context
    return True


def no_approval(command: BaseModel, snapshot: SagaSnapshot, context: PolicyContext) -> bool:
    del command, snapshot, context
    return False


def no_duplicate(
    identity: ProposedEffectIdentity,
    snapshot: SagaSnapshot,
    context: PolicyContext,
) -> bool:
    del identity, snapshot, context
    return False


def duplicate(
    identity: ProposedEffectIdentity,
    snapshot: SagaSnapshot,
    context: PolicyContext,
) -> bool:
    del identity, snapshot, context
    return True


def failing_duplicate(
    identity: ProposedEffectIdentity,
    snapshot: SagaSnapshot,
    policy_context: PolicyContext,
) -> bool:
    del identity, snapshot, policy_context
    raise RuntimeError("duplicate-callback-secret")


def failing_approval_required(
    command: BaseModel, snapshot: SagaSnapshot, policy_context: PolicyContext
) -> bool:
    del command, snapshot
    assert policy_context.approval is not None
    raise RuntimeError(policy_context.approval.auth_proof)


def verify_approval(
    decision: HumanDecision,
    proposal: ToolCall,
    snapshot: SagaSnapshot,
    context: PolicyContext,
) -> bool:
    del proposal, snapshot, context
    return decision.auth_proof == "verified-proof"


def capabilities() -> ToolCapabilities:
    return ToolCapabilities(
        idempotency_retention_seconds=86_400,
        reconciliation_supported=True,
        cancellation_supported=False,
        fencing_supported=True,
        reversibility=Reversibility.SEMANTIC,
        partial_effects_possible=False,
    )


def effect_definition() -> EffectToolDefinition[ChargeCommand]:
    return EffectToolDefinition(
        name="generic_effect",
        definition_version="generic-effect-v1",
        command_schema_version="generic-command-v1",
        input_model=ChargeCommand,
        adapter=GenericEffectAdapter(),
        capabilities=capabilities(),
        compensate_with="generic_repair",
    )


def second_effect_definition() -> EffectToolDefinition[ChargeCommand]:
    return EffectToolDefinition(
        name="generic_effect_two",
        definition_version="generic-effect-two-v1",
        command_schema_version="generic-command-v1",
        input_model=ChargeCommand,
        adapter=EffectAdapter(),
        capabilities=capabilities(),
        compensate_with="generic_repair",
    )


def nested_effect_definition() -> EffectToolDefinition[BaseModel]:
    return EffectToolDefinition(
        name="nested_effect",
        definition_version="nested-effect-v1",
        command_schema_version="nested-command-v1",
        input_model=NestedCommand,
        adapter=GenericEffectAdapter(),
        capabilities=capabilities(),
        compensate_with="generic_repair",
    )


def read_definition() -> ReadToolDefinition[ReadCommand, ReadResult]:
    return ReadToolDefinition(
        name="generic_read",
        input_model=ReadCommand,
        result_model=ReadResult,
        adapter=ReadAdapter(),
    )


def rules() -> PolicyRules:
    return PolicyRules(
        is_duplicate_effect=no_duplicate,
        approval_required=no_approval,
        approval_verifier=verify_approval,
    )


def advertise_sensitive(tool_name: str, current: SagaSnapshot) -> JsonObject:
    del tool_name, current
    return {"api_key": "raw-secret"}


def test_should_keep_human_resolution_authentication_store_owned() -> None:
    # Given
    policy = engine()

    # When
    rule_fields = tuple(field.name for field in fields(PolicyRules))

    # Then
    assert rule_fields == (
        "is_duplicate_effect",
        "approval_required",
        "approval_verifier",
        "tool_advertisement",
    )
    assert not hasattr(policy, "authenticate_human_resolution")


def engine(
    *, registry: ToolRegistry | None = None, policy_rules: PolicyRules | None = None
) -> PolicyEngine:
    return PolicyEngine(
        registry=registry
        or ToolRegistry(
            (
                effect_definition(),
                second_effect_definition(),
                nested_effect_definition(),
                read_definition(),
            )
        ),
        identity_factory=OperationIdentityFactory(namespace=b"policy-tests"),
        rules=policy_rules or rules(),
    )


def budget(**changes: int) -> ExecutionBudget:
    values = {
        "turn_limit": 10,
        "tool_call_limit": 10,
        "elapsed_ms_limit": 10_000,
        "token_limit": 10_000,
    } | changes
    return ExecutionBudget.model_validate(values)


def context(**changes: object) -> PolicyContext:
    values: dict[str, object] = {
        "step_instance_id": STEP_ID,
        "direction": Direction.FORWARD,
        "semantic_generation": 0,
        "fence_token": 7,
        "budget": budget(),
        "turns_used": 1,
        "tool_calls_used": 1,
        "elapsed_ms": 100,
        "tokens_used": 100,
        "policy_evidence": {"source": "authoritative"},
        "resource_identity": {"resource_id": "resource_1"},
        "compensates_operation_id": None,
        "approval": None,
        "used_approval_ids": (),
    }
    return PolicyContext.model_validate(values | changes)


def test_should_reject_unenforceable_provider_cost_context() -> None:
    # Given / When / Then
    with pytest.raises(ValidationError, match="cost_microusd"):
        context(cost_microusd=1)


def snapshot(
    *,
    seq: int = 2,
    status: SagaStatus = SagaStatus.RUNNING,
    operations: dict[str, OperationRecord] | None = None,
    obligations: dict[str, CompensationObligation] | None = None,
    pending_approval: bool = False,
) -> SagaSnapshot:
    return SagaSnapshot(
        saga_id=SAGA_ID,
        seq=seq,
        status=status,
        definition_version="generic-v1",
        operations=operations or {},
        obligations=obligations or {},
        pending_approval=pending_approval,
    )


def proposal(
    *,
    seq: int = 2,
    tool_name: str = "generic_effect",
    amount: int | str = 400,
    resource_id: str = "resource_1",
) -> ToolCall:
    return ToolCall(
        proposal_id="proposal_00000001",
        tool_name=tool_name,
        arguments={
            "tenant_id": "tenant_1",
            "resource_id": resource_id,
            "amount_minor": amount,
            "currency": "USD",
        },
        based_on_saga_seq=seq,
        rationale="Apply the next authorized effect.",
    )


def operation(status: OperationStatus) -> OperationRecord:
    return OperationRecord(
        operation_id=f"op_{'a' * 64}",
        step_instance_id="step_aaaaaaaa",
        direction=Direction.FORWARD,
        semantic_generation=0,
        delivery_attempt=1,
        tool_name="generic_effect",
        status=status,
        redacted_command={},
        command_hash=COMMAND_HASH,
    )


def canonical_proposal_hash(
    call: ToolCall,
    current: SagaSnapshot | None = None,
    policy_context: PolicyContext | None = None,
) -> str:
    active_snapshot = current or snapshot()
    active_context = policy_context or context()
    command = call.model_dump(mode="json")["arguments"]
    command_digest = sha256(canonical_json(command)).hexdigest()
    effect_identity = {
        "tool_name": call.tool_name,
        "direction": active_context.direction.value,
        "step_instance_id": active_context.step_instance_id,
        "semantic_generation": active_context.semantic_generation,
        "command_digest": command_digest,
        "resource_identity": active_context.model_dump(mode="json")["resource_identity"],
        "compensates_operation_id": active_context.compensates_operation_id,
    }
    envelope = {
        "schema_version": "1.0",
        "saga_id": active_snapshot.saga_id,
        "definition_version": active_snapshot.definition_version,
        "proposal": call.model_dump(mode="json"),
        "step_instance_id": active_context.step_instance_id,
        "direction": active_context.direction.value,
        "semantic_generation": active_context.semantic_generation,
        "command": command,
        "effect_identity": effect_identity,
    }
    return sha256(canonical_json(envelope)).hexdigest()


def canonical_json(value: object) -> bytes:
    return dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def legacy_proposal_hash(
    call: ToolCall,
    current: SagaSnapshot,
    policy_context: PolicyContext,
) -> str:
    envelope = {
        "schema_version": "1.0",
        "saga_id": current.saga_id,
        "definition_version": current.definition_version,
        "proposal": call.model_dump(mode="json"),
        "step_instance_id": policy_context.step_instance_id,
        "direction": policy_context.direction.value,
        "semantic_generation": policy_context.semantic_generation,
        "command": call.model_dump(mode="json")["arguments"],
    }
    return sha256(canonical_json(envelope)).hexdigest()


def approval(
    call: ToolCall | None = None,
    current: SagaSnapshot | None = None,
    policy_context: PolicyContext | None = None,
    **changes: object,
) -> HumanDecision:
    approved = call or proposal()
    active_snapshot = current or snapshot()
    values: dict[str, object] = {
        "decision_id": "decision_1",
        "saga_id": active_snapshot.saga_id,
        "based_on_saga_seq": active_snapshot.seq,
        "action": "approve",
        "proposal_hash": canonical_proposal_hash(approved, active_snapshot, policy_context),
        "actor": "authorized-operator",
        "issued_at": datetime(2026, 9, 6, 12, tzinfo=UTC),
        "auth_proof": "verified-proof",
    }
    return HumanDecision.model_validate(values | changes)


def legacy_approval(
    call: ToolCall, current: SagaSnapshot, policy_context: PolicyContext
) -> HumanDecision:
    return approval(
        call,
        current,
        policy_context,
        proposal_hash=legacy_proposal_hash(call, current, policy_context),
    )


def nested_proposal(*, items: list[str]) -> ToolCall:
    adapter: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
    arguments = adapter.validate_python({"resource_id": "resource_1", "items": items})
    return ToolCall(
        proposal_id="proposal_00000002",
        tool_name="nested_effect",
        arguments=arguments,
        based_on_saga_seq=2,
        rationale="Apply the nested command.",
    )


def forward_operation(status: OperationStatus) -> OperationRecord:
    return OperationRecord(
        operation_id=OPERATION_ID,
        step_instance_id="step_forward1",
        direction=Direction.FORWARD,
        semantic_generation=0,
        delivery_attempt=1,
        tool_name="generic_forward",
        status=status,
        redacted_command={"resource_id": "resource_1"},
        command_hash=COMMAND_HASH,
    )


def confirmed_effect(identity: ProposedEffectIdentity) -> OperationRecord:
    return OperationRecord(
        operation_id=f"op_{'e' * 64}",
        step_instance_id=identity.step_instance_id,
        direction=identity.direction,
        semantic_generation=identity.semantic_generation,
        delivery_attempt=1,
        tool_name=identity.tool_name,
        status=OperationStatus.EFFECT_CONFIRMED,
        redacted_command=identity.resource_identity,
        command_hash=identity.command_digest,
    )


def recorded_duplicate(
    identity: ProposedEffectIdentity,
    current: SagaSnapshot,
    policy_context: PolicyContext,
) -> bool:
    del policy_context
    return any(
        item.status is OperationStatus.EFFECT_CONFIRMED
        and item.tool_name == identity.tool_name
        and item.command_hash == identity.command_digest
        and item.redacted_command == identity.resource_identity
        for item in current.operations.values()
    )


@pytest.mark.parametrize(
    "status",
    [
        SagaStatus.SUCCEEDED_VERIFIED,
        SagaStatus.COMPENSATED_VERIFIED,
        SagaStatus.ABORTED_CLEAN,
        SagaStatus.RESOLVED_WITH_EXCEPTION,
    ],
)
def test_should_deny_mutation_when_saga_is_terminal(status: SagaStatus) -> None:
    # Given / When
    decision = engine().authorize(proposal(seq=1), snapshot(seq=1, status=status), context())

    # Then
    assert decision.allowed is False
    assert decision.code == "immutable_saga"
    assert decision.explanation == "The Saga is already terminal and cannot change."
    assert decision.authorized_call is None


def test_should_deny_stale_proposal_before_tool_lookup() -> None:
    # Given / When
    decision = engine(registry=ToolRegistry(())).authorize(
        proposal(seq=1, tool_name="missing"), snapshot(seq=2), context()
    )

    # Then
    assert decision.code == "stale_proposal"
    assert decision.explanation == "The proposal is not based on the current Saga state."
    assert decision.authorized_call is None


@pytest.mark.parametrize(
    "status",
    [
        SagaStatus.CREATED,
        SagaStatus.RETRY_WAIT,
        SagaStatus.RECONCILING_UNKNOWN,
        SagaStatus.RECOVERY_PLAN_REQUIRED,
        SagaStatus.HUMAN_REQUIRED,
        SagaStatus.COMPENSATING,
    ],
)
def test_should_deny_forward_effect_outside_running(status: SagaStatus) -> None:
    # Given / When
    decision = engine().authorize(proposal(), snapshot(status=status), context())

    # Then
    assert decision.code == "saga_phase_denied"
    assert decision.authorized_call is None


@pytest.mark.parametrize(
    "status",
    [
        SagaStatus.CREATED,
        SagaStatus.RUNNING,
        SagaStatus.RETRY_WAIT,
        SagaStatus.RECONCILING_UNKNOWN,
        SagaStatus.RECOVERY_PLAN_REQUIRED,
        SagaStatus.HUMAN_REQUIRED,
    ],
)
def test_should_deny_compensation_direction_outside_compensating(
    status: SagaStatus,
) -> None:
    # Given
    repair_context = context(
        direction=Direction.COMPENSATION,
        compensates_operation_id=OPERATION_ID,
    )

    # When
    decision = engine().authorize(proposal(), snapshot(status=status), repair_context)

    # Then
    assert decision.code == "saga_phase_denied"


def test_should_deny_forward_effect_with_compensation_target() -> None:
    # Given / When
    decision = engine().authorize(
        proposal(),
        snapshot(),
        context(compensates_operation_id=OPERATION_ID),
    )

    # Then
    assert decision.code == "compensation_target_denied"


@pytest.mark.parametrize(
    "obligation_status",
    [None, ObligationStatus.ARMED, ObligationStatus.IN_PROGRESS, ObligationStatus.SATISFIED],
)
def test_should_deny_compensation_without_explicit_eligible_target(
    obligation_status: ObligationStatus | None,
) -> None:
    # Given
    obligations = (
        {}
        if obligation_status is None
        else {
            OPERATION_ID: CompensationObligation(
                forward_operation_id=OPERATION_ID,
                compensation_tool_name="generic_effect",
                status=obligation_status,
            )
        }
    )
    repair_context = context(
        direction=Direction.COMPENSATION,
        compensates_operation_id=OPERATION_ID,
    )

    # When
    decision = engine().authorize(
        proposal(),
        snapshot(
            status=SagaStatus.COMPENSATING,
            operations={OPERATION_ID: forward_operation(OperationStatus.EFFECT_CONFIRMED)},
            obligations=obligations,
        ),
        repair_context,
    )

    # Then
    assert decision.code == "compensation_target_denied"


def test_should_deny_compensation_without_exact_target_identity() -> None:
    # Given
    repair_context = context(direction=Direction.COMPENSATION)

    # When
    decision = engine().authorize(
        proposal(),
        snapshot(status=SagaStatus.COMPENSATING),
        repair_context,
    )

    # Then
    assert decision.code == "compensation_target_denied"


def test_should_deny_eligible_compensation_when_forward_record_is_missing() -> None:
    # Given
    eligible = CompensationObligation(
        forward_operation_id=OPERATION_ID,
        compensation_tool_name="generic_effect",
        status=ObligationStatus.ELIGIBLE,
    )
    repair_context = context(
        direction=Direction.COMPENSATION,
        compensates_operation_id=OPERATION_ID,
    )

    # When
    decision = engine().authorize(
        proposal(),
        snapshot(
            status=SagaStatus.COMPENSATING,
            obligations={OPERATION_ID: eligible},
        ),
        repair_context,
    )

    # Then
    assert decision.code == "compensation_target_denied"


@pytest.mark.parametrize(
    "forward_status",
    [OperationStatus.EFFECT_CONFIRMED, OperationStatus.PARTIAL_EFFECT_CONFIRMED],
)
def test_should_authorize_compensation_for_explicit_eligible_target(
    forward_status: OperationStatus,
) -> None:
    # Given
    obligation = CompensationObligation(
        forward_operation_id=OPERATION_ID,
        compensation_tool_name="generic_effect",
        status=ObligationStatus.ELIGIBLE,
    )
    repair_context = context(
        direction=Direction.COMPENSATION,
        compensates_operation_id=OPERATION_ID,
    )

    # When
    decision = engine().authorize(
        proposal(),
        snapshot(
            status=SagaStatus.COMPENSATING,
            operations={OPERATION_ID: forward_operation(forward_status)},
            obligations={OPERATION_ID: obligation},
        ),
        repair_context,
    )

    # Then
    assert decision.allowed is True
    assert decision.authorized_call is not None
    assert decision.authorized_call.direction is Direction.COMPENSATION
    assert decision.effect_identity is not None
    assert decision.effect_identity.compensates_operation_id == OPERATION_ID


@pytest.mark.parametrize(
    "forward_status",
    [OperationStatus.NO_EFFECT_CONFIRMED, OperationStatus.PLANNED],
)
def test_should_deny_compensation_without_confirmed_forward_effect(
    forward_status: OperationStatus,
) -> None:
    # Given
    obligation = CompensationObligation(
        forward_operation_id=OPERATION_ID,
        compensation_tool_name="generic_effect",
        status=ObligationStatus.ELIGIBLE,
    )
    repair_context = context(
        direction=Direction.COMPENSATION,
        compensates_operation_id=OPERATION_ID,
    )

    # When
    decision = engine().authorize(
        proposal(),
        snapshot(
            status=SagaStatus.COMPENSATING,
            operations={OPERATION_ID: forward_operation(forward_status)},
            obligations={OPERATION_ID: obligation},
        ),
        repair_context,
    )

    # Then
    assert decision.code == "compensation_target_denied"


def test_should_deny_compensation_with_wrong_registered_tool() -> None:
    # Given
    obligation = CompensationObligation(
        forward_operation_id=OPERATION_ID,
        compensation_tool_name="generic_effect_two",
        status=ObligationStatus.ELIGIBLE,
    )
    repair_context = context(
        direction=Direction.COMPENSATION,
        compensates_operation_id=OPERATION_ID,
    )

    # When
    decision = engine().authorize(
        proposal(),
        snapshot(
            status=SagaStatus.COMPENSATING,
            operations={OPERATION_ID: forward_operation(OperationStatus.EFFECT_CONFIRMED)},
            obligations={OPERATION_ID: obligation},
        ),
        repair_context,
    )

    # Then
    assert decision.code == "compensation_target_denied"


@pytest.mark.parametrize(
    ("record_id", "obligation_id"),
    [(OTHER_OPERATION_ID, OPERATION_ID), (OPERATION_ID, OTHER_OPERATION_ID)],
)
def test_should_deny_compensation_with_misbound_target_records(
    record_id: str, obligation_id: str
) -> None:
    # Given
    record = forward_operation(OperationStatus.EFFECT_CONFIRMED).model_copy(
        update={"operation_id": record_id}
    )
    obligation = CompensationObligation(
        forward_operation_id=obligation_id,
        compensation_tool_name="generic_effect",
        status=ObligationStatus.ELIGIBLE,
    )
    current = snapshot(
        status=SagaStatus.COMPENSATING,
        operations={OPERATION_ID: record},
        obligations={OPERATION_ID: obligation},
    )

    # When
    decision = engine().authorize(
        proposal(),
        current,
        context(direction=Direction.COMPENSATION, compensates_operation_id=OPERATION_ID),
    )

    # Then
    assert decision.code == "compensation_target_denied"


def test_should_deny_unknown_tool_even_when_output_claims_capability() -> None:
    # Given
    unsafe_context = context(policy_evidence={"grant_tool": "missing", "approved": True})

    # When
    decision = engine(registry=ToolRegistry(())).authorize(
        proposal(tool_name="missing"), snapshot(), unsafe_context
    )

    # Then
    assert decision.code == "unknown_tool"
    assert "missing" not in decision.explanation


def test_should_deny_registered_read_tool_before_argument_validation() -> None:
    # Given
    call = proposal(tool_name="generic_read")

    # When
    decision = engine().authorize(call, snapshot(), context())

    # Then
    assert decision.code == "read_only_tool"


def test_should_hide_sensitive_advertised_constraints() -> None:
    # Given
    configured = replace(rules(), tool_advertisement=advertise_sensitive)

    # When
    constraints = engine(policy_rules=configured).advertised_constraints(
        "generic_effect", snapshot()
    )

    # Then
    assert constraints is None


def test_should_apply_common_denial_before_authorizing_read() -> None:
    # Given
    call = proposal(seq=1, tool_name="generic_read")

    # When
    decision = engine().authorize_read(call, snapshot(seq=2), context())

    # Then
    assert decision.code == "stale_proposal"


def test_should_deny_effect_tool_at_read_boundary() -> None:
    # Given / When
    decision = engine().authorize_read(proposal(), snapshot(), context())

    # Then
    assert decision.code == "effect_only_tool"


def test_should_convert_malformed_command_to_redacted_denial() -> None:
    # Given
    call = proposal(amount="super-secret")

    # When
    decision = engine().authorize(call, snapshot(), context())

    # Then
    assert decision.code == "invalid_command"
    assert "super-secret" not in decision.explanation
    assert decision.authorized_call is None


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (OperationStatus.OUTCOME_UNKNOWN, "unknown_operation"),
        (OperationStatus.PLANNED, "operation_inflight"),
        (OperationStatus.DISPATCHED, "operation_inflight"),
        (OperationStatus.INTENT_DURABLE, "operation_inflight"),
    ],
)
def test_should_deny_any_conflicting_operation(status: OperationStatus, expected_code: str) -> None:
    # Given
    record = operation(status)
    current = snapshot(operations={record.operation_id: record})

    # When
    decision = engine().authorize(proposal(), current, context())

    # Then
    assert decision.code == expected_code


def test_should_deny_pending_human_approval_before_duplicate_check() -> None:
    # Given
    configured = replace(rules(), is_duplicate_effect=duplicate)

    # When
    decision = engine(policy_rules=configured).authorize(
        proposal(), snapshot(pending_approval=True), context()
    )

    # Then
    assert decision.code == "pending_human_approval"


def test_should_deny_duplicate_semantic_effect_before_approval() -> None:
    # Given
    configured = replace(rules(), is_duplicate_effect=duplicate, approval_required=allow_rule)

    # When
    decision = engine(policy_rules=configured).authorize(proposal(), snapshot(), context())

    # Then
    assert decision.code == "duplicate_business_effect"
    assert decision.explanation == "The semantic business effect is already represented."


def test_duplicate_policy_receives_the_exact_authorization_context() -> None:
    seen: list[PolicyContext] = []

    def duplicate_with_context(
        identity: ProposedEffectIdentity, current: SagaSnapshot, policy_context: PolicyContext
    ) -> bool:
        del identity, current
        seen.append(policy_context)
        return True

    expected = context()
    configured = replace(rules(), is_duplicate_effect=duplicate_with_context)
    decision = engine(policy_rules=configured).authorize(proposal(), snapshot(), expected)

    assert decision.code == "duplicate_business_effect"
    assert seen == [expected]


@pytest.mark.parametrize(
    ("decision", "expected_code"),
    [
        (None, "approval_required"),
        (approval(proposal_hash="d" * 64), "approval_mismatch"),
        (approval(saga_id="saga_fedcba9876543210"), "approval_mismatch"),
        (approval(based_on_saga_seq=1), "approval_mismatch"),
        (approval(action="reject"), "approval_mismatch"),
        (approval(auth_proof="invalid-proof"), "approval_verification_failed"),
    ],
)
def test_should_deny_unverified_approval(
    decision: HumanDecision | None, expected_code: str
) -> None:
    # Given
    # When
    configured = replace(rules(), approval_required=allow_rule)
    result = engine(policy_rules=configured).authorize(
        proposal(), snapshot(), context(approval=decision)
    )

    # Then
    assert result.code == expected_code
    if expected_code == "approval_required":
        assert result.explanation == "A verified approval is required."


def test_should_deny_consumed_approval() -> None:
    # Given
    decision = approval()

    # When
    configured = replace(rules(), approval_required=allow_rule)
    result = engine(policy_rules=configured).authorize(
        proposal(),
        snapshot(),
        context(approval=decision, used_approval_ids=(decision.decision_id,)),
    )

    # Then
    assert result.code == "approval_already_used"
    assert result.explanation == "The approval was already consumed."


@pytest.mark.parametrize(
    ("original", "mutated"),
    [
        (proposal(), proposal(amount=401)),
        (proposal(), proposal(tool_name="generic_effect_two")),
        (nested_proposal(items=["item_1"]), nested_proposal(items=["item_2"])),
    ],
)
def test_should_deny_mutated_proposal_when_reusing_approval(
    original: ToolCall, mutated: ToolCall
) -> None:
    # Given
    configured = replace(rules(), approval_required=allow_rule)

    # When
    decision = engine(policy_rules=configured).authorize(
        mutated,
        snapshot(),
        context(approval=approval(original)),
    )

    # Then
    assert decision.code == "approval_mismatch"
    assert decision.consumed_approval is None


def test_should_bind_approval_to_authoritative_resource_identity() -> None:
    # Given
    call = proposal()
    current = snapshot()
    approved_context = context(resource_identity={"resource_id": "resource_1"})
    decision = approval(call, current, approved_context)
    configured = replace(rules(), approval_required=allow_rule)

    # When
    accepted = engine(policy_rules=configured).authorize(
        call, current, approved_context.model_copy(update={"approval": decision})
    )
    substituted = engine(policy_rules=configured).authorize(
        call,
        current,
        context(resource_identity={"resource_id": "resource_2"}, approval=decision),
    )

    # Then
    assert accepted.allowed is True
    assert substituted.code == "approval_mismatch"


def test_should_reject_legacy_approval_when_resource_identity_changes() -> None:
    # Given
    call = proposal()
    current = snapshot()
    approved_context = context(resource_identity={"resource_id": "resource_1"})
    decision = legacy_approval(call, current, approved_context)
    configured = replace(rules(), approval_required=allow_rule)

    # When
    result = engine(policy_rules=configured).authorize(
        call,
        current,
        context(resource_identity={"resource_id": "resource_2"}, approval=decision),
    )

    # Then
    assert result.code == "approval_mismatch"


def two_target_compensation_snapshot() -> SagaSnapshot:
    second = forward_operation(OperationStatus.EFFECT_CONFIRMED).model_copy(
        update={"operation_id": OTHER_OPERATION_ID}
    )
    obligations = {
        target: CompensationObligation(
            forward_operation_id=target,
            compensation_tool_name="generic_effect",
            status=ObligationStatus.ELIGIBLE,
        )
        for target in (OPERATION_ID, OTHER_OPERATION_ID)
    }
    return snapshot(
        status=SagaStatus.COMPENSATING,
        operations={
            OPERATION_ID: forward_operation(OperationStatus.EFFECT_CONFIRMED),
            OTHER_OPERATION_ID: second,
        },
        obligations=obligations,
    )


def test_should_bind_compensation_approval_to_exact_target() -> None:
    # Given
    call = proposal()
    current = two_target_compensation_snapshot()
    approved_context = context(
        direction=Direction.COMPENSATION, compensates_operation_id=OPERATION_ID
    )
    decision = approval(call, current, approved_context)
    configured = replace(rules(), approval_required=allow_rule)

    # When
    accepted = engine(policy_rules=configured).authorize(
        call, current, approved_context.model_copy(update={"approval": decision})
    )
    substituted = engine(policy_rules=configured).authorize(
        call,
        current,
        context(
            direction=Direction.COMPENSATION,
            compensates_operation_id=OTHER_OPERATION_ID,
            approval=decision,
        ),
    )

    # Then
    assert accepted.allowed is True
    assert substituted.code == "approval_mismatch"


def test_should_reject_legacy_approval_when_compensation_target_changes() -> None:
    # Given
    call = proposal()
    current = two_target_compensation_snapshot()
    approved_context = context(
        direction=Direction.COMPENSATION, compensates_operation_id=OPERATION_ID
    )
    decision = legacy_approval(call, current, approved_context)
    configured = replace(rules(), approval_required=allow_rule)

    # When
    result = engine(policy_rules=configured).authorize(
        call,
        current,
        context(
            direction=Direction.COMPENSATION,
            compensates_operation_id=OTHER_OPERATION_ID,
            approval=decision,
        ),
    )

    # Then
    assert result.code == "approval_mismatch"


def test_should_carry_verified_approval_without_authentication_secret() -> None:
    # Given
    call = proposal()
    configured = replace(rules(), approval_required=allow_rule)

    # When
    decision = engine(policy_rules=configured).authorize(
        call,
        snapshot(),
        context(approval=approval(call)),
    )

    # Then
    assert decision.allowed is True
    assert decision.consumed_approval is not None
    assert decision.consumed_approval.decision_id == "decision_1"
    assert decision.consumed_approval.actor == "authorized-operator"
    assert decision.consumed_approval.proposal_hash == canonical_proposal_hash(call)
    assert decision.consumed_approval.verification_result is True
    assert "auth_proof" not in decision.consumed_approval.model_dump(mode="json")


def test_should_validate_nested_arrays_as_exact_command_containers() -> None:
    # Given
    call = nested_proposal(items=["item_1", "item_2"])

    # When
    decision = engine().authorize(call, snapshot(), context())

    # Then
    assert decision.authorized_call is not None
    assert isinstance(decision.authorized_call.command, NestedCommand)
    assert type(decision.authorized_call.command.items) is list


def test_should_deny_actual_confirmed_semantic_duplicate() -> None:
    # Given
    call = proposal()
    first = engine().authorize(call, snapshot(), context())
    assert first.effect_identity is not None
    prior = confirmed_effect(first.effect_identity)
    configured = replace(rules(), is_duplicate_effect=recorded_duplicate)

    # When
    decision = engine(policy_rules=configured).authorize(
        call,
        snapshot(operations={prior.operation_id: prior}),
        context(),
    )

    # Then
    assert decision.code == "duplicate_business_effect"


@pytest.mark.parametrize(
    ("candidate", "resource_id"),
    [
        (proposal(resource_id="resource_2"), "resource_2"),
        (proposal(amount=401), "resource_1"),
        (proposal(tool_name="generic_effect_two"), "resource_1"),
    ],
)
def test_should_allow_near_miss_that_is_not_same_semantic_effect(
    candidate: ToolCall, resource_id: str
) -> None:
    # Given
    first = engine().authorize(proposal(), snapshot(), context())
    assert first.effect_identity is not None
    prior = confirmed_effect(first.effect_identity)
    configured = replace(rules(), is_duplicate_effect=recorded_duplicate)

    # When
    decision = engine(policy_rules=configured).authorize(
        candidate,
        snapshot(operations={prior.operation_id: prior}),
        context(resource_identity={"resource_id": resource_id}),
    )

    # Then
    assert decision.allowed is True


@pytest.mark.parametrize(
    ("limit_name", "usage_name", "expected_code"),
    [
        ("turn_limit", "turns_used", "turn_budget_exhausted"),
        ("tool_call_limit", "tool_calls_used", "tool_budget_exhausted"),
        ("elapsed_ms_limit", "elapsed_ms", "time_budget_exhausted"),
        ("token_limit", "tokens_used", "token_budget_exhausted"),
    ],
)
def test_should_deny_each_exhausted_budget_independently(
    limit_name: str, usage_name: str, expected_code: str
) -> None:
    # Given
    limit = 10 if limit_name in {"elapsed_ms_limit", "token_limit"} else 1
    exhausted = budget(**{limit_name: limit})

    # When
    decision = engine().authorize(
        proposal(), snapshot(), context(budget=exhausted, **{usage_name: limit})
    )

    # Then
    assert decision.code == expected_code


def test_should_check_budgets_after_verified_approval() -> None:
    # Given
    # When
    configured = replace(rules(), approval_required=allow_rule)
    decision = engine(policy_rules=configured).authorize(
        proposal(),
        snapshot(),
        context(approval=approval(), budget=budget(turn_limit=1), turns_used=1),
    )

    # Then
    assert decision.code == "turn_budget_exhausted"


def test_should_authorize_typed_command_with_kernel_identity_and_fence() -> None:
    # Given
    current_context = context()

    # When
    decision = engine().authorize(proposal(), snapshot(), current_context)

    # Then
    assert decision.allowed is True
    assert decision.code == "authorized"
    assert decision.fence_token == current_context.fence_token
    assert decision.authorized_call is not None
    assert isinstance(decision.authorized_call.command, ChargeCommand)
    assert decision.authorized_call.step_instance_id == STEP_ID
    assert decision.authorized_call.direction is Direction.FORWARD
    assert decision.authorized_call.semantic_generation == 0
    assert decision.authorized_call.operation_id.startswith("op_")
    assert "idempotency_key" not in decision.authorized_call.model_dump(mode="json")
    assert decision.effect_identity is not None
    assert decision.effect_identity.tool_name == "generic_effect"
    assert decision.effect_identity.resource_identity == {"resource_id": "resource_1"}


@pytest.mark.parametrize(
    "control",
    [
        Finish(
            proposal_id="proposal_00000003",
            based_on_saga_seq=2,
            rationale="Request deterministic proof.",
            target_status="succeeded_verified",
        ),
        Escalate(
            proposal_id="proposal_00000004",
            based_on_saga_seq=2,
            reason_code="needs_operator",
            rationale="Deterministic progress is unsafe.",
        ),
    ],
)
def test_should_never_create_authorized_effect_for_control_proposal(
    control: Finish | Escalate,
) -> None:
    # Given / When
    decision = engine().authorize(control, snapshot(), context())

    # Then
    assert decision.allowed is True
    assert decision.code == "control_proposal"
    assert decision.authorized_call is None
    assert decision.fence_token is None
    assert decision.effect_identity is None
    assert decision.consumed_approval is None


def test_should_deny_finish_target_that_is_illegal_from_current_phase() -> None:
    finish = Finish(
        proposal_id="proposal_00000005",
        based_on_saga_seq=2,
        rationale="Compensation appears complete.",
        target_status="compensated_verified",
    )

    decision = engine().authorize(finish, snapshot(), context())

    assert decision.allowed is False
    assert decision.code == "saga_phase_denied"


def test_should_deny_escalation_from_created_phase() -> None:
    escalation = Escalate(
        proposal_id="proposal_00000006",
        based_on_saga_seq=2,
        reason_code="needs_operator",
        rationale="Autonomous progress is unsafe.",
    )

    decision = engine().authorize(escalation, snapshot(status=SagaStatus.CREATED), context())

    assert decision.allowed is False
    assert decision.code == "saga_phase_denied"


def _begin_compensation(seq: int = 2) -> BeginCompensation:
    return BeginCompensation(
        proposal_id="proposal_00000007",
        based_on_saga_seq=seq,
        reason_code="goal_unreachable",
        rationale="Compensate the confirmed effects in the verified frontier.",
    )


def _eligible_obligation() -> tuple[OperationRecord, CompensationObligation]:
    confirmed = operation(OperationStatus.EFFECT_CONFIRMED).model_copy(
        update={"receipts": ({"receipt_id": "receipt_1"},)}
    )
    obligation = CompensationObligation(
        forward_operation_id=confirmed.operation_id,
        compensation_tool_name="generic_effect",
        status=ObligationStatus.ELIGIBLE,
        receipts=confirmed.receipts,
    )
    return confirmed, obligation


def test_should_allow_begin_compensation_only_with_settled_eligible_work() -> None:
    # Given
    confirmed, obligation = _eligible_obligation()
    current = snapshot(
        operations={confirmed.operation_id: confirmed},
        obligations={confirmed.operation_id: obligation},
    )

    # When
    decision = engine().authorize(_begin_compensation(), current, context())

    # Then
    assert decision.allowed is True
    assert decision.code == "control_proposal"


@pytest.mark.parametrize("status", [SagaStatus.CREATED, SagaStatus.COMPENSATING])
def test_should_deny_begin_compensation_outside_running_phase(status: SagaStatus) -> None:
    # Given / When
    decision = engine().authorize(_begin_compensation(), snapshot(status=status), context())

    # Then
    assert decision.allowed is False
    assert decision.code == "saga_phase_denied"


def test_should_deny_begin_compensation_without_eligible_obligation() -> None:
    # Given / When
    decision = engine().authorize(_begin_compensation(), snapshot(), context())

    # Then
    assert decision.allowed is False
    assert decision.code == "compensation_not_ready"


@pytest.mark.parametrize(
    "blocking_status",
    [
        OperationStatus.PLANNED,
        OperationStatus.INTENT_DURABLE,
        OperationStatus.DISPATCHED,
        OperationStatus.OUTCOME_UNKNOWN,
    ],
)
def test_should_deny_begin_compensation_while_an_effect_is_unsettled(
    blocking_status: OperationStatus,
) -> None:
    confirmed, obligation = _eligible_obligation()
    blocking = operation(blocking_status).model_copy(update={"operation_id": f"op_{'b' * 64}"})
    operations = {confirmed.operation_id: confirmed, blocking.operation_id: blocking}
    obligations = {confirmed.operation_id: obligation}

    decision = engine().authorize(
        _begin_compensation(), snapshot(operations=operations, obligations=obligations), context()
    )

    assert decision.allowed is False
    assert decision.code == "compensation_not_ready"


def test_should_sanitize_duplicate_callback_error_as_denial() -> None:
    configured = replace(rules(), is_duplicate_effect=failing_duplicate)

    decision = engine(policy_rules=configured).authorize(proposal(), snapshot(), context())

    assert decision.code == "policy_callback_error"
    assert "duplicate-callback-secret" not in decision.model_dump_json()


def test_should_sanitize_approval_required_error_with_auth_proof() -> None:
    configured = replace(rules(), approval_required=failing_approval_required)
    policy_context = context(approval=approval())

    decision = engine(policy_rules=configured).authorize(proposal(), snapshot(), policy_context)

    assert decision.code == "policy_callback_error"
    assert policy_context.approval is not None
    assert policy_context.approval.auth_proof not in decision.model_dump_json()


def test_should_raise_when_approval_requirement_returns_non_boolean() -> None:
    # Given
    def broken(command: BaseModel, current: SagaSnapshot, policy_context: PolicyContext) -> bool:
        del command, current, policy_context
        return 1  # type: ignore[return-value]

    # When / Then
    with pytest.raises(TypeError, match="boolean"):
        configured = replace(rules(), approval_required=broken)
        engine(policy_rules=configured).authorize(proposal(), snapshot(), context())


def test_should_raise_when_approval_verifier_returns_non_boolean() -> None:
    # Given
    def broken(
        decision: HumanDecision,
        proposed: ToolCall,
        current: SagaSnapshot,
        policy_context: PolicyContext,
    ) -> bool:
        del decision, proposed, current, policy_context
        return 1  # type: ignore[return-value]

    # When / Then
    with pytest.raises(TypeError, match="boolean"):
        configured = replace(rules(), approval_required=allow_rule, approval_verifier=broken)
        engine(policy_rules=configured).authorize(
            proposal(), snapshot(), context(approval=approval())
        )


def test_should_keep_budget_and_context_strict_frozen_and_deeply_immutable() -> None:
    # Given
    source = {"nested": {"allowed": False}}
    current = context(policy_evidence=source)
    source["nested"]["allowed"] = True

    # When / Then
    nested = current.policy_evidence["nested"]
    assert isinstance(nested, Mapping)
    assert nested["allowed"] is False
    with pytest.raises(ValidationError):
        budget(turn_limit="10")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        context(turns_used="1")
    with pytest.raises(ValidationError):
        current.turns_used = 2
