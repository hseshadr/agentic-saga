from __future__ import annotations

import asyncio
import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Literal, cast

import pytest
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentic_saga.contracts.common import JsonObject, Reversibility, sha256_json
from agentic_saga.contracts.events import (
    DispatchAbortedBeforeEntry,
    DispatchStarted,
    EffectOutcomeRecorded,
)
from agentic_saga.contracts.outcomes import (
    EffectConfirmed,
    EffectOutcome,
    OutcomeUnknown,
    PartialEffectConfirmed,
    ReconcileEffectConfirmed,
    ReconciliationOutcome,
    generated_outcome_correlation,
    safe_outcome_json,
)
from agentic_saga.contracts.tools import (
    EffectAdapter,
    EffectContext,
    EffectToolDefinition,
    ReadToolDefinition,
    ReconcileContext,
    ToolCapabilities,
    ToolRegistry,
)
from agentic_saga.execution.dispatcher import Dispatcher, DispatchResult
from agentic_saga.execution.leases import LeaseService
from agentic_saga.kernel.ports import (
    ClaimedCommand,
    InjectedStoreFailure,
    Lease,
    OutboxState,
    StoreConflict,
    StoreFailpoint,
    TransitionBatch,
)
from agentic_saga.kernel.reducer import reduce_event
from agentic_saga.kernel.runtime import StableIdFactory
from agentic_saga.kernel.state import OperationStatus
from agentic_saga.storage import SQLiteKernelStore
from tests.support.kernel_harness import (
    COMMAND_SCHEMA_VERSION,
    DEFINITION_VERSION,
    SAGA_ID,
    TOOL_NAME,
    HarnessCommand,
    KernelHarness,
)
from tests.support.reconciliation_adapter import (
    ProbeAdapter as DurableProbeAdapter,
)
from tests.support.reconciliation_adapter import (
    initialize_probe,
    probe_counts,
    probe_entered,
    release_probe,
)

_CONTEXT_FIELDS = frozenset(
    {
        "saga_id",
        "step_instance_id",
        "operation_id",
        "fence_token",
        "delivery_attempt",
        "forward_receipts",
    }
)


def _confirmed() -> EffectConfirmed:
    return EffectConfirmed(receipt={"provider_receipt": "opaque://provider/receipt00000001/v1"})


def _command_id(harness: KernelHarness) -> str:
    return StableIdFactory(b"task-8-harness").command_id(harness.operation_id)


def _operation_status(
    harness: KernelHarness, store: SQLiteKernelStore | None = None
) -> OperationStatus:
    source = harness.store if store is None else store
    return source.load_snapshot(harness.lease.saga_id).operations[harness.operation_id].status


def _assert_failure(result: DispatchResult, code: str) -> None:
    assert result.failure is not None
    assert result.failure.code == code


async def _cancelled(task: asyncio.Task[DispatchResult], detail: str | None = None) -> None:
    if detail is None:
        task.cancel()
    else:
        task.cancel(detail)
    with pytest.raises(asyncio.CancelledError):
        await task


def _assert_no_marker_or_digest(durable: bytes, marker: str) -> None:
    assert marker.encode() not in durable
    assert sha256(marker.encode()).hexdigest().encode() not in durable


def _durable_database_bytes(path: Path) -> bytes:
    files = sorted(path.parent.glob(f"{path.name}*"))
    return b"".join(candidate.read_bytes() for candidate in files if candidate.is_file())


def _capabilities() -> ToolCapabilities:
    return ToolCapabilities(
        idempotency_retention_seconds=86_400,
        reconciliation_supported=True,
        cancellation_supported=False,
        fencing_supported=True,
        reversibility=Reversibility.SEMANTIC,
        partial_effects_possible=True,
    )


class ProbeAdapter:
    def __init__(self, store: SQLiteKernelStore, outcome: EffectOutcome | None = None) -> None:
        self.store = store
        self.outcome = outcome or _confirmed()
        self.calls = 0
        self.commands: list[HarnessCommand] = []
        self.contexts: list[EffectContext] = []
        self.observed: list[OperationStatus] = []

    async def execute(self, command: HarnessCommand, context: EffectContext) -> EffectOutcome:
        self.calls += 1
        self.commands.append(command)
        self.contexts.append(context)
        operation = self.store.load_snapshot(context.saga_id).operations[context.operation_id]
        self.observed.append(operation.status)
        return self.outcome

    async def reconcile(
        self, command: HarnessCommand, context: ReconcileContext
    ) -> ReconciliationOutcome:
        del command, context
        return ReconcileEffectConfirmed(receipt={"provider_receipt": "probe"})


class BlockingAdapter:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def execute(self, command: HarnessCommand, context: EffectContext) -> EffectOutcome:
        del command, context
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return _confirmed()

    async def reconcile(
        self, command: HarnessCommand, context: ReconcileContext
    ) -> ReconciliationOutcome:
        del command, context
        return ReconcileEffectConfirmed(receipt={"provider_receipt": "blocking"})


class RaisingAdapter:
    def __init__(self, failure: Exception) -> None:
        self.failure = failure
        self.calls = 0

    async def execute(self, command: HarnessCommand, context: EffectContext) -> EffectOutcome:
        del command, context
        self.calls += 1
        raise self.failure

    async def reconcile(
        self, command: HarnessCommand, context: ReconcileContext
    ) -> ReconciliationOutcome:
        del command, context
        raise self.failure


class MalformedOutcomeAdapter:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, command: HarnessCommand, context: EffectContext) -> EffectOutcome:
        del command, context
        self.calls += 1
        return cast(EffectOutcome, {"kind": "effect_confirmed", "receipt": []})

    async def reconcile(
        self, command: HarnessCommand, context: ReconcileContext
    ) -> ReconciliationOutcome:
        del command, context
        return ReconcileEffectConfirmed(receipt={"provider_receipt": "malformed"})


