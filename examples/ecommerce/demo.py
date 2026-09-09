from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, TypeAdapter

from agentic_saga import SagaDefinition, SagaGoal, SagaRuntime, compose_runtime
from agentic_saga.contracts.actions import AgentProposal, BeginCompensation, Finish, ToolCall
from agentic_saga.contracts.clock import FakeClock
from agentic_saga.contracts.common import (
    Direction,
    JsonObject,
    Reversibility,
    thaw_json_object,
)
from agentic_saga.contracts.events import CompensationIntentRecorded, HumanRequired, LedgerEvent
from agentic_saga.contracts.runtime import (
    AgentDriver,
    ExecutionBudget,
    SagaObservation,
    SagaResult,
    SagaStatus,
    TerminalRequirement,
    ToolDescriptor,
)
from agentic_saga.contracts.tools import (
    EffectToolDefinition,
    ReadToolDefinition,
    ToolCapabilities,
    ToolRegistry,
)
from agentic_saga.contracts.trace import RunTrace
from agentic_saga.evidence.run_trace import RunTraceExporter
from agentic_saga.kernel.compensation import CompensationItem, CompensationPlanner
from agentic_saga.kernel.definitions import DefinitionCatalog
from agentic_saga.kernel.identity import OperationIdentityFactory
from agentic_saga.kernel.invariants import (
    InvariantEvidence,
    InvariantResult,
    TerminalGate,
)
from agentic_saga.kernel.policy import (
    PolicyContext,
    PolicyEngine,
    PolicyRules,
    ProposedEffectIdentity,
    VersionedPolicyRule,
)
from agentic_saga.kernel.ports import Lease
from agentic_saga.kernel.runtime import (
    InvariantEvidenceProvider,
    PolicyContextProvider,
)
from agentic_saga.kernel.state import OperationStatus, SagaSnapshot
from agentic_saga.manifest import SagaContext, load_saga_context
from agentic_saga.storage import SQLiteKernelStore
from examples.ecommerce.domain import (
    CancelFulfillment,
    CapabilityOverride,
    ChargePayment,
    CheckInventory,
    DemoRun,
    EscalationPacket,
    InspectOrder,
    InventoryView,
    OrderView,
    ProviderState,
    RefundPayment,
    ReleaseInventory,
    ReserveInventory,
    ScenarioName,
    ScheduleFulfillment,
)
from examples.ecommerce.provider import (
    EcommerceProvider,
    ProviderEffectAdapter,
    ProviderReadAdapter,
)

if TYPE_CHECKING:
    from examples.ecommerce.evaluation import EvalCase

type _TerminalTarget = Literal[
    "succeeded_verified",
    "compensated_verified",
    "aborted_clean",
    "resolved_with_exception",
]
_JSON: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
_NAMESPACE = b"agentic-saga-ecommerce-v1"
_NOW = datetime(2026, 1, 1, tzinfo=UTC)
type _ClaimIdFactory = Callable[[], str]
_MANIFEST = Path(__file__).with_name("saga.yaml")
_POLICY_CHECKS = ("amount_within_limit", "customer_authorized")
_INVARIANT_CHECKS = (
    "inventory_reserved",
    "order_fulfilled",
    "payment_captured",
    "inventory_released",
    "order_cancelled",
    "payment_refunded",
    "no_external_effects",
)


@dataclass(frozen=True)
class _Assembly:
    runtime: SagaRuntime
    definition: SagaDefinition
    context: SagaContext
    catalog: DefinitionCatalog
    store: SQLiteKernelStore
    provider: EcommerceProvider
    clock: FakeClock
    overrides: tuple[CapabilityOverride, ...]


@dataclass(frozen=True)
class EvalEvidence:
    result: SagaResult
    trace: RunTrace
    context: SagaContext


@dataclass(frozen=True)
class _KernelParts:
    registry: ToolRegistry
    planner: CompensationPlanner
    policy: PolicyEngine
    definition: SagaDefinition
    context: SagaContext


@dataclass(frozen=True)
class _Resources:
    store: SQLiteKernelStore
    provider: EcommerceProvider
    clock: FakeClock


@dataclass(frozen=True)
class _Completion:
    scenario: ScenarioName
    assembly: _Assembly
    result: SagaResult
    proposals: tuple[str, ...]
    restarted: bool


