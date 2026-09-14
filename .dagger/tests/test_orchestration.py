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


@dataclass
class FailureProbe:
    events: list[str] = field(default_factory=list)
    started: asyncio.Event = field(default_factory=asyncio.Event)

    async def sibling(self) -> None:
        self.events.append("started")
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.events.append("stopped")

    async def fail(self) -> None:
        await self.started.wait()
        raise RuntimeError("python 3.13 failed")


async def _observe_failure_cleanup(probe: FailureProbe) -> tuple[str, ...]:
    try:
        await main._bounded_gather(probe.sibling(), probe.fail(), limit=CONCURRENCY_LIMIT)
    except RuntimeError:
        return tuple(probe.events)
    raise AssertionError("runtime failure was swallowed")


async def _observe_parent_cancellation(probe: FailureProbe) -> tuple[str, ...]:
    runner = asyncio.create_task(
        main._bounded_gather(probe.sibling(), asyncio.Event().wait(), limit=CONCURRENCY_LIMIT)
    )
    await probe.started.wait()
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner
    return tuple(probe.events)


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
    # Given a failing runtime lane and a sibling that is observably active.
    probe = FailureProbe()

    # When the bounded fan-out awaits that lane.
    # Then its failure remains visible and the helper stops its sibling before returning.
    with pytest.raises(RuntimeError, match=r"python 3\.13 failed"):
        asyncio.run(main._bounded_gather(probe.sibling(), probe.fail(), limit=CONCURRENCY_LIMIT))
    assert probe.events == ["started", "stopped"]


def test_bounded_gather_awaits_sibling_cancellation_before_propagating_failure() -> None:
    # Given a failing lane and a sibling whose cleanup is observable inside the event loop.
    probe = FailureProbe()

    # When the caller observes the propagated failure.
    events_at_failure = asyncio.run(_observe_failure_cleanup(probe))

    # Then helper-owned sibling cancellation has already completed.
    assert events_at_failure == ("started", "stopped")


def test_bounded_gather_awaits_siblings_when_its_parent_is_cancelled() -> None:
    # Given an active fan-out cancelled by its parent.
    probe = FailureProbe()

    # When cancellation reaches the bounded helper.
    events_at_cancellation = asyncio.run(_observe_parent_cancellation(probe))

    # Then active siblings finish cleanup before cancellation propagates.
    assert events_at_cancellation == ("started", "stopped")
