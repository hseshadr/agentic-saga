from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Protocol, cast

from pydantic import TypeAdapter, ValidationError

from agentic_saga.contracts.common import Direction, JsonObject, SagaId, canonical_json, sha256_json
from agentic_saga.contracts.events import (
    AgentTurnFailed,
    AgentTurnReserved,
    ApprovalConsumed,
    CompensationIntentRecorded,
    CompensationStarted,
    DispatchAbortedBeforeEntry,
    DispatchStarted,
    EffectIntentRecorded,
    EffectOutcomeRecorded,
    HumanRequired,
    HumanResolutionRecorded,
    InvariantEvaluated,
    LedgerEvent,
    ProposalRejected,
    ReadObserved,
    ReadStarted,
    ReadUnavailable,
    ReconciliationRecorded,
    RecoveryPlanAccepted,
    RecoveryPlanRejected,
    RecoveryPlanRequired,
    SagaCreated,
    SagaStarted,
    TerminalAssigned,
    TerminalDenied,
)
from agentic_saga.contracts.outcomes import is_safe_outcome_correlation
from agentic_saga.contracts.redaction import RedactionPolicy, redact_json
from agentic_saga.contracts.runtime import SagaStatus
from agentic_saga.contracts.trace import RunTrace, TraceAuthority, TraceEvent, TraceProof
from agentic_saga.kernel.definitions import (
    DefinitionCatalog,
    DefinitionUnavailable,
    SagaDefinition,
)
from agentic_saga.kernel.ports import StoreError
from agentic_saga.kernel.reducer import InvalidTransition, reduce_event
from agentic_saga.kernel.state import SagaSnapshot

_JSON_OBJECT: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
_MAX_DEPTH = 16
_MAX_NODES = 100_000
_MAX_STRING = 20_000
_MAX_TRACE_BYTES = 8 * 1024 * 1024
_MAX_EVENTS = 10_000
_MIN_TERMINAL_EVENTS = 2
_TERMINAL = frozenset(
    {
        SagaStatus.SUCCEEDED_VERIFIED,
        SagaStatus.COMPENSATED_VERIFIED,
        SagaStatus.ABORTED_CLEAN,
        SagaStatus.RESOLVED_WITH_EXCEPTION,
    }
)
_SUPPORTED = (
    SagaCreated,
    SagaStarted,
    AgentTurnReserved,
    AgentTurnFailed,
    ReadStarted,
    ReadObserved,
    ReadUnavailable,
    EffectIntentRecorded,
    DispatchStarted,
    DispatchAbortedBeforeEntry,
    EffectOutcomeRecorded,
    ReconciliationRecorded,
    RecoveryPlanRequired,
    RecoveryPlanAccepted,
    RecoveryPlanRejected,
    ProposalRejected,
    ApprovalConsumed,
    CompensationStarted,
    CompensationIntentRecorded,
    InvariantEvaluated,
    HumanRequired,
    HumanResolutionRecorded,
    TerminalAssigned,
    TerminalDenied,
)
_AUTHORITY_BY_TYPE: Mapping[type[object], TraceAuthority] = {
    SagaCreated: TraceAuthority.KERNEL,
    SagaStarted: TraceAuthority.KERNEL,
    AgentTurnReserved: TraceAuthority.AGENT,
    AgentTurnFailed: TraceAuthority.AGENT,
    ReadStarted: TraceAuthority.EFFECT,
    ReadObserved: TraceAuthority.EFFECT,
    ReadUnavailable: TraceAuthority.EFFECT,
    EffectIntentRecorded: TraceAuthority.EFFECT,
    DispatchStarted: TraceAuthority.EFFECT,
    DispatchAbortedBeforeEntry: TraceAuthority.EFFECT,
    EffectOutcomeRecorded: TraceAuthority.EFFECT,
    ReconciliationRecorded: TraceAuthority.EFFECT,
    RecoveryPlanRequired: TraceAuthority.KERNEL,
    RecoveryPlanAccepted: TraceAuthority.POLICY,
    RecoveryPlanRejected: TraceAuthority.POLICY,
    ProposalRejected: TraceAuthority.POLICY,
    ApprovalConsumed: TraceAuthority.POLICY,
    CompensationStarted: TraceAuthority.COMPENSATION,
    CompensationIntentRecorded: TraceAuthority.COMPENSATION,
    InvariantEvaluated: TraceAuthority.PROOF,
    HumanRequired: TraceAuthority.HUMAN,
    HumanResolutionRecorded: TraceAuthority.HUMAN,
    TerminalAssigned: TraceAuthority.KERNEL,
    TerminalDenied: TraceAuthority.POLICY,
}
_INPUT = (
    ReadStarted,
    EffectIntentRecorded,
    DispatchStarted,
    DispatchAbortedBeforeEntry,
    CompensationIntentRecorded,
)
_OUTPUT = (ReadObserved, EffectOutcomeRecorded, ReconciliationRecorded)
_BASE_FIELDS = frozenset(
    {
        "event_id",
        "saga_id",
        "saga_seq",
        "schema_version",
        "definition_version",
        "definition_fingerprint",
        "fence_token",
        "actor",
        "trace_id",
        "recorded_at",
        "event_type",
        "operation_id",
        "step_instance_id",
        "direction",
        "semantic_generation",
        "delivery_attempt",
        "tool_name",
        "redacted_command",
        "redacted_goal",
        "redacted_result",
        "outcome",
        "forward_receipts",
    }
)


