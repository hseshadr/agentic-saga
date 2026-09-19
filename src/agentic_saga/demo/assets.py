from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from importlib import resources
from pathlib import Path
from typing import Final, Literal, cast

from agentic_saga.contracts.trace import RunTrace

_MAX_RUNS: Final[int] = 100
_MAX_STATIC_FILES: Final[int] = 200
_MAX_STATIC_BYTES: Final[int] = 32 * 1024 * 1024
_MAX_TRACE_BYTES: Final[int] = 8 * 1024 * 1024
_MAX_INDEX_BYTES: Final[int] = 512 * 1024
_MAX_NAME_CHARS: Final[int] = 120
_MAX_SUMMARY_CHARS: Final[int] = 500
_SCENARIO = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_Presentation = Literal["ecommerce"]


class MaterializationError(ValueError):
    """Raised when recorder files cannot be safely materialized."""


@dataclass(frozen=True)
class _Anchor:
    parent_descriptor: int
    site_descriptor: int
    name: str
    site_identity: tuple[int, int]


def materialize_recorder_site(
    destination: Path,
    traces: Mapping[str, RunTrace],
    *,
    presentation: _Presentation | None = None,
) -> None:
    """Build a fresh recorder site, purging trace payloads after failure.

    An unserved empty root can remain for caller cleanup; same-UID mutation is out of scope.
    """
    _require_destination(destination, traces)
    _require_posix_capabilities()
    anchor = _claim_destination(destination)
    try:
        _materialize_claim(anchor, traces, presentation)
    finally:
        os.close(anchor.site_descriptor)
        os.close(anchor.parent_descriptor)


def _require_destination(destination: Path, traces: Mapping[str, RunTrace]) -> None:
    _require_bounded_traces(traces)
    for scenario, trace in traces.items():
        _require_scenario(scenario)
        _require_trace(trace)


def _require_bounded_traces(traces: Mapping[str, RunTrace]) -> None:
    if not traces:
        raise MaterializationError("recorder requires a bounded non-empty trace mapping")
    if len(traces) > _MAX_RUNS:
        raise MaterializationError("recorder requires a bounded non-empty trace mapping")


def _require_scenario(scenario: str) -> None:
    if type(scenario) is not str or _SCENARIO.fullmatch(scenario) is None:
        raise MaterializationError("recorder scenario name is unsafe")


def _require_trace(trace: RunTrace) -> None:
    if not isinstance(trace, RunTrace):
        raise MaterializationError("recorder trace is invalid")
    try:
        RunTrace.model_validate_json(trace.model_dump_json(), strict=True)
    except (TypeError, ValueError) as error:
        raise MaterializationError("recorder trace is invalid") from error


def _claim_destination(destination: Path) -> _Anchor:
    parent_descriptor, name = _open_parent(destination)
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
    except OSError as error:
        os.close(parent_descriptor)
        raise MaterializationError("recorder destination already exists or is unsafe") from error
    try:
        site_descriptor = _open_empty_site(parent_descriptor, name)
    except (OSError, MaterializationError) as error:
        os.close(parent_descriptor)
        raise MaterializationError("recorder destination claim is unsafe") from error
    return _Anchor(parent_descriptor, site_descriptor, name, _descriptor_identity(site_descriptor))


def _open_empty_site(parent_descriptor: int, name: str) -> int:
    site_descriptor = _open_site_descriptor(parent_descriptor, name)
    try:
        _require_empty_site(site_descriptor)
    except (OSError, MaterializationError):
        os.close(site_descriptor)
        raise
    return site_descriptor


def _open_site_descriptor(parent_descriptor: int, name: str) -> int:
    return os.open(name, _directory_flags(), dir_fd=parent_descriptor)


def _require_empty_site(site_descriptor: int) -> None:
    if os.listdir(site_descriptor):
        raise MaterializationError("recorder destination claim is not empty")


def _open_parent(destination: Path) -> tuple[int, str]:
    absolute = Path(os.path.abspath(destination))
    if absolute.name in {"", "."}:
        raise MaterializationError("recorder destination is unsafe")
    return _open_components(absolute.parent.parts[1:]), absolute.name


