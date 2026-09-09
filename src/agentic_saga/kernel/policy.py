from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from inspect import getclosurevars, getsource
from textwrap import dedent
from typing import Annotated, Protocol, cast, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, ValidationError

from agentic_saga.contracts.actions import (
    AgentProposal,
    AuthorizedToolCall,
    BeginCompensation,
    Escalate,
    Finish,
    HumanDecision,
    ToolCall,
)
from agentic_saga.contracts.common import (
    Direction,
    FenceToken,
    JsonObject,
    OperationId,
    StepInstanceId,
    sha256_json,
    thaw_json_object,
)
from agentic_saga.contracts.redaction import RedactionPolicy, contains_sensitive_json, redact_json
from agentic_saga.contracts.runtime import ExecutionBudget as _ExecutionBudget
from agentic_saga.contracts.runtime import SagaStatus
from agentic_saga.contracts.tools import (
    EffectToolDefinition,
    ReadToolDefinition,
    ToolRegistry,
    UnknownToolError,
)
from agentic_saga.kernel.identity import OperationIdentityFactory
from agentic_saga.kernel.state import ObligationStatus, OperationStatus, SagaSnapshot

type _HashDigest = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-f0-9]{64}$")]
type _ReasonCode = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_]*$")]
type _Explanation = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=300)]
_MAX_BEHAVIOR_VERSION_LENGTH = 200


class StaleProposal(ValueError):
    """Raised internally when a proposal is not bound to the current Saga sequence."""


class _PolicyDenied(Exception):
    def __init__(self, decision: PolicyDecision) -> None:
        super().__init__(decision.code)
        self.decision = decision