@dataclass(frozen=True)
class _EffectSpec:
    name: str
    model: type[BaseModel]
    compensate_with: str | None
    dependencies: tuple[str, ...] = ()


_EMPTY_USAGE = {
    "turns_used": 0,
    "tool_calls_used": 0,
    "elapsed_ms": 0,
    "tokens_used": 0,
}


# Application policy context and terminal evidence.
@dataclass(frozen=True)
class EcommerceContexts(PolicyContextProvider):
    budget: ExecutionBudget
    registry: ToolRegistry
    planner: CompensationPlanner
    provider: EcommerceProvider

    def build(self, snapshot: SagaSnapshot, proposal: AgentProposal, lease: Lease) -> PolicyContext:
        item = _compensation_item(self, snapshot, proposal)
        return _policy_context(self.budget, proposal, lease, item, self.provider.snapshot())


def _policy_context(
    budget: ExecutionBudget,
    proposal: AgentProposal,
    lease: Lease,
    item: CompensationItem | None,
    state: ProviderState,
) -> PolicyContext:
    values = _policy_binding(proposal, item) | {
        "fence_token": lease.fence_token,
        "budget": budget,
        "policy_evidence": _payment_evidence(state),
        "resource_identity": {"order_id": state.order_id, "customer_id": state.customer_id},
    }
    return PolicyContext.model_validate(values | _EMPTY_USAGE)


def _payment_evidence(state: ProviderState) -> JsonObject:
    return _JSON.validate_python(
        {
            "source": "ecommerce-manifest-v1",
            "order_id": state.order_id,
            "amount_minor": state.order_amount_minor,
            "currency": state.currency,
            "customer_id": state.customer_id,
            "customer_authorized": state.customer_authorized,
            "captured_amount_minor": state.captured_amount_minor,
            "captured_currency": state.captured_currency,
            "captured_customer_id": state.captured_customer_id,
            "inventory_version": state.inventory_version,
        }
    )


def _policy_binding(proposal: AgentProposal, item: CompensationItem | None) -> dict[str, object]:
    return {
        "step_instance_id": _step_id(proposal, item),
        "direction": Direction.FORWARD if item is None else Direction.COMPENSATION,
        "semantic_generation": 0 if item is None else item.semantic_generation,
        "compensates_operation_id": None if item is None else item.forward_operation_id,
    }


def _compensation_item(
    contexts: EcommerceContexts, snapshot: SagaSnapshot, proposal: AgentProposal
) -> CompensationItem | None:
    if snapshot.status is not SagaStatus.COMPENSATING or not isinstance(proposal, ToolCall):
        return None
    frontier = contexts.planner.plan(snapshot, contexts.registry)
    return next(
        (item for item in frontier.runnable if item.tool_name == proposal.tool_name),
        None,
    )


@dataclass(frozen=True)
class EcommerceEvidence(InvariantEvidenceProvider):
    provider: EcommerceProvider

    def evaluate(
        self, saga_id: str, snapshot: SagaSnapshot, target_status: SagaStatus
    ) -> InvariantEvidence:
        results = _invariant_results(target_status, self.provider.snapshot())
        return InvariantEvidence(
            saga_id=saga_id,
            definition_version=snapshot.definition_version,
            evaluated_at_seq=snapshot.seq,
            target_status=target_status,
            invariant_version="ecommerce-invariants-v1",
            results=results,
        )


# Proposal-only deterministic agent used by the offline demo and BDD.
@dataclass
class ScriptedProposalDriver:
    proposals: list[str] = field(default_factory=list)

    async def next_action(
        self, observation: SagaObservation, available_tools: Sequence[ToolDescriptor]
    ) -> AgentProposal:
        public = thaw_json_object(observation.projection)
        snapshot = SagaSnapshot.model_validate(public, strict=False)
        proposal = self._choose(observation, snapshot, available_tools)
        self.proposals.append(_proposal_name(proposal))
        return proposal

    def _choose(
        self,
        observation: SagaObservation,
        snapshot: SagaSnapshot,
        tools: Sequence[ToolDescriptor],
    ) -> AgentProposal:
        if snapshot.status is SagaStatus.COMPENSATING:
            return _compensation_proposal(observation, tools)
        return _forward_proposal(observation, snapshot)


