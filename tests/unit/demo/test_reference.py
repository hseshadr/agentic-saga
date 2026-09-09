from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from agentic_saga.contracts.runtime import SagaStatus
from agentic_saga.demo.reference import ReferenceTraceError, load_reference_trace

_SCENARIOS = (
    "happy-path",
    "business-failure",
    "lost-response",
    "compensation-failure",
)


@pytest.mark.parametrize("scenario", _SCENARIOS)
def test_should_load_each_strict_captured_reference_trace(scenario: str) -> None:
    # Given / When
    trace = load_reference_trace(scenario)

    # Then
    assert trace.schema_version == "1.0"
    assert trace.events


def test_should_keep_compensation_failure_quiescent_for_human_review() -> None:
    # Given / When
    trace = load_reference_trace("compensation-failure")

    # Then
    assert trace.outcome is SagaStatus.HUMAN_REQUIRED
    assert trace.finished_at is None


def test_should_prefer_packaged_reference_over_editable_fallback(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    # Given
    package = tmp_path / "package"
    traces = package / "traces"
    traces.mkdir(parents=True)
    source = Path("examples/ecommerce/flight-recorder/traces")
    (traces / "index.json").write_bytes((source / "index.json").read_bytes())
    (traces / "happy-path.json").write_bytes((source / "happy-path.json").read_bytes())
    monkeypatch.setattr("agentic_saga.demo.reference.resources.files", lambda _: package)
    monkeypatch.setattr(
        "agentic_saga.demo.reference._source_reference_root",
        lambda: pytest.fail("editable fallback must not be consulted"),
    )

    # When
    trace = load_reference_trace("happy-path")

    # Then
    assert trace.outcome is SagaStatus.SUCCEEDED_VERIFIED


def test_should_use_authoritative_source_in_exact_editable_layout() -> None:
    # Given / When
    trace = load_reference_trace("business-failure")

    # Then
    assert trace.outcome is SagaStatus.COMPENSATED_VERIFIED


def test_should_refuse_ambient_fixture_tree_outside_editable_layout(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    # Given
    installed = tmp_path / "installed" / "agentic_saga" / "demo" / "reference.py"
    installed.parent.mkdir(parents=True)
    installed.write_text("repackaged")
    ambient = tmp_path / "examples" / "ecommerce" / "flight-recorder" / "traces"
    ambient.mkdir(parents=True)
    source = Path("examples/ecommerce/flight-recorder/traces")
    (ambient / "happy-path.json").write_bytes((source / "happy-path.json").read_bytes())
    package = tmp_path / "empty-package"
    package.mkdir()
    monkeypatch.setattr("agentic_saga.demo.reference.__file__", str(installed))
    monkeypatch.setattr("agentic_saga.demo.reference.resources.files", lambda _: package)

    # When / Then
    with pytest.raises(ReferenceTraceError, match="reference trace is unavailable"):
        load_reference_trace("happy-path")


def test_should_reject_unknown_reference_scenario_without_reading_a_file() -> None:
    # Given / When / Then
    with pytest.raises(ReferenceTraceError, match="reference scenario is invalid"):
        load_reference_trace("invented")


def test_should_report_safe_error_when_reference_trace_is_missing(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    # Given
    missing = tmp_path / "customer-secret-missing.json"
    monkeypatch.setattr("agentic_saga.demo.reference._reference_resource", lambda _: missing)

    # When / Then
    with pytest.raises(ReferenceTraceError) as exc_info:
        load_reference_trace("happy-path")
    assert str(exc_info.value) == "reference trace is unavailable"
    assert "customer-secret" not in str(exc_info.value)


def test_should_reject_oversized_reference_trace_before_parsing(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    # Given
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    monkeypatch.setattr("agentic_saga.demo.reference._reference_resource", lambda _: oversized)

    # When / Then
    with pytest.raises(ReferenceTraceError, match="reference trace exceeds safety bounds"):
        load_reference_trace("happy-path")


def test_should_reject_reference_replaced_by_symlink_before_read(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    # Given
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(
        Path("examples/ecommerce/flight-recorder/traces/happy-path.json").read_bytes()
    )
    selected = tmp_path / "selected.json"
    selected.write_bytes(replacement.read_bytes())
    original_lstat = Path.lstat

    def replace_after_metadata(path: Path) -> os.stat_result:
        metadata = original_lstat(path)
        if path == selected:
            path.unlink()
            path.symlink_to(replacement)
        return metadata

    monkeypatch.setattr(Path, "lstat", replace_after_metadata)
    monkeypatch.setattr("agentic_saga.demo.reference._reference_resource", lambda _: selected)

    # When / Then
    with pytest.raises(ReferenceTraceError, match="reference trace is unavailable"):
        load_reference_trace("happy-path")


@pytest.mark.parametrize(
    "payload",
    [
        b"provider-token=super-secret-not-json",
        json.dumps({"schema_version": "1.0", "prompt": "super-secret"}).encode(),
    ],
)
def test_should_report_safe_error_when_reference_trace_is_malformed(
    payload: bytes, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    # Given
    malformed = tmp_path / "malformed.json"
    malformed.write_bytes(payload)
    monkeypatch.setattr("agentic_saga.demo.reference._reference_resource", lambda _: malformed)

    # When / Then
    with pytest.raises(ReferenceTraceError) as exc_info:
        load_reference_trace("happy-path")
    assert str(exc_info.value) == "reference trace is invalid"
    assert "super-secret" not in str(exc_info.value)
