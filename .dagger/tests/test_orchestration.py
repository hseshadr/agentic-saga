from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from agentic_saga_ci import main

CONCURRENCY_LIMIT = 2


@dataclass
class ConcurrencyProbe:
    active: int = 0
    maximum_active: int = 0
    completed: list[int] = field(default_factory=list)

    async def operation(self, index: int) -> None:
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        await asyncio.sleep(0)
        self.completed.append(index)
        self.active -= 1


def test_bounded_gather_never_runs_more_than_two_operations() -> None:
    # Given five real awaitables that expose their active concurrency.
    probe = ConcurrencyProbe()

    # When the bounded fan-out executes them with two permits.
    operations = (probe.operation(index) for index in range(5))
    asyncio.run(main._bounded_gather(*operations, limit=CONCURRENCY_LIMIT))

    # Then every operation completes without more than two running together.
    assert probe.maximum_active == CONCURRENCY_LIMIT
    assert sorted(probe.completed) == list(range(5))


def test_bounded_gather_propagates_a_lane_failure() -> None:
    # Given one runtime lane that fails with its concrete cause.
    async def fail() -> None:
        raise RuntimeError("python 3.13 failed")

    # When the bounded fan-out awaits that lane.
    # Then its failure remains visible to the public CI caller.
    with pytest.raises(RuntimeError, match=r"python 3\.13 failed"):
        asyncio.run(main._bounded_gather(fail(), limit=CONCURRENCY_LIMIT))
