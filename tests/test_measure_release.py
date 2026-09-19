from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

import pytest

import scripts.measure_release as orchestrator
import scripts.release_runner as runner
from scripts.release_contract import (
    BUDGETS,
    BudgetResult,
    EnvironmentIdentity,
    ReleaseReport,
    evaluate,
)


def _coverage(prefix: str, covered: int = 9, total: int = 10) -> dict[str, object]:
    return {
        "files": {
            f"{prefix}module.py": {"summary": {"covered_branches": covered, "num_branches": total}}
        }
    }


def _identity() -> EnvironmentIdentity:
    return EnvironmentIdentity(
        "a" * 40, "clean", "test", "test", "Python 3.13.5", "v24.6.0", "11.5.0"
    )


def _record_commands(commands: list[tuple[str, ...]]) -> Callable[..., str]:
    def record(command: Sequence[str], *_args: object) -> str:
        commands.append(tuple(command))
        return ""

    return record


class _TimeoutProcess:
    def __init__(self) -> None:
        self.killed = False
        self.stdout = None
        self.stderr = None

    def terminate(self) -> None:
        return None

    def wait(self, timeout: int) -> None:
        if not self.killed:
            raise subprocess.TimeoutExpired("demo", timeout)

    def kill(self) -> None:
        self.killed = True


def _quality() -> tuple[BudgetResult, ...]:
    names = (
        "core_branch_coverage_percent",
        "release_scripts_branch_coverage_percent",
        "frontend_branch_coverage_percent",
        "python_complexity_grade_a",
        "browser_behavior_checks",
    )
    return tuple(evaluate(name, BUDGETS[name].limit) for name in names)


def test_coverage_results_measure_current_package_not_deleted_kernel() -> None:
    frontend = {"total": {"branches": {"covered": 9, "total": 10}}}
    results = runner.coverage_results(
        _coverage("src/agentic_saga/"), _coverage("scripts/"), frontend
    )

    assert [result.actual for result in results] == [90, 90, 90]


def test_runtime_contract_results_prove_temporal_cutover() -> None:
    results = runner.runtime_contract_results()

    assert {result.name for result in results} == {
        "temporal_runtime_dependency",
        "legacy_runtime_absent",
        "public_api_imports",
        "temporal_tests_passed",
    }
    assert all(result.passed for result in results)


@pytest.mark.parametrize("configured", ["relative", "missing"])
def test_artifact_root_rejects_noncanonical_input(tmp_path: Path, configured: str) -> None:
    value = configured if configured == "relative" else str(tmp_path / "missing")
    with pytest.raises(ValueError, match="release artifacts"):
        runner._artifact_root(value)


def test_single_wheel_requires_exactly_one(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        runner._single_wheel(tmp_path)
    wheel = tmp_path / "agentic_saga.whl"
    wheel.write_bytes(b"wheel")
    assert runner._single_wheel(tmp_path) == wheel


def test_package_results_require_import_and_recorder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    python = tmp_path / "venv" / "bin" / "python"
    monkeypatch.setattr(runner, "_installed_python", lambda _: python)
    monkeypatch.setattr(runner, "_installed_surface", lambda _: True)
    monkeypatch.setattr(runner, "_recorder_ready", lambda _: True)

    results = runner.package_results(tmp_path)

    assert all(result.passed for result in results)


def test_measure_release_combines_quality_runtime_and_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runner, "quality_results_from_proof", lambda _: _quality())
    monkeypatch.setattr(
        runner,
        "runtime_contract_results",
        lambda: tuple(
            evaluate(name, 1)
            for name in (
                "temporal_runtime_dependency",
                "legacy_runtime_absent",
                "public_api_imports",
                "temporal_tests_passed",
            )
        ),
    )
    monkeypatch.setattr(
        runner,
        "package_results",
        lambda _: (evaluate("package_build_install", 1), evaluate("recorder_cli_ready", 1)),
    )
    monkeypatch.setattr(runner, "collect_environment", _identity)

    report = runner.measure_release(tmp_path / "proof.json")

    assert tuple(result.name for result in report.results) == tuple(BUDGETS)


def test_internal_coverage_command_fails_below_floor(tmp_path: Path) -> None:
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(_coverage("src/agentic_saga/", covered=8)))

    assert runner.internal_main(("--check-python-coverage", str(path))) == 1
    with pytest.raises(ValueError, match="unsupported"):
        runner.internal_main(("--unknown",))