class PolicyContext(BaseModel):
    """Deterministic kernel-owned inputs for one authorization decision."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    step_instance_id: StepInstanceId
    direction: Direction
    semantic_generation: int = Field(strict=True, ge=0)
    fence_token: FenceToken
    budget: _ExecutionBudget
    turns_used: int = Field(strict=True, ge=0)
    tool_calls_used: int = Field(strict=True, ge=0)
    elapsed_ms: int = Field(strict=True, ge=0)
    tokens_used: int = Field(strict=True, ge=0)
    policy_evidence: JsonObject
    resource_identity: JsonObject
    compensates_operation_id: OperationId | None = None
    approval: HumanDecision | None = None
    used_approval_ids: tuple[str, ...] = ()


type PolicyRule = Callable[[BaseModel, SagaSnapshot, PolicyContext], bool]
type DuplicateEffectRule = Callable[[ProposedEffectIdentity, SagaSnapshot, PolicyContext], bool]
type ApprovalVerifier = Callable[[HumanDecision, ToolCall, SagaSnapshot, PolicyContext], bool]
type ToolAdvertisementRule = Callable[[str, SagaSnapshot], JsonObject | None]


@runtime_checkable
class _PolicyIdentityProvider(Protocol):
    def definition_identity(self) -> JsonObject: ...


@dataclass(frozen=True)
class VersionedPolicyRule[**P, R]:
    """Bind transitive policy behavior to an explicit public version."""

    behavior: Callable[P, R]
    behavior_version: str

    def __post_init__(self) -> None:
        if not callable(self.behavior):
            raise TypeError("policy behavior must be callable")
        if type(self.behavior_version) is not str:
            raise ValueError("policy behavior_version must contain between 1 and 200 characters")
        if not 1 <= len(self.behavior_version) <= _MAX_BEHAVIOR_VERSION_LENGTH:
            raise ValueError("policy behavior_version must contain between 1 and 200 characters")

    def definition_identity(self) -> JsonObject:
        payload = {
            "behavior_version": self.behavior_version,
            "behavior": _direct_callable_identity(self.behavior),
        }
        return _public_policy_config(payload)

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R:
        return self.behavior(*args, **kwargs)


def _deny_tool_advertisement(tool_name: str, snapshot: SagaSnapshot) -> JsonObject | None:
    del tool_name, snapshot
    return None


def _callable_name(value: object) -> str:
    target = type(value) if isinstance(value, _PolicyIdentityProvider) else value
    module: object = getattr(target, "__module__", None)
    qualname: object = getattr(target, "__qualname__", None)
    if not isinstance(module, str) or not isinstance(qualname, str):
        raise ValueError("policy callables must expose stable qualified names")
    return f"{module}.{qualname}"


def _direct_callable_identity(value: object) -> JsonObject:
    target = type(value) if isinstance(value, _PolicyIdentityProvider) else value
    try:
        source = dedent(getsource(cast(Callable[..., object], target))).strip().encode("utf-8")
    except (OSError, TypeError) as error:
        raise ValueError("policy callables must expose stable source") from error
    payload = {
        "qualified_name": _callable_name(value),
        "source_hash": sha256(source).hexdigest(),
    }
    return _JSON_OBJECT_ADAPTER.validate_python(payload, strict=True)


def _callable_identity(value: object) -> JsonObject:
    payload = {
        "behavior": _direct_callable_identity(value),
        "config": _callable_config(value),
    }
    return _JSON_OBJECT_ADAPTER.validate_python(payload, strict=True)


def _callable_config(value: object) -> JsonObject:
    owner = _bound_owner(value)
    if owner is not None:
        if not isinstance(owner, _PolicyIdentityProvider):
            raise ValueError("bound policy owners must expose definition_identity")
        payload = {"owner": _stable_policy_identity(owner), "defaults": _callable_defaults(value)}
        return _public_policy_config(payload)
    if isinstance(value, _PolicyIdentityProvider):
        return _stable_policy_identity(value)
    return _callable_closure(value)


def _bound_owner(value: object) -> object | None:
    owner = getattr(value, "__self__", None)
    return None if owner is None or isinstance(owner, type) else owner


def _stable_policy_identity(value: _PolicyIdentityProvider) -> JsonObject:
    first = _public_policy_identity(value)
    if first != _public_policy_identity(value):
        raise ValueError("policy definition_identity must be deterministic")
    return first


def _public_policy_identity(value: _PolicyIdentityProvider) -> JsonObject:
    try:
        identity = _JSON_OBJECT_ADAPTER.validate_python(value.definition_identity(), strict=True)
    except (TypeError, ValueError, ValidationError):
        raise ValueError("policy definition_identity must be stable public JSON") from None
    if contains_sensitive_json(identity, RedactionPolicy()):
        raise ValueError("policy definition_identity must contain only public values")
    return identity


def _callable_closure(value: object) -> JsonObject:
    try:
        variables = getclosurevars(cast(Callable[..., object], value))
    except TypeError:
        raise ValueError("policy closure configuration must be stable public JSON") from None
    payload = {
        "nonlocals": variables.nonlocals,
        "defaults": _callable_defaults(value),
        "globals": _public_json_globals(variables.globals),
    }
    return _public_policy_config(payload)


def _callable_defaults(value: object) -> JsonObject:
    payload = {
        "positional": list(getattr(value, "__defaults__", None) or ()),
        "keyword": getattr(value, "__kwdefaults__", None) or {},
    }
    return _public_policy_config(payload)


def _public_json_globals(values: Mapping[str, object]) -> JsonObject:
    public: dict[str, object] = {}
    for name, value in values.items():
        try:
            public[name] = _JSON_OBJECT_ADAPTER.validate_python({name: value}, strict=True)[name]
        except ValidationError:
            raise ValueError(
                "policy rules with transitive behavior must use VersionedPolicyRule"
            ) from None
    return _public_policy_config(public)


def _public_policy_config(value: object) -> JsonObject:
    try:
        validated = _JSON_OBJECT_ADAPTER.validate_python(value, strict=True)
    except ValidationError:
        raise ValueError("policy configuration must be stable public JSON") from None
    if contains_sensitive_json(validated, RedactionPolicy()):
        raise ValueError("policy configuration must contain only public values")
    return validated


class ProposedEffectIdentity(BaseModel):
    """Bind an effect proposal to its semantic operation and canonical command."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    tool_name: str
    direction: Direction
    step_instance_id: StepInstanceId
    semantic_generation: int = Field(strict=True, ge=0)
    command_digest: _HashDigest
    resource_identity: JsonObject
    compensates_operation_id: OperationId | None = None