def _step_id(proposal: AgentProposal, item: CompensationItem | None) -> str:
    if item is not None:
        return item.step_instance_id
    name = proposal.tool_name if isinstance(proposal, ToolCall) else proposal.kind
    return f"step_{sha256(f'ecommerce:{name}'.encode()).hexdigest()[:16]}"


def _proposal_name(proposal: AgentProposal) -> str:
    return proposal.tool_name if isinstance(proposal, ToolCall) else proposal.kind


def _proposal_id(observation: SagaObservation, action: str) -> str:
    payload = f"{observation.saga_id}\0{observation.saga_seq}\0{action}".encode()
    return f"proposal_{sha256(payload).hexdigest()[:20]}"


def _tool_call(observation: SagaObservation, name: str, arguments: JsonObject) -> ToolCall:
    return ToolCall(
        proposal_id=_proposal_id(observation, name),
        tool_name=name,
        arguments=arguments,
        based_on_saga_seq=observation.saga_seq,
        rationale="The latest public evidence supports this next reversible step.",
    )


def _finish(observation: SagaObservation, target: SagaStatus) -> Finish:
    return Finish(
        proposal_id=_proposal_id(observation, target.value),
        based_on_saga_seq=observation.saga_seq,
        rationale="Fresh authoritative evidence is ready for deterministic verification.",
        target_status=cast(_TerminalTarget, target.value),
    )


def _begin_compensation(observation: SagaObservation) -> BeginCompensation:
    return BeginCompensation(
        proposal_id=_proposal_id(observation, "begin_compensation"),
        based_on_saga_seq=observation.saga_seq,
        reason_code="fulfillment_rejected",
        rationale="The customer goal failed, so every confirmed effect must be safely unwound.",
    )


def _confirmed_forward_tools(snapshot: SagaSnapshot) -> frozenset[str]:
    return frozenset(
        item.tool_name
        for item in snapshot.operations.values()
        if item.direction is Direction.FORWARD and item.status is OperationStatus.EFFECT_CONFIRMED
    )


def _last_read(observation: SagaObservation, tool: str) -> JsonObject | None:
    action = observation.last_action
    if action is None or action.get("event_type") != "read_observed":
        return None
    result = action.get("redacted_result")
    if action.get("tool_name") != tool or not isinstance(result, Mapping):
        return None
    return cast(JsonObject, result)


def _forward_proposal(observation: SagaObservation, snapshot: SagaSnapshot) -> AgentProposal:
    confirmed = _confirmed_forward_tools(snapshot)
    if "reserve_inventory" not in confirmed:
        return _inventory_proposal(observation)
    if "charge_payment" not in confirmed:
        return _tool_call(observation, "charge_payment", _arguments("charge_payment"))
    if "schedule_fulfillment" not in confirmed:
        return _tool_call(observation, "schedule_fulfillment", _arguments("schedule_fulfillment"))
    return _completion_proposal(observation)


def _inventory_proposal(observation: SagaObservation) -> ToolCall:
    result = _last_read(observation, "check_inventory")
    if result is None or result.get("available") == 0:
        return _tool_call(observation, "check_inventory", _arguments("check_inventory"))
    return _tool_call(observation, "reserve_inventory", _reservation_arguments(observation, result))


def _reservation_arguments(observation: SagaObservation, result: JsonObject) -> JsonObject:
    values = thaw_json_object(_arguments("reserve_inventory")) | {
        "expected_version": result.get("version", 1),
        "quantity": observation.goal.context.get("quantity", 1),
    }
    return _JSON.validate_python(values)


def _completion_proposal(observation: SagaObservation) -> AgentProposal:
    result = _last_read(observation, "inspect_order")
    if result is None:
        return _tool_call(observation, "inspect_order", _arguments("inspect_order"))
    if result.get("fulfillment") == "rejected":
        return _begin_compensation(observation)
    return _finish(observation, SagaStatus.SUCCEEDED_VERIFIED)


def _compensation_proposal(
    observation: SagaObservation, tools: Sequence[ToolDescriptor]
) -> AgentProposal:
    effects = tuple(item.name for item in tools if item.kind == "effect")
    if not effects:
        return _finish(observation, SagaStatus.COMPENSATED_VERIFIED)
    name = effects[0]
    return _tool_call(observation, name, _arguments(name))


