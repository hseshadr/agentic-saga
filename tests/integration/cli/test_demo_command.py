from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from socket import SO_REUSEADDR, SOL_SOCKET, socket
from urllib.parse import urlsplit
from urllib.request import urlopen

import pytest
from pytest import MonkeyPatch

from agentic_saga.cli.demo import DemoArguments, run_demo
from agentic_saga.contracts.trace import RunTrace
from agentic_saga.demo.assets import MaterializationError
from agentic_saga.demo.reference import REFERENCE_SCENARIOS, ReferenceTraceError
from agentic_saga.demo.server import RecorderServer

pytestmark = pytest.mark.enable_socket


@dataclass
class _Output:
    stream: StringIO
    flushes: int = 0

    def write(self, value: str) -> int:
        return self.stream.write(value)

    def flush(self) -> None:
        self.flushes += 1


def _arguments(*, open_browser: bool = False, port: int = 0) -> DemoArguments:
    return DemoArguments(scenario="business-failure", open_browser=open_browser, port=port)


def _interrupt_wait(_: RecorderServer) -> None:
    raise KeyboardInterrupt


def test_should_print_flush_and_stop_cleanly_on_keyboard_interrupt(
    monkeypatch: MonkeyPatch,
) -> None:
    # Given
    output = _Output(StringIO())
    monkeypatch.setattr(RecorderServer, "wait", _interrupt_wait)
    monkeypatch.setattr("agentic_saga.cli.demo.webbrowser.open", _unexpected_browser_open)

    # When
    result = run_demo(_arguments(), output=output)

    # Then
    assert result == 0
    assert output.stream.getvalue().startswith("Agentic Saga recorder: http://127.0.0.1:")
    assert "All 4 scenario recordings are complete" in output.stream.getvalue()
    assert "no live model calls or JEV adapter" in output.stream.getvalue()
    assert "web server stays open for browsing" in output.stream.getvalue()
    assert "Ctrl+C" in output.stream.getvalue()
    assert output.flushes == 4


def _unexpected_browser_open(_: str) -> bool:
    raise AssertionError("browser must remain opt-in")


@pytest.mark.parametrize(
    "browser_result",
    [
        False,
        OSError("desktop-secret"),
        RuntimeError("browser-backend-secret"),
        Exception("launcher detail"),
    ],
)
def test_should_warn_and_continue_when_browser_cannot_open(
    browser_result: bool | Exception, monkeypatch: MonkeyPatch
) -> None:
    # Given
    errors = StringIO()
    monkeypatch.setattr(RecorderServer, "wait", _interrupt_wait)
    monkeypatch.setattr("agentic_saga.cli.demo.webbrowser.open", _browser_behavior(browser_result))

    # When
    result = run_demo(_arguments(open_browser=True), errors=errors)

    # Then
    assert result == 0
    assert errors.getvalue() == "Warning: unable to open the recorder in a browser.\n"
    assert "desktop-secret" not in errors.getvalue()
    assert "launcher detail" not in errors.getvalue()
    assert "Traceback" not in errors.getvalue()


def _browser_behavior(result: bool | Exception) -> Callable[[str], bool]:
    def open_browser(_: str) -> bool:
        if isinstance(result, Exception):
            raise result
        return result

    return open_browser


@pytest.mark.parametrize(
    ("target", "error"),
    [
        ("agentic_saga.cli.demo.load_reference_trace", ReferenceTraceError("provider-secret")),
        (
            "agentic_saga.cli.demo.materialize_recorder_site",
            MaterializationError("trace-secret"),
        ),
    ],
)
def test_should_return_safe_one_line_when_demo_preparation_fails(
    target: str, error: ValueError, monkeypatch: MonkeyPatch
) -> None:
    # Given
    errors = StringIO()
    monkeypatch.setattr(target, _raise(error))

    # When
    result = run_demo(_arguments(), errors=errors)

    # Then
    assert result == 2
    assert errors.getvalue() == "Error: unable to start the recorder demo.\n"
    assert "secret" not in errors.getvalue()


def _raise(error: ValueError) -> Callable[..., None]:
    def fail(*_: object, **__: object) -> None:
        raise error

    return fail


def test_should_return_safe_one_line_when_loopback_port_is_unavailable(
    monkeypatch: MonkeyPatch,
) -> None:
    # Given
    errors = StringIO()
    with _occupied_loopback_port() as port:
        # When
        result = run_demo(_arguments(port=port), errors=errors)

    # Then
    assert result == 2
    assert errors.getvalue() == "Error: unable to start the recorder demo.\n"


@contextmanager
def _occupied_loopback_port() -> Iterator[int]:
    holder = socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    try:
        yield int(holder.getsockname()[1])
    finally:
        holder.close()


def test_should_serve_all_traces_with_selected_default_and_exit_zero_on_sigint(
    tmp_path: Path,
) -> None:
    # Given
    process = _start_demo_process("compensation-failure", temp_root=tmp_path)

    try:
        # When
        url = _read_server_url(process)
        with urlopen(f"{url}/traces/index.json") as response:  # noqa: S310
            index = json.loads(response.read())

        # Then
        assert {entry["id"] for entry in index["runs"]} == set(REFERENCE_SCENARIOS)
        assert index["default_run_id"] == "compensation-failure"
        for entry in index["runs"]:
            assert entry["presentation"] == "ecommerce"
            assert entry["name"] != f"Recorded run: {entry['id']}"
            with urlopen(f"{url}/traces/{entry['trace_ref']}") as response:  # noqa: S310
                trace = RunTrace.model_validate_json(response.read(), strict=True)
            assert trace.events
        assert process.poll() is None
    finally:
        _stop_demo_process(process)
    assert process.returncode == 0
    assert list(tmp_path.iterdir()) == []
    _assert_port_is_released(url)


def _start_demo_process(scenario: str, *, temp_root: Path) -> subprocess.Popen[str]:
    command = [_console_script(), "demo", "--scenario", scenario]
    return subprocess.Popen(
        command,
        cwd=Path(__file__).parents[3],
        env={**os.environ, "PYTHONUNBUFFERED": "1", "TMPDIR": str(temp_root)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _console_script() -> str:
    return str(Path(sys.executable).with_name("agentic-saga"))


def _read_server_url(process: subprocess.Popen[str]) -> str:
    assert process.stdout is not None
    ready, _, _ = select.select([process.stdout], [], [], 10)
    assert ready, _process_error(process)
    line = process.stdout.readline().strip()
    return line.removeprefix("Agentic Saga recorder: ")


def _process_error(process: subprocess.Popen[str]) -> str:
    assert process.stderr is not None
    return process.stderr.read() if process.poll() is not None else "recorder did not become ready"


def _stop_demo_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
    process.communicate(timeout=10)


def _assert_port_is_released(url: str) -> None:
    port = urlsplit(url).port
    assert port is not None
    probe = socket()
    try:
        assert probe.connect_ex(("127.0.0.1", port)) != 0
    finally:
        probe.close()
    rebound = socket()
    try:
        rebound.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
        rebound.bind(("127.0.0.1", port))
    finally:
        rebound.close()
