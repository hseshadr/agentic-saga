from __future__ import annotations

import argparse
import asyncio
import sys
import webbrowser
from importlib import resources
from pathlib import Path
from tempfile import TemporaryDirectory

from agentic_saga.contracts.runtime import SagaStatus
from agentic_saga.contracts.trace import TraceEvent
from agentic_saga.demo import materialize_recorder_site, serve_recorder
from agentic_saga.temporal import connect_local_client
from examples.ecommerce.demo import EcommerceRun, run_with_client
from examples.ecommerce.domain import ScenarioName

_MAX_PORT = 65535
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
        choices=("all", *(item.value for item in ScenarioName)),
    )
    parser.add_argument("--temporal-address", default="localhost:7233")
    parser.add_argument("--task-queue", default=None)
    parser.add_argument("--open", action="store_true", dest="open_browser")
    parser.add_argument("--port", type=_port, default=0)
    return parser


def _port(value: str) -> int:
    port = int(value)
    if not 0 <= port <= _MAX_PORT:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return port


def _line(entry: TraceEvent) -> str:
    tool = "" if entry.tool_name is None else f"  {entry.tool_name}"
    return f"{entry.saga_seq:02d}  {entry.event_type}{tool}"


def _render(run: EcommerceRun) -> str:
    heading = f"Agentic Saga · {run.scenario.value}"
    entries = tuple(_line(item) for item in run.trace.events if item.event_type in _VISIBLE)
    summary = f"Outcome: {run.state.status.value} · provider effects: {_sum_counts(run)}"
    proposals = f"Agent proposals: {' → '.join(run.proposals)}"
    return "\n".join((heading, proposals, *entries, summary, _result(run), ""))


def _result(run: EcommerceRun) -> str:
    if run.state.status is SagaStatus.SUCCEEDED_VERIFIED:
        return "Order succeeded. Verified complete."
    if run.state.status is SagaStatus.COMPENSATED_VERIFIED:
        human = (
            " Demo human resolution was verified before recovery resumed."
            if run.human_pause_status
            else ""
        )
        return f"Order failed. Recovery succeeded: all effects safely undone.{human}"
    return f"Order is unresolved: {run.state.status.value}."


def _sum_counts(run: EcommerceRun) -> int:
    return sum(item.effects for item in run.counts)


async def _run(arguments: argparse.Namespace) -> tuple[EcommerceRun, ...]:
    client = await connect_local_client(arguments.temporal_address)
    scenarios = (
        tuple(ScenarioName) if arguments.scenario == "all" else (ScenarioName(arguments.scenario),)
    )
    runs = []
    for scenario in scenarios:
        print(f"Running {scenario.value} on local Temporal…", flush=True)
        run = await run_with_client(client, scenario, task_queue=arguments.task_queue)
        print(_render(run), flush=True)
        runs.append(run)
    return tuple(runs)


def _serve(runs: tuple[EcommerceRun, ...], port: int) -> None:
    with (
        TemporaryDirectory(prefix="agentic-saga-ecommerce-") as workspace,
        resources.as_file(resources.files("agentic_saga.demo").joinpath("static")) as static,
    ):
        destination = Path(workspace).resolve(strict=True) / "site"
        traces = {run.scenario.value: run.trace for run in runs}
        materialize_recorder_site(
            destination,
            traces,
            presentation="ecommerce",
            default_run_id=runs[0].scenario.value,
            recording_mode="scripted",
        )
        with serve_recorder(destination, port=port, static_directory=static) as server:
            print(f"Flight Recorder: {server.url}", flush=True)
            print(
                "All selected runs finished. Only the replay web server remains active; "
                "Ctrl+C stops it.",
                flush=True,
            )
            _open_browser(server.url)
            server.wait()


def _open_browser(url: str) -> None:
    try:
        opened = webbrowser.open(url)
    except Exception:
        opened = False
    if not opened:
        print(f"Open {url} in your browser to view the results.", file=sys.stderr)


def _main() -> int:
    arguments = _parser().parse_args()
    print(
        "Execution: local Temporal · scripted CheckoutAgent · simulated providers · "
        "no JEV / no live model calls.",
        flush=True,
    )
    try:
        runs = asyncio.run(_run(arguments))
        print(f"Completed {len(runs)}/{len(runs)} scenarios.", flush=True)
        if arguments.open_browser:
            _serve(runs, arguments.port)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