class MappingOutcomeAdapter(MalformedOutcomeAdapter):
    async def execute(self, command: HarnessCommand, context: EffectContext) -> EffectOutcome:
        del command, context
        self.calls += 1
        raw = {"kind": "partial_effect_confirmed", "receipts": [{"receipt": "coercible"}]}
        return cast(EffectOutcome, raw)


class OversizedPartialOutcomeAdapter(MalformedOutcomeAdapter):
    async def execute(self, command: HarnessCommand, context: EffectContext) -> EffectOutcome:
        del command, context
        self.calls += 1
        receipts = tuple(
            {"provider_receipt": f"oversized-{index:03d}-{'x' * 300}"} for index in range(256)
        )
        return PartialEffectConfirmed(receipts=receipts)


class UnsafeUnknownAdapter(MalformedOutcomeAdapter):
    async def execute(self, command: HarnessCommand, context: EffectContext) -> EffectOutcome:
        del command, context
        self.calls += 1
        return OutcomeUnknown(correlation="bad")


class NeverReturnsAdapter(BlockingAdapter):
    async def execute(self, command: HarnessCommand, context: EffectContext) -> EffectOutcome:
        del command, context
        self.calls += 1
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class CancellationSwallowingAdapter:
    async def execute(self, command: HarnessCommand, context: EffectContext) -> EffectOutcome:
        del command, context
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return _confirmed()
        raise AssertionError("unreachable")

    async def reconcile(
        self, command: HarnessCommand, context: ReconcileContext
    ) -> ReconciliationOutcome:
        del command, context
        return ReconcileEffectConfirmed(receipt={"provider_receipt": "cancelled"})


class SimulatedCrash(BaseException):
    pass


class CrashAfterProviderAdapter:
    def __init__(self, harness: KernelHarness) -> None:
        self.harness = harness
        self.calls = 0

    async def execute(self, command: HarnessCommand, context: EffectContext) -> EffectOutcome:
        self.calls += 1
        await self.harness.provider.execute(command, context)
        raise SimulatedCrash

    async def reconcile(
        self, command: HarnessCommand, context: ReconcileContext
    ) -> ReconciliationOutcome:
        del command, context
        raise AssertionError("unreachable")


class AdvancingAdapter:
    def __init__(self, harness: KernelHarness, *, takeover: bool) -> None:
        self.harness = harness
        self.takeover = takeover
        self.calls = 0

    async def execute(self, command: HarnessCommand, context: EffectContext) -> EffectOutcome:
        del command, context
        self.calls += 1
        self.harness.clock.advance(timedelta(minutes=5))
        if self.takeover:
            LeaseService(self.harness.store).acquire(
                self.harness.lease.saga_id, "worker-b", timedelta(minutes=5)
            )
        return _confirmed()

    async def reconcile(
        self, command: HarnessCommand, context: ReconcileContext
    ) -> ReconciliationOutcome:
        del command, context
        return ReconcileEffectConfirmed(receipt={"provider_receipt": "advancing"})


class ReadResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    found: bool


class ReadAdapter:
    async def read(self, command: HarnessCommand) -> ReadResult:
        del command
        return ReadResult(found=True)


class InvalidCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    impossible_field: str


class RewritingCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    resource_id: str
    amount_minor: int = Field(strict=True, gt=0)
    currency: Literal["USD"]
    credential_ref: str

    @field_validator("amount_minor")
    @classmethod
    def rewrite_amount(cls, value: int) -> int:
        return value + 1


def _dispatcher(
    harness: KernelHarness,
    *,
    claim_duration: timedelta = timedelta(minutes=1),
    effect_timeout: timedelta = timedelta(seconds=30),
) -> Dispatcher:
    return Dispatcher(
        harness.store,
        harness.registry,
        clock=harness.clock,
        claim_duration=claim_duration,
        effect_timeout=effect_timeout,
    )


def _model_definition(
    capabilities: ToolCapabilities,
    model: type[BaseModel],
    adapter: EffectAdapter[BaseModel],
    *,
    definition_version: str = DEFINITION_VERSION,
    schema_version: str = COMMAND_SCHEMA_VERSION,
) -> EffectToolDefinition[BaseModel]:
    return EffectToolDefinition(
        name=TOOL_NAME,
        definition_version=definition_version,
        command_schema_version=schema_version,
        input_model=model,
        adapter=adapter,
        capabilities=capabilities,
        compensate_with=None,
    )


def _probe_adapter(
    probe: Path, mode: str, harness: KernelHarness | None = None
) -> DurableProbeAdapter:
    raw = {"mode": mode, "probe_path": str(probe)}
    if harness is not None:
        raw |= {"kernel_path": str(harness.store.path), "now": harness.clock.now().isoformat()}
    return DurableProbeAdapter(cast(JsonObject, raw))


def _probe_harness(directory: Path, mode: str) -> tuple[KernelHarness, Path]:
    probe = directory / "effect-probe.db"
    initialize_probe(probe)
    adapter = _probe_adapter(probe, mode)
    return KernelHarness.create_with_capabilities(directory, _capabilities(), adapter), probe


async def _wait_for_probe(probe: Path) -> None:
    while not probe_entered(probe):  # noqa: ASYNC110 - durable child-entry barrier
        await asyncio.sleep(0.01)


def test_proposal_records_registered_effect_versions(tmp_path: Path) -> None:
    # Given / When
    harness = KernelHarness.create(tmp_path)
    claimed = harness.store.claim_outbox("worker-a", timedelta(minutes=1))

    # Then
    assert claimed is not None
    assert claimed.definition_version == DEFINITION_VERSION
    assert claimed.command_schema_version == COMMAND_SCHEMA_VERSION
    assert claimed.envelope.capability_proof is not None
    definition = harness.registry.definition(TOOL_NAME)
    assert isinstance(definition, EffectToolDefinition)
    assert claimed.envelope.capability_proof.capabilities == definition.capabilities


