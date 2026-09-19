from pathlib import Path

from tests.contract.tools import ToolAdapterContract
from tests.support.durable_tool import DurableFakeTool


class TestDurableFakeTool(ToolAdapterContract):
    def make_tool(self, path: Path) -> DurableFakeTool:
        return DurableFakeTool.initialize(path, "contract-tool")