class ConsumedApproval(BaseModel):
    """Carry verified human approval evidence into an authorization decision."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    decision_id: str
    actor: str
    proposal_hash: _HashDigest
    verification_result: bool


@dataclass(frozen=True)
class PolicyRules:
    """Domain-neutral duplicate, approval, and advertisement policies."""

    is_duplicate_effect: DuplicateEffectRule
    approval_required: PolicyRule
    approval_verifier: ApprovalVerifier
    tool_advertisement: ToolAdvertisementRule = _deny_tool_advertisement


class PolicyDecision(BaseModel):
    """Return a deterministic allow-or-deny result with any authorized call."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    allowed: bool
    code: _ReasonCode
    explanation: _Explanation
    authorized_call: AuthorizedToolCall[BaseModel] | None
    fence_token: FenceToken | None = None
    effect_identity: ProposedEffectIdentity | None = None
    consumed_approval: ConsumedApproval | None = None


@dataclass(frozen=True)
class _BudgetCheck:
    used: Callable[[PolicyContext], int]
    limit: Callable[[_ExecutionBudget], int]
    code: str
    explanation: str


@dataclass(frozen=True)
class _Authorization:
    proposal: ToolCall
    command: BaseModel
    snapshot: SagaSnapshot
    context: PolicyContext
    identity: ProposedEffectIdentity


_TERMINAL_STATUSES = frozenset(
    {
        SagaStatus.SUCCEEDED_VERIFIED,
        SagaStatus.COMPENSATED_VERIFIED,
        SagaStatus.ABORTED_CLEAN,
        SagaStatus.RESOLVED_WITH_EXCEPTION,
    }
)
_CONFLICT_STATUSES = frozenset(
    {
        OperationStatus.PLANNED,
        OperationStatus.INTENT_DURABLE,
        OperationStatus.DISPATCHED,
    }
)
_ESCALATION_SOURCES = frozenset(
    {
        SagaStatus.RUNNING,
        SagaStatus.RECONCILING_UNKNOWN,
        SagaStatus.RECOVERY_PLAN_REQUIRED,
        SagaStatus.COMPENSATING,
    }
)
_TERMINAL_TARGETS: Mapping[SagaStatus, frozenset[SagaStatus]] = {
    SagaStatus.RUNNING: frozenset({SagaStatus.SUCCEEDED_VERIFIED, SagaStatus.ABORTED_CLEAN}),
    SagaStatus.COMPENSATING: frozenset({SagaStatus.COMPENSATED_VERIFIED}),
    SagaStatus.HUMAN_REQUIRED: frozenset({SagaStatus.RESOLVED_WITH_EXCEPTION}),
}
_COMPENSABLE_STATUSES = frozenset(
    {OperationStatus.EFFECT_CONFIRMED, OperationStatus.PARTIAL_EFFECT_CONFIRMED}
)
_JSON_OBJECT_ADAPTER: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)


def _denied(code: str, explanation: str) -> PolicyDecision:
    return PolicyDecision(
        allowed=False,
        code=code,
        explanation=explanation,
        authorized_call=None,
    )


def _require_current(proposal: AgentProposal, snapshot: SagaSnapshot) -> None:
    if proposal.based_on_saga_seq != snapshot.seq:
        raise StaleProposal("proposal sequence is stale")


def _common_denial(proposal: AgentProposal, snapshot: SagaSnapshot) -> PolicyDecision | None:
    if snapshot.status in _TERMINAL_STATUSES:
        return _denied("immutable_saga", "The Saga is already terminal and cannot change.")
    try:
        _require_current(proposal, snapshot)
    except StaleProposal:
        return _denied("stale_proposal", "The proposal is not based on the current Saga state.")
    return None


def _control_decision() -> PolicyDecision:
    return PolicyDecision(
        allowed=True,
        code="control_proposal",
        explanation="The control proposal requires its dedicated deterministic kernel gate.",
        authorized_call=None,
    )


def _read_decision() -> PolicyDecision:
    return PolicyDecision(
        allowed=True,
        code="read_authorized",
        explanation="The read passed deterministic policy boundaries.",
        authorized_call=None,
    )


def _phase_denied() -> PolicyDecision:
    return _denied("saga_phase_denied", "The Saga phase does not permit this effect.")


def _target_denied() -> PolicyDecision:
    return _denied(
        "compensation_target_denied",
        "The compensation target is not currently eligible.",
    )


def _control_denial(proposal: AgentProposal, snapshot: SagaSnapshot) -> PolicyDecision | None:
    if isinstance(proposal, ToolCall):
        return None
    if isinstance(proposal, BeginCompensation):
        return _compensation_control_denial(snapshot)
    if not _control_allowed(proposal, snapshot):
        return _phase_denied()
    return None


