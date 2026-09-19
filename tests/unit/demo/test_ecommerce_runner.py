from __future__ import annotations

import asyncio
import json
import webbrowser
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentic_saga.contracts.runtime import SagaStatus
from agentic_saga.contracts.trace import RunTrace
from agentic_saga.temporal.contracts import WorkflowState
from examples.ecommerce import demo, run
from examples.ecommerce.demo import EcommerceRun
from examples.ecommerce.domain import ProviderState, ScenarioName


def _run(scenario: ScenarioName) -> EcommerceRun:
    source = Path("examples/ecommerce/flight-recorder/traces") / f"{scenario.value}.json"
    trace = RunTrace.model_validate_json(source.read_bytes())
    state = WorkflowState(
        saga_id=trace.saga_id,
        status=trace.outcome,
        events=(),
        compensations=(),
        human_required_reason="refund_unproven"
        if trace.outcome is SagaStatus.HUMAN_REQUIRED
        else None,
    )
    return EcommerceRun(scenario, state, trace, (), (), (), (), ProviderState())


@pytest.mark.asyncio
async def test_all_executes_each_scenario_once_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    client = object()
    connect = AsyncMock(return_value=client)
    execute = AsyncMock(side_effect=[_run(scenario) for scenario in ScenarioName])
    monkeypatch.setattr(run, "connect_local_client", connect)
    monkeypatch.setattr(run, "run_with_client", execute)

    result = await run._run(run._parser().parse_args(["all", "--task-queue", "demo-test"]))

    assert tuple(item.scenario for item in result) == tuple(ScenarioName)
    assert [call.args for call in execute.await_args_list] == [
        (client, scenario) for scenario in ScenarioName
    ]
    assert all(call.kwargs == {"task_queue": "demo-test"} for call in execute.await_args_list)
    connect.assert_awaited_once_with("localhost:7233")


@pytest.mark.asyncio
async def test_single_selection_only_executes_selected_scenario(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execute = AsyncMock(return_value=_run(ScenarioName.LOST_RESPONSE))
    monkeypatch.setattr(run, "connect_local_client", AsyncMock())
    monkeypatch.setattr(run, "run_with_client", execute)

    result = await run._run(run._parser().parse_args(["lost-response"]))

    assert len(result) == 1
    assert execute.await_args is not None
    assert execute.await_args.args[1] is ScenarioName.LOST_RESPONSE


@pytest.mark.parametrize("port", ["-1", "65536", "oops"])
def test_invalid_ports_are_rejected(port: str) -> None:
    with pytest.raises(SystemExit):
        run._parser().parse_args(["all", "--open", "--port", port])


def test_results_distinguish_order_failure_from_successful_recovery() -> None:
    assert "Order succeeded." in run._render(_run(ScenarioName.HAPPY_PATH))
    assert "Order failed. Recovery succeeded" in run._render(_run(ScenarioName.BUSINESS_FAILURE))
    assert "Order is unresolved: human_required" in run._render(
        _run(ScenarioName.COMPENSATION_FAILURE)
    )


def test_serving_exports_every_actual_trace_and_explains_server_lifetime(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = tuple(_run(scenario) for scenario in ScenarioName)
    ports: list[int] = []
    server = MagicMock()
    server.url = "http://127.0.0.1:12345"
    server.__enter__.return_value = server

    def serve(destination: Path, *, port: int, static_directory: Path) -> MagicMock:
        ports.append(port)
        assert (static_directory / "index.html").is_file()
        catalog = json.loads((destination / "traces" / "index.json").read_text())
        assert {item["id"] for item in catalog["runs"]} == set(ScenarioName)
        assert all(item["presentation"] == "ecommerce" for item in catalog["runs"])
        for actual in runs:
            payload = destination / "traces" / f"{actual.scenario.value}.json"
            assert RunTrace.model_validate_json(payload.read_bytes()) == actual.trace
        return server

    monkeypatch.setattr(run, "serve_recorder", serve)
    browser = MagicMock(return_value=True)
    monkeypatch.setattr(webbrowser, "open", browser)

    run._serve(runs, 12345)

    assert ports == [12345]
    browser.assert_called_once_with(server.url)
    server.wait.assert_called_once()
    output = capsys.readouterr().out
    assert "All selected runs finished" in output
    assert "Only the replay web server remains active" in output


@pytest.mark.asyncio
async def test_human_wait_allows_more_than_one_hundred_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = _run(ScenarioName.HAPPY_PATH).state
    paused = _run(ScenarioName.COMPENSATION_FAILURE).state
    handle = MagicMock()
    handle.query = AsyncMock(side_effect=[pending] * 101 + [paused])
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    assert await demo._wait_for_human(handle) == paused
    assert handle.query.await_count == 102


@pytest.mark.asyncio
async def test_human_wait_has_a_wall_clock_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow_query(*_: object) -> None:
        await asyncio.sleep(1)

    handle = MagicMock()
    handle.query = slow_query
    monkeypatch.setattr(demo, "_HUMAN_WAIT_SECONDS", 0.001)

    with pytest.raises(TimeoutError):
        await demo._wait_for_human(handle)
