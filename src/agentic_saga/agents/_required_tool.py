from __future__ import annotations

from collections.abc import Callable

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import RunContext


def _required(_: RunContext[object]) -> ModelSettings:
    return ModelSettings(tool_choice="required")


class RequireToolCall(AbstractCapability[object]):
    """Require the single external proposal call while keeping text output type-safe."""

    def get_model_settings(self) -> Callable[[RunContext[object]], ModelSettings]:
        return _required


__all__ = ["RequireToolCall"]
