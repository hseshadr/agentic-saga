from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel, ValidationError

from agentic_saga.contracts.actions import HumanDecision, ToolCall
from agentic_saga.contracts.common import SagaId, sha256_json
from agentic_saga.contracts.events import (
    AgentTurnReserved,
    InvariantEvaluated,
    LedgerEvent,
    ReadObserved,
    ReadStarted,
    ReadUnavailable,
    SagaCreated,
    SagaStarted,
)
from agentic_saga.contracts.redaction import RedactionPolicy
from agentic_saga.contracts.runtime import (
    ExecutionBudget,
    SagaStatus,
    TerminalRequirement,
)
from agentic_saga.contracts.tools import ToolRegistry
from agentic_saga.contracts.trace import RunTrace, TraceAuthority, TraceEvent
from agentic_saga.evidence.run_trace import RunTraceExporter, TraceExportError
from agentic_saga.kernel.definitions import DefinitionCatalog, SagaDefinition
from agentic_saga.kernel.identity import OperationIdentityFactory
from agentic_saga.kernel.policy import (
    PolicyContext,
    PolicyEngine,
    PolicyRules,
    ProposedEffectIdentity,
)
from agentic_saga.kernel.reducer import reduce_event
from agentic_saga.kernel.state import SagaSnapshot
from agentic_saga.storage import SQLiteKernelStore
from tests.integration.kernel.test_proposal_to_outbox import Adapter, registry
from tests.integration.kernel.test_terminal_assignment import Evidence, finish, terminal_kernel
from tests.support.kernel_harness import KernelHarness

NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)
SAGA_ID: SagaId = "saga_0000000000000001"


def valid_trace_payload() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "run_id": "trace_0000000000000001",
        "saga_id": SAGA_ID,
        "definition_version": "checkout-v1",
        "started_at": NOW,
        "finished_at": None,
        "outcome": SagaStatus.CREATED,
        "events": (_trace_created(),),
        "proofs": (),
        "final_projection_hash": "a" * 64,
    }


def _trace_created() -> TraceEvent:
    return TraceEvent(
        event_id="evt_0000000000000001",
        saga_seq=1,
        recorded_at=NOW,
        authority=TraceAuthority.KERNEL,
        event_type="saga_created",
        actor="kernel",
        trace_id="trace_0000000000000001",
        definition_version="checkout-v1",
        fence_token=None,
        before_status=None,
        after_status=SagaStatus.CREATED,
        rationale={"definition_name": "checkout"},
    )


def test_trace_contract_rejects_unknown_schema_version() -> None:
    payload = valid_trace_payload() | {"schema_version": "2.0"}

    with pytest.raises(ValidationError):
        RunTrace.model_validate(payload)


def test_trace_contract_is_strict_and_frozen() -> None:
    trace = RunTrace.model_validate(valid_trace_payload())

    with pytest.raises(ValidationError):
        RunTrace.model_validate(valid_trace_payload() | {"browser_event": {}})
    with pytest.raises(ValidationError):
        trace.outcome = SagaStatus.HUMAN_REQUIRED