class TraceExportError(ValueError):
    """Raised when durable history cannot produce a safe canonical trace."""


class TraceStorage(Protocol):
    """Provide the durable ledger and verified projection needed for trace export."""

    def read_events(self, saga_id: SagaId) -> tuple[LedgerEvent, ...]: ...
    def load_snapshot(self, saga_id: SagaId) -> SagaSnapshot: ...
    def rebuild_and_verify(self, saga_id: SagaId) -> SagaSnapshot: ...


class RunTraceExporter:
    """Export a bounded audit trace only after replay and storage verification agree."""

    def __init__(
        self,
        store: TraceStorage,
        definitions: DefinitionCatalog,
    ) -> None:
        self._store = store
        self._definitions = definitions

    def export(self, saga_id: SagaId) -> RunTrace:
        try:
            return self._export(saga_id)
        except TraceExportError:
            raise
        except (
            StoreError,
            InvalidTransition,
            ValidationError,
            ValueError,
            TypeError,
            RecursionError,
        ) as error:
            raise TraceExportError("durable trace evidence is invalid") from error

    def _export(self, saga_id: SagaId) -> RunTrace:
        events = self._store.read_events(saga_id)
        _require_ordered_events(events, saga_id)
        snapshot = self._verified_snapshot(events, saga_id)
        created = cast(SagaCreated, events[0])
        definition = self._definition(created)
        trace = self._build_trace(created, events, snapshot, definition.redaction_policy)
        _require_trace_size(trace)
        return trace

    def _verified_snapshot(self, events: tuple[LedgerEvent, ...], saga_id: SagaId) -> SagaSnapshot:
        replayed = _replay(events)
        rebuilt = self._store.rebuild_and_verify(saga_id)
        loaded = self._store.load_snapshot(saga_id)
        if replayed != rebuilt or rebuilt != loaded:
            raise TraceExportError("stored projection does not match ordered ledger replay")
        return loaded

    def _definition(self, created: SagaCreated) -> SagaDefinition:
        try:
            return self._definitions.resolve(created.definition_name, created.definition_version)
        except DefinitionUnavailable as error:
            raise TraceExportError("exact pinned definition is unavailable") from error

    def _build_trace(
        self,
        created: SagaCreated,
        events: tuple[LedgerEvent, ...],
        snapshot: SagaSnapshot,
        policy: RedactionPolicy,
    ) -> RunTrace:
        translated = self._translate_events(events, policy)
        proofs = _proofs(events)
        _require_terminal_proof(events, snapshot)
        identity = _trace_header(created, events, snapshot)
        evidence = _trace_body(translated, proofs, snapshot, policy)
        return RunTrace.model_validate(identity | evidence)

    def _translate_events(
        self, events: tuple[LedgerEvent, ...], policy: RedactionPolicy
    ) -> tuple[TraceEvent, ...]:
        translated: list[TraceEvent] = []
        prior: SagaSnapshot | None = None
        for event in events:
            current = reduce_event(prior, event)
            translated.append(_trace_event(event, prior, current, policy))
            prior = current
        return tuple(translated)


