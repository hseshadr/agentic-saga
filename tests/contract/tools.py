from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field

from agentic_saga.contracts.outcomes import (
    EffectConfirmed,
    PartialEffectConfirmed,
    ReconcileEffectConfirmed,
)
from agentic_saga.contracts.tools import EffectContext, ReconcileContext
from tests.support.durable_tool import (
    DurableFakeTool,
    DurableResponseLost,
    DurableStaleFence,
    DurableToolFault,
    DurableToolIdentityConflict,
)

_FORWARD_RECEIPTS = ({"forward_receipt": "opaque://provider/forward00000001/v1"},)


class ContractCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    resource_id: str
    amount_minor: int = Field(strict=True, gt=0)


def _context(
    operation: str,
    fence: int = 1,
    saga_id: str = "saga_0000000000000014",
    *,
    compensation: bool = False,
) -> EffectContext:
    return EffectContext(
        saga_id=saga_id,
        step_instance_id="step_00000014",
        operation_id=f"op_{operation * 64}",
        fence_token=fence,
        delivery_attempt=1,
        forward_receipts=_FORWARD_RECEIPTS if compensation else (),
    )


def _reconcile(context: EffectContext) -> ReconcileContext:
    return ReconcileContext(
        **context.model_dump(), correlation="opaque://provider/request00000014/v1"
    )


async def require_declared_fence(tool: DurableFakeTool) -> None:
    command = ContractCommand(resource_id="shared-order", amount_minor=1400)
    await tool.execute(command, _context("4", fence=2))
    try:
        await tool.execute(command, _context("5", fence=1))
    except DurableStaleFence:
        return
    raise AssertionError("adapter declared fencing but accepted a stale resource fence")


class ToolAdapterContract:
    def make_tool(self, path: Path) -> DurableFakeTool:
        raise NotImplementedError

    @pytest.mark.asyncio
    async def test_stable_key_deduplicates_same_command(self, tmp_path: Path) -> None:
        tool = self.make_tool(tmp_path / "provider.db")
        context = _context("1")
        command = ContractCommand(resource_id="order-14", amount_minor=1400)

        first = await tool.execute(command, context)
        second = await tool.execute(command, context.model_copy(update={"delivery_attempt": 2}))

        assert first == second
        assert tool.execute_call_count == 2
        assert tool.effect_count(context.operation_id) == 1

    @pytest.mark.asyncio
    async def test_same_key_with_different_command_is_rejected(self, tmp_path: Path) -> None:
        tool = self.make_tool(tmp_path / "provider.db")
        context = _context("2")
        await tool.execute(ContractCommand(resource_id="order-14", amount_minor=1400), context)

        with pytest.raises(DurableToolIdentityConflict):
            await tool.execute(ContractCommand(resource_id="order-14", amount_minor=1500), context)
        assert tool.effect_count(context.operation_id) == 1
        assert tool.execute_call_count == 2
        assert len(tool.calls) == 2
        assert tool.calls[-1].receipt_ref is None

    @pytest.mark.asyncio
    async def test_lost_response_reconciles_without_second_effect(self, tmp_path: Path) -> None:
        tool = self.make_tool(tmp_path / "provider.db")
        tool.set_fault(DurableToolFault.EFFECT_THEN_LOSE_RESPONSE)
        context = _context("3")
        command = ContractCommand(resource_id="order-14", amount_minor=1400)

        with pytest.raises(DurableResponseLost):
            await tool.execute(command, context)
        outcome = await tool.reconcile(command, _reconcile(context))

        assert isinstance(outcome, ReconcileEffectConfirmed)
        assert tool.execute_call_count == 1
        assert tool.effect_count(context.operation_id) == 1

    @pytest.mark.asyncio
    async def test_declared_fence_rejects_stale_cross_saga_writer(self, tmp_path: Path) -> None:
        tool = self.make_tool(tmp_path / "provider.db")
        command = ContractCommand(resource_id="shared-order", amount_minor=1400)
        first = _context("4", fence=2, saga_id="saga_0000000000000014")
        second = _context("5", fence=1, saga_id="saga_0000000000000015")

        await tool.execute(command, first)
        with pytest.raises(DurableStaleFence):
            await tool.execute(command, second)

        assert first.saga_id != second.saga_id
        assert tool.effect_count(second.operation_id) == 0
        assert tool.execute_call_count == 2
        assert len(tool.calls) == 2
        assert tool.calls[-1].receipt_ref is None

    @pytest.mark.asyncio
    async def test_partial_effect_returns_exact_receipts(self, tmp_path: Path) -> None:
        tool = self.make_tool(tmp_path / "provider.db")
        tool.set_fault(DurableToolFault.PARTIAL_EFFECT)

        outcome = await tool.execute(
            ContractCommand(resource_id="order-14", amount_minor=1400), _context("6")
        )

        assert isinstance(outcome, PartialEffectConfirmed)
        assert outcome.receipts == tool.partial_receipts(_context("6").operation_id)

    @pytest.mark.asyncio
    async def test_compensation_retries_idempotently(self, tmp_path: Path) -> None:
        tool = self.make_tool(tmp_path / "provider.db")
        tool.set_fault(DurableToolFault.COMPENSATION_FAILURE_ONCE)
        context = _context("7", compensation=True)
        command = ContractCommand(resource_id="order-14", amount_minor=1400)

        with pytest.raises(RuntimeError, match="transient"):
            await tool.execute(command, context)
        outcome = await tool.execute(command, context.model_copy(update={"delivery_attempt": 2}))

        assert isinstance(outcome, EffectConfirmed)
        assert tool.effect_count(context.operation_id) == 1


__all__ = ["ContractCommand", "ToolAdapterContract", "require_declared_fence"]
