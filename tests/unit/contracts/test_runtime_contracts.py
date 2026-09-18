from __future__ import annotations

from collections.abc import Sequence
from dataclasses import FrozenInstanceError, dataclass, replace
from hashlib import sha256
from inspect import getsource

import pytest
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from agentic_saga.contracts.actions import AgentProposal, HumanDecision, ToolCall
from agentic_saga.contracts.common import JsonObject, Reversibility, thaw_json_object
from agentic_saga.contracts.outcomes import EffectConfirmed, ReconcileEffectConfirmed
from agentic_saga.contracts.redaction import RedactionPolicy
from agentic_saga.contracts.runtime import (
    AgentDriver,
    ControlProposalCapabilities,
    ExecutionBudget,
    ReadEvidence,
    SagaGoal,
    SagaObservation,
    SagaResult,
    SagaStatus,
    TerminalRequirement,
    ToolDescriptor,
)
from agentic_saga.contracts.tools import (
    EffectContext,
    EffectToolDefinition,
    ReadToolDefinition,
    ReconcileContext,
    ToolCapabilities,
    ToolRegistry,
    ToolRegistryFrozen,
)
from agentic_saga.kernel.definitions import (
    DefinitionCatalog,
    DefinitionConflict,
    DefinitionUnavailable,
    SagaDefinition,
)
from agentic_saga.kernel.identity import OperationIdentityFactory
from agentic_saga.kernel.policy import (
    PolicyContext,
    PolicyEngine,
    PolicyRule,
    PolicyRules,
    ProposedEffectIdentity,
    VersionedPolicyRule,
)
from agentic_saga.kernel.state import SagaSnapshot

SAGA_ID = "saga_0123456789abcdef"
_JSON_OBJECT: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
_GLOBAL_APPROVAL_ENABLED = False
_OPAQUE_POLICY_CONFIG = object()


