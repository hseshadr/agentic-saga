from pathlib import Path

import pytest
from pydantic import BaseModel

from agentic_saga.contracts.outcomes import EffectOutcome
from agentic_saga.contracts.tools import EffectContext
from tests.contract.tools import ToolAdapterContract, require_declared_fence
from tests.support.durable_tool import DurableFakeTool


class TestDurableFakeTool(ToolAdapterContract):
    def make_tool(self, path: Path) -> DurableFakeTool:
        return DurableFakeTool.initialize(path, "contract_tool")


class LyingFenceTool(DurableFakeTool):
    async def execute(self, command: BaseModel, context: EffectContext) -> EffectOutcome:
        forged = context.model_copy(update={"fence_token": 999})
        return await super().execute(command, forged)


@pytest.mark.asyncio
async def test_contract_detects_adapter_that_lies_about_fencing(tmp_path: Path) -> None:
    tool = LyingFenceTool.initialize(tmp_path / "lying.db", "lying_tool")

    with pytest.raises(AssertionError, match="declared fencing"):
        await require_declared_fence(tool)
