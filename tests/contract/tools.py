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
    DurableToolFault,
    DurableToolIdentityConflict,
)


class ContractCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    resource_id: str
    amount_minor: int = Field(strict=True, gt=0)


def _context(operation: str, *, compensation: bool = False) -> EffectContext:
    return EffectContext(
        saga_id="saga_0000000000000014",
        step_instance_id="step_00000014",
        operation_id=f"op_{operation * 64}",
        fence_token=1,
        delivery_attempt=1,
        forward_receipts=({"receipt_ref": "opaque://provider/forward00000001/v1"},)
        if compensation
        else (),
    )


def _reconcile(context: EffectContext) -> ReconcileContext:
    return ReconcileContext(
        **context.model_dump(),
        correlation="opaque://provider/request00000014/v1",
    )


class ToolAdapterContract:
    def make_tool(self, path: Path) -> DurableFakeTool:
        raise NotImplementedError

    @pytest.mark.asyncio
    async def test_same_key_and_command_is_idempotent(self, tmp_path: Path) -> None:
        tool = self.make_tool(tmp_path / "provider")
        context = _context("1")
        command = ContractCommand(resource_id="order-14", amount_minor=1400)

        first = await tool.execute(command, context)
        second = await tool.execute(command, context.model_copy(update={"delivery_attempt": 2}))

        assert first == second
        assert tool.effect_count(context.operation_id) == 1

    @pytest.mark.asyncio
    async def test_same_key_with_changed_command_is_rejected(self, tmp_path: Path) -> None:
        tool = self.make_tool(tmp_path / "provider")
        context = _context("2")
        await tool.execute(ContractCommand(resource_id="order-14", amount_minor=1400), context)

        with pytest.raises(DurableToolIdentityConflict):
            await tool.execute(ContractCommand(resource_id="order-14", amount_minor=1500), context)

    @pytest.mark.asyncio
    async def test_lost_response_reconciles_without_repeating_effect(self, tmp_path: Path) -> None:
        tool = self.make_tool(tmp_path / "provider")
        tool.set_fault(DurableToolFault.EFFECT_THEN_LOSE_RESPONSE)
        context = _context("3")
        command = ContractCommand(resource_id="order-14", amount_minor=1400)

        with pytest.raises(DurableResponseLost):
            await tool.execute(command, context)
        outcome = await tool.reconcile(command, _reconcile(context))

        assert isinstance(outcome, ReconcileEffectConfirmed)
        assert tool.effect_count(context.operation_id) == 1

    @pytest.mark.asyncio
    async def test_partial_effect_returns_exact_receipts(self, tmp_path: Path) -> None:
        tool = self.make_tool(tmp_path / "provider")
        tool.set_fault(DurableToolFault.PARTIAL_EFFECT)
        context = _context("4")

        outcome = await tool.execute(
            ContractCommand(resource_id="order-14", amount_minor=1400), context
        )

        assert isinstance(outcome, PartialEffectConfirmed)
        assert outcome.receipts == tool.partial_receipts(context.operation_id)

    @pytest.mark.asyncio
    async def test_compensation_retries_idempotently(self, tmp_path: Path) -> None:
        tool = self.make_tool(tmp_path / "provider")
        tool.set_fault(DurableToolFault.COMPENSATION_FAILURE_ONCE)
        context = _context("5", compensation=True)
        command = ContractCommand(resource_id="order-14", amount_minor=1400)

        with pytest.raises(RuntimeError, match="transient"):
            await tool.execute(command, context)
        outcome = await tool.execute(command, context.model_copy(update={"delivery_attempt": 2}))

        assert isinstance(outcome, EffectConfirmed)
        assert tool.effect_count(context.operation_id) == 1


__all__ = ["ContractCommand", "ToolAdapterContract"]