@pytest.mark.parametrize(
    ("claim_duration", "effect_timeout"),
    [(timedelta(0), timedelta(seconds=1)), (timedelta(seconds=1), timedelta(0))],
)
def test_dispatcher_rejects_nonpositive_timing_configuration(
    tmp_path: Path, claim_duration: timedelta, effect_timeout: timedelta
) -> None:
    harness = KernelHarness.create(tmp_path)
    with pytest.raises(ValueError, match="must be positive"):
        Dispatcher(
            harness.store,
            harness.registry,
            clock=harness.clock,
            claim_duration=claim_duration,
            effect_timeout=effect_timeout,
        )


def _dispatch_ledger_fields(harness: KernelHarness) -> dict[str, object]:
    snapshot = harness.store.load_snapshot(harness.lease.saga_id)
    return {
        "event_id": "evt_0000000000008100",
        "saga_id": snapshot.saga_id,
        "saga_seq": snapshot.seq + 1,
        "definition_version": snapshot.definition_version,
        "fence_token": harness.lease.fence_token,
        "actor": "dispatcher",
        "trace_id": "trace_0000000000008100",
        "recorded_at": harness.clock.now(),
    }


def _dispatch_claim_fields(claimed: ClaimedCommand) -> dict[str, object]:
    return {
        "operation_id": claimed.operation_id,
        "step_instance_id": claimed.step_instance_id,
        "direction": claimed.direction,
        "semantic_generation": claimed.semantic_generation,
        "delivery_attempt": claimed.delivery_attempt,
        "tool_name": claimed.tool_name,
        "redacted_command": claimed.command,
        "command_hash": claimed.command_hash,
    }


type _DispatchLifecycleEvent = DispatchStarted | DispatchAbortedBeforeEntry | EffectOutcomeRecorded


def _claim_transition_batch(
    harness: KernelHarness, event: _DispatchLifecycleEvent, transition_id: str
) -> TransitionBatch:
    snapshot = harness.store.load_snapshot(harness.lease.saga_id)
    return TransitionBatch(
        transition_id=transition_id,
        saga_id=snapshot.saga_id,
        expected_seq=snapshot.seq,
        expected_fence_token=harness.lease.fence_token,
        lease_owner=harness.lease.owner,
        events=(event,),
        projection=reduce_event(snapshot, event),
    )


def _started_batch(harness: KernelHarness, claimed: ClaimedCommand) -> TransitionBatch:
    event = DispatchStarted.model_validate(
        _dispatch_ledger_fields(harness) | _dispatch_claim_fields(claimed)
    )
    return _claim_transition_batch(harness, event, "txn_0000000000008100")


def _confirmed_batch(harness: KernelHarness, claimed: ClaimedCommand) -> TransitionBatch:
    outcome = _confirmed()
    result = safe_outcome_json(outcome)
    fields = (
        _dispatch_ledger_fields(harness)
        | _dispatch_claim_fields(claimed)
        | {"event_id": "evt_0000000000008101"}
    )
    event = EffectOutcomeRecorded.model_validate(
        fields | {"outcome": outcome, "redacted_result": result, "result_hash": sha256_json(result)}
    )
    return _claim_transition_batch(harness, event, "txn_0000000000008101")


def _aborted_batch(harness: KernelHarness, claimed: ClaimedCommand) -> TransitionBatch:
    fields = (
        _dispatch_ledger_fields(harness)
        | _dispatch_claim_fields(claimed)
        | {"event_id": "evt_0000000000008101"}
    )
    event = DispatchAbortedBeforeEntry.model_validate(fields)
    return _claim_transition_batch(harness, event, "txn_0000000000008101")


def _rewrite_durable_claim_owner(
    harness: KernelHarness, claimed: ClaimedCommand, owner: str
) -> ClaimedCommand:
    with closing(sqlite3.connect(harness.store.path)) as connection, connection:
        connection.execute(
            "UPDATE outbox_commands SET claim_owner = ? WHERE command_id = ?",
            (owner, claimed.command_id),
        )
    metadata = claimed.claim.model_copy(update={"claim_owner": owner})
    return claimed.model_copy(update={"claim": metadata})


def _assert_changed_abort_rejected(
    harness: KernelHarness, claimed: ClaimedCommand, batch: TransitionBatch
) -> None:
    changed_event = batch.events[0].model_copy(update={"actor": "different-dispatcher"})
    changed_payload = batch.model_copy(update={"events": (changed_event,)})
    with pytest.raises(StoreConflict, match="different payload"):
        harness.store.abort_dispatch(claimed, changed_payload)
    changed_id = batch.model_copy(update={"transition_id": "txn_0000000000008102"})
    with pytest.raises(StoreConflict, match="claim"):
        harness.store.abort_dispatch(claimed, changed_id)


def test_committed_dispatch_start_exact_retry_returns_receipt_after_takeover(
    tmp_path: Path,
) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)
    claimed = harness.store.claim_outbox("worker-a", timedelta(minutes=1))
    assert claimed is not None
    batch = _started_batch(harness, claimed)
    harness.store.start_dispatch(claimed, batch)
    harness.clock.advance(timedelta(minutes=5))
    LeaseService(harness.store).acquire(harness.lease.saga_id, "worker-b", timedelta(minutes=5))

    # When / Then
    assert harness.store.start_dispatch(claimed, batch) == batch.projection


def test_store_rejects_claim_owner_different_from_saga_lease_owner(tmp_path: Path) -> None:
    harness = KernelHarness.create(tmp_path)
    claimed = harness.store.claim_outbox("worker-b", timedelta(minutes=1))
    assert claimed is not None
    batch = _started_batch(harness, claimed)
    before = harness.store.load_snapshot(harness.lease.saga_id)

    with pytest.raises(StoreConflict, match="claim owner"):
        harness.store.start_dispatch(claimed, batch)

    assert harness.store.load_snapshot(harness.lease.saga_id) == before
    assert harness.store.outbox_state(_command_id(harness)) is OutboxState.CLAIMED