def test_render_report_includes_environment_and_each_result() -> None:
    report = ReleaseReport(
        _identity(), tuple(evaluate(name, spec.limit) for name, spec in BUDGETS.items())
    )
    rendered = runner.render_report(report)

    assert "release_environment: PASS" in rendered
    assert all(f"{name}: PASS" in rendered for name in BUDGETS)


def test_orchestrator_rejects_invalid_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    assert orchestrator.main(("--unknown",)) == 2
    assert "usage:" in capsys.readouterr().err


def test_orchestrator_reports_measurement_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail() -> ReleaseReport:
        raise RuntimeError("evidence unavailable")

    monkeypatch.setattr(orchestrator, "measure_release", fail)
    assert orchestrator.main(()) == 2
    assert "evidence unavailable" in capsys.readouterr().err


def test_checked_surfaces_failed_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    failed = subprocess.CompletedProcess(("tool",), 1, "", "failure")
    monkeypatch.setattr(runner, "_run", lambda *_args, **_kwargs: failed)

    with pytest.raises(RuntimeError, match="failure"):
        runner._checked(("tool",))


def test_release_environment_is_offline_and_secret_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inherited = {
        "OPENROUTER_API_KEY": "secret",
        "TYPESAFE_API_KEY": "secret",
        "DAGGER_CLOUD_TOKEN": "secret",
        "TEMPORAL_API_KEY": "secret",
        "AWS_SECRET_ACCESS_KEY": "secret",
        "PYTHONPATH": "unsafe",
        "UNRELATED_SECRET": "secret",
    }
    for name, value in inherited.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", "/trusted/bin")
    monkeypatch.setenv("TMPDIR", "/trusted/tmp")
    monkeypatch.setenv("AGENTIC_SAGA_RELEASE_ARTIFACTS", "/release")

    environment = runner._environment()

    assert not inherited.keys() & environment.keys()
    assert environment["PATH"] == "/trusted/bin"
    assert environment["TMPDIR"] == "/trusted/tmp"
    assert environment["AGENTIC_SAGA_RELEASE_ARTIFACTS"] == "/release"
    assert environment["npm_config_offline"] == "true"
    assert environment["UV_PYTHON_DOWNLOADS"] == "never"


def test_pnpm_command_uses_exact_local_or_pinned_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = subprocess.CompletedProcess(("pnpm",), 0, "11.5.0\n", "")
    monkeypatch.setattr(runner, "_run", lambda *_args, **_kwargs: local)
    assert runner._pnpm_command("gate") == ("pnpm", "gate")

    stale = subprocess.CompletedProcess(("pnpm",), 0, "11.4.0\n", "")
    monkeypatch.setattr(runner, "_run", lambda *_args, **_kwargs: stale)
    assert runner._pnpm_command("gate") == ("npx", "--yes", "pnpm@11.5.0", "gate")


def test_collect_environment_records_commit_tree_and_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    outputs = iter(("changes\n", "a" * 40 + "\n", "v24.6.0\n", "11.5.0\n"))
    monkeypatch.setattr(runner, "_checked", lambda *_args, **_kwargs: next(outputs))

    identity = runner.collect_environment(tmp_path)

    assert identity.commit == "a" * 40
    assert identity.tree_state == "dirty"
    assert identity.node == "v24.6.0"
    assert identity.pnpm == "11.5.0"


def test_quality_results_run_gates_and_read_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(runner, "_checked", _record_commands(commands))
    frontend = {"total": {"branches": {"covered": 9, "total": 10}}}
    monkeypatch.setattr(
        runner,
        "_read_json",
        lambda path: (
            frontend
            if "coverage-summary" in str(path)
            else _coverage("scripts/" if "release" in path.name else "src/agentic_saga/")
        ),
    )

    results = runner._quality_results()

    assert len(commands) == 2
    assert all(result.passed for result in results)