def _open_components(parts: tuple[str, ...]) -> int:
    descriptor = os.open("/", _directory_flags())
    try:
        for part in parts:
            descriptor = _open_child(descriptor, part)
        return descriptor
    except OSError as error:
        os.close(descriptor)
        raise MaterializationError("recorder destination parent is unsafe") from error


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _require_posix_capabilities() -> None:
    required = ("O_DIRECTORY", "O_NOFOLLOW", "supports_dir_fd")
    if os.name != "posix" or any(not hasattr(os, name) for name in required):
        raise MaterializationError("recorder materialization requires POSIX descriptor support")
    if not _has_directory_operations():
        raise MaterializationError("recorder materialization requires POSIX descriptor support")


def _has_directory_operations() -> bool:
    required = {os.mkdir, os.open, os.stat, os.unlink}
    return required.issubset(os.supports_dir_fd)


def _open_child(descriptor: int, part: str) -> int:
    child = os.open(part, _directory_flags(), dir_fd=descriptor)
    os.close(descriptor)
    return child


def _materialize_claim(
    anchor: _Anchor,
    traces: Mapping[str, RunTrace],
    presentation: _Presentation | None,
) -> None:
    try:
        _write_site(anchor.site_descriptor, traces, presentation)
        _validate_site_at(anchor.site_descriptor)
        _require_current_claim(anchor)
    except BaseException as original:
        _cleanup_failure(original, anchor)
        raise


def _cleanup_failure(original: BaseException, anchor: _Anchor) -> None:
    try:
        _remove_trace_payloads(anchor.site_descriptor)
    except OSError as cleanup:
        errors = [original, cleanup]
        group = BaseExceptionGroup("recorder materialization and cleanup failed", errors)
        raise group from original


def _remove_trace_payloads(site_descriptor: int) -> None:
    try:
        trace_descriptor = os.open("traces", _directory_flags(), dir_fd=site_descriptor)
    except FileNotFoundError:
        return
    try:
        for name in os.listdir(trace_descriptor):
            _unlink_trace_file(trace_descriptor, name)
    finally:
        os.close(trace_descriptor)


def _unlink_trace_file(trace_descriptor: int, name: str) -> None:
    os.unlink(name, dir_fd=trace_descriptor)


def _require_current_claim(anchor: _Anchor) -> None:
    if _name_identity(anchor) != anchor.site_identity:
        raise MaterializationError("recorder destination changed during construction")


