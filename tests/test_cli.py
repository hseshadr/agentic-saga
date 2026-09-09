import sys
from collections.abc import Callable

import pytest
from pytest import CaptureFixture, MonkeyPatch

from agentic_saga.cli.demo import DemoArguments
from agentic_saga.cli.main import entrypoint, main


def test_cli_reports_version(capsys: CaptureFixture[str]) -> None:
    # Given / When
    result = main(["--version"])

    # Then
    assert result == 0
    assert capsys.readouterr().out.strip() == "agentic-saga 0.1.0"


def test_cli_prints_help_by_default(capsys: CaptureFixture[str]) -> None:
    # Given / When
    result = main([])

    # Then
    assert result == 0
    assert "usage: agentic-saga" in capsys.readouterr().out


def test_entrypoint_exits_zero(monkeypatch: MonkeyPatch) -> None:
    # Given
    monkeypatch.setattr(sys, "argv", ["agentic-saga"])

    # When / Then
    with pytest.raises(SystemExit) as exc_info:
        entrypoint()
    assert exc_info.value.code == 0


def test_should_list_exact_reference_scenarios_in_demo_help(
    capsys: CaptureFixture[str],
) -> None:
    # Given / When
    with pytest.raises(SystemExit) as exc_info:
        main(["demo", "--help"])

    # Then
    output = capsys.readouterr().out
    assert exc_info.value.code == 0
    assert "{happy-path,business-failure,lost-response,compensation-failure}" in output
    assert "--open" in output
    assert "--port" in output
    assert "--live" not in output


def test_should_default_demo_to_business_failure(monkeypatch: MonkeyPatch) -> None:
    # Given
    observed: list[DemoArguments] = []
    monkeypatch.setattr("agentic_saga.cli.main.run_demo", _record_demo_arguments(observed))

    # When
    result = main(["demo"])

    # Then
    assert result == 0
    assert len(observed) == 1
    arguments = observed[0]
    assert arguments.scenario == "business-failure"
    assert arguments.open_browser is False
    assert arguments.port == 0


def _record_demo_arguments(
    observed: list[DemoArguments],
) -> Callable[[DemoArguments], int]:
    def record(arguments: DemoArguments) -> int:
        observed.append(arguments)
        return 0

    return record


@pytest.mark.parametrize(
    "arguments",
    [
        ["demo", "--scenario", "invented"],
        ["demo", "--port", "-1"],
        ["demo", "--port", "65536"],
        ["demo", "--port", "not-a-port"],
    ],
)
def test_should_exit_two_when_demo_argument_is_invalid(arguments: list[str]) -> None:
    # Given / When / Then
    with pytest.raises(SystemExit) as exc_info:
        main(arguments)
    assert exc_info.value.code == 2
