from __future__ import annotations

from pathlib import Path

import agentic_saga
from agentic_saga import temporal
from agentic_saga.temporal import WorkflowState, start_saga

_REQUIRED = frozenset(
    {
        "SagaContext",
        "SagaGoal",
        "SagaManifest",
        "SagaWorkflowInput",
        "WorkflowResult",
        "WorkflowState",
        "WorkflowTool",
        "load_saga_context",
        "query_saga_state",
        "resolve_human_compensation",
        "start_saga",
    }
)
_LEGACY = frozenset({"SagaDefinition", "SagaRuntime", "compose_runtime"})
_TEMPORAL_LEGOS = frozenset(
    {"TemporalActivities", "build_worker", "connect_client", "project_run_trace"}
)


def test_root_exports_the_typed_temporal_surface() -> None:
    exports = frozenset(agentic_saga.__all__)

    assert exports >= _REQUIRED
    assert not exports & _LEGACY
    assert agentic_saga.WorkflowState is WorkflowState
    assert agentic_saga.start_saga is start_saga


def test_legacy_runtime_packages_are_absent() -> None:
    package = Path(agentic_saga.__file__).parent
    for name in ("execution", "kernel", "storage"):
        assert not tuple((package / name).glob("*.py"))


def test_temporal_package_exports_integration_legos() -> None:
    assert frozenset(temporal.__all__) >= _TEMPORAL_LEGOS
    assert all(callable(getattr(temporal, name)) for name in _TEMPORAL_LEGOS)