def _arguments(tool: str) -> JsonObject:
    common = {"order_id": "order_demo_001"}
    payment = common | {"customer_id": "customer_demo_001", "amount_minor": 7900, "currency": "USD"}
    values: dict[str, object] = {
        "check_inventory": {"sku": "sku_travel_pack", "quantity": 1},
        "inspect_order": common,
        "reserve_inventory": common | {"sku": "sku_travel_pack", "quantity": 1},
        "release_inventory": common | {"sku": "sku_travel_pack", "quantity": 1},
        "charge_payment": payment,
        "refund_payment": payment,
        "schedule_fulfillment": common,
        "cancel_fulfillment": common,
    }
    return _JSON.validate_python(values[tool])


# Typed provider registry and application-owned policy.
def _capabilities(
    reversibility: Reversibility, override: CapabilityOverride | None = None
) -> ToolCapabilities:
    capabilities = ToolCapabilities(
        idempotency_retention_seconds=86_400,
        reconciliation_supported=True,
        cancellation_supported=False,
        fencing_supported=True,
        reversibility=reversibility,
        partial_effects_possible=False,
    )
    if override is None:
        return capabilities
    return capabilities.model_copy(
        update={"idempotency_retention_seconds": override.idempotency_retention_seconds}
    )


def _read_tools(provider: EcommerceProvider) -> tuple[object, ...]:
    inventory: ProviderReadAdapter[CheckInventory, InventoryView] = ProviderReadAdapter(
        provider, "check_inventory", InventoryView
    )
    order: ProviderReadAdapter[InspectOrder, OrderView] = ProviderReadAdapter(
        provider, "inspect_order", OrderView
    )
    return (
        ReadToolDefinition("check_inventory", CheckInventory, InventoryView, inventory),
        ReadToolDefinition("inspect_order", InspectOrder, OrderView, order),
    )


def _effect_tool(
    provider: EcommerceProvider,
    spec: _EffectSpec,
    overrides: tuple[CapabilityOverride, ...],
) -> EffectToolDefinition[BaseModel]:
    reversible = (
        Reversibility.SEMANTIC if spec.compensate_with is not None else Reversibility.IRREVERSIBLE
    )
    return EffectToolDefinition(
        spec.name,
        "ecommerce-effect-v1",
        "ecommerce-command-v1",
        spec.model,
        ProviderEffectAdapter(provider, spec.name),
        _capabilities(reversible, _override(overrides, spec.name)),
        spec.compensate_with,
        compensation_dependencies=spec.dependencies,
    )


def _effect_tools(
    provider: EcommerceProvider, overrides: tuple[CapabilityOverride, ...]
) -> tuple[object, ...]:
    return tuple(_effect_tool(provider, spec, overrides) for spec in _EFFECT_SPECS)


def _override(overrides: tuple[CapabilityOverride, ...], tool: str) -> CapabilityOverride | None:
    return next((item for item in overrides if item.tool_name == tool), None)


_EFFECT_SPECS = (
    _EffectSpec("reserve_inventory", ReserveInventory, "release_inventory"),
    _EffectSpec("charge_payment", ChargePayment, "refund_payment", ("reserve_inventory",)),
    _EffectSpec(
        "schedule_fulfillment", ScheduleFulfillment, "cancel_fulfillment", ("charge_payment",)
    ),
    _EffectSpec("release_inventory", ReleaseInventory, None),
    _EffectSpec("refund_payment", RefundPayment, None),
    _EffectSpec("cancel_fulfillment", CancelFulfillment, None),
)


def build_registry(
    provider: EcommerceProvider, overrides: tuple[CapabilityOverride, ...] = ()
) -> ToolRegistry:
    return ToolRegistry(_read_tools(provider) + _effect_tools(provider, overrides))


def _not_duplicate(
    identity: ProposedEffectIdentity, snapshot: SagaSnapshot, context: PolicyContext
) -> bool:
    del context
    return any(
        operation.direction is identity.direction
        and operation.step_instance_id == identity.step_instance_id
        for operation in snapshot.operations.values()
    )


def _never_approve(*values: object) -> bool:
    del values
    return False


def _requires_exception_approval(
    command: BaseModel, snapshot: SagaSnapshot, context: PolicyContext
) -> bool:
    if isinstance(command, ChargePayment):
        return not _charge_is_authorized(command, context)
    if isinstance(command, ReserveInventory):
        return command.expected_version != _inventory_version(context)
    if isinstance(command, RefundPayment):
        return not (
            _matches_captured_payment(command, context)
            and _refund_matches_forward(command, snapshot, context)
        )
    return False