def _trace_header(
    created: SagaCreated, events: tuple[LedgerEvent, ...], snapshot: SagaSnapshot
) -> dict[str, object]:
    return {
        "run_id": created.trace_id,
        "saga_id": created.saga_id,
        "definition_version": created.definition_version,
        "started_at": created.recorded_at,
        "finished_at": _finished_at(events, snapshot),
        "outcome": snapshot.status,
    }


def _trace_body(
    events: tuple[TraceEvent, ...],
    proofs: tuple[TraceProof, ...],
    snapshot: SagaSnapshot,
    policy: RedactionPolicy,
) -> dict[str, object]:
    return {
        "events": events,
        "proofs": proofs,
        "final_projection_hash": _projection_hash(snapshot, policy),
    }


def _require_ordered_events(events: tuple[LedgerEvent, ...], saga_id: SagaId) -> None:
    if type(events) is not tuple or not events or len(events) > _MAX_EVENTS:
        raise TraceExportError("ordered ledger must contain real events")
    _require_monotonic_times(events)
    for expected, event in enumerate(events, 1):
        _require_event_identity(event, saga_id, expected)


def _require_monotonic_times(events: tuple[LedgerEvent, ...]) -> None:
    if any(not hasattr(event, "recorded_at") for event in events):
        return
    times = tuple(event.recorded_at for event in events)
    if times != tuple(sorted(times)):
        raise TraceExportError("ordered ledger timestamp moved backward")


def _require_event_identity(event: object, saga_id: SagaId, expected: int) -> None:
    if type(event) not in _SUPPORTED:
        raise TraceExportError("unsupported ledger event")
    typed = cast(LedgerEvent, event)
    if typed.saga_seq != expected or typed.saga_id != saga_id:
        raise TraceExportError("ordered ledger has a gap, duplicate, or reorder")
    _require_event_metadata(typed)


def _require_event_metadata(event: LedgerEvent) -> None:
    if event.schema_version != "1.0":
        raise TraceExportError("unsupported ledger schema")
    if event.recorded_at.utcoffset() != UTC.utcoffset(event.recorded_at):
        raise TraceExportError("ledger timestamp is not canonical UTC")
    _require_bounds(event.model_dump(mode="json"))
    _require_source_hashes(event)


def _require_source_hashes(event: LedgerEvent) -> None:
    if isinstance(event, _INPUT) and event.command_hash != sha256_json(event.redacted_command):
        raise TraceExportError("ledger input hash does not bind public evidence")
    if isinstance(event, _OUTPUT) and event.result_hash != sha256_json(event.redacted_result):
        raise TraceExportError("ledger output hash does not bind public evidence")


def _replay(events: tuple[LedgerEvent, ...]) -> SagaSnapshot:
    snapshot: SagaSnapshot | None = None
    for event in events:
        snapshot = reduce_event(snapshot, event)
    if snapshot is None:
        raise TraceExportError("ordered ledger is empty")
    return snapshot


def _authority(event: LedgerEvent) -> TraceAuthority:
    if getattr(event, "direction", None) is Direction.COMPENSATION:
        return TraceAuthority.COMPENSATION
    return _AUTHORITY_BY_TYPE[type(event)]


def _trace_event(
    event: LedgerEvent,
    prior: SagaSnapshot | None,
    current: SagaSnapshot,
    policy: RedactionPolicy,
) -> TraceEvent:
    redacted_input = _event_input(event, policy)
    redacted_output = _event_output(event, policy)
    fields = _trace_identity(event, prior, current)
    evidence = _trace_evidence(event, redacted_input, redacted_output, policy)
    return TraceEvent.model_validate(fields | evidence)


