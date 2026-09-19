from __future__ import annotations

import argparse
import asyncio
import sys

from agentic_saga.contracts.trace import TraceEvent
from agentic_saga.temporal import connect_local_client
from examples.ecommerce.demo import EcommerceRun, run_with_client
from examples.ecommerce.domain import ScenarioName

_VISIBLE = frozenset(
    {
        "saga_started",
        "effect_outcome_recorded",
        "reconciliation_recorded",
        "compensation_started",
        "compensation_outcome_recorded",
        "invariant_evaluated",
        "human_required",
        "human_resolved",
        "terminal_assigned",
    }
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Temporal ecommerce Saga demo.")
    parser.add_argument(
        "scenario",
        nargs="?",
        default=ScenarioName.BUSINESS_FAILURE.value,
        choices=tuple(item.value for item in ScenarioName),
    )
    parser.add_argument("--temporal-address", default="localhost:7233")
    parser.add_argument("--task-queue", default=None)
    return parser


def _line(entry: TraceEvent) -> str:
    tool = "" if entry.tool_name is None else f"  {entry.tool_name}"
    return f"{entry.saga_seq:02d}  {entry.event_type}{tool}"


def _render(run: EcommerceRun) -> str:
    heading = f"Agentic Saga · {run.scenario.value}"
    entries = tuple(_line(item) for item in run.trace.events if item.event_type in _VISIBLE)
    summary = f"Outcome: {run.state.status.value} · provider effects: {_sum_counts(run)}"
    proposals = f"Agent proposals: {' → '.join(run.proposals)}"
    return "\n".join((heading, proposals, *entries, summary, ""))


def _sum_counts(run: EcommerceRun) -> int:
    return sum(item.effects for item in run.counts)


async def _run(arguments: argparse.Namespace) -> EcommerceRun:
    client = await connect_local_client(arguments.temporal_address)
    return await run_with_client(
        client,
        arguments.scenario,
        task_queue=arguments.task_queue,
    )


def _main() -> int:
    run = asyncio.run(_run(_parser().parse_args()))
    sys.stdout.write(_render(run))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
