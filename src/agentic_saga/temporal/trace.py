"""Project public Temporal workflow state into the strict Flight Recorder trace."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from agentic_saga.contracts.common import Direction, JsonObject, sha256_json
from agentic_saga.contracts.redaction import RedactionPolicy, redact_json
from agentic_saga.contracts.runtime import SagaStatus
from agentic_saga.contracts.trace import RunTrace, TraceAuthority, TraceEvent, TraceProof
from agentic_saga.temporal.contracts import WorkflowEvent, WorkflowState

_TERMINAL = frozenset(
    {
        SagaStatus.SUCCEEDED_VERIFIED,
        SagaStatus.COMPENSATED_VERIFIED,
        SagaStatus.ABORTED_CLEAN,
    }
)


def project_run_trace(state: WorkflowState, *, definition_version: str) -> RunTrace:
    """Build one deterministic, public, schema-validated recorder trace."""
    run_id = _trace_id(state)
    events = tuple(_trace_event(event, state, run_id, definition_version) for event in state.events)
    proofs = tuple(_proof(event) for event in events if _is_proof(event))
    finished_at = events[-1].recorded_at if state.status in _TERMINAL else None
    return RunTrace(
        run_id=run_id,
        saga_id=state.saga_id,
        definition_version=definition_version,
        started_at=events[0].recorded_at,
        finished_at=finished_at,
        outcome=state.status,
        events=events,
        proofs=proofs,
        final_projection_hash=_state_hash(state),
    )


def _trace_event(
    event: WorkflowEvent,
    state: WorkflowState,
    run_id: str,
    definition_version: str,
) -> TraceEvent:
    redacted_input = _json_detail(event, "arguments")
    receipt = _json_detail(event, "public_receipt")
    redacted_output = _public_output(event, receipt)
    return TraceEvent(
        event_id=_event_id(state, event),
        saga_seq=event.seq,
        recorded_at=event.recorded_at,
        authority=_authority(event),
        event_type=_event_type(event),
        actor=_actor(event),
        trace_id=run_id,
        definition_version=definition_version,
        fence_token=None,
        before_status=None if event.seq == 1 else event.before_status,
        after_status=event.after_status,
        operation_id=_text_detail(event, "operation_id"),
        step_instance_id=_text_detail(event, "step_instance_id"),
        direction=_direction(event),
        semantic_generation=_int_detail(event, "semantic_generation"),
        tool_name=_text_detail(event, "tool_name"),
        compensates_operation_id=_text_detail(event, "compensates_operation_id"),
        redacted_input=redacted_input,
        redacted_output=redacted_output,
        rationale=_rationale(event),
        receipt=receipt,
        correlation=_text_detail(event, "correlation_id"),
        input_hash=None if redacted_input is None else sha256_json(redacted_input),
        output_hash=None if redacted_output is None else sha256_json(redacted_output),
    )


def _public_output(event: WorkflowEvent, receipt: JsonObject | None) -> JsonObject | None:
    if event.kind == "reconciliation_result":
        return _outcome_payload(_reconciliation_kind(event), receipt, event)
    if event.kind not in {"forward_result", "compensation_result"}:
        return None
    if event.details.get("proof_for_success") is True:
        return cast(JsonObject, {"kind": "read_observed", "verified": _verified(event)})
    return _outcome_payload(_tool_outcome_kind(event), receipt, event)


def _tool_outcome_kind(event: WorkflowEvent) -> str:
    outcome = _text_detail(event, "outcome")
    if outcome == "failed":
        return "no_effect_confirmed"
    if outcome != "succeeded":
        return "outcome_unknown"
    return "read_observed" if event.details.get("declared_kind") == "read" else "effect_confirmed"


def _reconciliation_kind(event: WorkflowEvent) -> str:
    outcome = _text_detail(event, "outcome")
    if outcome == "confirmed_effect":
        return "reconcile_effect_confirmed"
    if outcome == "confirmed_no_effect":
        return "reconcile_no_effect_confirmed"
    return outcome or "reconcile_pending"


def _outcome_payload(
    kind: str,
    receipt: JsonObject | None,
    event: WorkflowEvent,
) -> JsonObject:
    payload: dict[str, object] = {"kind": kind}
    if receipt is not None:
        payload["receipt"] = receipt
    reason = _text_detail(event, "reason_code")
    if reason is not None:
        payload["reason_code"] = reason
    return cast(JsonObject, payload)


def _verified(event: WorkflowEvent) -> bool:
    return event.details.get("verified") is True


def _proof(event: TraceEvent) -> TraceProof:
    verified = _proof_result(event)
    return TraceProof(
        source_event_id=event.event_id,
        source_event_seq=event.saga_seq,
        invariant_version=cast(str, event.rationale["invariant_version"]),
        evaluated_at_seq=event.saga_seq - 1,
        target_status=SagaStatus(cast(str, event.rationale["target_status"])),
        rule_id=cast(str, event.rationale["rule_id"]),
        result="valid" if verified else "invalid",
        explanation="ledger_recorded_invariant_result",
    )


def _proof_result(event: TraceEvent) -> bool:
    results = event.rationale.get("results")
    rule_id = event.rationale.get("rule_id")
    return (
        isinstance(results, Mapping) and isinstance(rule_id, str) and results.get(rule_id) is True
    )


def _rationale(event: WorkflowEvent) -> JsonObject:
    if _event_type(event) != "invariant_evaluated":
        return _public_details(event.details)
    rule_id = _proof_rule_id(event)
    verified = _verified(event)
    rationale = cast(
        JsonObject,
        {
            "all_passed": _all_passed(event, verified),
            "invariant_version": _proof_version(event),
            "results": event.details.get("results") or {rule_id: verified},
            "rule_id": rule_id,
            "target_status": _proof_target(event),
        },
    )
    return _public_details(rationale)


def _proof_rule_id(event: WorkflowEvent) -> str:
    return _text_detail(event, "rule_id") or _text_detail(event, "tool_name") or "proof"


def _all_passed(event: WorkflowEvent, verified: bool) -> bool:
    return event.details.get("all_passed") is True or verified


def _proof_version(event: WorkflowEvent) -> str:
    return _text_detail(event, "invariant_version") or "temporal-proof-v1"


def _proof_target(event: WorkflowEvent) -> str:
    return _text_detail(event, "target_status") or SagaStatus.SUCCEEDED_VERIFIED.value


def _public_details(details: JsonObject) -> JsonObject:
    return cast(JsonObject, redact_json(details, RedactionPolicy()))


def _event_type(event: WorkflowEvent) -> str:
    if event.kind == "forward_result":
        return _forward_event_type(event)
    if event.kind == "status_changed":
        return _status_event_type(event.after_status)
    return _EVENT_TYPES.get(event.kind, event.kind)


def _forward_event_type(event: WorkflowEvent) -> str:
    if event.details.get("proof_for_success") is True:
        return "invariant_evaluated"
    if event.details.get("declared_kind") == "read":
        return "read_observed"
    return "effect_outcome_recorded"


def _status_event_type(status: SagaStatus) -> str:
    if status is SagaStatus.COMPENSATING:
        return "compensation_started"
    if status is SagaStatus.HUMAN_REQUIRED:
        return "human_required"
    if status in _TERMINAL:
        return "terminal_assigned"
    return "status_changed"


def _authority(event: WorkflowEvent) -> TraceAuthority:
    explicit = _AUTHORITY_BY_KIND.get(event.kind)
    if explicit is not None:
        return explicit
    if event.details.get("proof_for_success") is True:
        return TraceAuthority.PROOF
    if event.kind in {"forward_result", "reconciliation_result"}:
        return TraceAuthority.EFFECT
    return TraceAuthority.WORKFLOW


def _actor(event: WorkflowEvent) -> str:
    if event.kind == "agent_decision":
        return "agent_driver"
    if event.kind == "human_resolved":
        return _text_detail(event, "actor") or "human_verifier"
    if event.kind in {"forward_result", "compensation_result", "reconciliation_result"}:
        return "registered_tool"
    return "temporal_workflow"


def _direction(event: WorkflowEvent) -> Direction | None:
    if event.kind == "compensation_result":
        return Direction.COMPENSATION
    if event.kind == "reconciliation_result" and event.details.get("direction") == "compensation":
        return Direction.COMPENSATION
    if event.kind in {"forward_result", "reconciliation_result"}:
        return Direction.FORWARD
    return None


def _is_proof(event: TraceEvent) -> bool:
    return event.event_type == "invariant_evaluated"


def _text_detail(event: WorkflowEvent, key: str) -> str | None:
    value = event.details.get(key)
    return value if isinstance(value, str) else None


def _json_detail(event: WorkflowEvent, key: str) -> JsonObject | None:
    value = event.details.get(key)
    return cast(JsonObject, value) if isinstance(value, Mapping) else None


def _int_detail(event: WorkflowEvent, key: str) -> int | None:
    value = event.details.get(key)
    return value if type(value) is int else None


def _event_id(state: WorkflowState, event: WorkflowEvent) -> str:
    digest = sha256_json({"kind": event.kind, "saga_id": state.saga_id, "seq": event.seq})
    return f"evt_{digest[:32]}"


def _trace_id(state: WorkflowState) -> str:
    digest = sha256_json({"domain": "agentic-saga:temporal-trace:v1", "saga_id": state.saga_id})
    return f"trace_{digest[:32]}"


def _state_hash(state: WorkflowState) -> str:
    return sha256_json(cast(JsonObject, state.model_dump(mode="json")))


_EVENT_TYPES = {
    "agent_decision": "agent_decision_recorded",
    "compensation_result": "compensation_outcome_recorded",
    "compensation_verified": "invariant_evaluated",
    "human_resolved": "human_resolved",
    "reconciliation_result": "reconciliation_recorded",
    "started": "saga_started",
}

_AUTHORITY_BY_KIND = {
    "agent_decision": TraceAuthority.AGENT,
    "compensation_result": TraceAuthority.COMPENSATION,
    "compensation_verified": TraceAuthority.PROOF,
    "human_resolved": TraceAuthority.HUMAN,
}


__all__ = ["project_run_trace"]
