from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path

from pydantic import BaseModel

from agentic_saga.contracts.common import canonical_json
from agentic_saga.contracts.outcomes import (
    EffectConfirmed,
    EffectOutcome,
    PartialEffectConfirmed,
    ReconcileConflict,
    ReconcileEffectConfirmed,
    ReconcileNoEffectConfirmed,
    ReconciliationOutcome,
)
from agentic_saga.contracts.tools import EffectContext, ReconcileContext


class DurableResponseLost(RuntimeError):
    pass


class DurableToolIdentityConflict(RuntimeError):
    pass


class DurableToolFault(StrEnum):
    NONE = "none"
    EFFECT_THEN_LOSE_RESPONSE = "effect_then_lose_response"
    PARTIAL_EFFECT = "partial_effect"
    COMPENSATION_FAILURE_ONCE = "compensation_failure_once"


@dataclass(frozen=True)
class ToolCall:
    operation_id: str
    delivery_attempt: int
    receipt_ref: str | None


@dataclass(frozen=True)
class _Record:
    command_hash: str
    outcome: EffectOutcome


class DurableFakeTool:
    """In-memory provider simulator with a real idempotency boundary."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[ToolCall] = []
        self._records: dict[str, _Record] = {}
        self._fault = DurableToolFault.NONE
        self._lost: set[str] = set()
        self._failed: set[str] = set()

    @classmethod
    def initialize(cls, path: Path, name: str) -> DurableFakeTool:
        del path
        return cls(name)

    @property
    def execute_call_count(self) -> int:
        return len(self.calls)

    def set_fault(self, fault: DurableToolFault) -> None:
        self._fault = fault

    def effect_count(self, operation_id: str) -> int:
        return int(operation_id in self._records)

    def partial_receipts(self, operation_id: str) -> tuple[dict[str, str], ...]:
        return ({"receipt_ref": _reference(operation_id, "partial")},)

    async def execute(self, command: BaseModel, context: EffectContext) -> EffectOutcome:
        command_hash = sha256(canonical_json(command.model_dump(mode="json"))).hexdigest()
        existing = self._records.get(context.operation_id)
        self._require_same_command(existing, command_hash)
        if existing is not None:
            self._append_call(context, existing.outcome)
            return existing.outcome
        return self._first_execution(command_hash, context)

    async def reconcile(
        self,
        command: BaseModel,
        context: ReconcileContext,
    ) -> ReconciliationOutcome:
        command_hash = sha256(canonical_json(command.model_dump(mode="json"))).hexdigest()
        existing = self._records.get(context.operation_id)
        self._require_same_command(existing, command_hash)
        if existing is None:
            return ReconcileNoEffectConfirmed(reason="provider has no matching effect")
        if not isinstance(existing.outcome, EffectConfirmed):
            return ReconcileConflict(reason="provider recorded a partial effect")
        return ReconcileEffectConfirmed(receipt=existing.outcome.receipt)

    def _first_execution(self, command_hash: str, context: EffectContext) -> EffectOutcome:
        self._raise_transient_compensation(context)
        outcome = self._new_outcome(context.operation_id)
        self._records[context.operation_id] = _Record(command_hash, outcome)
        self._append_call(context, outcome)
        self._raise_lost_response(context.operation_id)
        return outcome

    def _raise_transient_compensation(self, context: EffectContext) -> None:
        should_fail = self._fault is DurableToolFault.COMPENSATION_FAILURE_ONCE
        if should_fail and context.forward_receipts and context.operation_id not in self._failed:
            self._failed.add(context.operation_id)
            self.calls.append(ToolCall(context.operation_id, context.delivery_attempt, None))
            raise RuntimeError("transient provider failure")

    def _new_outcome(self, operation_id: str) -> EffectOutcome:
        if self._fault is DurableToolFault.PARTIAL_EFFECT:
            return PartialEffectConfirmed(receipts=self.partial_receipts(operation_id))
        return EffectConfirmed(receipt={"receipt_ref": _reference(operation_id, "effect")})

    def _append_call(self, context: EffectContext, outcome: EffectOutcome) -> None:
        self.calls.append(
            ToolCall(context.operation_id, context.delivery_attempt, _receipt_reference(outcome))
        )

    def _raise_lost_response(self, operation_id: str) -> None:
        should_lose = self._fault is DurableToolFault.EFFECT_THEN_LOSE_RESPONSE
        if should_lose and operation_id not in self._lost:
            self._lost.add(operation_id)
            raise DurableResponseLost("provider response was lost")

    @staticmethod
    def _require_same_command(existing: _Record | None, command_hash: str) -> None:
        if existing is not None and existing.command_hash != command_hash:
            raise DurableToolIdentityConflict("operation identity reused with another command")


def _reference(operation_id: str, role: str) -> str:
    digest = sha256(f"{operation_id}:{role}".encode()).hexdigest()
    return f"opaque://provider/{digest}/v1"


def _receipt_reference(outcome: EffectOutcome) -> str | None:
    if isinstance(outcome, EffectConfirmed):
        return str(outcome.receipt["receipt_ref"])
    if isinstance(outcome, PartialEffectConfirmed):
        return str(outcome.receipts[0]["receipt_ref"])
    return None
