from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict

from agentic_saga.contracts.common import Direction, JsonObject
from agentic_saga.contracts.runtime import SagaStatus
from agentic_saga.temporal.contracts import (
    ActivityIdentity,
    CompensationActivityRequest,
    CompensationActivityResult,
    CompensationRecord,
    ForwardActivityRequest,
    ForwardActivityResult,
    WorkflowState,
)


class CompensationJournal(BaseModel):
    """Immutable, deterministic journal of proven forward obligations."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    entries: tuple[CompensationRecord, ...] = ()
    human_required_reason: str | None = None

    def record_forward(
        self,
        *,
        request: ForwardActivityRequest,
        result: ForwardActivityResult,
        compensation_tool: str,
        compensation_arguments: JsonObject,
    ) -> Self:
        if result.outcome != "succeeded":
            return self
        return self.record_forward_success(
            request=request,
            result=result,
            compensation_tool=compensation_tool,
            compensation_arguments=compensation_arguments,
        )

    def record_forward_success(
        self,
        *,
        request: ForwardActivityRequest,
        result: ForwardActivityResult,
        compensation_tool: str,
        compensation_arguments: JsonObject,
    ) -> Self:
        if result.receipt is None:
            raise ValueError("successful forward activity requires a receipt")
        if self._contains(request.identity.operation_id):
            return self
        entry = _record(request, result.receipt, compensation_tool, compensation_arguments)
        return self.model_copy(update={"entries": (*self.entries, entry)})

    def next_request(self) -> CompensationActivityRequest | None:
        if self.human_required_reason is not None:
            return None
        entry = next((item for item in reversed(self.entries) if item.state == "pending"), None)
        return None if entry is None else _request(entry)

    def record_compensation(
        self,
        request: CompensationActivityRequest,
        result: CompensationActivityResult,
    ) -> Self:
        expected = self.next_request()
        if request != expected:
            raise ValueError("compensation result is not for the current reverse frontier")
        entries = tuple(_updated(item, request, result) for item in self.entries)
        reason = result.reason_code if result.outcome == "unresolved" else None
        return self.model_copy(update={"entries": entries, "human_required_reason": reason})

    def project_state(self, state: WorkflowState) -> WorkflowState:
        if self.human_required_reason is not None:
            return state.model_copy(
                update={
                    "status": SagaStatus.HUMAN_REQUIRED,
                    "human_required_reason": self.human_required_reason,
                }
            )
        if self.entries and all(item.state == "succeeded" for item in self.entries):
            return state.model_copy(update={"status": SagaStatus.COMPENSATED_VERIFIED})
        return state

    def can_resolve(self, operation_id: str) -> bool:
        return any(
            item.state == "unresolved" and item.compensation_identity.operation_id == operation_id
            for item in self.entries
        )

    def resolve_unresolved(self, operation_id: str, receipt: JsonObject) -> Self:
        if not self.can_resolve(operation_id):
            raise ValueError("human resolution does not match unresolved compensation")
        entries = tuple(_human_resolved(item, operation_id, receipt) for item in self.entries)
        return self.model_copy(update={"entries": entries, "human_required_reason": None})

    def _contains(self, operation_id: str) -> bool:
        return any(item.forward_identity.operation_id == operation_id for item in self.entries)


def _record(
    request: ForwardActivityRequest,
    receipt: JsonObject,
    compensation_tool: str,
    compensation_arguments: JsonObject,
) -> CompensationRecord:
    identity = request.identity
    compensation = ActivityIdentity.create(
        identity.saga_id,
        identity.step_instance_id,
        Direction.COMPENSATION,
        identity.semantic_generation,
    )
    return CompensationRecord(
        forward_identity=identity,
        compensation_identity=compensation,
        compensation_tool=compensation_tool,
        compensation_arguments=compensation_arguments,
        forward_receipt=receipt,
    )


def _request(record: CompensationRecord) -> CompensationActivityRequest:
    return CompensationActivityRequest(
        identity=record.compensation_identity,
        tool_name=record.compensation_tool,
        arguments=record.compensation_arguments,
        compensates_operation_id=record.forward_identity.operation_id,
        forward_receipt=record.forward_receipt,
    )


def _updated(
    record: CompensationRecord,
    request: CompensationActivityRequest,
    result: CompensationActivityResult,
) -> CompensationRecord:
    if record.compensation_identity != request.identity:
        return record
    if result.outcome == "succeeded":
        return record.model_copy(
            update={"state": "succeeded", "compensation_receipt": result.receipt}
        )
    return record.model_copy(
        update={"state": "unresolved", "unresolved_reason": result.reason_code}
    )


def _human_resolved(
    record: CompensationRecord, operation_id: str, receipt: JsonObject
) -> CompensationRecord:
    if record.compensation_identity.operation_id != operation_id:
        return record
    return record.model_copy(
        update={
            "state": "succeeded",
            "compensation_receipt": receipt,
            "unresolved_reason": None,
        }
    )