def test_store_rejects_changed_claim_owner_during_finalization(tmp_path: Path) -> None:
    harness = KernelHarness.create(tmp_path)
    claimed = harness.store.claim_outbox("worker-a", timedelta(minutes=1))
    assert claimed is not None
    harness.store.start_dispatch(claimed, _started_batch(harness, claimed))
    changed = _rewrite_durable_claim_owner(harness, claimed, "worker-b")
    batch = _confirmed_batch(harness, changed)
    before = harness.store.load_snapshot(harness.lease.saga_id)

    with pytest.raises(StoreConflict, match="claim owner"):
        harness.store.complete_outbox(changed, batch)

    assert harness.store.load_snapshot(harness.lease.saga_id) == before
    assert harness.store.outbox_state(_command_id(harness)) is OutboxState.CLAIMED


def test_store_rejects_changed_claim_owner_during_dispatch_abort(tmp_path: Path) -> None:
    harness = KernelHarness.create(tmp_path)
    claimed = harness.store.claim_outbox("worker-a", timedelta(minutes=1))
    assert claimed is not None
    harness.store.start_dispatch(claimed, _started_batch(harness, claimed))
    changed = _rewrite_durable_claim_owner(harness, claimed, "worker-b")
    batch = _aborted_batch(harness, changed)
    before = harness.store.load_snapshot(harness.lease.saga_id)

    with pytest.raises(StoreConflict, match="claim owner"):
        harness.store.abort_dispatch(changed, batch)

    assert harness.store.load_snapshot(harness.lease.saga_id) == before
    assert harness.store.outbox_state(_command_id(harness)) is OutboxState.CLAIMED


def test_dispatch_authority_rejects_claim_owned_by_another_worker(tmp_path: Path) -> None:
    harness = KernelHarness.create(tmp_path)
    claimed = harness.store.claim_outbox("worker-a", timedelta(minutes=1))
    assert claimed is not None
    changed = _rewrite_durable_claim_owner(harness, claimed, "worker-b")
    lease = Lease.model_validate(harness.lease.model_dump())

    with pytest.raises(StoreConflict, match="claim owner"):
        harness.store.validate_dispatch_authority(changed, lease)


def test_dispatch_authority_uses_database_clock_without_injected_clock(tmp_path: Path) -> None:
    harness = KernelHarness.create(tmp_path)
    claimed = harness.store.claim_outbox("worker-a", timedelta(minutes=1))
    assert claimed is not None
    future = datetime(2999, 1, 1, tzinfo=UTC)
    with closing(sqlite3.connect(harness.store.path)) as connection, connection:
        connection.execute(
            "UPDATE sagas SET lease_expires_at = ? WHERE saga_id = ?",
            (future.isoformat(timespec="microseconds"), claimed.saga_id),
        )
        connection.execute(
            "UPDATE outbox_commands SET claim_expires_at = ? WHERE command_id = ?",
            (future.isoformat(timespec="microseconds"), claimed.command_id),
        )
    durable_claim = claimed.model_copy(
        update={"claim": claimed.claim.model_copy(update={"claim_expires_at": future})}
    )
    lease = Lease.model_validate(harness.lease.model_dump() | {"expires_at": future}, strict=True)
    store = SQLiteKernelStore.open(harness.store.path)

    store.validate_dispatch_authority(durable_claim, lease)


def test_committed_abort_exact_retry_survives_command_reclaim(tmp_path: Path) -> None:
    harness = KernelHarness.create(tmp_path)
    claimed = harness.store.claim_outbox("worker-a", timedelta(minutes=1))
    assert claimed is not None
    harness.store.start_dispatch(claimed, _started_batch(harness, claimed))
    batch = _aborted_batch(harness, claimed)
    harness.store.abort_dispatch(claimed, batch)
    reclaimed = harness.store.claim_outbox("worker-a", timedelta(minutes=1))
    assert reclaimed is not None and reclaimed.claim_id != claimed.claim_id
    before = harness.store.read_events(harness.lease.saga_id)

    assert harness.store.abort_dispatch(claimed, batch) == batch.projection
    assert harness.store.read_events(harness.lease.saga_id) == before
    assert harness.store.outbox_state(_command_id(harness)) is OutboxState.CLAIMED

    _assert_changed_abort_rejected(harness, claimed, batch)


@pytest.mark.asyncio
async def test_adapter_observes_durable_intent_before_effect(tmp_path: Path) -> None:
    harness = KernelHarness.create(tmp_path)
    result = await _dispatcher(harness).dispatch_one("worker-a")

    events = harness.store.read_events(harness.lease.saga_id)
    assert any(isinstance(event, DispatchStarted) for event in events)
    assert result.outcome is not None
    assert harness.provider.execute_call_count == 1
    assert harness.store.outbox_state(_command_id(harness)) is OutboxState.COMPLETED


@pytest.mark.asyncio
async def test_dispatcher_recursively_rehydrates_nested_canonical_command(tmp_path: Path) -> None:
    harness = KernelHarness.create(tmp_path)
    result = await _dispatcher(harness).dispatch_one("worker-a")

    assert result.failure is None
    assert harness.provider.execute_call_count == 1
    assert harness.provider.calls[0].operation_id == harness.operation_id


@pytest.mark.asyncio
async def test_adapter_context_contains_only_persisted_stable_identity(tmp_path: Path) -> None:
    harness = KernelHarness.create(tmp_path)
    await _dispatcher(harness).dispatch_one("worker-a")

    call = harness.provider.calls[0]
    assert call.operation_id == harness.operation_id
    assert call.fence_token == harness.lease.fence_token
    assert call.delivery_attempt == 1
    assert call.forward_receipts == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("authority", ["no_live_lease", "wrong_owner", "stale_fence"])