def _compensation_control_denial(snapshot: SagaSnapshot) -> PolicyDecision | None:
    if snapshot.status is not SagaStatus.RUNNING:
        return _phase_denied()
    if _unknown_conflict(snapshot) or _inflight_conflict(snapshot):
        message = "External effects must settle before compensation."
        return _denied("compensation_not_ready", message)
    if not _has_eligible_compensation(snapshot):
        return _denied("compensation_not_ready", "No verified compensation is eligible.")
    return None


def _has_eligible_compensation(snapshot: SagaSnapshot) -> bool:
    return any(item.status is ObligationStatus.ELIGIBLE for item in snapshot.obligations.values())


def _control_allowed(proposal: Finish | Escalate, snapshot: SagaSnapshot) -> bool:
    if isinstance(proposal, Escalate):
        return snapshot.status in _ESCALATION_SOURCES
    targets = _TERMINAL_TARGETS.get(snapshot.status, frozenset())
    return SagaStatus(proposal.target_status) in targets


def _callback_denied() -> PolicyDecision:
    return _denied("policy_callback_error", "An injected policy callback failed safely.")


def _callback_result[**P](
    callback: Callable[P, bool], *args: P.args, **kwargs: P.kwargs
) -> bool | PolicyDecision:
    try:
        result = callback(*args, **kwargs)
    except Exception:
        return _callback_denied()
    if type(result) is not bool:
        raise TypeError("policy callbacks must return a boolean")
    return result


def _unknown_conflict(snapshot: SagaSnapshot) -> PolicyDecision | None:
    unknown = any(
        operation.status is OperationStatus.OUTCOME_UNKNOWN
        for operation in snapshot.operations.values()
    )
    if unknown:
        return _denied("unknown_operation", "An unknown operation must be reconciled first.")
    return None


def _inflight_conflict(snapshot: SagaSnapshot) -> PolicyDecision | None:
    inflight = any(
        operation.status in _CONFLICT_STATUSES for operation in snapshot.operations.values()
    )
    if inflight:
        return _denied("operation_inflight", "An in-flight operation must settle first.")
    return None


def _approval_matches(
    decision: HumanDecision,
    proposal: ToolCall,
    snapshot: SagaSnapshot,
    proposal_hash: str,
) -> bool:
    return (
        decision.saga_id == snapshot.saga_id
        and decision.based_on_saga_seq == proposal.based_on_saga_seq
        and decision.proposal_hash == proposal_hash
        and decision.action == "approve"
    )


def _canonical_digest(value: JsonObject) -> str:
    return sha256_json(value)


def _approval_material(request: _Authorization) -> JsonObject:
    proposal = request.proposal.model_dump(mode="json")
    return _JSON_OBJECT_ADAPTER.validate_python(
        {
            "schema_version": "1.0",
            "saga_id": request.snapshot.saga_id,
            "definition_version": request.snapshot.definition_version,
            "proposal": proposal,
            "step_instance_id": request.context.step_instance_id,
            "direction": request.context.direction.value,
            "semantic_generation": request.context.semantic_generation,
            "command": request.command.model_dump(mode="json"),
            "effect_identity": request.identity.model_dump(mode="json"),
        }
    )


def _proposal_hash(request: _Authorization) -> str:
    return _canonical_digest(_approval_material(request))


def _eligible_obligation(request: ToolCall, snapshot: SagaSnapshot, target: OperationId) -> bool:
    obligation = snapshot.obligations.get(target)
    if obligation is None or obligation.status is not ObligationStatus.ELIGIBLE:
        return False
    if obligation.forward_operation_id != target:
        return False
    return obligation.compensation_tool_name == request.tool_name


def _confirmed_forward(snapshot: SagaSnapshot, target: OperationId) -> bool:
    operation = snapshot.operations.get(target)
    if operation is None or operation.direction is not Direction.FORWARD:
        return False
    if operation.operation_id != target:
        return False
    return operation.status in _COMPENSABLE_STATUSES


def _forward_phase_denial(snapshot: SagaSnapshot, context: PolicyContext) -> PolicyDecision | None:
    if snapshot.status is not SagaStatus.RUNNING:
        return _phase_denied()
    if context.compensates_operation_id is not None:
        return _target_denied()
    return None


