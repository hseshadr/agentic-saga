from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from agentic_saga.contracts.runtime import SagaGoal, SagaStatus
from agentic_saga.temporal.client import (
    SagaHandle,
    get_saga_handle,
    query_saga_state,
    resolve_human_compensation,
    start_saga,
)
from agentic_saga.temporal.contracts import (
    HumanCompensationResolution,
    SagaWorkflowInput,
    WorkflowState,
    WorkflowTool,
)
from agentic_saga.temporal.workflow import AgenticSagaWorkflow

_SAGA_ID = "saga_1234567890abcdef"


def _input() -> SagaWorkflowInput:
    return SagaWorkflowInput(
        saga_id=_SAGA_ID,
        goal=SagaGoal(goal_id="checkout", text="Checkout safely.", context={}),
        tools=(WorkflowTool(name="verify", kind="read"),),
        max_agent_turns=3,
        max_tool_calls=3,
    )


def _state() -> WorkflowState:
    return WorkflowState(
        saga_id=_SAGA_ID,
        status=SagaStatus.RUNNING,
        events=(),
        compensations=(),
    )


def _handle() -> SagaHandle:
    return cast(SagaHandle, MagicMock(spec=WorkflowHandle))


@pytest.mark.asyncio
async def test_start_saga_uses_exact_temporal_identity_and_queue() -> None:
    handle = _handle()
    client = cast(Client, MagicMock(spec=Client))
    start = cast(AsyncMock, client.start_workflow)
    start.return_value = handle
    saga = _input()

    result = await start_saga(client, saga, task_queue="agentic-saga-prod")

    assert result is handle
    start.assert_awaited_once_with(
        "agentic_saga.workflow",
        saga,
        id=saga.saga_id,
        task_queue="agentic-saga-prod",
        result_type=WorkflowState,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
    )


@pytest.mark.parametrize(
    "task_queue",
    ["", " ", "../queue", "queue/name", " queue", "queue\nname"],
)
@pytest.mark.asyncio
async def test_start_saga_rejects_unsafe_task_queue(task_queue: str) -> None:
    client = cast(Client, MagicMock(spec=Client))

    with pytest.raises(ValueError, match="task queue"):
        await start_saga(client, _input(), task_queue=task_queue)

    cast(AsyncMock, client.start_workflow).assert_not_awaited()


def test_get_saga_handle_preserves_temporal_handle_semantics() -> None:
    expected = _handle()
    client = cast(Client, MagicMock(spec=Client))
    get_handle = cast(MagicMock, client.get_workflow_handle)
    get_handle.return_value = expected

    result = get_saga_handle(client, _SAGA_ID)

    assert result is expected
    get_handle.assert_called_once_with(_SAGA_ID, result_type=WorkflowState)


@pytest.mark.asyncio
async def test_query_saga_state_uses_typed_workflow_query() -> None:
    handle = _handle()
    query = cast(AsyncMock, handle.query)
    query.return_value = _state()

    result = await query_saga_state(handle)

    assert result == _state()
    query.assert_awaited_once_with(AgenticSagaWorkflow.state)


@pytest.mark.asyncio
async def test_human_resolution_uses_typed_workflow_update() -> None:
    handle = _handle()
    update = cast(AsyncMock, handle.execute_update)
    update.return_value = _state()
    resolution = HumanCompensationResolution(
        operation_id="op_" + "1" * 64,
        based_on_event_seq=3,
        authorization_reference="authz_1234567890abcdef",
        receipt={"confirmed": True},
    )

    result = await resolve_human_compensation(handle, resolution)

    assert result == _state()
    update.assert_awaited_once_with(
        AgenticSagaWorkflow.resolve_compensation,
        resolution,
    )
