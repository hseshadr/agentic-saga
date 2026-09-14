from __future__ import annotations

import asyncio
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agentic_saga.contracts.tools import EffectContext
from tests.support.durable_tool import DurableToolIdentityConflict
from tests.support.kernel_harness import STEP_ID, KernelHarness

_SCHEDULING_DELAY_SECONDS = 0.25


def _same_command_effect_count(retries: int) -> int:
    with TemporaryDirectory(prefix="agentic-saga-idempotency-") as directory:
        harness = KernelHarness.create(Path(directory))
        asyncio.run(harness.dispatcher.dispatch_saga(harness.lease.saga_id, harness.lease.owner))
        for attempt in range(2, retries + 2):
            asyncio.run(harness.redeliver_same_operation(attempt))
        return harness.provider.effect_count(harness.operation_id)


def _changed_command_effect_count(quantity: int) -> int:
    with TemporaryDirectory(prefix="agentic-saga-identity-") as directory:
        harness = KernelHarness.create(Path(directory))
        asyncio.run(harness.dispatcher.dispatch_saga(harness.lease.saga_id, harness.lease.owner))
        command = harness.command.model_copy(update={"quantity": quantity})
        try:
            asyncio.run(harness.provider.execute(command, _redelivery_context(harness)))
        except DurableToolIdentityConflict:
            return harness.provider.effect_count(harness.operation_id)
    raise AssertionError("changed command identity was accepted")


def _redelivery_context(harness: KernelHarness) -> EffectContext:
    return EffectContext(
        saga_id=harness.lease.saga_id,
        step_instance_id=STEP_ID,
        operation_id=harness.operation_id,
        fence_token=harness.lease.fence_token,
        delivery_attempt=2,
    )


def _delayed_effect_count(_: int) -> int:
    time.sleep(_SCHEDULING_DELAY_SECONDS)
    return 1


@given(retries=st.integers(min_value=0, max_value=4))
@settings(deadline=None)
def test_same_operation_and_command_has_one_business_effect(retries: int) -> None:
    # Catches mutation: treating a durable redelivery as another provider business effect.
    assert _same_command_effect_count(retries) == 1


def test_should_tolerate_scheduling_delay_when_checking_idempotency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setitem(globals(), "_same_command_effect_count", _delayed_effect_count)
    # When
    result = test_same_operation_and_command_has_one_business_effect()
    # Then
    assert result is None


@given(quantity=st.integers(min_value=1, max_value=1_000))
@settings(deadline=None)
def test_changed_command_reuse_is_rejected(quantity: int) -> None:
    # Catches mutation: accepting changed command bytes under an existing operation identity.
    assert _changed_command_effect_count(quantity) == 1