def _inventory_version(context: PolicyContext) -> int:
    value = context.policy_evidence.get("inventory_version")
    return value if isinstance(value, int) else -1


def _charge_is_authorized(command: BaseModel, context: PolicyContext) -> bool:
    evidence = context.policy_evidence
    actual = command.model_dump(mode="json")
    keys = ("order_id", "amount_minor", "currency", "customer_id")
    return bool(evidence.get("customer_authorized")) and all(
        actual.get(key) == evidence.get(key) for key in keys
    )


def _matches_captured_payment(command: BaseModel, context: PolicyContext) -> bool:
    actual = command.model_dump(mode="json")
    evidence = context.policy_evidence
    pairs = (
        ("order_id", "order_id"),
        ("customer_id", "captured_customer_id"),
        ("amount_minor", "captured_amount_minor"),
        ("currency", "captured_currency"),
    )
    return all(
        actual.get(command_key) == evidence.get(proof_key) for command_key, proof_key in pairs
    )


def _refund_matches_forward(
    command: RefundPayment, snapshot: SagaSnapshot, context: PolicyContext
) -> bool:
    target = context.compensates_operation_id
    forward = None if target is None else snapshot.operations.get(target)
    if forward is None or forward.tool_name != "charge_payment" or not forward.receipts:
        return False
    return _payment_command_matches(command, forward.redacted_command)


def _payment_command_matches(command: BaseModel, expected: JsonObject) -> bool:
    actual = command.model_dump(mode="json")
    keys = ("order_id", "customer_id", "amount_minor", "currency")
    return all(actual.get(key) == expected.get(key) for key in keys)


def _advertise(
    registry: ToolRegistry, planner: CompensationPlanner, tool: str, snapshot: SagaSnapshot
) -> JsonObject | None:
    if snapshot.status is SagaStatus.RUNNING:
        return _running_constraint(registry, tool)
    if snapshot.status is SagaStatus.COMPENSATING:
        return _compensation_constraint(registry, planner, tool, snapshot)
    return None


def _running_constraint(registry: ToolRegistry, tool: str) -> JsonObject | None:
    definition = registry.definition(tool)
    if isinstance(definition, EffectToolDefinition) and definition.compensate_with is None:
        return None
    return _JSON.validate_python({"phase": "forward", "one_action_at_a_time": True})


def _compensation_constraint(
    registry: ToolRegistry,
    planner: CompensationPlanner,
    tool: str,
    snapshot: SagaSnapshot,
) -> JsonObject | None:
    definition = registry.definition(tool)
    if not isinstance(definition, EffectToolDefinition):
        return None
    runnable = frozenset(item.tool_name for item in planner.plan(snapshot, registry).runnable)
    if tool not in runnable:
        return None
    return _JSON.validate_python({"phase": "compensation", "kernel_frontier": True})


@dataclass(frozen=True)
class _AdvertisementPolicy:
    registry: ToolRegistry
    planner: CompensationPlanner

    def definition_identity(self) -> JsonObject:
        return _JSON.validate_python({"policy_version": "ecommerce-advertisement-v1"}, strict=True)

    def __call__(self, tool: str, snapshot: SagaSnapshot) -> JsonObject | None:
        return _advertise(self.registry, self.planner, tool, snapshot)


def _policy(registry: ToolRegistry, planner: CompensationPlanner) -> PolicyEngine:
    rules = PolicyRules(
        is_duplicate_effect=_not_duplicate,
        approval_required=VersionedPolicyRule(
            _requires_exception_approval, "ecommerce-approval-v1"
        ),
        approval_verifier=_never_approve,
        tool_advertisement=_AdvertisementPolicy(registry, planner),
    )
    return PolicyEngine(
        registry=registry,
        identity_factory=planner.identity_factory,
        rules=rules,
    )


def _requirement(names: tuple[str, ...]) -> TerminalRequirement:
    return TerminalRequirement(invariant_version="ecommerce-invariants-v1", required_rule_ids=names)


def _terminal_gate(context: SagaContext) -> TerminalGate:
    checks = context.manifest.checks
    requirements = {
        SagaStatus.SUCCEEDED_VERIFIED: _requirement(checks.success),
        SagaStatus.COMPENSATED_VERIFIED: _requirement(checks.compensation),
        SagaStatus.ABORTED_CLEAN: _requirement(checks.clean_abort),
        SagaStatus.RESOLVED_WITH_EXCEPTION: _requirement(("operator_accepted",)),
    }
    return TerminalGate(requirements)


