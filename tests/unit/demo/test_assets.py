from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest

from agentic_saga.contracts.trace import RunTrace
from agentic_saga.demo import assets
from agentic_saga.demo.assets import MaterializationError, materialize_recorder_site


def _trace() -> RunTrace:
    source = Path("examples/ecommerce/flight-recorder/traces/happy-path.json")
    return RunTrace.model_validate_json(source.read_bytes(), strict=True)


def _validate(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        assets._validate_site_at(descriptor)
    finally:
        os.close(descriptor)


def _trace_descriptor(directory: Path) -> int:
    site = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        return os.open("traces", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=site)
    finally:
        os.close(site)


def _copy_static_tree(source: Path, destination: Path) -> None:
    descriptor = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        assets._copy_checked_tree(source, descriptor)
    finally:
        os.close(descriptor)


def test_should_materialize_static_assets_and_strict_run_trace(tmp_path: Path) -> None:
    # Given
    destination = tmp_path / "recorder"
    trace = _trace()

    # When
    materialize_recorder_site(destination, {"happy-path": trace})

    # Then
    payload = (destination / "traces" / "happy-path.json").read_bytes()
    index = json.loads((destination / "traces" / "index.json").read_bytes())
    assert (destination / "index.html").is_file()
    assert RunTrace.model_validate_json(payload, strict=True) == trace
    assert index["runs"][0]["trace_sha256"] == sha256(payload).hexdigest()


def test_should_materialize_optional_ecommerce_presentation(tmp_path: Path) -> None:
    # Given
    destination = tmp_path / "recorder"

    # When
    materialize_recorder_site(
        destination,
        {"happy-path": _trace()},
        presentation="ecommerce",
    )

    # Then
    index = json.loads((destination / "traces" / "index.json").read_bytes())
    assert index["runs"][0]["presentation"] == "ecommerce"
    _validate(destination)


def test_should_omit_presentation_for_generic_recorder(tmp_path: Path) -> None:
    # Given / When
    destination = tmp_path / "recorder"
    materialize_recorder_site(destination, {"happy-path": _trace()})

    # Then
    index = json.loads((destination / "traces" / "index.json").read_bytes())
    assert "presentation" not in index["runs"][0]


@pytest.mark.parametrize("scenario", ["../escape", "UPPER", "", "x" * 81])
def test_should_reject_unsafe_scenario_name_when_materializing(
    tmp_path: Path, scenario: str
) -> None:
    # Given / When / Then
    with pytest.raises(MaterializationError):
        materialize_recorder_site(tmp_path / "recorder", {scenario: _trace()})


def test_should_reject_existing_destination_when_materializing(tmp_path: Path) -> None:
    # Given
    destination = tmp_path / "recorder"
    destination.mkdir()

    # When / Then
    with pytest.raises(MaterializationError):
        materialize_recorder_site(destination, {"happy-path": _trace()})


def test_should_reject_symlinked_parent_when_materializing(tmp_path: Path) -> None:
    # Given
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    symlink_parent = tmp_path / "symlink"
    symlink_parent.symlink_to(real_parent, target_is_directory=True)

    # When / Then
    with pytest.raises(MaterializationError):
        materialize_recorder_site(symlink_parent / "recorder", {"happy-path": _trace()})


def test_should_reject_nested_symlink_ancestor_when_materializing(tmp_path: Path) -> None:
    # Given
    real_parent = tmp_path / "real" / "nested"
    real_parent.mkdir(parents=True)
    linked_ancestor = tmp_path / "linked"
    linked_ancestor.symlink_to(tmp_path / "real", target_is_directory=True)

    # When / Then
    with pytest.raises(MaterializationError):
        materialize_recorder_site(linked_ancestor / "nested" / "recorder", {"happy-path": _trace()})


def test_should_reject_empty_or_unbounded_traces_when_materializing(tmp_path: Path) -> None:
    # Given
    excessive = {f"run-{number}": _trace() for number in range(101)}

    # When / Then
    with pytest.raises(MaterializationError):
        materialize_recorder_site(tmp_path / "empty", {})
    with pytest.raises(MaterializationError):
        materialize_recorder_site(tmp_path / "excessive", excessive)


def test_should_fail_closed_when_trace_index_digest_is_tampered(tmp_path: Path) -> None:
    # Given
    directory = tmp_path / "recorder"
    materialize_recorder_site(directory, {"happy-path": _trace()})
    index_path = directory / "traces" / "index.json"
    index = json.loads(index_path.read_bytes())
    index["runs"][0]["trace_sha256"] = "0" * 64
    index_path.write_text(json.dumps(index))

    # When / Then
    with pytest.raises(MaterializationError):
        _validate(directory)


@pytest.mark.parametrize("runs", [[], [{}]])
def test_should_reject_malformed_index_entry_when_validating(
    tmp_path: Path, runs: list[object]
) -> None:
    # Given
    directory = tmp_path / "recorder"
    materialize_recorder_site(directory, {"happy-path": _trace()})
    index_path = directory / "traces" / "index.json"
    index_path.write_text(json.dumps({"schema_version": "1.0", "runs": runs}))

    # When / Then
    with pytest.raises(MaterializationError):
        _validate(directory)


def test_should_reject_duplicate_index_entry_when_validating(tmp_path: Path) -> None:
    # Given
    directory = tmp_path / "recorder"
    materialize_recorder_site(directory, {"happy-path": _trace()})
    index_path = directory / "traces" / "index.json"
    index = json.loads(index_path.read_bytes())
    index["runs"].append(index["runs"][0])
    index_path.write_text(json.dumps(index))

    # When / Then
    with pytest.raises(MaterializationError):
        _validate(directory)


def test_should_reject_unrecognized_index_entry_field(tmp_path: Path) -> None:
    # Given
    directory = tmp_path / "recorder"
    materialize_recorder_site(directory, {"happy-path": _trace()})
    index_path = directory / "traces" / "index.json"
    index = json.loads(index_path.read_bytes())
    index["runs"][0]["unexpected"] = "unsafe"
    index_path.write_text(json.dumps(index))

    # When / Then
    with pytest.raises(MaterializationError):
        _validate(directory)


def test_should_reject_trace_or_static_symlink_when_validating(tmp_path: Path) -> None:
    # Given
    directory = tmp_path / "recorder"
    materialize_recorder_site(directory, {"happy-path": _trace()})
    trace = directory / "traces" / "happy-path.json"
    trace.unlink()
    trace.symlink_to(Path("examples/ecommerce/flight-recorder/traces/happy-path.json"))
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("safe")
    (static / "asset.js").symlink_to(trace)

    # When / Then
    with pytest.raises(MaterializationError):
        _validate(directory)
    with pytest.raises(MaterializationError):
        _copy_static_tree(static, tmp_path)


def test_should_reject_count_and_byte_bounds_for_static_and_trace_files(tmp_path: Path) -> None:
    # Given
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("safe")
    for number in range(200):
        (static / f"asset-{number}").write_text("x")
    directory = tmp_path / "recorder"
    (directory / "traces").mkdir(parents=True)
    oversized_trace = directory / "traces" / "trace.json"
    oversized_trace.write_bytes(b"x" * (8 * 1024 * 1024 + 1))

    # When / Then
    with pytest.raises(MaterializationError):
        _copy_static_tree(static, tmp_path)
    with pytest.raises(MaterializationError):
        descriptor = _trace_descriptor(directory)
        try:
            assets._read_file(descriptor, oversized_trace.name, 8 * 1024 * 1024)
        finally:
            os.close(descriptor)


def test_should_purge_trace_payloads_but_leave_partial_root_when_materialization_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    destination = tmp_path / "recorder"
    original_write = assets._write_file

    def fail_index_write(descriptor: int, name: str, payload: bytes) -> None:
        if name == "index.json":
            raise OSError("injected write failure")
        original_write(descriptor, name, payload)

    monkeypatch.setattr(assets, "_write_file", fail_index_write)

    # When / Then
    with pytest.raises(OSError, match="injected write failure"):
        materialize_recorder_site(destination, {"happy-path": _trace()})
    assert destination.is_dir()
    assert not any((destination / "traces").iterdir())


def test_should_preserve_failure_when_trace_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    destination = tmp_path / "recorder"

    original_write = assets._write_file

    def fail_index_write(descriptor: int, name: str, payload: bytes) -> None:
        if name == "index.json":
            raise OSError("injected write failure")
        original_write(descriptor, name, payload)

    def fail_trace_cleanup(_: int, name: str) -> None:
        if name == "happy-path.json":
            raise OSError("injected cleanup failure")

    monkeypatch.setattr(assets, "_write_file", fail_index_write)
    monkeypatch.setattr(assets, "_unlink_trace_file", fail_trace_cleanup)

    # When / Then
    with pytest.raises(BaseExceptionGroup) as error:
        materialize_recorder_site(destination, {"happy-path": _trace()})
    messages = {str(item) for item in error.value.exceptions}
    assert messages == {"injected write failure", "injected cleanup failure"}
    assert destination.is_dir()


def test_should_reject_oversized_index_before_reading_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    directory = tmp_path / "recorder"
    (directory / "traces").mkdir(parents=True)
    (directory / "traces" / "index.json").write_bytes(b"x" * (512 * 1024 + 1))

    def fail_read(_: int, __: int) -> bytes:
        raise AssertionError("oversized index must not be read")

    monkeypatch.setattr(os, "read", fail_read)

    # When / Then
    with pytest.raises(MaterializationError):
        descriptor = _trace_descriptor(directory)
        try:
            assets._load_index_at(descriptor)
        finally:
            os.close(descriptor)


def test_should_keep_publication_anchored_when_parent_is_retargeted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    nested = tmp_path / "parent" / "nested"
    nested.mkdir(parents=True)
    destination = nested / "recorder"
    moved = tmp_path / "moved"
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    original_write = assets._write_site

    def retarget_parent(
        site_descriptor: int,
        traces: object,
        presentation: assets._Presentation | None,
    ) -> None:
        nested.rename(moved)
        nested.symlink_to(attacker, target_is_directory=True)
        original_write(site_descriptor, cast(dict[str, RunTrace], traces), presentation)

    monkeypatch.setattr(assets, "_write_site", retarget_parent)

    # When
    materialize_recorder_site(destination, {"happy-path": _trace()})

    # Then
    assert (moved / "recorder" / "index.html").is_file()
    assert not (attacker / "recorder").exists()


def test_should_keep_exclusive_destination_claim_during_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    destination = tmp_path / "recorder"
    original_write = assets._write_site

    def reject_competing_destination(
        site_descriptor: int,
        traces: object,
        presentation: assets._Presentation | None,
    ) -> None:
        with pytest.raises(FileExistsError):
            destination.mkdir()
        original_write(site_descriptor, cast(dict[str, RunTrace], traces), presentation)

    monkeypatch.setattr(assets, "_write_site", reject_competing_destination)

    # When
    materialize_recorder_site(destination, {"happy-path": _trace()})

    # Then
    assert (destination / "index.html").is_file()


def test_should_anchor_each_parent_component_when_ancestor_is_retargeted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    parent = tmp_path / "parent"
    nested = parent / "nested"
    nested.mkdir(parents=True)
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    moved = tmp_path / "moved"
    original_open = assets._open_child

    def retarget_after_open(descriptor: int, name: str) -> int:
        child = original_open(descriptor, name)
        if name == "parent":
            parent.rename(moved)
            parent.symlink_to(attacker, target_is_directory=True)
        return child

    monkeypatch.setattr(assets, "_open_child", retarget_after_open)

    # When
    materialize_recorder_site(nested / "recorder", {"happy-path": _trace()})

    # Then
    assert (moved / "nested" / "recorder" / "index.html").is_file()
    assert not (attacker / "nested" / "recorder").exists()


def test_should_reject_replaced_claim_before_returning_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    destination = tmp_path / "recorder"
    moved = tmp_path / "moved"
    original_validate = assets._validate_site_at

    def replace_claim(descriptor: int) -> None:
        destination.rename(moved)
        destination.mkdir()
        original_validate(descriptor)

    monkeypatch.setattr(assets, "_validate_site_at", replace_claim)

    # When / Then
    with pytest.raises(MaterializationError):
        materialize_recorder_site(destination, {"happy-path": _trace()})
    assert destination.is_dir()
    assert not any(destination.iterdir())


def test_should_not_remove_substitution_immediately_before_trace_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    destination = tmp_path / "recorder"
    moved = tmp_path / "moved"

    def replace_then_fail(_: int, __: object, ___: assets._Presentation | None) -> None:
        destination.rename(moved)
        destination.mkdir()
        (destination / "competitor.txt").write_text("keep")
        raise OSError("injected write failure")

    monkeypatch.setattr(assets, "_write_site", replace_then_fail)

    # When / Then
    with pytest.raises(OSError, match="injected write failure"):
        materialize_recorder_site(destination, {"happy-path": _trace()})
    assert destination.is_dir()
    assert (destination / "competitor.txt").read_text() == "keep"


def test_should_leave_empty_claim_when_opening_it_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    destination = tmp_path / "recorder"

    def fail_claim_open(_: int, __: str) -> int:
        raise OSError("injected claim open failure")

    monkeypatch.setattr(assets, "_open_site_descriptor", fail_claim_open)

    # When / Then
    with pytest.raises(MaterializationError):
        materialize_recorder_site(destination, {"happy-path": _trace()})
    assert destination.is_dir()
    assert not any(destination.iterdir())


def test_should_not_touch_nonempty_substitution_between_claim_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    destination = tmp_path / "recorder"
    moved = tmp_path / "moved"
    original_open = assets._open_site_descriptor

    def substitute_claim(parent_descriptor: int, name: str) -> int:
        destination.rename(moved)
        (destination / "traces").mkdir(parents=True)
        (destination / "traces" / "secret").write_text("keep")
        return original_open(parent_descriptor, name)

    monkeypatch.setattr(assets, "_open_site_descriptor", substitute_claim)

    # When / Then
    with pytest.raises(MaterializationError):
        materialize_recorder_site(destination, {"happy-path": _trace()})
    assert (destination / "traces" / "secret").read_text() == "keep"
    assert not (destination / "index.html").exists()


def test_should_complete_empty_substitution_between_claim_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    destination = tmp_path / "recorder"
    moved = tmp_path / "moved"
    original_open = assets._open_site_descriptor

    def substitute_claim(parent_descriptor: int, name: str) -> int:
        destination.rename(moved)
        destination.mkdir()
        return original_open(parent_descriptor, name)

    monkeypatch.setattr(assets, "_open_site_descriptor", substitute_claim)

    # When
    materialize_recorder_site(destination, {"happy-path": _trace()})

    # Then
    assert (destination / "index.html").is_file()
    assert not any(moved.iterdir())


def test_should_fail_cleanly_without_posix_descriptor_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    monkeypatch.delattr(os, "O_NOFOLLOW")

    # When / Then
    with pytest.raises(MaterializationError, match="POSIX descriptor support"):
        materialize_recorder_site(tmp_path / "recorder", {"happy-path": _trace()})
