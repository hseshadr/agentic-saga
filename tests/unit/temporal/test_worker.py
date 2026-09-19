from __future__ import annotations

from collections.abc import Sequence
from unittest.mock import AsyncMock, Mock, patch

import pytest
from pydantic import SecretStr
from temporalio.service import TLSConfig

from agentic_saga.contracts.actions import AgentProposal, Finish
from agentic_saga.contracts.runtime import (
    ExecutionBudget,
    SagaObservation,
    ToolDescriptor,
)
from agentic_saga.contracts.tools import ToolRegistry
from agentic_saga.temporal.activities import TemporalActivities
from agentic_saga.temporal.contracts import (
    HumanResolutionVerificationRequest,
    HumanResolutionVerificationResult,
)
from agentic_saga.temporal.worker import (
    TemporalCloudConfig,
    build_worker,
    connect_client,
    connect_cloud_client,
    connect_local_client,
    saga_workflow_runner,
)
from agentic_saga.temporal.workflow import AgenticSagaWorkflow


class Agent:
    async def next_action(
        self, observation: SagaObservation, available_tools: Sequence[ToolDescriptor]
    ) -> AgentProposal:
        del available_tools
        return Finish(
            proposal_id="proposal_12345678",
            based_on_saga_seq=observation.saga_seq,
            rationale="The goal is complete.",
            target_status="succeeded_verified",
        )


def _budget() -> ExecutionBudget:
    return ExecutionBudget(
        turn_limit=1,
        tool_call_limit=1,
        elapsed_ms_limit=1,
        token_limit=1,
    )


async def _verify_human_resolution(
    request: HumanResolutionVerificationRequest,
) -> HumanResolutionVerificationResult:
    del request
    return HumanResolutionVerificationResult.accepted(
        trusted_actor="operator_1", authorization_id="authz_1234567890abcdef"
    )


@pytest.mark.asyncio
async def test_connect_client_uses_pydantic_converter() -> None:
    with patch("agentic_saga.temporal.worker.Client.connect", new_callable=AsyncMock) as connect:
        await connect_client("localhost:7233", namespace="sagas")

    assert connect.await_args is not None
    kwargs = connect.await_args.kwargs
    assert kwargs["namespace"] == "sagas"
    assert kwargs["data_converter"].payload_converter_class.__name__ == "PydanticPayloadConverter"


@pytest.mark.asyncio
async def test_local_client_rejects_non_loopback_target() -> None:
    with (
        patch("agentic_saga.temporal.worker.Client.connect", new_callable=AsyncMock) as connect,
        pytest.raises(ValueError, match="local loopback"),
    ):
        await connect_local_client("production.example.com:7233")

    connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_cloud_client_requires_typed_tls_and_masked_api_key() -> None:
    config = TemporalCloudConfig(
        target_host="namespace.tmprl.cloud:7233",
        namespace="namespace.account",
        api_key=SecretStr("not-logged-api-key"),
        tls_domain="namespace.tmprl.cloud",
    )

    with patch("agentic_saga.temporal.worker.Client.connect", new_callable=AsyncMock) as connect:
        await connect_cloud_client(config)

    assert "not-logged-api-key" not in repr(config)
    assert connect.await_args is not None
    kwargs = connect.await_args.kwargs
    assert kwargs["api_key"] == "not-logged-api-key"
    assert kwargs["namespace"] == "namespace.account"
    assert kwargs["tls"] == TLSConfig(domain="namespace.tmprl.cloud")
    assert kwargs["data_converter"].payload_converter_class.__name__ == "PydanticPayloadConverter"


def test_worker_registers_exact_workflow_and_named_activities() -> None:
    client = Mock()
    activities = TemporalActivities(Agent(), ToolRegistry(), _budget())

    with patch("agentic_saga.temporal.worker.Worker") as worker_type:
        worker = build_worker(
            client,
            task_queue="agentic-saga",
            activities=activities,
            human_resolution_activity=_verify_human_resolution,
        )

    assert worker is worker_type.return_value
    kwargs = worker_type.call_args.kwargs
    assert kwargs["task_queue"] == "agentic-saga"
    assert kwargs["workflows"] == [AgenticSagaWorkflow]
    assert kwargs["activities"] == [
        activities.decide,
        activities.execute_tool,
        activities.reconcile,
        _verify_human_resolution,
    ]


def test_worker_rejects_blank_task_queue() -> None:
    activities = TemporalActivities(Agent(), ToolRegistry(), _budget())

    with pytest.raises(ValueError, match="task queue"):
        build_worker(
            Mock(),
            task_queue="",
            activities=activities,
            human_resolution_activity=_verify_human_resolution,
        )


def test_workflow_runner_does_not_passthrough_the_application_package() -> None:
    runner = saga_workflow_runner()

    assert "agentic_saga" not in runner.restrictions.passthrough_modules