async def test_current_owner_and_fence_are_required_before_adapter_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, authority: str
) -> None:
    harness = KernelHarness.create(tmp_path)
    worker = _arrange_authority_case(harness, monkeypatch, authority)

    result = await _dispatcher(harness).dispatch_one(worker)

    assert harness.provider.execute_call_count == 0
    assert result.failure is not None
    assert result.failure.code == "authority_lost"
    expected = OutboxState.CLAIMED if authority == "stale_fence" else OutboxState.RUNNABLE
    assert harness.store.outbox_state(_command_id(harness)) is expected


def _arrange_authority_case(
    harness: KernelHarness, monkeypatch: pytest.MonkeyPatch, authority: str
) -> str:
    if authority == "no_live_lease":
        LeaseService(harness.store).release(harness.lease)
        return "worker-a"
    if authority == "wrong_owner":
        return "worker-b"
    _replace_lease_during_read(harness, monkeypatch)
    return "worker-a"


def _replace_lease_during_read(harness: KernelHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    original = harness.store.lease_state

    def takeover(saga_id: str) -> Lease | None:
        harness.clock.advance(timedelta(minutes=5))
        LeaseService(harness.store).acquire(saga_id, "worker-b", timedelta(minutes=5))
        return original(saga_id)

    monkeypatch.setattr(harness.store, "lease_state", takeover)


def _invalid_registry(case: str, harness: KernelHarness) -> ToolRegistry:
    if case == "unknown_tool":
        return ToolRegistry(())
    if case == "wrong_kind":
        return ToolRegistry(
            (ReadToolDefinition(TOOL_NAME, HarnessCommand, ReadResult, ReadAdapter()),)
        )
    return ToolRegistry((_invalid_definition(case, harness),))


def _invalid_definition(case: str, harness: KernelHarness) -> EffectToolDefinition[BaseModel]:
    registered = harness.registry.definition(TOOL_NAME)
    assert isinstance(registered, EffectToolDefinition)
    capabilities = registered.capabilities
    adapter = registered.adapter
    if case == "definition_version":
        return _model_definition(
            capabilities, HarnessCommand, adapter, definition_version="durable-action-v2"
        )
    if case == "schema_version":
        return _model_definition(
            capabilities, HarnessCommand, adapter, schema_version="action-command-v2"
        )
    if case == "capability_proof":
        changed = capabilities.model_copy(update={"cancellation_supported": True})
        return _model_definition(changed, HarnessCommand, adapter)
    model = InvalidCommand if case == "invalid_command" else RewritingCommand
    return _model_definition(capabilities, model, adapter)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "unknown_tool",
        "wrong_kind",
        "definition_version",
        "schema_version",
        "capability_proof",
        "invalid_command",
        "typed_byte_hash_mismatch",
    ],
)
async def test_invalid_effect_identity_or_command_parks_unknown_without_adapter_call(
    tmp_path: Path, case: str
) -> None:
    harness = KernelHarness.create(tmp_path)
    registry = _invalid_registry(case, harness)
    dispatcher = Dispatcher(harness.store, registry, clock=harness.clock)

    # When
    result = await dispatcher.dispatch_one("worker-a")

    assert harness.provider.execute_call_count == 0
    _assert_failure(result, "command_unavailable")
    assert _operation_status(harness) is OperationStatus.OUTCOME_UNKNOWN
    assert harness.store.outbox_state(_command_id(harness)) is OutboxState.PARKED


@pytest.mark.asyncio
async def test_missing_durable_capability_proof_fails_closed_before_entry(
    tmp_path: Path,
) -> None:
    harness = KernelHarness.create(tmp_path)
    with closing(sqlite3.connect(harness.store.path)) as connection, connection:
        connection.execute(
            "UPDATE outbox_commands SET capabilities_json = NULL, capability_digest = NULL"
        )

    result = await _dispatcher(harness).dispatch_one("worker-a")

    _assert_failure(result, "command_unavailable")
    assert harness.provider.execute_call_count == 0


@dataclass
class OneShotFailpoint:
    target: StoreFailpoint
    fired: bool = False

    def hit(self, point: StoreFailpoint) -> None:
        if point is self.target and not self.fired:
            self.fired = True
            raise InjectedStoreFailure(point)


@dataclass
class ArmableOneShotFailpoint:
    target: StoreFailpoint
    armed: bool = False
    fired: bool = False

    def hit(self, point: StoreFailpoint) -> None:
        if self.armed and point is self.target and not self.fired:
            self.fired = True
            raise InjectedStoreFailure(point)


@dataclass
class TakeoverAfterCommit:
    harness: KernelHarness
    fired: bool = False

    def hit(self, point: StoreFailpoint) -> None:
        if point is not StoreFailpoint.AFTER_COMMIT_BEFORE_RETURN or self.fired:
            return
        self.fired = True
        self.harness.clock.advance(timedelta(minutes=5))
        service = LeaseService(self.harness.store)
        service.acquire(self.harness.lease.saga_id, "worker-b", timedelta(minutes=5))
        raise InjectedStoreFailure(point)


@pytest.mark.asyncio
async def test_uncertain_dispatch_started_commit_retries_without_duplicate_or_double_entry(
    tmp_path: Path,
) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)
    failpoint = OneShotFailpoint(StoreFailpoint.AFTER_COMMIT_BEFORE_RETURN)
    store = SQLiteKernelStore.open(harness.store.path, clock=harness.clock, failpoint=failpoint)
    dispatcher = Dispatcher(store, harness.registry, clock=harness.clock)

    # When
    await dispatcher.dispatch_one("worker-a")

    # Then
    events = store.read_events(harness.lease.saga_id)
    assert sum(isinstance(event, DispatchStarted) for event in events) == 1
    assert sum(isinstance(event, EffectOutcomeRecorded) for event in events) == 1
    assert harness.provider.execute_call_count == 1


