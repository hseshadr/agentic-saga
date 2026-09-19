import re
from collections.abc import Awaitable, Callable
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, field_validator
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.service import TLSConfig
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from agentic_saga.temporal.activities import TemporalActivities
from agentic_saga.temporal.contracts import (
    HumanResolutionVerificationRequest,
    HumanResolutionVerificationResult,
)
from agentic_saga.temporal.workflow import AgenticSagaWorkflow

type HumanResolutionActivity = Callable[
    [HumanResolutionVerificationRequest], Awaitable[HumanResolutionVerificationResult]
]
type _NonBlank = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]
_LOCAL_TARGET = re.compile(r"^(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?$")


class TemporalCloudConfig(BaseModel):
    """Typed TLS and API-key settings for a Temporal Cloud connection."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)

    target_host: _NonBlank
    namespace: _NonBlank
    api_key: SecretStr = Field(repr=False)
    tls_domain: _NonBlank | None = None
    server_root_ca_cert: bytes | None = Field(default=None, repr=False)

    @field_validator("api_key")
    @classmethod
    def require_api_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            raise ValueError("api key must not be blank")
        return value


async def connect_client(target_host: str, *, namespace: str = "default") -> Client:
    """Backward-compatible local-development connection; rejects remote targets."""
    return await connect_local_client(target_host, namespace=namespace)


async def connect_local_client(target_host: str, *, namespace: str = "default") -> Client:
    """Connect without TLS only to an explicit loopback Temporal development server."""
    if _LOCAL_TARGET.fullmatch(target_host) is None:
        raise ValueError("local client requires a local loopback target")
    return await Client.connect(
        target_host,
        namespace=namespace,
        data_converter=pydantic_data_converter,
        tls=False,
    )


async def connect_cloud_client(config: TemporalCloudConfig) -> Client:
    """Connect with TLS and a masked API key using the official Temporal SDK."""
    tls = TLSConfig(
        server_root_ca_cert=config.server_root_ca_cert,
        domain=config.tls_domain,
    )
    return await Client.connect(
        config.target_host,
        namespace=config.namespace,
        api_key=config.api_key.get_secret_value(),
        data_converter=pydantic_data_converter,
        tls=tls,
    )


def build_worker(
    client: Client,
    *,
    task_queue: str,
    activities: TemporalActivities,
    human_resolution_activity: HumanResolutionActivity,
) -> Worker:
    """Register the one Workflow and its I/O-only Activities on an explicit queue."""
    if not task_queue.strip():
        raise ValueError("task queue must not be blank")
    registered = _activity_functions(activities, human_resolution_activity)
    return _build_worker(client, task_queue, registered)


def _build_worker(
    client: Client, task_queue: str, activities: list[Callable[..., object]]
) -> Worker:
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[AgenticSagaWorkflow],
        activities=activities,
        workflow_runner=saga_workflow_runner(),
    )


def _activity_functions(
    activities: TemporalActivities,
    human_resolution_activity: HumanResolutionActivity,
) -> list[Callable[..., object]]:
    return [
        activities.decide,
        activities.execute_tool,
        activities.reconcile,
        human_resolution_activity,
    ]


def saga_workflow_runner() -> SandboxedWorkflowRunner:
    """Pass through only immutable contracts required by the eager facade."""
    restrictions = SandboxRestrictions.default.with_passthrough_modules(
        "agentic_saga.contracts.actions",
        "agentic_saga.contracts.common",
        "agentic_saga.contracts.redaction",
        "agentic_saga.contracts.runtime",
        "agentic_saga.temporal.contracts",
        "agentic_saga.temporal.journal",
        "ruamel",
    )
    return SandboxedWorkflowRunner(restrictions=restrictions)