def test_read_json_requires_an_object(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    path.write_text("[]")
    with pytest.raises(ValueError, match="expected JSON object"):
        runner._read_json(path)


def test_wheel_input_supports_local_build_and_hardened_shared_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    built = tmp_path / "built.whl"
    monkeypatch.delenv("AGENTIC_SAGA_RELEASE_ARTIFACTS", raising=False)
    monkeypatch.setattr(runner, "_built_wheel", lambda _: built)
    assert runner._wheel_input(tmp_path) == (built, None, None)

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    wheel = artifacts / "agentic_saga.whl"
    wheel.write_bytes(b"wheel")
    requirements = artifacts / "runtime-requirements.txt"
    requirements.write_text("temporalio==1 --hash=sha256:abc\n")
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    monkeypatch.setenv("AGENTIC_SAGA_RELEASE_ARTIFACTS", str(artifacts))
    monkeypatch.setenv("AGENTIC_SAGA_RELEASE_WHEELHOUSE", str(wheelhouse))

    assert runner._wheel_input(tmp_path) == (wheel, requirements, wheelhouse)


def test_shared_artifacts_require_requirements_and_wheelhouse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "agentic_saga.whl").write_bytes(b"wheel")
    monkeypatch.setenv("AGENTIC_SAGA_RELEASE_ARTIFACTS", str(artifacts))
    with pytest.raises(ValueError, match="runtime requirements"):
        runner._wheel_input(tmp_path)

    (artifacts / "runtime-requirements.txt").write_text("")
    monkeypatch.delenv("AGENTIC_SAGA_RELEASE_WHEELHOUSE", raising=False)
    with pytest.raises(ValueError, match="wheelhouse"):
        runner._wheel_input(tmp_path)


def test_venv_and_install_commands_are_offline_and_hash_locked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(runner, "_checked", _record_commands(commands))
    python = runner._create_venv(tmp_path / "venv", isolated=False)
    requirements = tmp_path / "requirements.txt"
    wheelhouse = tmp_path / "wheelhouse"
    wheel = tmp_path / "package.whl"
    runner._install_dependencies(python, requirements, wheelhouse)
    runner._install_wheel(python, wheel)

    assert "--system-site-packages" in commands[0]
    assert {"--offline", "--require-hashes", "--no-index"} <= set(commands[1])
    assert {"--offline", "--no-deps"} <= set(commands[2])


def test_installed_surface_requires_temporal_public_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    success = subprocess.CompletedProcess(("python",), 0, "", "")
    failure = subprocess.CompletedProcess(("python",), 1, "", "")
    monkeypatch.setattr(runner, "_run", lambda *_args, **_kwargs: success)
    assert runner._installed_surface(tmp_path / "python")
    monkeypatch.setattr(runner, "_run", lambda *_args, **_kwargs: failure)
    assert not runner._installed_surface(tmp_path / "python")


def test_recorder_readiness_is_bounded_and_process_is_stopped(tmp_path: Path) -> None:
    cli = tmp_path / "agentic-saga"
    cli.write_text(
        "#!/usr/bin/env python3\n"
        "import time\n"
        "print('Agentic Saga demo: http://127.0.0.1:1234', flush=True)\n"
        "time.sleep(10)\n"
    )
    cli.chmod(0o755)

    assert runner._recorder_ready(cli)


def test_stop_process_escalates_after_timeout() -> None:
    process = _TimeoutProcess()
    runner._stop_process(cast(subprocess.Popen[str], process))
    assert process.killed


def test_measure_release_can_run_quality_gates_without_a_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "_quality_results", _quality)
    monkeypatch.setattr(runner, "runtime_contract_results", lambda: ())
    monkeypatch.setattr(runner, "package_results", lambda _: ())
    monkeypatch.setattr(runner, "collect_environment", _identity)

    report = runner.measure_release()

    assert report.results == _quality()


def test_internal_release_coverage_and_argument_validation(tmp_path: Path) -> None:
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(_coverage("scripts/")))

    assert runner.internal_main(("--check-release-coverage", str(path))) == 0
    with pytest.raises(ValueError, match="exactly one path"):
        runner.internal_main(("--check-release-coverage",))


def test_orchestrator_success_and_failed_budget_exit_codes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    passed = ReleaseReport(
        _identity(), tuple(evaluate(name, spec.limit) for name, spec in BUDGETS.items())
    )
    monkeypatch.setattr(orchestrator, "measure_release", lambda *_args: passed)
    assert orchestrator.main(("--quality-proof", "proof.json")) == 0
    assert "Agentic Saga release measurement" in capsys.readouterr().out

    failed_result = evaluate("core_branch_coverage_percent", 0)
    failed = ReleaseReport(_identity(), (failed_result, *passed.results[1:]))
    assert orchestrator._exit_status(failed) == 1