@pytest.mark.asyncio
async def test_takeover_during_uncertain_start_retry_prevents_adapter_entry(
    tmp_path: Path,
) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)
    failpoint = TakeoverAfterCommit(harness)
    store = SQLiteKernelStore.open(harness.store.path, clock=harness.clock, failpoint=failpoint)
    dispatcher = Dispatcher(store, harness.registry, clock=harness.clock)

    # When
    result = await dispatcher.dispatch_one("worker-a")

    # Then
    assert result.failure is not None
    assert result.failure.code == "authority_lost"
    assert harness.provider.execute_call_count == 0
    events = store.read_events(harness.lease.saga_id)
    assert sum(isinstance(event, DispatchStarted) for event in events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("takeover", [False, True])
async def test_authority_change_during_pre_entry_checkpoint_prevents_adapter_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, takeover: bool
) -> None:
    harness = KernelHarness.create(tmp_path)
    dispatched = asyncio.Event()
    _signal_after_call(monkeypatch, harness.store, "start_dispatch", dispatched)
    task = asyncio.create_task(_dispatcher(harness).dispatch_one("worker-a"))
    await dispatched.wait()
    _expire_or_take_over(harness, takeover)
    result = await task
    _assert_failure(result, "authority_lost")
    assert harness.provider.execute_call_count == 0
    assert _operation_status(harness) is OperationStatus.DISPATCHED


def _expire_or_take_over(harness: KernelHarness, takeover: bool) -> None:
    harness.clock.advance(timedelta(minutes=5))
    if takeover:
        LeaseService(harness.store).acquire(harness.lease.saga_id, "worker-b", timedelta(minutes=5))


def _signal_after_call(
    monkeypatch: pytest.MonkeyPatch,
    target: object,
    method_name: str,
    signal: asyncio.Event,
) -> None:
    original = cast(Callable[..., object], getattr(target, method_name))

    def signaled(*args: object, **kwargs: object) -> object:
        result = original(*args, **kwargs)
        signal.set()
        return result

    monkeypatch.setattr(target, method_name, signaled)


@pytest.mark.asyncio
async def test_cancellation_before_dispatch_releases_claim_without_ledger_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)
    claimed = asyncio.Event()
    _signal_after_call(monkeypatch, harness.store, "claim_outbox", claimed)
    before = harness.store.read_events(harness.lease.saga_id)

    # When
    task = asyncio.create_task(_dispatcher(harness).dispatch_one("worker-a"))
    await claimed.wait()
    await _cancelled(task)

    # Then
    assert harness.store.read_events(harness.lease.saga_id) == before
    assert harness.store.outbox_state(_command_id(harness)) is OutboxState.RUNNABLE
    assert harness.provider.execute_call_count == 0


@pytest.mark.asyncio
async def test_cancellation_after_dispatch_before_entry_aborts_and_requeues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)
    dispatched = asyncio.Event()
    _signal_after_call(monkeypatch, harness.store, "start_dispatch", dispatched)

    # When
    task = asyncio.create_task(_dispatcher(harness).dispatch_one("worker-a"))
    await dispatched.wait()
    await _cancelled(task)

    # Then
    events = harness.store.read_events(harness.lease.saga_id)
    assert isinstance(events[-1], DispatchAbortedBeforeEntry)
    assert _operation_status(harness) is OperationStatus.INTENT_DURABLE
    assert harness.store.outbox_state(_command_id(harness)) is OutboxState.RUNNABLE
    assert harness.provider.execute_call_count == 0


@pytest.mark.asyncio
async def test_pre_entry_abort_retries_uncertain_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = KernelHarness.create(tmp_path)
    failpoint = ArmableOneShotFailpoint(StoreFailpoint.BEFORE_COMMIT)
    store = SQLiteKernelStore.open(harness.store.path, clock=harness.clock, failpoint=failpoint)
    dispatched = asyncio.Event()
    _signal_after_call(monkeypatch, store, "start_dispatch", dispatched)
    dispatcher = Dispatcher(store, harness.registry, clock=harness.clock)
    task = asyncio.create_task(dispatcher.dispatch_one("worker-a"))

    await dispatched.wait()
    failpoint.armed = True
    await _cancelled(task)

    assert failpoint.fired
    assert _operation_status(harness, store) is OperationStatus.INTENT_DURABLE
    assert store.outbox_state(_command_id(harness)) is OutboxState.RUNNABLE


@pytest.mark.asyncio
async def test_pre_entry_cancellation_after_takeover_preserves_dispatch_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = KernelHarness.create(tmp_path)
    dispatched = asyncio.Event()
    _signal_after_call(monkeypatch, harness.store, "start_dispatch", dispatched)
    task = asyncio.create_task(_dispatcher(harness).dispatch_one("worker-a"))
    await dispatched.wait()
    harness.clock.advance(timedelta(minutes=5))
    LeaseService(harness.store).acquire(harness.lease.saga_id, "worker-b", timedelta(minutes=5))
    await _cancelled(task)
    assert _operation_status(harness) is OperationStatus.DISPATCHED
    assert harness.provider.execute_call_count == 0


