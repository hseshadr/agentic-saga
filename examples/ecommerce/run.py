from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

from agentic_saga.contracts.trace import TraceEvent
from examples.ecommerce.demo import run_scenario
from examples.ecommerce.domain import DemoRun, ScenarioName

_VISIBLE = frozenset(
    {
        "read_observed",
        "effect_intent_recorded",
        "compensation_started",
        "compensation_intent_recorded",
        "effect_outcome_recorded",
        "reconciliation_recorded",
        "invariant_evaluated",
        "human_required",
        "terminal_assigned",
    }
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the offline Agentic Saga ecommerce demo.")
    parser.add_argument(
        "scenario",
        nargs="?",
        default=ScenarioName.BUSINESS_FAILURE.value,
        choices=tuple(item.value for item in ScenarioName),
    )
    return parser


def _line(entry: TraceEvent) -> str:
    tool = "" if entry.tool_name is None else f"  {entry.tool_name}"
    return f"{entry.saga_seq:02d}  {entry.event_type}{tool}"


def _render(run: DemoRun) -> str:
    heading = f"Agentic Saga · {run.scenario.value}"
    entries = tuple(_line(item) for item in run.trace.events if item.event_type in _VISIBLE)
    summary = f"Outcome: {run.result.state.value} · provider effects: {_sum_counts(run)}"
    proposals = f"Agent proposals: {' → '.join(run.proposals)}"
    return "\n".join((heading, proposals, *entries, summary, ""))


def _sum_counts(run: DemoRun) -> int:
    return sum(item.effects for item in run.counts)


def _main() -> int:
    scenario = _parser().parse_args().scenario
    with tempfile.TemporaryDirectory(prefix="agentic-saga-") as directory:
        run = asyncio.run(run_scenario(scenario, Path(directory)))
    sys.stdout.write(_render(run))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