def _trace_identity(
    event: LedgerEvent, prior: SagaSnapshot | None, current: SagaSnapshot
) -> dict[str, object]:
    return _base_identity(event, prior, current) | _operation_identity(event)


def _base_identity(
    event: LedgerEvent, prior: SagaSnapshot | None, current: SagaSnapshot
) -> dict[str, object]:
    status: dict[str, object] = {
        "before_status": None if prior is None else prior.status,
        "after_status": current.status,
    }
    return _ledger_identity(event) | status


def _ledger_identity(event: LedgerEvent) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "saga_seq": event.saga_seq,
        "recorded_at": event.recorded_at,
        "authority": _authority(event),
        "event_type": event.event_type,
        "actor": event.actor,
        "trace_id": event.trace_id,
        "definition_version": event.definition_version,
        "fence_token": event.fence_token,
    }


def _operation_identity(event: LedgerEvent) -> dict[str, object]:
    return {
        "operation_id": getattr(event, "operation_id", None),
        "step_instance_id": getattr(event, "step_instance_id", None),
        "direction": getattr(event, "direction", None),
        "semantic_generation": getattr(event, "semantic_generation", None),
        "attempt": getattr(event, "delivery_attempt", None),
        "tool_name": getattr(event, "tool_name", None),
        "compensates_operation_id": getattr(event, "compensates_operation_id", None),
    }


def _trace_evidence(
    event: LedgerEvent,
    input_value: JsonObject | None,
    output_value: JsonObject | None,
    policy: RedactionPolicy,
) -> dict[str, object]:
    values: dict[str, object] = {
        "redacted_input": input_value,
        "redacted_output": output_value,
        "rationale": _rationale(event, policy),
        "policy_decision": _policy_decision(event, policy),
        "receipt": _receipt(event, policy),
        "correlation": _correlation(output_value),
    }
    return values | _evidence_hashes(input_value, output_value)


def _evidence_hashes(
    input_value: JsonObject | None, output_value: JsonObject | None
) -> dict[str, object]:
    return {
        "input_hash": None if input_value is None else sha256_json(input_value),
        "output_hash": None if output_value is None else sha256_json(output_value),
    }


def _event_input(event: LedgerEvent, policy: RedactionPolicy) -> JsonObject | None:
    if isinstance(event, SagaCreated):
        return _redacted_object(event.redacted_goal, policy)
    if isinstance(event, _INPUT):
        return _redacted_object(event.redacted_command, policy)
    return None


def _event_output(event: LedgerEvent, policy: RedactionPolicy) -> JsonObject | None:
    if isinstance(event, _OUTPUT):
        return _redacted_object(event.redacted_result, policy)
    return None


def _rationale(event: LedgerEvent, policy: RedactionPolicy) -> JsonObject:
    dumped = cast(dict[str, object], event.model_dump(mode="json"))
    evidence = {key: value for key, value in dumped.items() if _include_rationale(key)}
    return _redacted_object(evidence, policy)


def _include_rationale(key: str) -> bool:
    return key not in _BASE_FIELDS and not key.endswith(("_hash", "_digest"))


def _policy_decision(event: LedgerEvent, policy: RedactionPolicy) -> JsonObject | None:
    if _authority(event) is not TraceAuthority.POLICY:
        return None
    return _rationale(event, policy)


def _receipt(event: LedgerEvent, policy: RedactionPolicy) -> JsonObject | None:
    if isinstance(event, CompensationIntentRecorded):
        dumped = event.model_dump(mode="json")
        return _redacted_object({"forward_receipts": dumped["forward_receipts"]}, policy)
    output = _event_output(event, policy)
    if output is None or "receipt" not in output:
        return None
    return _redacted_object({"receipt": output["receipt"]}, policy)


def _correlation(output: JsonObject | None) -> str | None:
    if output is None:
        return None
    value = output.get("correlation")
    if not isinstance(value, str) or not is_safe_outcome_correlation(value):
        return None
    return value


def _redacted_object(value: object, policy: RedactionPolicy) -> JsonObject:
    _require_bounds(value)
    redacted = redact_json(value, policy)
    _require_bounds(redacted)
    return _JSON_OBJECT.validate_python(redacted)