def _compensation_phase_denial(
    proposal: ToolCall, snapshot: SagaSnapshot, context: PolicyContext
) -> PolicyDecision | None:
    if snapshot.status is not SagaStatus.COMPENSATING:
        return _phase_denied()
    target = context.compensates_operation_id
    if target is None:
        return _target_denied()
    if not _eligible_obligation(proposal, snapshot, target):
        return _target_denied()
    if not _confirmed_forward(snapshot, target):
        return _target_denied()
    return None


def _effect_phase_denial(
    proposal: ToolCall, snapshot: SagaSnapshot, context: PolicyContext
) -> PolicyDecision | None:
    if context.direction is Direction.FORWARD:
        return _forward_phase_denial(snapshot, context)
    return _compensation_phase_denial(proposal, snapshot, context)


_BUDGET_CHECKS = (
    _BudgetCheck(
        lambda item: item.turns_used,
        lambda item: item.turn_limit,
        "turn_budget_exhausted",
        "The turn budget is exhausted.",
    ),
    _BudgetCheck(
        lambda item: item.tool_calls_used,
        lambda item: item.tool_call_limit,
        "tool_budget_exhausted",
        "The tool-call budget is exhausted.",
    ),
    _BudgetCheck(
        lambda item: item.elapsed_ms,
        lambda item: item.elapsed_ms_limit,
        "time_budget_exhausted",
        "The elapsed-time budget is exhausted.",
    ),
    _BudgetCheck(
        lambda item: item.tokens_used,
        lambda item: item.token_limit,
        "token_budget_exhausted",
        "The token budget is exhausted.",
    ),
)


def _consumed_approval(decision: HumanDecision, proposal_hash: str) -> ConsumedApproval:
    return ConsumedApproval(
        decision_id=decision.decision_id,
        actor=decision.actor,
        proposal_hash=proposal_hash,
        verification_result=True,
    )