class Command(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    amount_minor: int = Field(strict=True, gt=0)


class Result(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    count: int = Field(strict=True, ge=0)


class MutableRedactionPolicy(RedactionPolicy):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=False)


class Adapter:
    async def read(self, command: Command) -> Result:
        return Result(count=command.amount_minor)


class StatefulAdapter:
    def __init__(self, revision: str) -> None:
        self.revision = revision

    async def read(self, command: Command) -> Result:
        return Result(count=command.amount_minor)


class IdentifiedAdapter(StatefulAdapter):
    def definition_identity(self) -> JsonObject:
        return _JSON_OBJECT.validate_python({"revision": self.revision})


class SensitiveIdentityAdapter(StatefulAdapter):
    def definition_identity(self) -> JsonObject:
        return _JSON_OBJECT.validate_python({"api_key": self.revision})


class NondeterministicIdentityAdapter(StatefulAdapter):
    calls: int = 0

    def definition_identity(self) -> JsonObject:
        self.calls += 1
        return _JSON_OBJECT.validate_python({"revision": self.revision, "calls": self.calls})


class GenericEffectAdapter:
    async def execute(self, command: Command, context: EffectContext) -> EffectConfirmed:
        del command, context
        return EffectConfirmed(receipt={})

    async def reconcile(
        self, command: Command, context: ReconcileContext
    ) -> ReconcileEffectConfirmed:
        del command, context
        return ReconcileEffectConfirmed(receipt={})


def budget() -> ExecutionBudget:
    return ExecutionBudget(
        turn_limit=4,
        tool_call_limit=3,
        elapsed_ms_limit=10_000,
        token_limit=2_000,
    )


def test_should_expose_only_kernel_enforceable_budget_dimensions() -> None:
    # Given / When
    value = ExecutionBudget(
        turn_limit=4,
        tool_call_limit=3,
        elapsed_ms_limit=10_000,
        token_limit=2_000,
    )

    # Then
    assert value.model_dump() == {
        "turn_limit": 4,
        "tool_call_limit": 3,
        "elapsed_ms_limit": 10_000,
        "token_limit": 2_000,
    }


def test_should_reject_unenforceable_provider_cost_budget() -> None:
    # Given
    raw = {
        "turn_limit": 4,
        "tool_call_limit": 3,
        "elapsed_ms_limit": 10_000,
        "token_limit": 2_000,
        "cost_microusd_limit": 500_000,
    }

    # When / Then
    with pytest.raises(ValidationError, match="cost_microusd_limit"):
        ExecutionBudget.model_validate(raw)


@pytest.mark.parametrize("field", ["elapsed_ms_limit", "token_limit"])
def test_should_require_one_planning_unit_per_positive_turn(field: str) -> None:
    # Given
    raw = {
        "turn_limit": 4,
        "tool_call_limit": 3,
        "elapsed_ms_limit": 10_000,
        "token_limit": 2_000,
        field: 3,
    }

    # When / Then
    with pytest.raises(ValidationError, match=field):
        ExecutionBudget.model_validate(raw)


def requirement(rule_id: str) -> TerminalRequirement:
    return TerminalRequirement(invariant_version="runtime-v1", required_rule_ids=(rule_id,))


def test_terminal_requirement_json_schema_preserves_named_contracts() -> None:
    # Given the public terminal requirement contract
    expected = {
        "$defs": {
            "_RuleId": {"maxLength": 200, "minLength": 1, "type": "string"},
            "_Version": {"maxLength": 200, "minLength": 1, "type": "string"},
        },
        "additionalProperties": False,
        "description": "Name the invariant version and rules required for one terminal status.",
        "properties": {
            "invariant_version": {"$ref": "#/$defs/_Version"},
            "required_rule_ids": {
                "items": {"$ref": "#/$defs/_RuleId"},
                "minItems": 1,
                "title": "Required Rule Ids",
                "type": "array",
            },
        },
        "required": ["invariant_version", "required_rule_ids"],
        "title": "TerminalRequirement",
        "type": "object",
    }

    # When its JSON Schema is generated
    schema = TerminalRequirement.model_json_schema()

    # Then its named aliases and public description remain explicit
    assert schema == expected


def read_definition() -> ReadToolDefinition[Command, Result]:
    return ReadToolDefinition(
        "observe_generic",
        Command,
        Result,
        Adapter(),
        description="Observe the current generic resource before choosing an effect.",
    )


def _allow(command: BaseModel, snapshot: SagaSnapshot, context: PolicyContext) -> bool:
    del command, snapshot, context
    return True


def _not_duplicate(
    identity: ProposedEffectIdentity, snapshot: SagaSnapshot, context: PolicyContext
) -> bool:
    del identity, snapshot, context
    return False


def _no_approval(command: BaseModel, snapshot: SagaSnapshot, context: PolicyContext) -> bool:
    del command, snapshot, context
    return False


def _verify(
    decision: HumanDecision,
    proposal: ToolCall,
    snapshot: SagaSnapshot,
    context: PolicyContext,
) -> bool:
    del decision, proposal, snapshot, context
    return False


def _global_approval(command: BaseModel, snapshot: SagaSnapshot, context: PolicyContext) -> bool:
    del command, snapshot, context
    return _GLOBAL_APPROVAL_ENABLED


def _transitive_policy_helper() -> bool:
    return False


def _transitive_approval(
    command: BaseModel, snapshot: SagaSnapshot, context: PolicyContext
) -> bool:
    del command, snapshot, context
    return _transitive_policy_helper()


def _opaque_approval(command: BaseModel, snapshot: SagaSnapshot, context: PolicyContext) -> bool:
    del command, snapshot, context
    return bool(_OPAQUE_POLICY_CONFIG)


def policy(registry: ToolRegistry) -> PolicyEngine:
    rules = PolicyRules(
        is_duplicate_effect=_not_duplicate,
        approval_required=_no_approval,
        approval_verifier=_verify,
    )
    return PolicyEngine(
        registry=registry,
        identity_factory=OperationIdentityFactory(namespace=b"runtime-tests"),
        rules=rules,
    )


def definition(*, version: str = "saga-v1") -> SagaDefinition:
    registry = ToolRegistry((read_definition(),))
    return SagaDefinition(
        name="generic_saga",
        version=version,
        registry=registry,
        policy=policy(registry),
        success_invariants=requirement("success"),
        compensation_invariants=requirement("compensation"),
        clean_abort_invariants=requirement("clean_abort"),
        exception_invariants=requirement("operator_accepted"),
        budget=budget(),
    )


def _adapter_definition(adapter: Adapter | StatefulAdapter) -> SagaDefinition:
    registry = ToolRegistry((ReadToolDefinition("observe_generic", Command, Result, adapter),))
    return SagaDefinition(
        name="generic_saga",
        version="adapter-v1",
        registry=registry,
        policy=policy(registry),
        success_invariants=requirement("success"),
        compensation_invariants=requirement("compensation"),
        clean_abort_invariants=requirement("clean_abort"),
        exception_invariants=requirement("operator_accepted"),
        budget=budget(),
    )


def _closure_definition(*, approval_enabled: bool) -> SagaDefinition:
    registry = ToolRegistry((read_definition(),))

    def approval_required(
        command: BaseModel, snapshot: SagaSnapshot, context: PolicyContext
    ) -> bool:
        del command, snapshot, context
        return approval_enabled

    rules = PolicyRules(_not_duplicate, approval_required, _verify)
    configured_policy = PolicyEngine(
        registry=registry,
        identity_factory=OperationIdentityFactory(namespace=b"runtime-tests"),
        rules=rules,
    )
    return replace(definition(), registry=registry, policy=configured_policy)


def _rule_definition(rule: PolicyRule) -> SagaDefinition:
    registry = ToolRegistry((read_definition(),))
    rules = PolicyRules(_not_duplicate, rule, _verify)
    configured_policy = PolicyEngine(
        registry=registry,
        identity_factory=OperationIdentityFactory(namespace=b"runtime-tests"),
        rules=rules,
    )
    return replace(definition(), registry=registry, policy=configured_policy)


def _default_definition(*, approval_enabled: bool) -> SagaDefinition:
    def approval_required(
        command: BaseModel,
        snapshot: SagaSnapshot,
        context: PolicyContext,
        enabled: bool = approval_enabled,
    ) -> bool:
        del command, snapshot, context
        return enabled

    return _rule_definition(approval_required)


@dataclass
class IdentifiedApprovalRule:
    enabled: bool

    def definition_identity(self) -> JsonObject:
        return _JSON_OBJECT.validate_python({"enabled": self.enabled}, strict=True)

    def required(self, command: BaseModel, snapshot: SagaSnapshot, context: PolicyContext) -> bool:
        del command, snapshot, context
        return self.enabled


@dataclass
class UnidentifiedApprovalRule:
    enabled: bool

    def required(self, command: BaseModel, snapshot: SagaSnapshot, context: PolicyContext) -> bool:
        del command, snapshot, context
        return self.enabled


def test_definition_owns_the_application_redaction_policy() -> None:
    # Given
    privacy = RedactionPolicy(sensitive_keys=("email", "name", "address"))
    registry = ToolRegistry()

    # When
    configured = SagaDefinition(
        name="generic_saga",
        version="privacy-v1",
        registry=registry,
        policy=policy(registry),
        success_invariants=requirement("success"),
        compensation_invariants=requirement("compensation"),
        clean_abort_invariants=requirement("clean_abort"),
        exception_invariants=requirement("operator_accepted"),
        budget=budget(),
        redaction_policy=privacy,
    )

    # Then
    assert configured.redaction_policy == privacy
    assert configured.redaction_policy is not privacy
    assert type(configured.redaction_policy) is RedactionPolicy


def test_definition_rejects_policy_bound_to_another_registry() -> None:
    # Given
    registry = ToolRegistry()
    other_registry = ToolRegistry()

    # When / Then
    with pytest.raises(ValueError, match="same ToolRegistry"):
        SagaDefinition(
            name="generic_saga",
            version="registry-binding-v1",
            registry=registry,
            policy=policy(other_registry),
            success_invariants=requirement("success"),
            compensation_invariants=requirement("compensation"),
            clean_abort_invariants=requirement("clean_abort"),
            exception_invariants=requirement("operator_accepted"),
            budget=budget(),
        )


def test_definition_rejects_redaction_policy_subclasses() -> None:
    base = definition()

    with pytest.raises(TypeError, match="RedactionPolicy"):
        SagaDefinition(
            name=base.name,
            version=base.version,
            registry=base.registry,
            policy=base.policy,
            success_invariants=base.success_invariants,
            compensation_invariants=base.compensation_invariants,
            clean_abort_invariants=base.clean_abort_invariants,
            exception_invariants=base.exception_invariants,
            budget=base.budget,
            redaction_policy=MutableRedactionPolicy(sensitive_keys=("email",)),
        )


def test_definition_revalidates_copied_redaction_policy_fields() -> None:
    base = definition()
    invalid = RedactionPolicy().model_copy(update={"sensitive_keys": ["email"]})

    with pytest.raises(ValidationError):
        SagaDefinition(
            name=base.name,
            version=base.version,
            registry=base.registry,
            policy=base.policy,
            success_invariants=base.success_invariants,
            compensation_invariants=base.compensation_invariants,
            clean_abort_invariants=base.clean_abort_invariants,
            exception_invariants=base.exception_invariants,
            budget=base.budget,
            redaction_policy=invalid,
        )


def goal() -> SagaGoal:
    return SagaGoal(
        goal_id="goal_order_1",
        text="Fulfill this request or restore a valid state.",
        context={"public_id": "resource_1"},
    )


def test_goal_and_observation_are_strict_frozen_and_sequence_bound() -> None:
    observation = SagaObservation(
        saga_id=SAGA_ID,
        saga_seq=4,
        state=SagaStatus.RUNNING,
        goal=goal(),
        last_action=None,
        projection={"resource_status": "created"},
        remaining_budget=budget(),
    )

    assert observation.saga_seq == 4
    with pytest.raises(ValidationError):
        SagaObservation.model_validate(observation.model_dump() | {"saga_seq": "4"})
    with pytest.raises(ValidationError):
        SagaGoal(goal_id="goal_order_1", text="valid", context={"api_key": "secret"})
    with pytest.raises(ValidationError):
        SagaGoal(goal_id="goal_order_1", text="Bearer raw-secret", context={})


def test_control_proposal_capabilities_are_strict_and_immutable() -> None:
    controls = ControlProposalCapabilities(
        finish_targets=("succeeded_verified", "aborted_clean"),
        begin_compensation=True,
        escalate_to_human=True,
    )

    assert controls.finish_targets == ("succeeded_verified", "aborted_clean")
    with pytest.raises(ValidationError):
        controls.begin_compensation = False
    with pytest.raises(ValidationError):
        ControlProposalCapabilities.model_validate(
            {"finish_targets": ("not_terminal",)}, strict=True
        )


def test_observation_read_evidence_is_backward_compatible_and_immutable() -> None:
    legacy = SagaObservation(
        saga_id=SAGA_ID,
        saga_seq=4,
        state=SagaStatus.RUNNING,
        goal=goal(),
        last_action=None,
        projection={"resource_status": "created"},
        remaining_budget=budget(),
    )
    evidence = ReadEvidence(
        tool_name="inspect_order",
        command={"order_id": "order_1"},
        result={"state": "paid"},
        observed_at_saga_seq=4,
        freshness="fresh",
    )

    observed = SagaObservation(
        saga_id=SAGA_ID,
        saga_seq=4,
        state=SagaStatus.RUNNING,
        goal=goal(),
        last_action=None,
        projection={"resource_status": "created"},
        remaining_budget=budget(),
        read_evidence=(evidence,),
    )

    assert legacy.read_evidence == ()
    assert observed.read_evidence == (evidence,)
    with pytest.raises(ValidationError):
        evidence.result = {"state": "changed"}
    with pytest.raises(ValidationError):
        ReadEvidence(
            tool_name="inspect_order",
            command={"order_id": "order_1"},
            result={"state": "paid"},
            unavailable_reason="read_unavailable",
            observed_at_saga_seq=4,
            freshness="fresh",
        )
    with pytest.raises(ValidationError):
        ReadEvidence(
            tool_name="inspect_order",
            command={"order_id": "order_1"},
            observed_at_saga_seq=4,
            freshness="fresh",
        )


@pytest.mark.parametrize(
    "missing",
    ["observed_at_saga_seq", "freshness"],
)
def test_read_evidence_requires_explicit_sequence_and_freshness(missing: str) -> None:
    values: dict[str, object] = {
        "tool_name": "inspect_order",
        "command": {"order_id": "order_1"},
        "result": {"state": "paid"},
        "observed_at_saga_seq": 4,
        "freshness": "fresh",
    }
    values.pop(missing)

    with pytest.raises(ValidationError):
        ReadEvidence.model_validate(values, strict=True)


def test_read_evidence_rejects_invalid_freshness_and_sequence() -> None:
    with pytest.raises(ValidationError):
        ReadEvidence(
            tool_name="inspect_order",
            command={"order_id": "order_1"},
            result={"state": "paid"},
            observed_at_saga_seq=0,
            freshness="fresh",
        )
    with pytest.raises(ValidationError):
        ReadEvidence.model_validate(
            {
                "tool_name": "inspect_order",
                "command": {"order_id": "order_1"},
                "result": {"state": "paid"},
                "observed_at_saga_seq": 4,
                "freshness": "unknown",
            },
            strict=True,
        )


def test_should_hide_private_input_when_goal_validation_rejects_context() -> None:
    # Given
    secret = "S3cr3t!"  # noqa: S105 - deliberate validation-leak sentinel
    digest = sha256(secret.encode()).hexdigest()

    # When
    with pytest.raises(ValidationError) as captured:
        SagaGoal(goal_id="goal_order_1", text="valid", context={"password": secret})

    # Then
    error = captured.value
    structures = error.errors(include_input=False)
    surfaces = (str(error), repr(error), error.json(include_input=False), repr(structures))
    assert all(secret not in surface and digest not in surface for surface in surfaces)
    assert all("input" not in item for item in structures)


def test_saga_status_result_cannot_claim_model_truth() -> None:
    result = SagaResult(
        saga_id=SAGA_ID,
        state=SagaStatus.HUMAN_REQUIRED,
        saga_seq=8,
        autonomous_quiescent=True,
        human_required_reason="agent_unavailable",
    )
    assert "target_status" not in result.model_dump()


def test_tool_descriptor_contains_schema_but_no_callable_or_private_config() -> None:
    descriptor = ToolDescriptor.from_definition(read_definition())

    assert descriptor.name == "observe_generic"
    assert descriptor.kind == "read"
    assert descriptor.description == (
        "Observe the current generic resource before choosing an effect."
    )
    schema = thaw_json_object(descriptor.input_schema)
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert "amount_minor" in properties
    public = descriptor.model_dump(mode="json")
    assert "adapter" not in public
    assert "secret_references" not in str(public)


def test_effect_descriptor_exposes_reversibility_without_adapter() -> None:
    tool = EffectToolDefinition(
        "mutate_generic",
        "effect-v1",
        "command-v1",
        Command,
        GenericEffectAdapter(),
        ToolCapabilities(
            idempotency_retention_seconds=10,
            reconciliation_supported=True,
            cancellation_supported=False,
            fencing_supported=True,
            reversibility=Reversibility.SEMANTIC,
            partial_effects_possible=False,
        ),
        None,
        description="Mutate the generic resource when current evidence permits it.",
    )
    descriptor = ToolDescriptor.from_definition(tool)

    assert descriptor.kind == "effect"
    assert descriptor.description == (
        "Mutate the generic resource when current evidence permits it."
    )
    assert descriptor.reversibility is Reversibility.SEMANTIC
    assert "adapter" not in descriptor.model_dump(mode="json")


def test_definition_is_frozen_and_catalog_resolution_is_exact() -> None:
    item = definition()
    catalog = DefinitionCatalog()
    catalog.register(item)

    assert catalog.resolve("generic_saga", "saga-v1") is item
    with pytest.raises(FrozenInstanceError):
        item.version = "changed"  # type: ignore[misc]
    with pytest.raises(DefinitionUnavailable):
        catalog.resolve("generic_saga", "saga-v2")


def test_catalog_rejects_conflicting_reuse_but_same_instance_is_idempotent() -> None:
    item = definition()
    catalog = DefinitionCatalog((item,))
    catalog.register(item)

    with pytest.raises(DefinitionConflict):
        catalog.register(definition())


def test_definition_fingerprint_captures_policy_closure_configuration() -> None:
    # Given / When
    denied = _closure_definition(approval_enabled=False)
    required = _closure_definition(approval_enabled=True)

    # Then
    assert denied.fingerprint != required.fingerprint


def test_definition_fingerprint_captures_policy_default_configuration() -> None:
    assert (
        _default_definition(approval_enabled=False).fingerprint
        != _default_definition(approval_enabled=True).fingerprint
    )


def test_definition_fingerprint_detects_public_global_policy_configuration_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = _rule_definition(_global_approval)
    monkeypatch.setitem(_global_approval.__globals__, "_GLOBAL_APPROVAL_ENABLED", True)

    with pytest.raises(DefinitionConflict, match="mutated"):
        _ = configured.fingerprint


def test_definition_rejects_unversioned_transitive_policy_behavior() -> None:
    with pytest.raises(ValueError, match="VersionedPolicyRule"):
        _rule_definition(_transitive_approval)


def test_definition_rejects_opaque_global_policy_configuration() -> None:
    with pytest.raises(ValueError, match="VersionedPolicyRule"):
        _rule_definition(_opaque_approval)


def test_versioned_transitive_policy_behavior_changes_fingerprint() -> None:
    first = _rule_definition(VersionedPolicyRule(_transitive_approval, "approval-v1"))
    second = _rule_definition(VersionedPolicyRule(_transitive_approval, "approval-v2"))

    assert first.fingerprint != second.fingerprint


def test_versioned_policy_rule_preserves_direct_behavior_identity() -> None:
    first = _rule_definition(VersionedPolicyRule(_transitive_approval, "approval-v1"))
    second = _rule_definition(VersionedPolicyRule(_opaque_approval, "approval-v1"))

    assert first.fingerprint != second.fingerprint


def test_definition_fingerprint_captures_identified_bound_policy_configuration() -> None:
    denied = _rule_definition(IdentifiedApprovalRule(False).required)
    required = _rule_definition(IdentifiedApprovalRule(True).required)

    assert denied.fingerprint != required.fingerprint


def test_definition_rejects_unidentified_bound_policy_configuration() -> None:
    with pytest.raises(ValueError, match="bound policy owners"):
        _rule_definition(UnidentifiedApprovalRule(False).required)


def test_definition_fingerprint_changes_with_invariant_contract_version() -> None:
    baseline = definition()
    changed = replace(
        baseline,
        success_invariants=requirement("success").model_copy(
            update={"invariant_version": "runtime-v2"}
        ),
    )

    assert baseline.fingerprint != changed.fingerprint


def test_definition_fingerprint_captures_custom_model_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = getsource
    model_source = "class Command: validator_revision = 1"

    def source(value: type[object]) -> str:
        if value is Command:
            return model_source
        return original(value)

    monkeypatch.setattr("agentic_saga.kernel.definitions.getsource", source)
    first = definition()
    first_fingerprint = first.fingerprint
    model_source = "class Command: validator_revision = 2"
    second = definition()

    assert first_fingerprint != second.fingerprint


def test_stateful_adapter_requires_explicit_stable_definition_identity() -> None:
    # Given / When / Then
    with pytest.raises(ValueError, match="definition_identity"):
        _adapter_definition(StatefulAdapter("adapter-v1"))


def test_adapter_definition_identity_changes_fingerprint_without_persisting_raw_config() -> None:
    # Given / When
    first = _adapter_definition(IdentifiedAdapter("revision-one"))
    second = _adapter_definition(IdentifiedAdapter("revision-two"))

    # Then
    assert first.fingerprint != second.fingerprint
    assert "revision-one" not in first.fingerprint


def test_adapter_definition_identity_rejects_sensitive_material() -> None:
    # Given / When / Then
    with pytest.raises(ValueError, match="public"):
        _adapter_definition(SensitiveIdentityAdapter("raw-secret"))


def test_adapter_definition_identity_rejects_nondeterministic_values() -> None:
    # Given / When / Then
    with pytest.raises(ValueError, match="deterministic"):
        _adapter_definition(NondeterministicIdentityAdapter("adapter-v1"))


def test_adapter_without_inspectable_source_is_rejected() -> None:
    # Given an adapter type synthesized without importable source
    dynamic_adapter = type("DynamicAdapter", (Adapter,), {})()

    # When / Then
    with pytest.raises(ValueError, match="stable source"):
        _adapter_definition(dynamic_adapter)


def test_agent_driver_protocol_keeps_one_typed_action_return() -> None:
    class Driver:
        async def next_action(
            self, observation: SagaObservation, available_tools: Sequence[ToolDescriptor]
        ) -> AgentProposal:
            del observation, available_tools
            raise RuntimeError("not called")

    driver: AgentDriver = Driver()
    assert driver is not None


def test_tool_capability_contract_remains_strict() -> None:
    capabilities = ToolCapabilities(
        idempotency_retention_seconds=None,
        reconciliation_supported=False,
        cancellation_supported=False,
        fencing_supported=False,
        reversibility=Reversibility.IRREVERSIBLE,
        partial_effects_possible=False,
    )
    assert capabilities.reversibility is Reversibility.IRREVERSIBLE


def test_registry_exposes_stably_sorted_public_definitions() -> None:
    registry = ToolRegistry((read_definition(),))

    assert tuple(item.name for item in registry.definitions()) == ("observe_generic",)


def test_definition_freezes_its_tool_registry_against_version_drift() -> None:
    item = definition()

    with pytest.raises(ToolRegistryFrozen):
        item.registry.register_read(read_definition())
