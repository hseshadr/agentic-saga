from __future__ import annotations

import agentic_saga
from agentic_saga import execution, storage
from agentic_saga.contracts import runtime as runtime_contracts
from agentic_saga.contracts.runtime import SagaGoal
from agentic_saga.execution import leases
from agentic_saga.execution.runtime import SagaRuntime
from agentic_saga.kernel import invariants, policy, state
from agentic_saga.kernel.definitions import SagaDefinition

_LEGACY_REEXPORTS = (
    (leases, "Lease"),
    (leases, "LeaseLost"),
    (leases, "LeaseUnavailable"),
    (invariants, "TerminalRequirement"),
    (policy, "ExecutionBudget"),
    (state, "SagaStatus"),
    (runtime_contracts, "SagaState"),
    (storage, "LeaseState"),
)


def test_should_export_supported_runtime_composition_from_public_facades() -> None:
    # Given
    root_exports = tuple(agentic_saga.__all__)
    execution_exports = tuple(execution.__all__)

    # When
    root_factory_is_public = "compose_runtime" in root_exports
    execution_factory_is_public = "compose_runtime" in execution_exports

    # Then
    assert root_factory_is_public
    assert execution_factory_is_public
    assert agentic_saga.compose_runtime is execution.compose_runtime
    assert agentic_saga.SagaRuntime is SagaRuntime
    assert execution.SagaRuntime is SagaRuntime
    assert agentic_saga.SagaDefinition is SagaDefinition
    assert agentic_saga.SagaGoal is SagaGoal


def test_should_expose_contracts_only_from_their_canonical_modules() -> None:
    # Given
    legacy_owners = _LEGACY_REEXPORTS

    # When
    exposed = tuple(symbol for module, symbol in legacy_owners if hasattr(module, symbol))

    # Then
    assert exposed == ()