@pytest.mark.parametrize(
    "change",
    [
        {"run_id": "trace_0000000000000002"},
        {"started_at": NOW + timedelta(seconds=1)},
        {"outcome": SagaStatus.RUNNING},
        {"finished_at": NOW},
    ],
)
def test_trace_contract_rejects_inconsistent_header(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RunTrace.model_validate(valid_trace_payload() | change)


def _allow(command: BaseModel, snapshot: SagaSnapshot, context: PolicyContext) -> bool:
    del command, snapshot, context
    return True


def _not_duplicate(
    identity: ProposedEffectIdentity, snapshot: SagaSnapshot, context: PolicyContext
) -> bool:
    del identity, snapshot, context
    return False


def _deny_approval(
    decision: HumanDecision,
    proposal: ToolCall,
    snapshot: SagaSnapshot,
    context: PolicyContext,
) -> bool:
    del decision, proposal, snapshot, context
    return False


def _catalog(
    name: str,
    version: str,
    tools: ToolRegistry,
    redaction_policy: RedactionPolicy | None = None,
) -> DefinitionCatalog:
    requirement = TerminalRequirement(
        invariant_version="invariants-v1", required_rule_ids=("settled",)
    )
    policy = PolicyEngine(
        registry=tools,
        identity_factory=OperationIdentityFactory(b"task-13-tests"),
        rules=_rules(),
    )
    definition = SagaDefinition(
        name,
        version,
        tools,
        policy,
        requirement,
        requirement,
        requirement,
        requirement,
        _budget(),
        redaction_policy or RedactionPolicy(),
    )
    return DefinitionCatalog((definition,))


def _rules() -> PolicyRules:
    return PolicyRules(
        is_duplicate_effect=_not_duplicate,
        approval_required=_allow,
        approval_verifier=_deny_approval,
    )


def _budget() -> ExecutionBudget:
    return ExecutionBudget(
        turn_limit=10,
        tool_call_limit=10,
        elapsed_ms_limit=10_000,
        token_limit=10_000,
    )


def _harness_exporter(harness: KernelHarness) -> RunTraceExporter:
    definitions = _catalog("harness", "harness-saga-v1", harness.registry)
    return RunTraceExporter(harness.store, definitions)


def test_nonterminal_export_is_ordered_deterministic_and_causally_structured(
    tmp_path: Path,
) -> None:
    harness = KernelHarness.create(tmp_path)
    exporter = _harness_exporter(harness)

    first = exporter.export(harness.lease.saga_id)
    second = exporter.export(harness.lease.saga_id)

    assert first.model_dump_json() == second.model_dump_json()
    assert [event.saga_seq for event in first.events] == [1, 2, 3]
    assert [event.event_id for event in first.events] == [
        event.event_id for event in harness.store.read_events(harness.lease.saga_id)
    ]
    assert [event.authority for event in first.events] == [
        TraceAuthority.KERNEL,
        TraceAuthority.KERNEL,
        TraceAuthority.EFFECT,
    ]
    intent = first.events[-1]
    assert (intent.before_status, intent.after_status) == (SagaStatus.RUNNING,) * 2
    assert (intent.operation_id, intent.step_instance_id, intent.attempt) == (
        harness.operation_id,
        "step_00008001",
        1,
    )
    assert first.finished_at is None


def test_terminal_export_contains_only_current_durable_proof(tmp_path: Path) -> None:
    runtime, store, lease = terminal_kernel(tmp_path / "saga.db", Evidence())
    runtime.submit_proposal(lease.saga_id, finish(), lease)
    definitions = _catalog("checkout", "checkout-v1", registry(Adapter()))

    trace = RunTraceExporter(store, definitions).export(lease.saga_id)

    proof_event = cast(InvariantEvaluated, store.read_events(lease.saga_id)[-2])
    proof = trace.proofs[-1]
    assert trace.outcome is SagaStatus.SUCCEEDED_VERIFIED
    assert trace.finished_at == trace.events[-1].recorded_at
    assert proof.source_event_seq == trace.events[-2].saga_seq
    assert proof.evaluated_at_seq == proof_event.evaluated_at_seq
    assert proof.inputs is None
    assert proof.explanation == "ledger_recorded_invariant_result"


def test_backup_and_reopen_export_identical_trace(tmp_path: Path) -> None:
    harness = KernelHarness.create(tmp_path)
    expected = _harness_exporter(harness).export(harness.lease.saga_id)
    backup = tmp_path / "backup.db"
    harness.store.backup_to(backup)

    reopened = SQLiteKernelStore.open(backup)
    actual = RunTraceExporter(reopened, _catalog("harness", "harness-saga-v1", harness.registry))

    assert actual.export(harness.lease.saga_id) == expected


def _unsafe_created() -> SagaCreated:
    return SagaCreated.model_validate(
        {
            "event_id": "evt_0000000000000001",
            "saga_id": SAGA_ID,
            "saga_seq": 1,
            "definition_version": "checkout-v1",
            "fence_token": None,
            "actor": "kernel",
            "trace_id": "trace_0000000000000001",
            "recorded_at": NOW,
            "definition_name": "checkout",
            "definition_fingerprint": "f" * 64,
            "redacted_goal": {
                "Authorization": "Bearer reusable-secret",
                "customer": {"CARD_NUMBER": "4242424242424242"},
            },
        }
    )


def test_export_redacts_nested_values_without_exporting_raw_digest(tmp_path: Path) -> None:
    store = SQLiteKernelStore.initialize(tmp_path / "saga.db")
    store.create_saga(_unsafe_created())
    definitions = _catalog("checkout", "checkout-v1", ToolRegistry(()))

    payload = RunTraceExporter(store, definitions).export(SAGA_ID).model_dump_json()

    assert "reusable-secret" not in payload
    assert "4242424242424242" not in payload
    assert sha256(b"reusable-secret").hexdigest() not in payload
    assert payload.count("[REDACTED]") == 2


def test_export_uses_the_exact_historical_application_privacy_policy(tmp_path: Path) -> None:
    private = ("customer@example.com", "Private Customer", "1 Private Lane")
    goal = dict(zip(("email", "name", "address"), private, strict=True))
    goal["public_id"] = "customer-7"
    created = _unsafe_created().model_copy(update={"redacted_goal": goal})
    store = SQLiteKernelStore.initialize(tmp_path / "application-privacy.db")
    store.create_saga(created)
    definitions = _catalog(
        "checkout",
        "checkout-v1",
        ToolRegistry(()),
        RedactionPolicy(sensitive_keys=("email", "name", "address")),
    )

    payload = RunTraceExporter(store, definitions).export(SAGA_ID).model_dump_json()

    assert all(value not in payload for value in private)
    assert "customer-7" in payload


@dataclass(frozen=True)
class FakeTraceStore:
    events: tuple[LedgerEvent, ...]
    loaded: SagaSnapshot
    rebuilt: SagaSnapshot

    def read_events(self, saga_id: SagaId) -> tuple[LedgerEvent, ...]:
        del saga_id
        return self.events

    def load_snapshot(self, saga_id: SagaId) -> SagaSnapshot:
        del saga_id
        return self.loaded

    def rebuild_and_verify(self, saga_id: SagaId) -> SagaSnapshot:
        del saga_id
        return self.rebuilt


def _started_events() -> tuple[LedgerEvent, ...]:
    created = _unsafe_created().model_copy(update={"redacted_goal": {"order_id": "order-1"}})
    started = SagaStarted(
        event_id="evt_0000000000000002",
        saga_id=SAGA_ID,
        saga_seq=2,
        definition_version="checkout-v1",
        fence_token=1,
        actor="kernel",
        trace_id="trace_0000000000000001",
        recorded_at=NOW + timedelta(seconds=1),
    )
    return created, started


def _read_events(result: dict[str, object]) -> tuple[LedgerEvent, ...]:
    created, started = _started_events()
    common = _read_event_base()
    reserved = AgentTurnReserved.model_validate(common | _reservation_fields())
    command: dict[str, object] = {"order_id": "order-1"}
    read = ReadStarted.model_validate(_read_start_fields(command))
    observed = ReadObserved.model_validate(_read_result_fields(result))
    return created, started, reserved, read, observed


def _read_event_base() -> dict[str, object]:
    return {
        "event_id": "evt_0000000000000003",
        "saga_id": SAGA_ID,
        "saga_seq": 3,
        "definition_version": "checkout-v1",
        "fence_token": 1,
        "actor": "kernel",
        "trace_id": "trace_0000000000000002",
        "recorded_at": NOW + timedelta(seconds=2),
    }


def _reservation_fields() -> dict[str, object]:
    return {
        "turn_id": "turn_0000000000000001",
        "turn_index": 1,
        "reserved_elapsed_ms": 10,
        "reserved_tokens": 10,
    }


def test_should_reject_unenforceable_provider_cost_reservation() -> None:
    # Given
    raw = _read_event_base() | _reservation_fields()
    raw["reserved_cost_microusd"] = 10

    # When / Then
    with pytest.raises(ValidationError, match="reserved_cost_microusd"):
        AgentTurnReserved.model_validate(raw)


def _read_start_fields(command: dict[str, object]) -> dict[str, object]:
    return _read_event_base() | {
        "event_id": "evt_0000000000000004",
        "saga_seq": 4,
        "recorded_at": NOW + timedelta(seconds=3),
        "turn_id": "turn_0000000000000001",
        "proposal_id": "proposal_0000000000000001",
        "tool_name": "read_order",
        "redacted_command": command,
        "command_hash": sha256_json(command),
    }


def _read_result_fields(result: dict[str, object]) -> dict[str, object]:
    return _read_event_base() | {
        "event_id": "evt_0000000000000005",
        "saga_seq": 5,
        "recorded_at": NOW + timedelta(seconds=4),
        "turn_id": "turn_0000000000000001",
        "proposal_id": "proposal_0000000000000001",
        "tool_name": "read_order",
        "redacted_result": result,
        "result_hash": sha256_json(result),
    }


def test_export_includes_read_unavailable_as_safe_effect_evidence() -> None:
    # Given
    events = (*_read_events({})[:-1], ReadUnavailable.model_validate(_read_unavailable_fields()))
    snapshot = _snapshot(events)
    store = FakeTraceStore(events, snapshot, snapshot)

    # When
    trace = RunTraceExporter(store, _catalog("checkout", "checkout-v1", ToolRegistry(()))).export(
        SAGA_ID
    )

    # Then
    unavailable = trace.events[-1]
    assert unavailable.event_type == "read_unavailable"
    assert unavailable.authority is TraceAuthority.EFFECT
    assert unavailable.rationale["reason_code"] == "read_unavailable"
    assert unavailable.redacted_output is None


def _read_unavailable_fields() -> dict[str, object]:
    return _read_event_base() | {
        "event_id": "evt_0000000000000005",
        "saga_seq": 5,
        "recorded_at": NOW + timedelta(seconds=4),
        "turn_id": "turn_0000000000000001",
        "proposal_id": "proposal_0000000000000001",
        "tool_name": "read_order",
        "reason_code": "read_unavailable",
    }


@pytest.mark.parametrize(
    "result",
    [
        {"correlation": "Bearer raw-correlation-secret"},
        {"correlation": "4242424242424242"},
        {"Correlation": "Bearer mixed-case-secret"},
        {"CORRELATION": "4242424242424242"},
    ],
)
def test_export_never_bypasses_redaction_through_correlation(
    result: dict[str, object],
) -> None:
    events = _read_events(result)
    snapshot = _snapshot(events)
    store = FakeTraceStore(events, snapshot, snapshot)

    trace = RunTraceExporter(store, _catalog("checkout", "checkout-v1", ToolRegistry(()))).export(
        SAGA_ID
    )

    assert trace.events[-1].correlation is None
    assert "raw-correlation-secret" not in trace.model_dump_json()
    assert "4242424242424242" not in trace.model_dump_json()


def test_export_keeps_only_validated_opaque_correlation() -> None:
    opaque = "opaque://agentic-saga/12345678/v1"
    events = _read_events({"correlation": opaque})
    snapshot = _snapshot(events)
    store = FakeTraceStore(events, snapshot, snapshot)

    trace = RunTraceExporter(store, _catalog("checkout", "checkout-v1", ToolRegistry(()))).export(
        SAGA_ID
    )

    assert trace.events[-1].correlation == opaque


def _snapshot(events: tuple[LedgerEvent, ...]) -> SagaSnapshot:
    state: SagaSnapshot | None = None
    for event in events:
        state = reduce_event(state, event)
    assert state is not None
    return state


def _fake_exporter(events: tuple[LedgerEvent, ...]) -> RunTraceExporter:
    snapshot = _snapshot(_started_events())
    store = FakeTraceStore(events, snapshot, snapshot)
    return RunTraceExporter(store, _catalog("checkout", "checkout-v1", ToolRegistry(())))


@pytest.mark.parametrize(
    "change",
    [
        lambda events: (events[1], events[0]),
        lambda events: (events[0], events[0]),
        lambda events: (events[0], events[1].model_copy(update={"saga_seq": 3})),
    ],
)
def test_export_rejects_reordered_duplicate_or_gapped_sequence(
    change: Callable[[tuple[LedgerEvent, ...]], tuple[LedgerEvent, ...]],
) -> None:
    with pytest.raises(TraceExportError, match="ordered ledger"):
        _fake_exporter(change(_started_events())).export(SAGA_ID)


def test_export_rejects_projection_mismatch() -> None:
    events = _started_events()
    rebuilt = _snapshot(events)
    stale = _snapshot(events[:1])
    exporter = RunTraceExporter(
        FakeTraceStore(events, stale, rebuilt),
        _catalog("checkout", "checkout-v1", ToolRegistry(())),
    )

    with pytest.raises(TraceExportError, match="projection"):
        exporter.export(SAGA_ID)


def test_export_rejects_missing_exact_definition() -> None:
    events = _started_events()
    snapshot = _snapshot(events)
    exporter = RunTraceExporter(FakeTraceStore(events, snapshot, snapshot), DefinitionCatalog())

    with pytest.raises(TraceExportError, match="definition"):
        exporter.export(SAGA_ID)


@pytest.mark.parametrize(
    "corruption",
    [
        {"schema_version": "2.0"},
        {"recorded_at": NOW.astimezone().replace(tzinfo=None)},
    ],
)
def test_export_rejects_unsupported_schema_or_corrupt_time(corruption: dict[str, object]) -> None:
    created, started = _started_events()
    broken = started.model_copy(update=corruption)
    snapshot = _snapshot((created, started))
    store = FakeTraceStore((created, broken), snapshot, snapshot)
    exporter = RunTraceExporter(store, _catalog("checkout", "checkout-v1", ToolRegistry(())))

    with pytest.raises(TraceExportError):
        exporter.export(SAGA_ID)


def test_export_rejects_nonmonotonic_utc_timestamps() -> None:
    created, started = _started_events()
    earlier = started.model_copy(update={"recorded_at": NOW - timedelta(seconds=1)})
    snapshot = _snapshot((created, started))
    store = FakeTraceStore((created, earlier), snapshot, snapshot)

    with pytest.raises(TraceExportError, match="timestamp"):
        RunTraceExporter(store, _catalog("checkout", "checkout-v1", ToolRegistry(()))).export(
            SAGA_ID
        )


def test_export_rejects_stale_invariant_proof(tmp_path: Path) -> None:
    runtime, store, lease = terminal_kernel(tmp_path / "saga.db", Evidence())
    runtime.submit_proposal(lease.saga_id, finish(), lease)
    events = store.read_events(lease.saga_id)
    stale = cast(InvariantEvaluated, events[-2]).model_copy(update={"evaluated_at_seq": 1})
    corrupted = (*events[:-2], stale, events[-1])
    snapshot = store.load_snapshot(lease.saga_id)
    fake = FakeTraceStore(corrupted, snapshot, snapshot)

    with pytest.raises(TraceExportError):
        RunTraceExporter(fake, _catalog("checkout", "checkout-v1", registry(Adapter()))).export(
            lease.saga_id
        )


def test_export_rejects_unsupported_event_type() -> None:
    created, started = _started_events()
    unknown = cast(LedgerEvent, object())
    snapshot = _snapshot((created, started))
    store = FakeTraceStore((created, unknown), snapshot, snapshot)

    with pytest.raises(TraceExportError, match="unsupported ledger event"):
        RunTraceExporter(store, _catalog("checkout", "checkout-v1", ToolRegistry(()))).export(
            SAGA_ID
        )


def test_export_rejects_overdeep_evidence() -> None:
    nested: object = "leaf"
    for _ in range(20):
        nested = {"public": nested}
    created = _unsafe_created().model_copy(update={"redacted_goal": {"nested": nested}})
    snapshot = _snapshot((created,))
    store = FakeTraceStore((created,), snapshot, snapshot)

    with pytest.raises(TraceExportError, match="bounds"):
        RunTraceExporter(store, _catalog("checkout", "checkout-v1", ToolRegistry(()))).export(
            SAGA_ID
        )
