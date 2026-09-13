from __future__ import annotations

import json
import os
import platform
import runpy
import signal
import subprocess
import sys
from pathlib import Path

import pytest

import agentic_saga.manifest as manifest_module
import scripts.measure_release as orchestrator
import scripts.release_runner as runner
from agentic_saga.agents import deepagents as deepagents_module
from agentic_saga.demo import assets as assets_module
from scripts.release_contract import BUDGETS, EnvironmentIdentity, ReleaseReport, evaluate


class _Stream:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _StubbornProcess:
    def __init__(self) -> None:
        self.stdout = _Stream()
        self.stderr = _Stream()
        self.actions: list[object] = []
        self.waits = 0

    def poll(self) -> int | None:
        return None

    def send_signal(self, value: int) -> None:
        self.actions.append(value)

    def terminate(self) -> None:
        self.actions.append("terminate")

    def kill(self) -> None:
        self.actions.append("kill")

    def wait(self, timeout: float) -> int:
        self.actions.append(("wait", timeout))
        self.waits += 1
        if self.waits < 3:
            raise subprocess.TimeoutExpired("fake", timeout)
        return -9

    def communicate(
        self, input: str | None = None, timeout: float | None = None
    ) -> tuple[str, str]:
        del input, timeout
        raise AssertionError("unexpected capture")


class _StubbornCapture(_StubbornProcess):
    def communicate(
        self, input: str | None = None, timeout: float | None = None
    ) -> tuple[str, str]:
        del input
        if timeout is None:
            raise AssertionError("timeout is required")
        self.actions.append(("communicate", timeout))
        self.waits += 1
        if self.waits < 4:
            raise subprocess.TimeoutExpired("fake", timeout)
        return '{"elapsed_ms": 7.5, "rss_raw": 64, "platform": "Linux"}\n', ""


class _NeverCaptured(_StubbornProcess):
    def communicate(
        self, input: str | None = None, timeout: float | None = None
    ) -> tuple[str, str]:
        del input
        if timeout is None:
            raise AssertionError("timeout is required")
        raise subprocess.TimeoutExpired("fake", timeout)


class _EventuallyStopped(_StubbornProcess):
    def __init__(self, successful_wait: int) -> None:
        super().__init__()
        self.successful_wait = successful_wait

    def wait(self, timeout: float) -> int:
        self.actions.append(("wait", timeout))
        self.waits += 1
        if self.waits < self.successful_wait:
            raise subprocess.TimeoutExpired("fake", timeout)
        return -signal.SIGINT


def _passing_report() -> ReleaseReport:
    results = tuple(
        evaluate(
            name,
            spec.limit,
            statistics=(spec.limit, spec.limit) if spec.samples > 1 else None,
        )
        for name, spec in BUDGETS.items()
    )
    identity = EnvironmentIdentity(
        "a" * 40, "clean", "TestOS", "TestCPU", "Python 3.12.9", "v24.0.0", "11.5.0"
    )
    return ReleaseReport(identity, results)


def test_quality_evidence_enforces_python_and_frontend_branches_separately() -> None:
    python = {
        "files": {
            "src/agentic_saga/kernel/runtime.py": {
                "summary": {"covered_branches": 95, "num_branches": 100}
            }
        }
    }
    release = {
        "files": {
            "scripts/release_runner.py": {"summary": {"covered_branches": 92, "num_branches": 100}}
        }
    }
    frontend = {"total": {"branches": {"covered": 89, "total": 100, "pct": 99}}}

    results = runner.coverage_results(python, release, frontend)

    assert results[0].actual == 95
    assert results[0].passed
    assert results[1].actual == 92
    assert results[1].passed
    assert results[2].actual == 89
    assert not results[2].passed


def test_repository_coverage_command_fails_below_true_branch_floor(tmp_path: Path) -> None:
    coverage = {
        "files": {
            "src/agentic_saga/kernel/runtime.py": {
                "summary": {"covered_branches": 89, "num_branches": 100}
            }
        }
    }
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(coverage))

    assert runner.check_python_coverage(path) == 1


def test_release_script_coverage_command_fails_below_true_branch_floor(tmp_path: Path) -> None:
    coverage = {
        "files": {
            "scripts/release_runner.py": {"summary": {"covered_branches": 89, "num_branches": 100}}
        }
    }
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(coverage))

    assert runner.check_release_coverage(path) == 1


