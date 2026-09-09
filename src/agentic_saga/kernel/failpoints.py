from __future__ import annotations

from enum import StrEnum
from typing import Protocol


class DurabilityPoint(StrEnum):
    """Name crash-test boundaries across intent, dispatch, outcome, and terminal writes."""

    BEFORE_INTENT_COMMIT = "before_intent_commit"
    AFTER_INTENT_COMMIT = "after_intent_commit"
    AFTER_DISPATCH_RECORD = "after_dispatch_record"
    AFTER_PROVIDER_EFFECT = "after_provider_effect"
    AFTER_OUTCOME_COMMIT = "after_outcome_commit"
    AFTER_COMPENSATION_INTENT = "after_compensation_intent"
    AFTER_COMPENSATION_EFFECT = "after_compensation_effect"
    BEFORE_TERMINAL_COMMIT = "before_terminal_commit"


class DurabilityFailpoint(Protocol):
    """Inject controlled failures at semantic durability boundaries."""

    def hit(self, point: DurabilityPoint) -> None: ...


class NoOpDurabilityFailpoint:
    """Leave every durability boundary uninterrupted in normal execution."""

    def hit(self, point: DurabilityPoint) -> None:
        del point


__all__ = ["DurabilityFailpoint", "DurabilityPoint", "NoOpDurabilityFailpoint"]