# Exact authoritative terminal evidence.
def _rule(rule_id: str, passed: bool, value: object) -> InvariantResult:
    return InvariantResult(
        rule_id=rule_id,
        passed=passed,
        inputs=_JSON.validate_python({"source": "ecommerce-provider", "value": value}),
        explanation=f"Authoritative ecommerce state {'passes' if passed else 'fails'} {rule_id}.",
    )


def _invariant_results(target: SagaStatus, state: ProviderState) -> tuple[InvariantResult, ...]:
    clean = _is_clean(state)
    rules = {
        SagaStatus.SUCCEEDED_VERIFIED: _success_rules(state),
        SagaStatus.COMPENSATED_VERIFIED: _compensation_rules(state),
        SagaStatus.ABORTED_CLEAN: (_rule("no_external_effects", clean, clean),),
        SagaStatus.RESOLVED_WITH_EXCEPTION: (_rule("operator_accepted", False, False),),
    }
    return rules[target]


def _success_rules(state: ProviderState) -> tuple[InvariantResult, ...]:
    return (
        _rule("inventory_reserved", state.reserved == state.order_quantity, state.reserved),
        _rule("order_fulfilled", state.fulfillment == "scheduled", state.fulfillment),
        _rule("payment_captured", _captured_exactly(state), _payment_proof(state)),
    )


def _compensation_rules(state: ProviderState) -> tuple[InvariantResult, ...]:
    return (
        _rule("inventory_released", state.reserved == 0, state.reserved),
        _rule("order_cancelled", state.fulfillment == "cancelled", state.fulfillment),
        _rule("payment_refunded", _refunded_exactly(state), _payment_proof(state)),
    )


def _captured_exactly(state: ProviderState) -> bool:
    return (
        state.payment == "captured"
        and state.captured_customer_id == state.customer_id
        and state.captured_amount_minor == state.order_amount_minor
        and state.captured_currency == state.currency
    )


def _refunded_exactly(state: ProviderState) -> bool:
    return (
        state.payment == "refunded"
        and _captured_terms_exact(state)
        and (state.refunded_amount_minor == state.captured_amount_minor)
    )


def _captured_terms_exact(state: ProviderState) -> bool:
    return (
        state.captured_customer_id == state.customer_id
        and state.captured_amount_minor == state.order_amount_minor
        and state.captured_currency == state.currency
    )


def _payment_proof(state: ProviderState) -> JsonObject:
    return _JSON.validate_python(
        {
            "status": state.payment,
            "captured_amount_minor": state.captured_amount_minor,
            "refunded_amount_minor": state.refunded_amount_minor,
            "currency": state.captured_currency,
        }
    )


def _is_clean(state: ProviderState) -> bool:
    return state.reserved == 0 and state.payment == "open" and state.fulfillment == "pending"


def _definition(
    registry: ToolRegistry, policy: PolicyEngine, context: SagaContext
) -> SagaDefinition:
    checks = context.manifest.checks
    return SagaDefinition(
        name=context.manifest.name,
        version=context.manifest.version,
        registry=registry,
        policy=policy,
        success_invariants=_requirement(checks.success),
        compensation_invariants=_requirement(checks.compensation),
        clean_abort_invariants=_requirement(checks.clean_abort),
        exception_invariants=_requirement(("operator_accepted",)),
        budget=context.budget,
    )


# Explicit composition of the generic runtime Lego pieces.
def _assembly(
    store: SQLiteKernelStore,
    provider: EcommerceProvider,
    clock: FakeClock,
    overrides: tuple[CapabilityOverride, ...] = (),
) -> _Assembly:
    registry = build_registry(provider, overrides)
    planner = CompensationPlanner(OperationIdentityFactory(_NAMESPACE))
    policy = _policy(registry, planner)
    context = _saga_context(registry)
    definition = _definition(registry, policy, context)
    parts = _KernelParts(registry, planner, policy, definition, context)
    return _services(_Resources(store, provider, clock), parts, overrides)


def _saga_context(registry: ToolRegistry) -> SagaContext:
    return load_saga_context(
        _MANIFEST,
        registry=registry,
        policy_checks=_POLICY_CHECKS,
        invariant_checks=_INVARIANT_CHECKS,
    )