def _name_identity(anchor: _Anchor) -> tuple[int, int] | None:
    try:
        claim = os.stat(anchor.name, dir_fd=anchor.parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return claim.st_dev, claim.st_ino


def _descriptor_identity(descriptor: int) -> tuple[int, int]:
    site = os.fstat(descriptor)
    return site.st_dev, site.st_ino


def _write_site(
    site_descriptor: int,
    traces: Mapping[str, RunTrace],
    presentation: _Presentation | None,
) -> None:
    _copy_static_assets(site_descriptor)
    os.mkdir("traces", mode=0o700, dir_fd=site_descriptor)
    trace_descriptor = os.open("traces", _directory_flags(), dir_fd=site_descriptor)
    try:
        entries = tuple(
            _write_trace(trace_descriptor, item, presentation) for item in sorted(traces.items())
        )
        _write_file(trace_descriptor, "index.json", _index_payload(entries))
    finally:
        os.close(trace_descriptor)


def _copy_static_assets(destination: int) -> None:
    resource = resources.files("agentic_saga.demo").joinpath("static")
    with resources.as_file(resource) as source:
        _copy_checked_tree(source, destination)


def _copy_checked_tree(source: Path, destination: int) -> None:
    files = tuple(candidate for candidate in source.rglob("*") if candidate.is_file())
    _require_static_tree(source, files)
    _copy_to_descriptor(files, source, destination)


def _copy_to_descriptor(files: tuple[Path, ...], source: Path, destination: int) -> None:
    for candidate in files:
        parent, name = _target_location(candidate.relative_to(source), destination)
        try:
            _write_file(parent, name, candidate.read_bytes())
        finally:
            os.close(parent)


def _target_location(relative: Path, root: int) -> tuple[int, str]:
    parent = os.dup(root)
    for part in relative.parts[:-1]:
        _make_directory(parent, part)
        parent = _open_child(parent, part)
    return parent, relative.name


def _make_directory(descriptor: int, name: str) -> None:
    try:
        os.mkdir(name, mode=0o700, dir_fd=descriptor)
    except FileExistsError:
        return


def _require_static_tree(source: Path, files: tuple[Path, ...]) -> None:
    _require_static_root(source)
    _require_static_files(files)
    _require_static_size(source, files)


def _require_static_root(source: Path) -> None:
    if source.is_symlink() or not (source / "index.html").is_file():
        raise MaterializationError("packaged recorder assets are unsafe")


def _require_static_files(files: tuple[Path, ...]) -> None:
    if not files or len(files) > _MAX_STATIC_FILES:
        raise MaterializationError("packaged recorder assets are unsafe")


def _require_static_size(source: Path, files: tuple[Path, ...]) -> None:
    if any(candidate.is_symlink() for candidate in source.rglob("*")):
        raise MaterializationError("packaged recorder assets contain a symlink")
    size = sum(candidate.stat().st_size for candidate in files)
    if size > _MAX_STATIC_BYTES:
        raise MaterializationError("packaged recorder assets exceed safety bounds")


def _write_trace(
    trace_directory: int,
    item: tuple[str, RunTrace],
    presentation: _Presentation | None,
) -> dict[str, str]:
    scenario, trace = item
    payload = _trace_payload(trace)
    trace_name = f"{scenario}.json"
    _write_file(trace_directory, trace_name, payload)
    return _index_entry(scenario, trace_name, payload, presentation)


def _trace_payload(trace: RunTrace) -> bytes:
    payload = f"{trace.model_dump_json(indent=2)}\n".encode()
    if len(payload) > _MAX_TRACE_BYTES:
        raise MaterializationError("recorder trace exceeds safety bounds")
    try:
        RunTrace.model_validate_json(payload, strict=True)
    except ValueError as error:
        raise MaterializationError("recorder trace serialization is invalid") from error
    return payload


def _index_entry(
    scenario: str,
    trace_name: str,
    payload: bytes,
    presentation: _Presentation | None,
) -> dict[str, str]:
    entry = {
        "id": scenario,
        "name": f"Recorded run: {scenario}",
        "summary": "Materialized strict RunTrace 1.0 evidence.",
        "mode": "scripted",
        "trace_ref": trace_name,
        "trace_sha256": sha256(payload).hexdigest(),
    }
    if presentation is not None:
        entry["presentation"] = presentation
    return entry


def _index_payload(entries: tuple[dict[str, str], ...]) -> bytes:
    payload = f"{json.dumps({'schema_version': '1.0', 'runs': entries}, indent=2)}\n".encode()
    if len(payload) > _MAX_INDEX_BYTES:
        raise MaterializationError("recorder trace index exceeds safety bounds")
    return payload


def _write_file(descriptor: int, name: str, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    target = os.open(name, flags, mode=0o600, dir_fd=descriptor)
    try:
        _write_all(target, payload)
    finally:
        os.close(target)


def _write_all(descriptor: int, payload: bytes) -> None:
    remainder = memoryview(payload)
    while remainder:
        written = os.write(descriptor, remainder)
        if written <= 0:
            raise OSError("recorder file write was incomplete")
        remainder = remainder[written:]


def _validate_site_at(site_descriptor: int) -> None:
    trace_descriptor = os.open("traces", _directory_flags(), dir_fd=site_descriptor)
    try:
        index = _load_index_at(trace_descriptor)
        _validate_entries_at(trace_descriptor, index["runs"])
    finally:
        os.close(trace_descriptor)


def _load_index_at(trace_descriptor: int) -> dict[str, object]:
    try:
        payload = json.loads(_read_file(trace_descriptor, "index.json", _MAX_INDEX_BYTES))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MaterializationError("recorder trace index is invalid") from error
    if not _has_expected_index_shape(payload):
        raise MaterializationError("recorder trace index is invalid")
    return cast(dict[str, object], payload)


def _read_file(descriptor: int, name: str, maximum: int) -> bytes:
    try:
        target = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
    except OSError as error:
        raise MaterializationError("recorder file is unsafe") from error
    try:
        size = os.fstat(target).st_size
        if size > maximum:
            raise MaterializationError("recorder file exceeds safety bounds")
        return _read_all(target, maximum)
    finally:
        os.close(target)


def _read_all(descriptor: int, maximum: int) -> bytes:
    chunks: list[bytes] = []
    remaining = maximum + 1
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > maximum:
        raise MaterializationError("recorder file exceeds safety bounds")
    return payload


def _has_expected_index_shape(payload: object) -> bool:
    return (
        isinstance(payload, dict)
        and set(payload) == {"schema_version", "runs"}
        and payload.get("schema_version") == "1.0"
    )


def _validate_entries_at(trace_descriptor: int, entries: object) -> None:
    checked_entries = _require_entries(entries)
    seen: set[str] = set()
    for entry in checked_entries:
        scenario = _validate_entry_at(trace_descriptor, entry)
        _record_unique_scenario(seen, scenario)


def _require_entries(entries: object) -> list[object]:
    if not isinstance(entries, list):
        raise MaterializationError("recorder trace index is invalid")
    if not entries or len(entries) > _MAX_RUNS:
        raise MaterializationError("recorder trace index is invalid")
    return entries


def _record_unique_scenario(seen: set[str], scenario: str) -> None:
    if scenario in seen:
        raise MaterializationError("recorder trace index is invalid")
    seen.add(scenario)


def _validate_entry_at(trace_descriptor: int, entry: object) -> str:
    index_entry = _require_entry(entry)
    scenario, reference, digest = _entry_trace_details(index_entry)
    _require_scenario(scenario)
    _require_reference(scenario, reference, digest)
    payload = _read_file(trace_descriptor, reference, _MAX_TRACE_BYTES)
    _require_digest(payload, digest)
    _require_trace_payload(payload)
    return scenario


def _require_entry(entry: object) -> dict[str, object]:
    if not _has_expected_entry_shape(entry):
        raise MaterializationError("recorder trace index is invalid")
    return cast(dict[str, object], entry)


def _entry_trace_details(entry: dict[str, object]) -> tuple[str, str, str]:
    return cast(str, entry["id"]), cast(str, entry["trace_ref"]), cast(str, entry["trace_sha256"])


def _require_reference(scenario: str, reference: str, digest: str) -> None:
    if reference != f"{scenario}.json":
        raise MaterializationError("recorder trace index digest is invalid")
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise MaterializationError("recorder trace index digest is invalid")


def _require_digest(payload: bytes, digest: str) -> None:
    if sha256(payload).hexdigest() != digest:
        raise MaterializationError("recorder trace index digest is invalid")


def _has_expected_entry_shape(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    required = {"id", "name", "summary", "mode", "trace_ref", "trace_sha256"}
    if not required.issubset(entry) or not set(entry) - required <= {"presentation"}:
        return False
    return _has_valid_entry_values(entry)


def _has_valid_entry_values(entry: dict[object, object]) -> bool:
    if not all(isinstance(entry.get(key), str) for key in entry):
        return False
    return (
        _has_valid_entry_text(entry) and _has_valid_mode(entry) and _has_valid_presentation(entry)
    )


def _has_valid_entry_text(entry: dict[object, object]) -> bool:
    return _is_bounded_text(entry["name"], _MAX_NAME_CHARS) and _is_bounded_text(
        entry["summary"], _MAX_SUMMARY_CHARS
    )


def _has_valid_mode(entry: dict[object, object]) -> bool:
    return entry["mode"] in {"scripted", "live"}


def _has_valid_presentation(entry: dict[object, object]) -> bool:
    return entry.get("presentation", "ecommerce") == "ecommerce"


def _is_bounded_text(value: object, maximum: int) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= maximum


def _require_trace_payload(payload: bytes) -> None:
    try:
        RunTrace.model_validate_json(payload, strict=True)
    except ValueError as error:
        raise MaterializationError("recorder trace serialization is invalid") from error


__all__ = ["MaterializationError", "materialize_recorder_site"]
