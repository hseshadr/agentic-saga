from __future__ import annotations

import argparse
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final, Protocol

from agentic_saga.demo.assets import MaterializationError, materialize_recorder_site
from agentic_saga.demo.reference import (
    DEFAULT_REFERENCE_SCENARIO,
    REFERENCE_SCENARIOS,
    ReferenceScenario,
    ReferenceTraceError,
    load_reference_trace,
)
from agentic_saga.demo.server import RecorderServer, RecorderServerError, serve_recorder

_MAX_PORT: Final[int] = 65535


class _Writer(Protocol):
    def write(self, value: str) -> object: ...

    def flush(self) -> None: ...


@dataclass(frozen=True)
class DemoArguments:
    """Validated inputs for the packaged recorder demonstration."""

    scenario: ReferenceScenario = DEFAULT_REFERENCE_SCENARIO
    open_browser: bool = False
    port: int = 0


def configure_demo_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Register the deterministic packaged recorder command."""
    parser = subparsers.add_parser("demo", help="serve a captured Saga trace")
    parser.add_argument(
        "--scenario", choices=REFERENCE_SCENARIOS, default=DEFAULT_REFERENCE_SCENARIO
    )
    parser.add_argument("--open", action="store_true", dest="open_browser")
    parser.add_argument("--port", type=_port, default=0)
    return parser


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer from 0 to 65535") from error
    if not 0 <= port <= _MAX_PORT:
        raise argparse.ArgumentTypeError("port must be an integer from 0 to 65535")
    return port


def run_demo(
    arguments: DemoArguments,
    *,
    output: _Writer | None = None,
    errors: _Writer | None = None,
) -> int:
    """Serve one captured reference trace until interrupted."""
    output_stream, error_stream = _demo_streams(output, errors)
    try:
        return _serve_demo(arguments, output_stream, error_stream)
    except KeyboardInterrupt:
        return 0
    except (
        ReferenceTraceError,
        MaterializationError,
        RecorderServerError,
        OSError,
        ExceptionGroup,
    ):
        _write_line(error_stream, "Error: unable to start the recorder demo.")
        return 2


def _demo_streams(output: _Writer | None, errors: _Writer | None) -> tuple[_Writer, _Writer]:
    output_stream = output if output is not None else sys.stdout
    error_stream = errors if errors is not None else sys.stderr
    return output_stream, error_stream


def _serve_demo(arguments: DemoArguments, output: _Writer, errors: _Writer) -> int:
    with TemporaryDirectory(prefix="agentic-saga-") as workspace:
        destination = Path(workspace).resolve(strict=True) / "site"
        trace = load_reference_trace(arguments.scenario)
        materialize_recorder_site(
            destination,
            {arguments.scenario: trace},
            presentation="ecommerce",
        )
        with serve_recorder(destination, port=arguments.port) as server:
            _write_line(output, f"Agentic Saga recorder: {server.url}")
            _open_browser(arguments.open_browser, server, errors)
            server.wait()
    return 0


def _open_browser(enabled: bool, server: RecorderServer, errors: _Writer) -> None:
    if not enabled:
        return
    try:
        opened = webbrowser.open(server.url)
    except Exception:
        opened = False
    if not opened:
        _write_line(errors, "Warning: unable to open the recorder in a browser.")


def _write_line(stream: _Writer, message: str) -> None:
    stream.write(f"{message}\n")
    stream.flush()


__all__ = ["DemoArguments", "configure_demo_parser", "run_demo"]