def _services(
    resources: _Resources, parts: _KernelParts, overrides: tuple[CapabilityOverride, ...]
) -> _Assembly:
    catalog = DefinitionCatalog((parts.definition,))
    runtime = _ecommerce_runtime(resources, parts)
    return _Assembly(
        runtime,
        parts.definition,
        parts.context,
        catalog,
        resources.store,
        resources.provider,
        resources.clock,
        overrides,
    )


def _ecommerce_runtime(resources: _Resources, parts: _KernelParts) -> SagaRuntime:
    return compose_runtime(
        store=resources.store,
        definition=parts.definition,
        policy_context_provider=EcommerceContexts(
            parts.context.budget, parts.registry, parts.planner, resources.provider
        ),
        terminal_gate=_terminal_gate(parts.context),
        invariant_evidence_provider=EcommerceEvidence(resources.provider),
        clock=resources.clock,
        worker_id="ecommerce-demo-runtime",
        id_namespace=_NAMESPACE,
    )


def _goal(objective: str) -> SagaGoal:
    return SagaGoal(
        goal_id="goal_ecommerce_demo_001",
        text=objective,
        context={"order_id": "order_demo_001", "sku": "sku_travel_pack", "quantity": 1},
    )


def prepare_eval_case(case: EvalCase, directory: Path, clock: FakeClock) -> _Assembly:
    fixture = case.fixture
    directory.mkdir(parents=True, exist_ok=True)
    provider = EcommerceProvider.initialize_fixture(
        directory / "provider.db",
        clock,
        fixture.provider_state,
        fixture.faults,
    )
    store = SQLiteKernelStore.initialize(directory / "saga.db", clock=clock)
    return _assembly(store, provider, clock, fixture.capability_overrides)


async def run_with_agent(assembly: _Assembly, case: EvalCase, agent: AgentDriver) -> EvalEvidence:
    result = await assembly.runtime.start(
        definition=assembly.definition,
        goal=_eval_goal(case),
        agent=agent,
    )
    assembly, result = await _resume_eval(assembly, result, agent)
    trace = RunTraceExporter(assembly.store, assembly.catalog).export(result.saga_id)
    return EvalEvidence(result, trace, assembly.context)


def _eval_goal(case: EvalCase) -> SagaGoal:
    context = _eval_context(case)
    return SagaGoal(
        goal_id=f"goal_{case.case_id}", text=case.goal, context=_JSON.validate_python(context)
    )


def _eval_context(case: EvalCase) -> dict[str, object]:
    state = case.fixture.provider_state
    return {
        "order_id": state.order_id,
        "customer_id": state.customer_id,
        "sku": state.sku,
        "quantity": state.order_quantity,
        "amount_minor": state.order_amount_minor,
        "currency": state.currency,
        "alternate_warehouse_id": state.alternate_warehouse_id,
        "capability_overrides": [
            item.model_dump(mode="json") for item in case.fixture.capability_overrides
        ],
    }


async def _resume_eval(
    assembly: _Assembly, result: SagaResult, agent: AgentDriver
) -> tuple[_Assembly, SagaResult]:
    if result.state is not SagaStatus.RECONCILING_UNKNOWN:
        return assembly, result
    assembly.clock.advance(timedelta(minutes=6))
    reopened = _reopen(assembly, assembly.clock)
    resumed = await reopened.runtime.resume(saga_id=result.saga_id, agent=agent)
    return reopened, resumed


# Scenario lifecycle, durable restart, and evidence export.
async def run_scenario(
    name: str, directory: Path, *, claim_id_factory: _ClaimIdFactory | None = None
) -> DemoRun:
    scenario = ScenarioName(name)
    clock = FakeClock(_NOW)
    assembly = await asyncio.to_thread(_initialize, directory, scenario, clock, claim_id_factory)
    driver = ScriptedProposalDriver()
    result = await assembly.runtime.start(
        definition=assembly.definition,
        goal=_goal(assembly.context.manifest.objective),
        agent=driver,
    )
    if scenario is not ScenarioName.LOST_RESPONSE:
        completed = _Completion(scenario, assembly, result, tuple(driver.proposals), False)
        return _demo_run(completed)
    return await _resume_lost_response(assembly, result, driver.proposals, clock, claim_id_factory)