class PolicyEngine:
    """Authorize agent proposals against tools, saga state, budgets, and approvals."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        identity_factory: OperationIdentityFactory,
        rules: PolicyRules,
    ) -> None:
        self._registry = registry
        self._identity_factory = identity_factory
        self._rules = rules

    @property
    def registry(self) -> ToolRegistry:
        """Return the exact registry this policy authorizes against."""
        return self._registry

    def definition_identity(self) -> JsonObject:
        """Return public digests that pin deterministic policy behavior and identity framing."""
        rules = {
            "is_duplicate_effect": _callable_identity(self._rules.is_duplicate_effect),
            "approval_required": _callable_identity(self._rules.approval_required),
            "approval_verifier": _callable_identity(self._rules.approval_verifier),
            "tool_advertisement": _callable_identity(self._rules.tool_advertisement),
        }
        payload = {
            "engine": f"{type(self).__module__}.{type(self).__qualname__}",
            "identity_namespace_hash": sha256(self._identity_factory.namespace).hexdigest(),
            "rules": rules,
        }
        return _JSON_OBJECT_ADAPTER.validate_python(payload, strict=True)

    def authorize(
        self,
        proposal: AgentProposal,
        snapshot: SagaSnapshot,
        context: PolicyContext,
    ) -> PolicyDecision:
        common = _common_denial(proposal, snapshot)
        if common is not None:
            return common
        control = _control_denial(proposal, snapshot)
        if control is not None:
            return control
        if not isinstance(proposal, ToolCall):
            return _control_decision()
        phase = _effect_phase_denial(proposal, snapshot, context)
        if phase is not None:
            return phase
        return self._authorize_tool(proposal, snapshot, context)

    def advertised_constraints(self, tool_name: str, snapshot: object) -> JsonObject | None:
        saga_snapshot = cast(SagaSnapshot, snapshot)
        try:
            raw = self._rules.tool_advertisement(tool_name, saga_snapshot)
            if raw is None:
                return None
            constraints = _JSON_OBJECT_ADAPTER.validate_python(raw, strict=True)
            public = thaw_json_object(constraints)
            if redact_json(public, RedactionPolicy()) != public:
                return None
            return constraints
        except Exception:
            return None

    def authorize_read(
        self,
        proposal: ToolCall,
        snapshot: SagaSnapshot,
        context: PolicyContext,
    ) -> PolicyDecision:
        common = _common_denial(proposal, snapshot)
        if common is not None:
            return common
        command = self._read_command(proposal)
        if isinstance(command, PolicyDecision):
            return command
        return self._budget_denial(context) or _read_decision()

    def _read_command(self, proposal: ToolCall) -> BaseModel | PolicyDecision:
        try:
            definition = self._registry.definition(proposal.tool_name)
        except UnknownToolError:
            return _denied("unknown_tool", "The requested tool is not registered.")
        if not isinstance(definition, ReadToolDefinition):
            return _denied("effect_only_tool", "The requested tool is not read-only.")
        try:
            return definition.input_model.model_validate(
                thaw_json_object(proposal.arguments), strict=True
            )
        except ValidationError:
            return _denied("invalid_command", "The read did not match its strict schema.")

    def _authorize_tool(
        self,
        proposal: ToolCall,
        snapshot: SagaSnapshot,
        context: PolicyContext,
    ) -> PolicyDecision:
        try:
            definition = self._effect_definition(proposal.tool_name)
            command = self._validated_command(proposal, definition)
            identity = self._effect_identity(proposal, command, context)
            request = _Authorization(proposal, command, snapshot, context, identity)
            return self._authorize_command(request)
        except _PolicyDenied as denied:
            return denied.decision

    def _effect_definition(self, name: str) -> EffectToolDefinition[BaseModel]:
        try:
            definition = self._registry.definition(name)
        except UnknownToolError as error:
            decision = _denied("unknown_tool", "The requested tool is not registered.")
            raise _PolicyDenied(decision) from error
        if not isinstance(definition, EffectToolDefinition):
            decision = _denied("read_only_tool", "Read-only tools cannot authorize an effect.")
            raise _PolicyDenied(decision)
        return definition

    def _validated_command(
        self,
        proposal: ToolCall,
        definition: EffectToolDefinition[BaseModel],
    ) -> BaseModel:
        try:
            raw = thaw_json_object(proposal.arguments)
            return definition.input_model.model_validate(raw)
        except ValidationError as error:
            decision = _denied("invalid_command", "The command did not match its strict schema.")
            raise _PolicyDenied(decision) from error

    def _effect_identity(
        self, proposal: ToolCall, command: BaseModel, context: PolicyContext
    ) -> ProposedEffectIdentity:
        payload = self._command_payload(command)
        return ProposedEffectIdentity(
            tool_name=proposal.tool_name,
            direction=context.direction,
            step_instance_id=context.step_instance_id,
            semantic_generation=context.semantic_generation,
            command_digest=_canonical_digest(payload),
            resource_identity=context.resource_identity,
            compensates_operation_id=context.compensates_operation_id,
        )

    def _command_payload(self, command: BaseModel) -> JsonObject:
        try:
            return _JSON_OBJECT_ADAPTER.validate_python(command.model_dump(mode="json"))
        except ValidationError as error:
            decision = _denied("invalid_command", "The command did not produce strict JSON.")
            raise _PolicyDenied(decision) from error

    def _authorize_command(self, request: _Authorization) -> PolicyDecision:
        denial = self._conflict_denial(request.snapshot)
        if denial is not None:
            return denial
        return self._authorize_after_conflict(request)

    def _authorize_after_conflict(self, request: _Authorization) -> PolicyDecision:
        denial = self._duplicate_denial(request)
        if denial is not None:
            return denial
        return self._authorize_after_duplicate(request)

    def _authorize_after_duplicate(self, request: _Authorization) -> PolicyDecision:
        consumed, denial = self._approval_result(request)
        if denial is not None:
            return denial
        return self._authorize_after_approval(request, consumed)

    def _authorize_after_approval(
        self, request: _Authorization, consumed: ConsumedApproval | None
    ) -> PolicyDecision:
        denial = self._budget_denial(request.context)
        if denial is not None:
            return denial
        return self._authorized(request, consumed)

    def _conflict_denial(self, snapshot: SagaSnapshot) -> PolicyDecision | None:
        denial = _unknown_conflict(snapshot)
        denial = denial or _inflight_conflict(snapshot)
        if denial is not None:
            return denial
        if snapshot.pending_approval:
            return _denied("pending_human_approval", "Human approval is still pending.")
        return None

    def _duplicate_denial(self, request: _Authorization) -> PolicyDecision | None:
        if request.context.direction is Direction.COMPENSATION:
            return None
        duplicate = _callback_result(
            self._rules.is_duplicate_effect, request.identity, request.snapshot, request.context
        )
        if isinstance(duplicate, PolicyDecision):
            return duplicate
        if duplicate:
            return _denied(
                "duplicate_business_effect",
                "The semantic business effect is already represented.",
            )
        return None

    def _approval_result(
        self, request: _Authorization
    ) -> tuple[ConsumedApproval | None, PolicyDecision | None]:
        required = _callback_result(
            self._rules.approval_required, request.command, request.snapshot, request.context
        )
        if isinstance(required, PolicyDecision):
            return None, required
        if not required:
            return None, None
        return self._required_approval_result(request)

    def _required_approval_result(
        self, request: _Authorization
    ) -> tuple[ConsumedApproval | None, PolicyDecision | None]:
        decision = request.context.approval
        if decision is None:
            denial = _denied("approval_required", "A verified approval is required.")
            return None, denial
        if decision.decision_id in request.context.used_approval_ids:
            denial = _denied("approval_already_used", "The approval was already consumed.")
            return None, denial
        return self._matched_approval_result(decision, request)

    def _matched_approval_result(
        self, decision: HumanDecision, request: _Authorization
    ) -> tuple[ConsumedApproval | None, PolicyDecision | None]:
        proposal_hash = _proposal_hash(request)
        if not _approval_matches(decision, request.proposal, request.snapshot, proposal_hash):
            denial = _denied("approval_mismatch", "The approval does not match this proposal.")
            return None, denial
        return self._verified_approval_result(decision, request, proposal_hash)

    def _verified_approval_result(
        self,
        decision: HumanDecision,
        request: _Authorization,
        proposal_hash: str,
    ) -> tuple[ConsumedApproval | None, PolicyDecision | None]:
        verified = _callback_result(
            self._rules.approval_verifier,
            decision,
            request.proposal,
            request.snapshot,
            request.context,
        )
        if isinstance(verified, PolicyDecision):
            return None, verified
        if not verified:
            denial = _denied("approval_verification_failed", "Approval verification failed.")
            return None, denial
        return _consumed_approval(decision, proposal_hash), None

    def _budget_denial(self, context: PolicyContext) -> PolicyDecision | None:
        for check in _BUDGET_CHECKS:
            if check.used(context) >= check.limit(context.budget):
                return _denied(check.code, check.explanation)
        return None

    def _authorized(
        self, request: _Authorization, consumed: ConsumedApproval | None
    ) -> PolicyDecision:
        call = self._build_authorized_call(request)
        return PolicyDecision(
            allowed=True,
            code="authorized",
            explanation="The command passed every deterministic policy check.",
            authorized_call=call,
            fence_token=request.context.fence_token,
            effect_identity=request.identity,
            consumed_approval=consumed,
        )

    def _build_authorized_call(self, request: _Authorization) -> AuthorizedToolCall[BaseModel]:
        context = request.context
        operation_id = self._operation_id(request.snapshot, context)
        return AuthorizedToolCall[BaseModel](
            operation_id=operation_id,
            step_instance_id=context.step_instance_id,
            direction=context.direction,
            semantic_generation=context.semantic_generation,
            command=request.command,
        )

    def _operation_id(self, snapshot: SagaSnapshot, context: PolicyContext) -> OperationId:
        return self._identity_factory.create(
            snapshot.saga_id,
            context.step_instance_id,
            context.direction,
            context.semantic_generation,
        )


def human_resolution_digest(decision: HumanDecision) -> str:
    """Return the canonical digest that binds a stored human decision."""

    material = _JSON_OBJECT_ADAPTER.validate_python(
        {
            "schema_version": "1.0",
            "decision_id": decision.decision_id,
            "saga_id": decision.saga_id,
            "based_on_saga_seq": decision.based_on_saga_seq,
            "action": decision.action,
            "actor": decision.actor,
            "issued_at": decision.issued_at.isoformat(),
        }
    )
    return sha256_json(material)


__all__ = [
    "ConsumedApproval",
    "PolicyContext",
    "PolicyDecision",
    "PolicyEngine",
    "PolicyRule",
    "PolicyRules",
    "ProposedEffectIdentity",
    "StaleProposal",
    "VersionedPolicyRule",
    "human_resolution_digest",
]