@pytest.mark.asyncio
async def test_cancellation_after_entry_shields_private_unknown_before_propagating(
    tmp_path: Path,
) -> None:
    harness, probe = _probe_harness(tmp_path, "block")
    dispatcher = _dispatcher(harness)

    task = asyncio.create_task(dispatcher.dispatch_one("worker-a"))
    await _wait_for_probe(probe)
    await _cancelled(task, "private cancellation detail")

    durable = _durable_database_bytes(harness.store.path)
    _assert_no_marker_or_digest(durable, "private cancellation detail")
    assert _operation_status(harness) is OperationStatus.OUTCOME_UNKNOWN
    assert harness.store.outbox_state(_command_id(harness)) is OutboxState.PARKED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "point", [StoreFailpoint.BEFORE_COMMIT, StoreFailpoint.AFTER_COMMIT_BEFORE_RETURN]
)
async def test_post_entry_cancellation_survives_uncertain_outcome_commit(
    tmp_path: Path, point: StoreFailpoint
) -> None:
    probe = tmp_path / "effect-probe.db"
    initialize_probe(probe)
    adapter = _probe_adapter(probe, "block")
    harness = KernelHarness.create_with_capabilities(tmp_path, _capabilities(), adapter)
    failpoint = ArmableOneShotFailpoint(point)
    store = SQLiteKernelStore.open(harness.store.path, clock=harness.clock, failpoint=failpoint)
    dispatcher = Dispatcher(store, harness.registry, clock=harness.clock)
    task = asyncio.create_task(dispatcher.dispatch_one("worker-a"))

    await _wait_for_probe(probe)
    failpoint.armed = True
    await _cancelled(task, "private cancellation detail")

    assert failpoint.fired
    assert _operation_status(harness, store) is OperationStatus.OUTCOME_UNKNOWN
    assert store.outbox_state(_command_id(harness)) is OutboxState.PARKED


@pytest.mark.asyncio
async def test_adapter_exception_text_and_direct_digest_never_become_durable(
    tmp_path: Path,
) -> None:
    private_material = "provider exception contains reusable-secret"
    os.environ["AGENTIC_SAGA_PROVIDER_TEST_SECRET"] = private_material
    harness, _ = _probe_harness(tmp_path, "exception")

    result = await _dispatcher(harness).dispatch_one("worker-a")

    # Then
    durable = _durable_database_bytes(harness.store.path)
    _assert_no_marker_or_digest(durable, private_material)
    assert private_material not in result.model_dump_json()
    assert isinstance(result.outcome, OutcomeUnknown)
    expected = generated_outcome_correlation(SAGA_ID, harness.operation_id, 1, "adapter_exception")
    assert result.outcome.correlation == expected


@pytest.mark.asyncio
async def test_dispatch_timeout_parks_unknown_without_timeout_detail(tmp_path: Path) -> None:
    harness, probe = _probe_harness(tmp_path, "block")
    dispatcher = _dispatcher(harness, effect_timeout=timedelta(milliseconds=200))

    # When
    result = await dispatcher.dispatch_one("worker-a")

    assert probe_counts(probe)[0] == 1
    assert isinstance(result.outcome, OutcomeUnknown)
    expected = generated_outcome_correlation(SAGA_ID, harness.operation_id, 1, "adapter_timeout")
    assert result.outcome.correlation == expected
    assert harness.store.outbox_state(_command_id(harness)) is OutboxState.PARKED


@pytest.mark.asyncio
async def test_malformed_adapter_outcome_parks_safe_unknown(tmp_path: Path) -> None:
    os.environ["AGENTIC_SAGA_PROVIDER_TEST_SECRET"] = "malformed-private"  # noqa: S105
    harness, probe = _probe_harness(tmp_path, "malformed")

    result = await _dispatcher(harness).dispatch_one("worker-a")

    assert probe_counts(probe)[0] == 1
    _assert_failure(result, "outcome_unknown")
    assert isinstance(result.outcome, OutcomeUnknown)
    expected = generated_outcome_correlation(
        SAGA_ID, harness.operation_id, 1, "adapter_invalid_outcome"
    )
    assert result.outcome.correlation == expected
    assert _operation_status(harness) is OperationStatus.OUTCOME_UNKNOWN


@pytest.mark.asyncio
async def test_mapping_adapter_outcome_fails_strict_validation(tmp_path: Path) -> None:
    adapter = MappingOutcomeAdapter()
    harness = KernelHarness.create_with_capabilities(tmp_path, _capabilities(), adapter)

    result = await _dispatcher(harness).dispatch_one("worker-a")

    assert adapter.calls == 1
    assert isinstance(result.outcome, OutcomeUnknown)
    expected = generated_outcome_correlation(
        SAGA_ID, harness.operation_id, 1, "adapter_invalid_outcome"
    )
    assert result.outcome.correlation == expected


@pytest.mark.asyncio
async def test_oversized_normalized_outcome_parks_durable_safe_unknown(tmp_path: Path) -> None:
    # Given
    adapter = OversizedPartialOutcomeAdapter()
    harness = KernelHarness.create_with_capabilities(tmp_path, _capabilities(), adapter)

    # When
    result = await _dispatcher(harness).dispatch_one("worker-a")

    # Then
    _assert_failure(result, "outcome_unknown")
    assert isinstance(result.outcome, OutcomeUnknown)
    assert b"oversized-255" not in _durable_database_bytes(harness.store.path)
    assert _operation_status(harness) is OperationStatus.OUTCOME_UNKNOWN
    assert harness.store.outbox_state(_command_id(harness)) is OutboxState.PARKED


@pytest.mark.asyncio
async def test_adapter_unknown_uses_kernel_generated_fallback(tmp_path: Path) -> None:
    adapter = UnsafeUnknownAdapter()
    harness = KernelHarness.create_with_capabilities(tmp_path, _capabilities(), adapter)

    result = await _dispatcher(harness).dispatch_one("worker-a")

    _assert_failure(result, "outcome_unknown")
    assert isinstance(result.outcome, OutcomeUnknown)
    expected = generated_outcome_correlation(SAGA_ID, harness.operation_id, 1, "provider_unknown")
    assert result.outcome.correlation == expected