def _initialize(
    directory: Path,
    scenario: ScenarioName,
    clock: FakeClock,
    claim_id_factory: _ClaimIdFactory | None = None,
) -> _Assembly:
    directory.mkdir(parents=True, exist_ok=True)
    provider = EcommerceProvider.initialize(directory / "provider.db", clock, scenario)
    store = _initialize_store(directory / "saga.db", clock, claim_id_factory)
    return _assembly(store, provider, clock)


def _initialize_store(
    path: Path, clock: FakeClock, claim_id_factory: _ClaimIdFactory | None
) -> SQLiteKernelStore:
    if claim_id_factory is None:
        return SQLiteKernelStore.initialize(path, clock=clock)
    return SQLiteKernelStore.initialize(path, claim_id_factory=claim_id_factory, clock=clock)


async def _resume_lost_response(
    assembly: _Assembly,
    result: SagaResult,
    proposals: list[str],
    clock: FakeClock,
    claim_id_factory: _ClaimIdFactory | None,
) -> DemoRun:
    if result.state is not SagaStatus.RECONCILING_UNKNOWN:
        return _lost_run(assembly, result, proposals, False)
    clock.advance(timedelta(minutes=6))
    reopened = _reopen(assembly, clock, claim_id_factory)
    driver = ScriptedProposalDriver()
    resumed = await reopened.runtime.resume(saga_id=result.saga_id, agent=driver)
    return _lost_run(reopened, resumed, proposals + driver.proposals, True)


def _reopen(
    assembly: _Assembly,
    clock: FakeClock,
    claim_id_factory: _ClaimIdFactory | None = None,
) -> _Assembly:
    store = _open_store(assembly.store.path, clock, claim_id_factory)
    provider = EcommerceProvider.open(assembly.provider.path, clock)
    return _assembly(store, provider, clock, assembly.overrides)


def _open_store(
    path: Path, clock: FakeClock, claim_id_factory: _ClaimIdFactory | None
) -> SQLiteKernelStore:
    if claim_id_factory is None:
        return SQLiteKernelStore.open(path, clock=clock)
    return SQLiteKernelStore.open(path, claim_id_factory=claim_id_factory, clock=clock)


def _lost_run(
    assembly: _Assembly, result: SagaResult, proposals: list[str], restarted: bool
) -> DemoRun:
    completed = _Completion(
        ScenarioName.LOST_RESPONSE, assembly, result, tuple(proposals), restarted
    )
    return _demo_run(completed)


def _demo_run(completed: _Completion) -> DemoRun:
    assembly = completed.assembly
    events = assembly.store.read_events(completed.result.saga_id)
    trace = RunTraceExporter(assembly.store, assembly.catalog).export(completed.result.saga_id)
    return DemoRun(
        scenario=completed.scenario,
        result=completed.result,
        trace=trace,
        proposals=completed.proposals,
        compensation_tools=_compensation_tools(events),
        counts=assembly.provider.counts(),
        restarted=completed.restarted,
        escalation=_escalation(completed.result, events, assembly.store),
    )


def _compensation_tools(events: tuple[LedgerEvent, ...]) -> tuple[str, ...]:
    return tuple(
        event.tool_name for event in events if isinstance(event, CompensationIntentRecorded)
    )


def _escalation(
    result: SagaResult,
    events: tuple[LedgerEvent, ...],
    store: SQLiteKernelStore,
) -> EscalationPacket | None:
    event = _human_event(events)
    if event is None:
        return None
    snapshot = store.load_snapshot(result.saga_id)
    return _packet(result, event, snapshot)


def _packet(result: SagaResult, event: HumanRequired, snapshot: SagaSnapshot) -> EscalationPacket:
    return EscalationPacket(
        saga_id=result.saga_id,
        reason_code=event.reason_code,
        last_sequence=snapshot.seq,
        unresolved_operation_ids=_unresolved_operations(snapshot),
        recommended_action="Inspect the cited provider operation and record verified evidence.",
    )


def _human_event(events: tuple[LedgerEvent, ...]) -> HumanRequired | None:
    return next((item for item in reversed(events) if isinstance(item, HumanRequired)), None)


def _unresolved_operations(snapshot: SagaSnapshot) -> tuple[str, ...]:
    return tuple(
        item.operation_id
        for item in snapshot.operations.values()
        if item.status is OperationStatus.OUTCOME_UNKNOWN
    )