def _require_bounds(value: object) -> None:
    pending: list[tuple[object, int]] = [(value, 0)]
    seen = 0
    while pending:
        current, depth = pending.pop()
        seen += 1
        if depth > _MAX_DEPTH or seen > _MAX_NODES:
            raise TraceExportError("trace evidence exceeds deterministic bounds")
        _push_children(pending, current, depth)


def _push_children(pending: list[tuple[object, int]], value: object, depth: int) -> None:
    pending.extend((item, depth + 1) for item in _children(value))
    if isinstance(value, str) and len(value) > _MAX_STRING:
        raise TraceExportError("trace evidence exceeds deterministic bounds")


def _children(value: object) -> tuple[object, ...]:
    if isinstance(value, Mapping):
        return (*value.keys(), *value.values())
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return ()


def _proofs(events: tuple[LedgerEvent, ...]) -> tuple[TraceProof, ...]:
    proofs: list[TraceProof] = []
    for event in events:
        if isinstance(event, InvariantEvaluated):
            proofs.extend(_event_proofs(event))
    return tuple(proofs)


def _event_proofs(event: InvariantEvaluated) -> tuple[TraceProof, ...]:
    proofs: list[TraceProof] = []
    for rule_id, passed in event.results.items():
        if type(passed) is not bool:
            raise TraceExportError("invariant result is not durable boolean evidence")
        proofs.append(_trace_proof(event, rule_id, passed))
    return tuple(proofs)


def _trace_proof(event: InvariantEvaluated, rule_id: str, passed: bool) -> TraceProof:
    return TraceProof(
        source_event_id=event.event_id,
        source_event_seq=event.saga_seq,
        invariant_version=event.invariant_version,
        evaluated_at_seq=event.evaluated_at_seq,
        target_status=SagaStatus(event.target_status),
        rule_id=rule_id,
        result="valid" if passed else "invalid",
        explanation="ledger_recorded_invariant_result",
    )


def _require_terminal_proof(events: tuple[LedgerEvent, ...], snapshot: SagaSnapshot) -> None:
    if snapshot.status not in _TERMINAL:
        return
    _require_terminal_event(events)
    proof = events[-2]
    if not isinstance(proof, InvariantEvaluated):
        raise TraceExportError("terminal projection lacks current invariant proof")
    if snapshot.last_invariant_seq != proof.saga_seq:
        raise TraceExportError("terminal invariant proof is stale")


def _require_terminal_event(events: tuple[LedgerEvent, ...]) -> None:
    if len(events) < _MIN_TERMINAL_EVENTS:
        raise TraceExportError("terminal projection lacks exact terminal evidence")
    if not isinstance(events[-1], TerminalAssigned):
        raise TraceExportError("terminal projection lacks exact terminal evidence")


def _finished_at(events: tuple[LedgerEvent, ...], snapshot: SagaSnapshot) -> datetime | None:
    return events[-1].recorded_at if snapshot.status in _TERMINAL else None


def _projection_hash(snapshot: SagaSnapshot, policy: RedactionPolicy) -> str:
    dumped = snapshot.model_dump(mode="json")
    public = _strip_digests(cast(dict[str, object], dumped))
    redacted = _redacted_object(public, policy)
    return sha256_json(redacted)


def _strip_digests(value: object) -> object:
    if isinstance(value, dict):
        return _strip_mapping(value)
    if isinstance(value, list):
        return [_strip_digests(item) for item in value]
    return value


def _strip_mapping(value: dict[str, object]) -> dict[str, object]:
    return {
        key: _strip_digests(item)
        for key, item in value.items()
        if not key.endswith(("_hash", "_digest"))
    }


def _require_trace_size(trace: RunTrace) -> None:
    if len(canonical_json(trace.model_dump(mode="json"))) > _MAX_TRACE_BYTES:
        raise TraceExportError("canonical trace exceeds deterministic size bound")


__all__ = ["RunTraceExporter", "TraceExportError", "TraceStorage"]
