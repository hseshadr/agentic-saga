from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from threading import Barrier

import pytest
from pydantic import BaseModel, ConfigDict

from agentic_saga.contracts.common import sha256_json
from agentic_saga.contracts.outcomes import EffectOutcome
from agentic_saga.contracts.tools import EffectContext
from tests.support.durable_tool import DurableFakeTool, DurableToolIdentityConflict
from tests.support.kernel_harness import SAGA_ID, STEP_ID, TOOL_NAME, KernelHarness


class PrivateCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    resource_id: str
    api_key: str


def _context(operation_id: str, attempt: int = 1) -> EffectContext:
    return EffectContext(
        saga_id=SAGA_ID,
        step_instance_id=STEP_ID,
        operation_id=operation_id,
        fence_token=1,
        delivery_attempt=attempt,
    )


type _RaceResult = EffectOutcome | DurableToolIdentityConflict


def _execute_at_barrier(
    provider: DurableFakeTool,
    command: PrivateCommand,
    context: EffectContext,
    barrier: Barrier,
) -> EffectOutcome:
    barrier.wait()
    return asyncio.run(provider.execute(command, context))


def _race_result(future: Future[EffectOutcome]) -> _RaceResult:
    try:
        return future.result()
    except DurableToolIdentityConflict as error:
        return error


def _race(
    first: DurableFakeTool,
    second: DurableFakeTool,
    commands: tuple[PrivateCommand, PrivateCommand],
    operation_id: str,
) -> tuple[_RaceResult, _RaceResult]:
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        context = _context(operation_id)
        left = executor.submit(_execute_at_barrier, first, commands[0], context, barrier)
        right = executor.submit(_execute_at_barrier, second, commands[1], context, barrier)
    return _race_result(left), _race_result(right)


def _assert_private_command_absent(provider: DurableFakeTool, command: PrivateCommand) -> None:
    material = command.api_key
    artifacts = (
        material.encode(),
        sha256(material.encode()).hexdigest().encode(),
        sha256_json(command.model_dump(mode="json")).encode(),
    )
    assert all(artifact not in provider.durable_bytes() for artifact in artifacts)
    assert all(material not in repr(call) for call in provider.calls)


@pytest.mark.asyncio
async def test_one_hundred_deliveries_reuse_one_identity_and_provider_effect(
    tmp_path: Path,
) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)

    # When
    await harness.dispatcher.dispatch_one("worker-a")
    for attempt in range(2, 101):
        await harness.redeliver_same_operation(attempt)

    # Then
    assert harness.provider.effect_count(harness.operation_id) == 1
    assert {call.operation_id for call in harness.provider.calls} == {harness.operation_id}
    assert {call.delivery_attempt for call in harness.provider.calls} == set(range(1, 101))


def test_independent_fake_instances_concurrently_create_one_provider_effect(
    tmp_path: Path,
) -> None:
    # Given
    path = tmp_path / "provider.db"
    first = DurableFakeTool.initialize(path, TOOL_NAME)
    second = DurableFakeTool.open(path, TOOL_NAME)
    command = PrivateCommand(resource_id="order_8", api_key="not-persisted-credential")
    operation_id = f"op_{'9' * 64}"

    # When
    outcomes = _race(first, second, (command, command), operation_id)

    # Then
    assert outcomes[0] == outcomes[1]
    assert first.effect_count(operation_id) == 1


def test_concurrent_changed_public_hash_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "provider.db"
    first = DurableFakeTool.initialize(path, TOOL_NAME)
    second = DurableFakeTool.open(path, TOOL_NAME)
    operation_id = f"op_{'5' * 64}"
    commands = (
        PrivateCommand(resource_id="order_8", api_key="first-private-value"),
        PrivateCommand(resource_id="order_9", api_key="second-private-value"),
    )

    results = _race(first, second, commands, operation_id)

    assert sum(isinstance(result, DurableToolIdentityConflict) for result in results) == 1
    assert first.effect_count(operation_id) == 1


@pytest.mark.asyncio
async def test_fake_tool_fails_closed_when_same_identity_has_changed_command_hash(
    tmp_path: Path,
) -> None:
    # Given
    provider = DurableFakeTool.initialize(tmp_path / "provider.db", TOOL_NAME)
    operation_id = f"op_{'7' * 64}"
    first = PrivateCommand(resource_id="order_8", api_key="first-private-value")
    changed = PrivateCommand(resource_id="order_9", api_key="second-private-value")
    await provider.execute(first, _context(operation_id))

    # When / Then
    with pytest.raises(DurableToolIdentityConflict, match="identity conflict"):
        await provider.execute(changed, _context(operation_id, 2))
    assert provider.effect_count(operation_id) == 1


@pytest.mark.asyncio
async def test_fake_tool_private_field_change_preserves_public_audit_identity(
    tmp_path: Path,
) -> None:
    provider = DurableFakeTool.initialize(tmp_path / "provider.db", TOOL_NAME)
    operation_id = f"op_{'8' * 64}"
    first = PrivateCommand(resource_id="order_8", api_key="first-private-value")
    changed_private = PrivateCommand(resource_id="order_8", api_key="second-private-value")

    first_outcome = await provider.execute(first, _context(operation_id))
    second_outcome = await provider.execute(changed_private, _context(operation_id, 2))

    assert first_outcome == second_outcome
    assert provider.effect_count(operation_id) == 1
    assert len({call.command_hash for call in provider.calls}) == 1
    assert len({call.receipt_ref for call in provider.calls}) == 1
    _assert_private_command_absent(provider, first)
    _assert_private_command_absent(provider, changed_private)


@pytest.mark.asyncio
async def test_fake_provider_audit_persists_no_raw_credential_or_direct_digest(
    tmp_path: Path,
) -> None:
    # Given
    provider = DurableFakeTool.initialize(tmp_path / "provider.db", TOOL_NAME)
    private_material = "provider-must-not-record-this-credential"
    command = PrivateCommand(resource_id="order_8", api_key=private_material)
    operation_id = f"op_{'6' * 64}"

    # When
    await provider.execute(command, _context(operation_id))

    # Then
    _assert_private_command_absent(provider, command)