def test_release_coverage_commands_accept_true_branch_floor(tmp_path: Path) -> None:
    coverage = {
        "files": {
            "src/agentic_saga/kernel/runtime.py": {
                "summary": {"covered_branches": 9, "num_branches": 10}
            },
            "scripts/release_runner.py": {"summary": {"covered_branches": 19, "num_branches": 20}},
        }
    }
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(coverage))

    assert runner.check_python_coverage(path) == 0
    assert runner.check_release_coverage(path) == 0


def test_internal_cli_dispatches_both_coverage_checks(tmp_path: Path) -> None:
    coverage = {
        "files": {
            "src/agentic_saga/kernel/runtime.py": {
                "summary": {"covered_branches": 9, "num_branches": 10}
            },
            "scripts/release_runner.py": {"summary": {"covered_branches": 9, "num_branches": 10}},
        }
    }
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(coverage))

    assert runner.internal_main(("--check-python-coverage", str(path))) == 0
    assert runner.internal_main(("--check-release-coverage", str(path))) == 0


def test_internal_cli_dispatches_rejection_child_and_rejects_unknown_args(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert runner.internal_main(("--rejection-child",)) == 0
    assert "elapsed_ms" in capsys.readouterr().out

    with pytest.raises(ValueError, match="unsupported internal release-runner arguments"):
        runner.internal_main(("unknown",))


def test_command_runner_is_offline_and_reports_failure_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-leak")
    monkeypatch.setenv("PYTHONPATH", "must-not-leak")
    success = runner._checked((sys.executable, "-c", "print('ok')"))

    assert success == "ok\n"
    assert "OPENROUTER_API_KEY" not in runner._environment()
    assert "PYTHONPATH" not in runner._environment()
    assert runner._environment()["UV_OFFLINE"] == "1"
    with pytest.raises(RuntimeError, match="release measurement command failed"):
        runner._checked(
            (sys.executable, "-c", "import sys; sys.stderr.write('broken'); sys.exit(4)")
        )


def test_version_and_pnpm_resolution_use_exact_tool_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "pnpm"
    executable.write_text("#!/bin/sh\nprintf '11.5.0\\n'\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    assert runner._version((sys.executable, "-c", "print('v1.2.3')")) == "v1.2.3"
    assert runner._pnpm_command("gate") == ("pnpm", "gate")

    executable.write_text("#!/bin/sh\nprintf '11.4.0\\n'\n")
    assert runner._pnpm_command("gate") == ("npx", "--yes", "pnpm@11.5.0", "gate")


def test_json_reader_and_shape_fail_closed_for_invalid_documents(tmp_path: Path) -> None:
    mapping = tmp_path / "mapping.json"
    sequence = tmp_path / "sequence.json"
    mapping.write_text('{"nested": [1, 2]}')
    sequence.write_text("[]")

    assert runner._read_json(mapping) == {"nested": [1, 2]}
    assert runner._shape({"nested": [1, 2]}) == (3, 4)
    assert runner._shape("leaf") == (1, 1)
    with pytest.raises(ValueError, match="expected JSON object"):
        runner._read_json(sequence)


@pytest.mark.parametrize("value", [True, "1", 1.5, None])
def test_integer_boundary_rejects_non_integer_values(value: object) -> None:
    with pytest.raises(ValueError, match="must be an integer"):
        runner._integer(value, "field")


@pytest.mark.parametrize("value", [0, -1])
def test_positive_integer_boundary_rejects_nonpositive_values(value: int) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        runner._positive_integer(value, "field")


def test_schema_bound_reader_rejects_missing_integer_bounds() -> None:
    assert runner._integer_bounds(1, 2, "field") == (1, 2)
    with pytest.raises(RuntimeError, match="missing integer schema bounds"):
        runner._integer_bounds(None, 2, "field")


@pytest.mark.parametrize(
    ("module", "name", "value", "result_name"),
    [
        (manifest_module, "_MAX_SOURCE_BYTES", 65_537, "manifest_parser_max_bytes"),
        (manifest_module, "_MAX_DOCUMENT_DEPTH", 17, "manifest_parser_max_depth"),
        (manifest_module, "_MAX_DOCUMENT_NODES", 4_097, "manifest_parser_max_nodes"),
        (assets_module, "_MAX_STATIC_FILES", 201, "materializer_max_static_files"),
        (assets_module, "_MAX_STATIC_BYTES", 32 * 1024 * 1024 + 1, "materializer_max_static_bytes"),
    ],
)
def test_implementation_constant_weakening_fails_fixed_budget(
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    name: str,
    value: int,
    result_name: str,
) -> None:
    monkeypatch.setattr(module, name, value)

    results = runner.implementation_limit_results()

    assert not next(item for item in results if item.name == result_name).passed


def test_implementation_limits_cover_schema_bounds_and_lease_positivity() -> None:
    results = {result.name: result for result in runner.implementation_limit_results()}

    assert results["execution_turn_schema_minimum"].actual == 1
    assert not any("cost_microusd" in name for name in results)
    assert results["model_timeout_schema_minimum_ms"].actual == 100
    assert results["lease_accepts_positive"].passed
    assert results["lease_rejects_nonpositive"].passed
    assert results["lease_rejects_over_max"].passed


def test_reference_execution_budget_must_be_positive_and_schema_bounded() -> None:
    budgets = {
        "turn_limit": 0,
        "tool_call_limit": 10_001,
        "elapsed_ms_limit": 1,
        "token_limit": 1,
    }

    results = {result.name: result for result in runner._execution_results(budgets)}

    assert not results["execution_turn_limit"].passed
    assert not results["execution_tool_call_limit"].passed
    assert set(results) == {
        "execution_turn_limit",
        "execution_tool_call_limit",
        "execution_elapsed_ms_limit",
        "execution_token_limit",
    }


def test_effective_model_timeout_is_capped_by_the_saga_budget() -> None:
    budgets = {
        "turn_limit": 20,
        "elapsed_ms_limit": 120_000,
    }

    result = runner.effective_model_timeout_result(budgets)

    assert result.name == "effective_model_timeout_ms"
    assert result.actual == 6_000
    assert result.passed


def test_effective_model_timeout_uses_fixed_floor_reservation() -> None:
    # Given
    budgets = {"turn_limit": 3, "elapsed_ms_limit": 10}

    # When
    result = runner.effective_model_timeout_result(budgets)

    # Then
    assert result.actual == 3


def test_effective_timeout_rejects_invalid_saga_divisor() -> None:
    with pytest.raises(ValueError, match="turn_limit must be positive"):
        runner.effective_model_timeout_result({"turn_limit": 0, "elapsed_ms_limit": 120_000})


def test_manifest_measurement_reads_shape_budgets_and_effective_timeout() -> None:
    results = {result.name: result for result in runner._manifest_results()}

    assert results["manifest_bytes"].passed
    assert results["manifest_depth"].passed
    assert results["effective_model_timeout_ms"].actual == 6_000


def test_manifest_measurement_rejects_non_mapping_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "examples" / "ecommerce"
    manifest.mkdir(parents=True)
    (manifest / "saga.yaml").write_text("- not-a-mapping\n")
    monkeypatch.setattr(runner, "ROOT", tmp_path)

    with pytest.raises(ValueError, match="reference manifest must be a mapping"):
        runner._manifest_results()


def test_manifest_measurement_requires_budget_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "examples" / "ecommerce"
    manifest.mkdir(parents=True)
    (manifest / "saga.yaml").write_text("budgets: []\n")
    monkeypatch.setattr(runner, "ROOT", tmp_path)

    with pytest.raises(ValueError, match="reference manifest has no budgets"):
        runner._manifest_results()


def test_static_boundary_measurements_use_real_package_limits() -> None:
    assets = {result.name: result for result in runner._asset_results()}
    boundaries = {result.name: result for result in runner._boundary_results()}
    sqlite = runner._sqlite_result()

    assert assets["reference_trace_count"].actual == 4
    assert boundaries["optional_model_calls"].actual == 1
    assert boundaries["model_timeout_ms"].actual == 10_000
    assert sqlite.actual == 5_000


def test_model_call_probe_requires_exactly_one_configured_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deepagents_module, "_create_graph", lambda *args: object())

    with pytest.raises(RuntimeError, match="exactly one model-call limit"):
        runner._configured_model_call_limit()


def test_demo_shutdown_escalates_and_closes_pipes() -> None:
    process = _StubbornProcess()

    runner.stop_process(process, grace_seconds=0.01)

    assert process.actions == [
        signal.SIGINT,
        ("wait", 0.01),
        "terminate",
        ("wait", 0.01),
        "kill",
        ("wait", 0.01),
    ]
    assert process.stdout.closed
    assert process.stderr.closed


@pytest.mark.parametrize(
    ("successful_wait", "expected_actions"),
    [
        (1, [signal.SIGINT, ("wait", 0.01)]),
        (2, [signal.SIGINT, ("wait", 0.01), "terminate", ("wait", 0.01)]),
    ],
)
def test_demo_shutdown_stops_at_the_first_successful_escalation(
    successful_wait: int, expected_actions: list[object]
) -> None:
    process = _EventuallyStopped(successful_wait)

    runner.stop_process(process, grace_seconds=0.01)

    assert process.actions == expected_actions
    assert process.stdout.closed
    assert process.stderr.closed


def test_demo_shutdown_closes_pipes_when_child_cannot_be_reaped() -> None:
    process = _EventuallyStopped(4)

    with pytest.raises(RuntimeError, match="could not be reaped"):
        runner.stop_process(process, grace_seconds=0.01)

    assert process.stdout.closed
    assert process.stderr.closed


def test_demo_shutdown_does_not_signal_an_exited_child() -> None:
    process = _StubbornProcess()
    process.poll = lambda: 0  # type: ignore[method-assign]

    runner.stop_process(process, grace_seconds=0.01)

    assert process.actions == []
    assert process.stdout.closed
    assert process.stderr.closed


def test_rejection_child_timeout_escalates_and_parses_child_rss() -> None:
    process = _StubbornCapture()

    output = runner.bounded_output(process, grace_seconds=0.01)
    results = runner.rejection_results_from_output(output)

    assert signal.SIGINT in process.actions
    assert "terminate" in process.actions
    assert "kill" in process.actions
    assert results[0].actual == 7.5
    assert results[1].actual == 64 * 1024
    assert process.stdout.closed
    assert process.stderr.closed


def test_capture_returns_immediately_and_closes_optional_pipes() -> None:
    process = _StubbornCapture()
    process.waits = 3
    process.stderr = None  # type: ignore[assignment]

    output = runner.bounded_output(process, grace_seconds=0.01)

    assert '"elapsed_ms": 7.5' in output
    assert process.stdout.closed


def test_capture_raises_after_bounded_kill_and_closes_pipes() -> None:
    process = _NeverCaptured()

    with pytest.raises(RuntimeError, match="could not be reaped"):
        runner.bounded_output(process, grace_seconds=0.01)

    assert process.stdout.closed
    assert process.stderr.closed


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "{}",
        '{"elapsed_ms": 1, "rss_raw": 2}',
        '{"elapsed_ms": 1, "rss_raw": 2, "platform": "Plan9"}',
        "not-json",
    ],
)
def test_rejection_child_metrics_fail_closed(payload: str) -> None:
    with pytest.raises(ValueError, match="rejection child"):
        runner.rejection_results_from_output(payload)


