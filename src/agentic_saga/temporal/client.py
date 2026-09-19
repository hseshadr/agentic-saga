"""Thin typed client operations for the Temporal saga workflow."""

from __future__ import annotations

import re
from typing import cast

from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from agentic_saga.contracts.common import SagaId
from agentic_saga.temporal.contracts import (
    HumanCompensationResolution,
    SagaWorkflowInput,
    WorkflowState,
)
from agentic_saga.temporal.workflow import AgenticSagaWorkflow

type SagaHandle = WorkflowHandle[AgenticSagaWorkflow, WorkflowState]
_WORKFLOW_NAME = "agentic_saga.workflow"
_SAFE_TASK_QUEUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


async def start_saga(
    client: Client,
    saga: SagaWorkflowInput,
    *,
    task_queue: str,
) -> SagaHandle:
    """Start one saga without obscuring the returned Temporal handle."""
    queue = _validate_task_queue(task_queue)
    return await _start_workflow(client, saga, queue)


async def _start_workflow(client: Client, saga: SagaWorkflowInput, queue: str) -> SagaHandle:
    handle = await client.start_workflow(
        _WORKFLOW_NAME,
        saga,
        id=saga.saga_id,
        task_queue=queue,
        result_type=WorkflowState,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
    )
    return cast(SagaHandle, handle)


def get_saga_handle(client: Client, saga_id: SagaId) -> SagaHandle:
    """Get a typed Temporal handle for an existing saga."""
    handle = client.get_workflow_handle(saga_id, result_type=WorkflowState)
    return cast(SagaHandle, handle)


async def query_saga_state(handle: SagaHandle) -> WorkflowState:
    """Read the workflow's durable typed state Query."""
    return await handle.query(AgenticSagaWorkflow.state)


async def resolve_human_compensation(
    handle: SagaHandle,
    resolution: HumanCompensationResolution,
) -> WorkflowState:
    """Submit the workflow's validated human compensation Update."""
    return await handle.execute_update(AgenticSagaWorkflow.resolve_compensation, resolution)


def _validate_task_queue(task_queue: str) -> str:
    if _SAFE_TASK_QUEUE.fullmatch(task_queue) is None:
        raise ValueError("task queue must be a safe non-blank identifier")
    return task_queue
