"""Supported composition path for the durable Saga runtime."""

from __future__ import annotations

from typing import Final

from agentic_saga.contracts.clock import Clock
from agentic_saga.contracts.runtime import SagaStatus, TerminalRequirement
from agentic_saga.execution.dispatcher import Dispatcher
from agentic_saga.execution.leases import LeaseService
from agentic_saga.execution.reconciliation import Reconciler
from agentic_saga.execution.runtime import SagaRuntime
from agentic_saga.execution.unwind import EmergencyUnwinder
from agentic_saga.kernel.definitions import DefinitionCatalog, SagaDefinition
from agentic_saga.kernel.invariants import TerminalGate
from agentic_saga.kernel.ports import KernelStore
from agentic_saga.kernel.runtime import (
    EventMetadataFactory,
    InvariantEvidenceProvider,
    PolicyContextProvider,
    SagaKernel,
    StableIdFactory,
)

_MAX_WORKER_ID_LENGTH: Final[int] = 200


def _require_worker_id(worker_id: object) -> None:
    if type(worker_id) is not str:
        raise TypeError("worker_id must be an exact str")
    if not 1 <= len(worker_id) <= _MAX_WORKER_ID_LENGTH:
        raise ValueError("worker_id must contain between 1 and 200 characters")


def _require_id_namespace(id_namespace: object) -> None:
    if type(id_namespace) is not bytes:
        raise TypeError("id_namespace must be exact bytes")
    if not id_namespace:
        raise ValueError("id_namespace must not be empty")


def _validate_identity(worker_id: object, id_namespace: object) -> None:
    _require_worker_id(worker_id)
    _require_id_namespace(id_namespace)


def _require_instance(value: object, expected: type[object], role: str) -> None:
    if not isinstance(value, expected):
        raise TypeError(f"{role} does not satisfy {expected.__name__}")


def _validate_collaborators(
    store: object,
    clock: object,
    contexts: object,
    terminal_gate: object,
    evidence: object,
) -> None:
    _require_instance(store, KernelStore, "store")
    _require_instance(clock, Clock, "clock")
    _require_instance(contexts, PolicyContextProvider, "policy_context_provider")
    _require_instance(terminal_gate, TerminalGate, "terminal_gate")
    _require_instance(evidence, InvariantEvidenceProvider, "invariant_evidence_provider")


def _definition_requirements(
    definition: SagaDefinition,
) -> dict[SagaStatus, TerminalRequirement]:
    return {
        SagaStatus.SUCCEEDED_VERIFIED: definition.success_invariants,
        SagaStatus.COMPENSATED_VERIFIED: definition.compensation_invariants,
        SagaStatus.ABORTED_CLEAN: definition.clean_abort_invariants,
        SagaStatus.RESOLVED_WITH_EXCEPTION: definition.exception_invariants,
    }


def _require_matching_gate(definition: SagaDefinition, terminal_gate: TerminalGate) -> None:
    if dict(terminal_gate.requirements) != _definition_requirements(definition):
        raise ValueError("terminal requirements differ from SagaDefinition")


def _kernel(  # noqa: PLR0913, PLR0917
    store: KernelStore,
    definition: SagaDefinition,
    contexts: PolicyContextProvider,
    terminal_gate: TerminalGate,
    evidence: InvariantEvidenceProvider,
    clock: Clock,
    id_namespace: bytes,
) -> SagaKernel:
    return SagaKernel(
        store=store,
        policy=definition.policy,
        registry=definition.registry,
        policy_context_provider=contexts,
        event_metadata_factory=EventMetadataFactory(clock),
        id_factory=StableIdFactory(id_namespace),
        terminal_gate=terminal_gate,
        invariant_evidence_provider=evidence,
        redaction_policy=definition.redaction_policy,
    )


def _runtime(
    definition: SagaDefinition,
    kernel: SagaKernel,
    leases: LeaseService,
    clock: Clock,
    worker_id: str,
) -> SagaRuntime:
    dispatcher = _dispatcher(definition, kernel, clock)
    reconciler = _reconciler(definition, kernel, leases, clock)
    return SagaRuntime(
        kernel=kernel,
        dispatcher=dispatcher,
        reconciler=reconciler,
        unwinder=EmergencyUnwinder(kernel.store, clock=clock),
        leases=leases,
        definitions=DefinitionCatalog((definition,)),
        clock=clock,
        worker_id=worker_id,
    )


def _dispatcher(definition: SagaDefinition, kernel: SagaKernel, clock: Clock) -> Dispatcher:
    return Dispatcher(
        kernel.store,
        definition.registry,
        clock=clock,
        redaction_policy=definition.redaction_policy,
    )


def _reconciler(
    definition: SagaDefinition, kernel: SagaKernel, leases: LeaseService, clock: Clock
) -> Reconciler:
    return Reconciler(
        kernel.store,
        definition.registry,
        lease_service=leases,
        clock=clock,
        redaction_policy=definition.redaction_policy,
    )


def _validated_kernel(  # noqa: PLR0913, PLR0917
    store: KernelStore,
    definition: SagaDefinition,
    contexts: PolicyContextProvider,
    terminal_gate: TerminalGate,
    evidence: InvariantEvidenceProvider,
    clock: Clock,
    worker_id: str,
    id_namespace: bytes,
) -> tuple[SagaKernel, LeaseService]:
    _validate_identity(worker_id, id_namespace)
    _validate_collaborators(store, clock, contexts, terminal_gate, evidence)
    _require_matching_gate(definition, terminal_gate)
    leases = LeaseService(store)
    kernel = _kernel(store, definition, contexts, terminal_gate, evidence, clock, id_namespace)
    return kernel, leases


def compose_runtime(  # noqa: PLR0913
    *,
    store: KernelStore,
    definition: SagaDefinition,
    policy_context_provider: PolicyContextProvider,
    terminal_gate: TerminalGate,
    invariant_evidence_provider: InvariantEvidenceProvider,
    clock: Clock,
    worker_id: str,
    id_namespace: bytes,
) -> SagaRuntime:
    """Compose one runtime from explicit application-owned collaborators."""
    kernel, leases = _validated_kernel(
        store,
        definition,
        policy_context_provider,
        terminal_gate,
        invariant_evidence_provider,
        clock,
        worker_id,
        id_namespace,
    )
    return _runtime(definition, kernel, leases, clock, worker_id)


__all__ = ["compose_runtime"]
