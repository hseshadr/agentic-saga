from __future__ import annotations

import pytest

from agentic_saga.contracts.common import Direction
from agentic_saga.contracts.runtime import SagaStatus
from agentic_saga.temporal.contracts import (
    ActivityIdentity,
    CompensationActivityResult,
    ForwardActivityRequest,
    ForwardActivityResult,
    WorkflowState,
)
from agentic_saga.temporal.journal import CompensationJournal

SAGA_ID = "saga_1234567890abcdef"


def _forward(index: int) -> ForwardActivityRequest:
    identity = ActivityIdentity.create(
        SAGA_ID,
        f"step_{index:08d}",
        Direction.FORWARD,
        0,
    )
    return ForwardActivityRequest(
        identity=identity,
        tool_name=f"forward_{index}",
        arguments={"index": index},
        declared_kind="effect",
        declared_compensation_tool=f"undo_{index}",
    )


def _record(journal: CompensationJournal, index: int) -> CompensationJournal:
    return journal.record_forward_success(
        request=_forward(index),
        result=ForwardActivityResult.succeeded({"receipt": index}),
        compensation_tool=f"undo_{index}",
        compensation_arguments={"index": index},
    )


def test_journal_unwinds_successful_receipts_in_reverse_order() -> None:
    journal = _record(_record(_record(CompensationJournal(), 1), 2), 3)

    first = journal.next_request()
    assert first is not None and first.tool_name == "undo_3"
    journal = journal.record_compensation(first, CompensationActivityResult.succeeded({"ok": 3}))
    second = journal.next_request()
    assert second is not None and second.tool_name == "undo_2"
    journal = journal.record_compensation(second, CompensationActivityResult.succeeded({"ok": 2}))
    third = journal.next_request()
    assert third is not None and third.tool_name == "undo_1"


def test_journal_does_not_record_an_unsuccessful_forward_result() -> None:
    journal = CompensationJournal()
    result = ForwardActivityResult.unresolved("provider_timeout")

    updated = journal.record_forward(
        request=_forward(1),
        result=result,
        compensation_tool="undo_1",
        compensation_arguments={"index": 1},
    )

    assert updated == journal
    assert updated.next_request() is None


def test_journal_records_a_success_through_the_general_entrypoint() -> None:
    result = ForwardActivityResult.succeeded({"receipt": 1})

    updated = CompensationJournal().record_forward(
        request=_forward(1),
        result=result,
        compensation_tool="undo_1",
        compensation_arguments={"index": 1},
    )

    assert len(updated.entries) == 1


def test_success_helper_rejects_a_constructed_result_without_receipt() -> None:
    invalid = ForwardActivityResult.model_construct(
        outcome="succeeded", receipt=None, reason_code=None
    )

    try:
        CompensationJournal().record_forward_success(
            request=_forward(1),
            result=invalid,
            compensation_tool="undo_1",
            compensation_arguments={"index": 1},
        )
    except ValueError as error:
        assert str(error) == "successful forward activity requires a receipt"
    else:
        raise AssertionError("receipt-free success was accepted")


def test_recording_the_same_forward_operation_is_idempotent() -> None:
    journal = _record(CompensationJournal(), 1)

    repeated = _record(journal, 1)

    assert repeated == journal


def test_journal_rejects_out_of_order_compensation_result() -> None:
    journal = _record(_record(CompensationJournal(), 1), 2)
    current = journal.next_request()
    assert current is not None
    stale = current.model_copy(update={"tool_name": "undo_1"})

    try:
        journal.record_compensation(stale, CompensationActivityResult.succeeded({"ok": 1}))
    except ValueError as error:
        assert str(error) == "compensation result is not for the current reverse frontier"
    else:
        raise AssertionError("out-of-order result was accepted")


def test_unresolved_compensation_requires_human_and_stops_unwind() -> None:
    journal = _record(_record(CompensationJournal(), 1), 2)
    request = journal.next_request()
    assert request is not None

    updated = journal.record_compensation(
        request,
        CompensationActivityResult.unresolved("outcome_unknown"),
    )

    assert updated.human_required_reason == "outcome_unknown"
    assert updated.next_request() is None


def test_human_resolution_rejects_an_operation_that_is_not_unresolved() -> None:
    journal = _record(CompensationJournal(), 1)
    request = journal.next_request()
    assert request is not None
    unresolved = journal.record_compensation(
        request,
        CompensationActivityResult.unresolved("outcome_unknown"),
    )

    with pytest.raises(ValueError, match="does not match unresolved compensation"):
        unresolved.resolve_unresolved("op_" + "f" * 64, {"confirmed": True})


def test_journal_projects_human_required_workflow_state() -> None:
    journal = _record(CompensationJournal(), 1)
    request = journal.next_request()
    assert request is not None
    journal = journal.record_compensation(
        request,
        CompensationActivityResult.unresolved("receipt_unavailable"),
    )
    state = WorkflowState(
        saga_id=SAGA_ID,
        status=SagaStatus.COMPENSATING,
        events=(),
        compensations=journal.entries,
    )

    projected = journal.project_state(state)

    assert projected.status is SagaStatus.HUMAN_REQUIRED
    assert projected.human_required_reason == "receipt_unavailable"


def test_completed_journal_projects_compensated_state() -> None:
    journal = _record(CompensationJournal(), 1)
    request = journal.next_request()
    assert request is not None
    journal = journal.record_compensation(request, CompensationActivityResult.succeeded({"ok": 1}))
    state = WorkflowState(
        saga_id=SAGA_ID,
        status=SagaStatus.COMPENSATING,
        events=(),
        compensations=journal.entries,
    )

    projected = journal.project_state(state)

    assert projected.status is SagaStatus.COMPENSATED_VERIFIED
    assert projected.human_required_reason is None


def test_pending_journal_keeps_compensating_state() -> None:
    journal = _record(CompensationJournal(), 1)
    state = WorkflowState(
        saga_id=SAGA_ID,
        status=SagaStatus.COMPENSATING,
        events=(),
        compensations=journal.entries,
    )

    assert journal.project_state(state) == state
