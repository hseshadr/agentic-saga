from __future__ import annotations

import os
import stat
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Final, Literal

from agentic_saga.contracts.trace import RunTrace

type ReferenceScenario = Literal[
    "happy-path",
    "business-failure",
    "lost-response",
    "compensation-failure",
]

REFERENCE_SCENARIOS: Final[tuple[ReferenceScenario, ...]] = (
    "happy-path",
    "business-failure",
    "lost-response",
    "compensation-failure",
)
REFERENCE_PRESENTATIONS: Final[dict[str, str]] = {
    "happy-path": "Everything works",
    "business-failure": "Delivery fails after payment",
    "lost-response": "The payment reply is lost",
    "compensation-failure": "The refund needs human verification",
}
DEFAULT_REFERENCE_SCENARIO: Final[ReferenceScenario] = "business-failure"
_MAX_TRACE_BYTES: Final[int] = 8 * 1024 * 1024


class ReferenceTraceError(ValueError):
    """Raised when a packaged reference trace cannot be loaded safely."""


def reference_summary(trace: RunTrace) -> str:
    """Describe the recorded ecommerce outcome without inferring it from a scenario name."""
    if trace.outcome.value == "succeeded_verified":
        return _success_summary(trace)
    if trace.outcome.value == "compensated_verified":
        return _recovery_summary(trace)
    if trace.outcome.value == "human_required":
        return "The order did not complete. Recovery is unresolved and needs human review."
    return "The recording ends before a verified order or recovery outcome."


def _success_summary(trace: RunTrace) -> str:
    if any(event.event_type == "reconciliation_recorded" for event in trace.events):
        return "The order succeeds: payment was reconciled without charging twice."
    return "The order succeeds: stock, payment, and delivery are verified."


def _recovery_summary(trace: RunTrace) -> str:
    if any(event.event_type == "human_resolved" for event in trace.events):
        return (
            "The order fails. Human verification resolves the uncertain refund; recovery succeeds."
        )
    return "The order fails. Recovery succeeds: payment is refunded and stock is released."


def load_reference_trace(scenario: str) -> RunTrace:
    """Load one bounded, strict, captured reference trace."""
    _require_scenario(scenario)
    try:
        with resources.as_file(_reference_resource(scenario)) as path:
            payload = _read_reference(path)
    except ReferenceTraceError:
        raise
    except OSError as error:
        raise ReferenceTraceError("reference trace is unavailable") from error
    return _parse_reference(payload)


def _require_scenario(scenario: str) -> None:
    if scenario not in REFERENCE_SCENARIOS:
        raise ReferenceTraceError("reference scenario is invalid")


def _reference_resource(scenario: str) -> Traversable:
    packaged = resources.files("agentic_saga.demo").joinpath("traces")
    if packaged.joinpath("index.json").is_file():
        return packaged.joinpath(f"{scenario}.json")
    return _source_reference_root().joinpath(f"{scenario}.json")


def _source_reference_root() -> Path:
    try:
        module = Path(__file__).resolve(strict=True)
    except OSError as error:
        raise ReferenceTraceError("reference trace is unavailable") from error
    root = _editable_root(module)
    traces = root / "examples" / "ecommerce" / "flight-recorder" / "traces"
    if not (root / "pyproject.toml").is_file() or not (traces / "index.json").is_file():
        raise ReferenceTraceError("reference trace is unavailable")
    return traces


def _editable_root(module: Path) -> Path:
    expected = ("reference.py", "demo", "agentic_saga", "src")
    actual = (module.name, module.parent.name, module.parents[1].name, module.parents[2].name)
    if actual != expected:
        raise ReferenceTraceError("reference trace is unavailable")
    return module.parents[3]


def _read_reference(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ReferenceTraceError("reference trace is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise ReferenceTraceError("reference trace is unavailable")
    if metadata.st_size > _MAX_TRACE_BYTES:
        raise ReferenceTraceError("reference trace exceeds safety bounds")
    return _bounded_read(path)


def _bounded_read(path: Path) -> bytes:
    descriptor = _open_reference(path)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ReferenceTraceError("reference trace is unavailable")
        if metadata.st_size > _MAX_TRACE_BYTES:
            raise ReferenceTraceError("reference trace exceeds safety bounds")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            payload = stream.read(_MAX_TRACE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(payload) > _MAX_TRACE_BYTES:
        raise ReferenceTraceError("reference trace exceeds safety bounds")
    return payload


def _open_reference(path: Path) -> int:
    try:
        return os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise ReferenceTraceError("reference trace is unavailable") from error


def _parse_reference(payload: bytes) -> RunTrace:
    try:
        return RunTrace.model_validate_json(payload, strict=True)
    except (TypeError, ValueError) as error:
        raise ReferenceTraceError("reference trace is invalid") from error


__all__ = [
    "DEFAULT_REFERENCE_SCENARIO",
    "REFERENCE_SCENARIOS",
    "ReferenceScenario",
    "ReferenceTraceError",
    "load_reference_trace",
]