def test_rejection_rss_conversion_uses_child_platform() -> None:
    darwin = json.dumps({"elapsed_ms": 1, "rss_raw": 64, "platform": "Darwin"})
    linux = json.dumps({"elapsed_ms": 1, "rss_raw": 64, "platform": "Linux"})

    assert runner.rejection_results_from_output(darwin)[1].actual == 64
    assert runner.rejection_results_from_output(linux)[1].actual == 64 * 1024


def test_real_oversized_trace_is_rejected_in_the_measurement_child(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert runner._rejection_child() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["elapsed_ms"] >= 0
    assert payload["rss_raw"] > 0
    assert payload["platform"] in {"Darwin", "Linux"}


def test_expected_rejection_fails_if_reader_accepts_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(assets_module, "_read_file", lambda *arguments: b"accepted")

    with pytest.raises(RuntimeError, match="over-limit recorder trace was accepted"):
        runner._read_expected_rejection(0, "trace.json")


def test_ready_reader_accepts_only_loopback_cli_banner() -> None:
    valid = subprocess.Popen(
        (sys.executable, "-c", "print('Agentic Saga recorder: http://127.0.0.1:4321')"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    invalid = subprocess.Popen(
        (sys.executable, "-c", "print('ready on an external host')"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert runner._read_ready(valid) == "http://127.0.0.1:4321"
        with pytest.raises(RuntimeError, match="valid ready URL"):
            runner._read_ready(invalid)
    finally:
        runner.stop_process(valid, grace_seconds=0.1)
        runner.stop_process(invalid, grace_seconds=0.1)


def test_ready_reader_rejects_missing_stdout() -> None:
    process = _StubbornProcess()
    process.stdout = None  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="stdout is unavailable"):
        runner._read_ready(process)  # type: ignore[arg-type]


@pytest.mark.parametrize("payload", [{}, {"samples_ms": "bad"}, {"samples_ms": [1, "bad"]}])
def test_browser_samples_reject_invalid_payload(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="invalid samples"):
        runner._browser_samples(payload)


def test_browser_samples_accept_numbers() -> None:
    assert runner._browser_samples({"samples_ms": [1, 2.5]}) == (1.0, 2.5)


def test_browser_result_parses_written_measurement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def packaged(_cli: Path, output: Path) -> subprocess.CompletedProcess[str]:
        output.write_text(json.dumps({"samples_ms": list(range(1, 11))}))
        return subprocess.CompletedProcess(("packaged",), 0, "", "")

    monkeypatch.setattr(runner, "_run_packaged_browser", packaged)

    result = runner._browser_result(Path("/unused"))

    assert result.actual == 10
    assert result.p50 == 5.5


def test_browser_result_reports_failed_packaged_suite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed = subprocess.CompletedProcess(("packaged",), 2, "", "browser failed")
    monkeypatch.setattr(runner, "_run_packaged_browser", lambda *_arguments: failed)

    with pytest.raises(RuntimeError, match="browser failed"):
        runner._browser_result(Path("/unused"))


def test_memory_result_parses_rss_and_rejects_missing_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "_checked", lambda *_arguments: "WORKLOAD_RSS=64\n")
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    assert runner._representative_memory_result().actual == 64 * 1024

    monkeypatch.setattr(runner, "_checked", lambda *_arguments: "no measurement\n")
    with pytest.raises(RuntimeError, match="did not report peak RSS"):
        runner._representative_memory_result()


def test_wheel_cli_uses_explicit_installed_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = tmp_path / "agentic-saga"
    cli.write_text("executable")
    monkeypatch.setenv("AGENTIC_SAGA_CLI", str(cli))

    assert runner._wheel_cli(tmp_path / "workspace") == cli.resolve()


def test_wheel_cli_installs_only_from_explicit_release_wheelhouse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a verified release wheelhouse and an otherwise empty measurement workspace.
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    commands: list[tuple[str, ...]] = []
    monkeypatch.delenv("AGENTIC_SAGA_CLI", raising=False)
    monkeypatch.setenv("AGENTIC_SAGA_RELEASE_WHEELHOUSE", str(wheelhouse))

    def checked(command: tuple[str, ...], cwd: Path = runner.ROOT) -> str:
        del cwd
        commands.append(command)
        if command[:2] == ("uv", "build"):
            (workspace / "wheel" / "agentic_saga-0.1.0-py3-none-any.whl").touch()
        return ""

    monkeypatch.setattr(runner, "_checked", checked)

    # When the release measurement prepares its isolated wheel CLI.
    runner._wheel_cli(workspace)

    # Then every install is index-free and resolves dependencies only from that wheelhouse.
    installs = tuple(command for command in commands if command[:3] == ("uv", "pip", "install"))
    assert len(installs) == len(("dependencies", "project"))
    assert all("--no-index" in command for command in installs)
    assert all(
        command[command.index("--find-links") + 1] == str(wheelhouse.resolve())
        for command in installs
    )


def test_rendered_dirty_node_26_report_cannot_say_pass() -> None:
    identity = EnvironmentIdentity(
        "d" * 40, "dirty", "TestOS", "TestCPU", "Python 3.12.9", "v26.0.0", "11.5.0"
    )
    report = ReleaseReport(identity, (evaluate("sdk_retries", 0),))

    rendered = runner.render_report(report)

    assert f"commit: {'d' * 40}" in rendered
    assert "python: Python 3.12.9" in rendered
    assert "node: v26.0.0" in rendered
    assert "pnpm: 11.5.0" in rendered
    assert "release_environment: FAIL" in rendered
    assert "overall: FAIL" in rendered
    assert "node must be 24.x" in rendered


def test_rendered_complete_report_includes_ranges_and_timing() -> None:
    rendered = runner.render_report(_passing_report())

    assert "execution_turn_limit: PASS" in rendered
    assert "materialization_ms: PASS" in rendered
    assert "p50=500.000 p95=500.000 max=500.000" in rendered
    assert "release_environment: PASS" in rendered
    assert rendered.endswith("overall: PASS\n")


def test_quality_rows_are_emitted_only_after_executed_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []

    def checked(command: tuple[str, ...], cwd: Path = runner.ROOT) -> str:
        del cwd
        commands.append(command)
        return "passed"

    python = {
        "files": {
            "src/agentic_saga/kernel/runtime.py": {
                "summary": {"covered_branches": 9, "num_branches": 10}
            }
        }
    }
    release = {
        "files": {
            "scripts/release_runner.py": {"summary": {"covered_branches": 19, "num_branches": 20}}
        }
    }
    frontend = {"total": {"branches": {"covered": 9, "total": 10}}}
    monkeypatch.setattr(runner, "_checked", checked)
    monkeypatch.setattr(runner, "_pnpm_command", lambda *arguments: ("pnpm", *arguments))
    monkeypatch.setattr(runner, "_frontend_coverage", lambda: frontend)
    monkeypatch.setattr(
        runner, "_read_json", lambda path: release if "release-scripts" in path.name else python
    )

    results = {result.name: result for result in runner._quality_results()}

    assert commands == [("uv", "run", "poe", "gate"), ("pnpm", "gate")]
    assert results["python_complexity_grade_a"].passed
    assert results["browser_behavior_checks"].passed


def test_failed_quality_gate_emits_no_quality_results(monkeypatch: pytest.MonkeyPatch) -> None:
    def failed(_command: tuple[str, ...], _cwd: Path = runner.ROOT) -> str:
        raise RuntimeError("quality gate failed")

    monkeypatch.setattr(runner, "_checked", failed)

    with pytest.raises(RuntimeError, match="quality gate failed"):
        runner._quality_results()


def test_offline_scenario_p95_tolerates_only_one_outlier() -> None:
    name = "offline_lost-response_p95_ms"
    one_outlier = (*([100.0] * 19), 2_100.0)
    two_outliers = (*([100.0] * 18), 2_100.0, 2_200.0)

    result = runner.timing_result(name, one_outlier)

    assert BUDGETS[name].samples == 20
    assert (result.actual, result.maximum, result.passed) == (100.0, 2_100.0, True)
    assert not runner.timing_result(name, two_outliers).passed


def test_offline_scenario_measurement_uses_fresh_in_process_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directories: list[Path] = []

    async def run_scenario(name: str, directory: Path) -> object:
        directories.append(directory)
        assert name == "happy-path"
        return object()

    def reject_subprocess(_command: tuple[str, ...], _cwd: Path = runner.ROOT) -> str:
        pytest.fail("offline runtime measurement must not launch a CLI subprocess")

    monkeypatch.setattr(runner, "run_scenario", run_scenario, raising=False)
    monkeypatch.setattr(runner, "_checked", reject_subprocess)

    result = runner._scenario_result("happy-path")

    assert result.samples == 20
    assert len(set(directories)) == 20
    assert all(not directory.exists() for directory in directories)


def test_materialization_uses_exact_sample_contract(tmp_path: Path) -> None:
    samples = runner.measure_samples(25, lambda: None)

    result = runner.timing_result("materialization_ms", samples)

    assert result.samples == 25
    assert result.temperature == "fresh-destination"

    with pytest.raises(ValueError, match="exactly 25 samples"):
        runner.timing_result("materialization_ms", samples[:-1])


def test_release_orchestrator_rejects_arguments_before_measuring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        orchestrator, "measure_release", lambda: pytest.fail("measurement must not start")
    )

    assert orchestrator.main(("unexpected",)) == 2


def test_release_orchestrator_renders_complete_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(orchestrator, "measure_release", _passing_report)

    assert orchestrator.main(()) == 0
    assert capsys.readouterr().out.endswith("overall: PASS\n")


def test_release_orchestrator_returns_failure_for_failed_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _passing_report()
    failed = evaluate("release_scripts_branch_coverage_percent", 89)
    index = next(index for index, item in enumerate(report.results) if item.name == failed.name)
    results = (*report.results[:index], failed, *report.results[index + 1 :])
    monkeypatch.setattr(
        orchestrator,
        "measure_release",
        lambda: ReleaseReport(report.environment, results),
    )

    assert orchestrator.main(()) == 1


def test_release_orchestrator_reports_measurement_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def failed() -> ReleaseReport:
        raise RuntimeError("evidence unavailable")

    monkeypatch.setattr(orchestrator, "measure_release", failed)

    assert orchestrator.main(()) == 2
    assert "release measurement failed: evidence unavailable" in capsys.readouterr().err


def test_release_orchestrator_direct_script_path_rejects_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", [str(Path(orchestrator.__file__)), "unexpected"])

    with pytest.raises(SystemExit, match="2"):
        runpy.run_path(str(Path(orchestrator.__file__)), run_name="__main__")