@pytest.mark.asyncio
async def test_raw_adapter_outcome_is_normalized_before_any_durable_byte(
    tmp_path: Path,
) -> None:
    marker = "confirmed-secret"
    os.environ["AGENTIC_SAGA_PROVIDER_TEST_SECRET"] = marker
    harness, _ = _probe_harness(tmp_path, "secret_receipt")

    result = await _dispatcher(harness).dispatch_one("worker-a")

    durable = _durable_database_bytes(harness.store.path)
    _assert_no_marker_or_digest(durable, marker)
    assert result.outcome is not None
    assert marker not in result.outcome.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_state"),
    [
        (_confirmed(), OutboxState.COMPLETED),
        (OutcomeUnknown(correlation="bad"), OutboxState.PARKED),
    ],
)
async def test_outcome_event_and_outbox_disposition_roll_back_together_at_failpoint(
    tmp_path: Path, outcome: EffectOutcome, expected_state: OutboxState
) -> None:
    harness = (
        KernelHarness.create(tmp_path)
        if expected_state is OutboxState.COMPLETED
        else _probe_harness(tmp_path, "unknown")[0]
    )
    failpoint = OneShotFailpoint(StoreFailpoint.AFTER_OUTBOX_UPDATE)
    store = SQLiteKernelStore.open(harness.store.path, clock=harness.clock, failpoint=failpoint)
    dispatcher = Dispatcher(store, harness.registry, clock=harness.clock)

    # When / Then
    with pytest.raises(InjectedStoreFailure):
        await dispatcher.dispatch_one("worker-a")
    assert store.outbox_state(_command_id(harness)) is OutboxState.CLAIMED
    assert (
        store.load_snapshot(harness.lease.saga_id).operations[harness.operation_id].status
        is OperationStatus.DISPATCHED
    )
    assert expected_state is not store.outbox_state(_command_id(harness))


@pytest.mark.asyncio
async def test_reclaimed_dispatched_command_parks_unknown_without_redelivery(
    tmp_path: Path,
) -> None:
    harness = KernelHarness.create(tmp_path)
    claimed = harness.store.claim_outbox("worker-a", timedelta(seconds=30))
    assert claimed is not None
    harness.store.start_dispatch(claimed, _started_batch(harness, claimed))
    context = EffectContext(
        saga_id=claimed.saga_id,
        step_instance_id=claimed.step_instance_id,
        operation_id=claimed.operation_id,
        fence_token=claimed.saga_fence_token,
        delivery_attempt=claimed.delivery_attempt,
    )
    await harness.provider.execute(harness.command, context)
    harness.clock.advance(timedelta(seconds=31))

    result = await _dispatcher(harness, claim_duration=timedelta(seconds=30)).dispatch_one(
        "worker-a"
    )

    _assert_failure(result, "prior_dispatch_unknown")
    assert harness.provider.execute_call_count == 1
    assert harness.provider.effect_count(harness.operation_id) == 1
    assert harness.store.outbox_state(_command_id(harness)) is OutboxState.PARKED


@pytest.mark.asyncio
@pytest.mark.parametrize("takeover", [False, True])
async def test_expiry_or_takeover_during_adapter_prevents_stale_known_finalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, takeover: bool
) -> None:
    harness = KernelHarness.create(tmp_path)
    _advance_after_effect(harness, monkeypatch, takeover)

    result = await _dispatcher(harness).dispatch_one("worker-a")

    _assert_failure(result, "authority_lost")
    assert harness.provider.execute_call_count == 1
    assert _operation_status(harness) is OperationStatus.DISPATCHED
    assert not any(
        isinstance(event, EffectOutcomeRecorded)
        for event in harness.store.read_events(harness.lease.saga_id)
    )


def _advance_after_effect(
    harness: KernelHarness, monkeypatch: pytest.MonkeyPatch, takeover: bool
) -> None:
    original = harness.provider.execute

    async def execute(command: BaseModel, context: EffectContext) -> EffectOutcome:
        outcome = await original(command, context)
        harness.clock.advance(timedelta(minutes=5))
        if takeover:
            LeaseService(harness.store).acquire(SAGA_ID, "worker-b", timedelta(minutes=5))
        return outcome

    monkeypatch.setattr(harness.provider, "execute", execute)


@pytest.mark.asyncio
async def test_two_concurrent_workers_enter_adapter_once(tmp_path: Path) -> None:
    harness, probe = _probe_harness(tmp_path, "block")
    first = _dispatcher(harness)
    second = _dispatcher(harness)

    results = await _concurrent_dispatch(first, second, probe)

    assert probe_counts(probe)[0] == 1
    assert sum(result.outcome is not None for result in results) == 1


@pytest.mark.asyncio
async def test_completed_queue_returns_no_work_without_adapter_entry(tmp_path: Path) -> None:
    harness = KernelHarness.create(tmp_path)
    dispatcher = _dispatcher(harness)
    await dispatcher.dispatch_one("worker-a")

    result = await dispatcher.dispatch_one("worker-a")

    _assert_failure(result, "no_work")
    assert result.operation_id is None
    assert harness.provider.execute_call_count == 1


async def _concurrent_dispatch(
    first: Dispatcher, second: Dispatcher, probe: Path
) -> tuple[DispatchResult, DispatchResult]:
    tasks = (
        asyncio.create_task(first.dispatch_one("worker-a")),
        asyncio.create_task(second.dispatch_one("worker-a")),
    )
    await _wait_for_probe(probe)
    release_probe(probe)
    return await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_abort_lifecycle_survives_open_backup_and_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = KernelHarness.create(tmp_path)
    dispatched = asyncio.Event()
    _signal_after_call(monkeypatch, harness.store, "start_dispatch", dispatched)
    task = asyncio.create_task(_dispatcher(harness).dispatch_one("worker-a"))
    await dispatched.wait()
    await _cancelled(task)
    backup = tmp_path / "backup.db"

    # When
    reopened = SQLiteKernelStore.open(harness.store.path, clock=harness.clock)
    reopened.backup_to(backup)
    restored = SQLiteKernelStore.restore_from(backup, tmp_path / "restored.db")

    # Then
    assert _operation_status(harness, restored) is OperationStatus.INTENT_DURABLE
    assert restored.outbox_state(_command_id(harness)) is OutboxState.RUNNABLE
