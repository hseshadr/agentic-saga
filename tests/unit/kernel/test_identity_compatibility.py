from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from agentic_saga.contracts.common import Direction, sha256_json
from agentic_saga.contracts.events import SagaCreated
from agentic_saga.execution import dispatcher, reconciliation
from agentic_saga.execution import runtime as execution_runtime
from agentic_saga.kernel import identity
from agentic_saga.kernel.identity import OperationIdentityFactory
from agentic_saga.kernel.ports import (
    ClaimedCommand,
    ClaimMetadata,
    OutboxCommand,
    ReconciliationJob,
    ReconciliationJobState,
)
from agentic_saga.kernel.runtime import StableIdFactory
from agentic_saga.storage import sqlite

_NOW = datetime(2026, 9, 8, 12, 34, 56, tzinfo=UTC)
_SAGA_ID = "saga_0123456789abcdef"
_STEP_ID = "step_01234567"
_OPERATION_FACTORY = OperationIdentityFactory(b"compatibility/v0.1")
_OPERATION_ID = _OPERATION_FACTORY.create(_SAGA_ID, _STEP_ID, Direction.FORWARD, 7)
_KERNEL_IDS = StableIdFactory(b"compatibility/v0.1")
_TRANSITION_ID = _KERNEL_IDS.transition_id(_SAGA_ID, "proposal_01234567")
_COMMAND = {"amount_minor": 14900}
_ENVELOPE = OutboxCommand(
    command_id=_KERNEL_IDS.command_id(_OPERATION_ID),
    saga_id=_SAGA_ID,
    operation_id=_OPERATION_ID,
    tool_name="charge_payment",
    definition_version="charge-v1",
    command_schema_version="charge-command-v1",
    step_instance_id=_STEP_ID,
    direction=Direction.FORWARD,
    semantic_generation=7,
    command=_COMMAND,
    command_hash=sha256_json(_COMMAND),
    available_at=_NOW,
)
_CLAIM = ClaimMetadata(
    claim_id="claim_0123456789abcdef0123456789abcdef",
    claim_owner="worker-a",
    claim_expires_at=_NOW + timedelta(minutes=1),
    claim_generation=3,
    delivery_attempt=2,
    saga_fence_token=11,
)
_CLAIMED = ClaimedCommand(envelope=_ENVELOPE, claim=_CLAIM)
_RECONCILIATION_JOB = ReconciliationJob(
    job_id=f"recon_{'1' * 64}",
    command_id=_ENVELOPE.command_id,
    saga_id=_SAGA_ID,
    operation_id=_OPERATION_ID,
    state=ReconciliationJobState.CLAIMED,
    due_at=_NOW,
    first_dispatch_at=_NOW - timedelta(seconds=30),
    claim_id=_CLAIM.claim_id,
    claim_owner=_CLAIM.claim_owner,
    claim_expires_at=_CLAIM.claim_expires_at,
    claim_generation=_CLAIM.claim_generation,
    claim_fence_token=_CLAIM.saga_fence_token,
    claimed_at=_NOW,
    lookup_attempt=2,
)
_LEDGER_EVENT = SagaCreated(
    event_id="evt_0000000000000001",
    saga_id=_SAGA_ID,
    saga_seq=1,
    definition_version="checkout-v1",
    fence_token=None,
    actor="kernel",
    trace_id="trace_0000000000000001",
    recorded_at=_NOW,
    definition_name="checkout",
    definition_fingerprint="f" * 64,
    redacted_goal={"order_id": "order-1"},
)


@dataclass(frozen=True)
class FactoryCase:
    name: str
    build: Callable[[], str]
    expected_v01_value: str


GOLDEN_FACTORY_CASES = (
    FactoryCase(
        "operation",
        lambda: _OPERATION_FACTORY.create(_SAGA_ID, _STEP_ID, Direction.FORWARD, 7),
        "op_b34592baae71e0c5b5eb3bbc3d0e0feb8fa8a103766e7138b9b316903ad56629",
    ),
    FactoryCase(
        "kernel-transition",
        lambda: _KERNEL_IDS.transition_id(_SAGA_ID, "proposal_01234567"),
        "txn_30372a56e9cc8348ff67dbe36f97e604aeb1af3f8d3f65d6d85a98467a4990ae",
    ),
    FactoryCase(
        "kernel-event",
        lambda: _KERNEL_IDS.event_id(_TRANSITION_ID, 4),
        "evt_fa36a9fc5a55a20019b951488cda754096684e1ac1ac3f6ca3a3766d4d44765d",
    ),
    FactoryCase(
        "kernel-command",
        lambda: _KERNEL_IDS.command_id(_OPERATION_ID),
        "cmd_ba1ef5acec538d58f3aa2dc8785a31ba145a1086ae5e6bc9789e8c44d1141647",
    ),
    FactoryCase(
        "kernel-trace",
        lambda: _KERNEL_IDS.trace_id(_TRANSITION_ID),
        "trace_46b0ad5b5182ca739cf0226ee7e86ba9da4baa7b15ddcf078d6583242c40184e",
    ),
    FactoryCase(
        "dispatcher-claim-transition",
        lambda: dispatcher._stable_id("txn", "dispatch-start", _CLAIMED),
        "txn_a9e8a7346632d6e56b7e0a3a136898f5e1c2bb01fa8193b13052b34f2261e8bc",
    ),
    FactoryCase(
        "dispatcher-event",
        lambda: dispatcher._stable_id("evt", "dispatch-start", _CLAIMED),
        "evt_a9e8a7346632d6e56b7e0a3a136898f5e1c2bb01fa8193b13052b34f2261e8bc",
    ),
    FactoryCase(
        "dispatcher-trace",
        lambda: dispatcher._stable_id("trace", "trace", _CLAIMED),
        "trace_1c6152eda858d4b0e254fb6a2def57af315f18b5235cb9a48b2de6b2dd28567c",
    ),
    FactoryCase(
        "reconciliation-transition",
        lambda: reconciliation._stable_id("txn", "decision", _RECONCILIATION_JOB),
        "txn_464696689c104e7d61c9d7369dc20b7b609cae762b58be6fb424febf82d1195b",
    ),
    FactoryCase(
        "reconciliation-event",
        lambda: reconciliation._stable_id("evt", "outcome", _RECONCILIATION_JOB),
        "evt_b9cef1d5afe24d6a7f075b5522e136498ce12166e4806181cfe6409e778323ed",
    ),
    FactoryCase(
        "reconciliation-trace",
        lambda: reconciliation._stable_id("trace", "trace", _RECONCILIATION_JOB),
        "trace_c8b72f7d3794a71107a9d4662c808da7c294c451d7572756168ddc2dd444245c",
    ),
    FactoryCase(
        "runtime-saga",
        lambda: execution_runtime._stable_id("saga", "goal_01234567"),
        "saga_30cf9dde337938465546e6bb9b14a33c696729a444bff9c854bc274092331a96",
    ),
    FactoryCase(
        "runtime-transition",
        lambda: execution_runtime._stable_id("txn", _SAGA_ID, "12", "reserve"),
        "txn_52a5055cd0aa89f23c5beec8cddcc715228d3da507bdfdd92c7e706dfd56e40e",
    ),
    FactoryCase(
        "recovery-job",
        lambda: sqlite._reconciliation_job_id(_OPERATION_ID),
        "recon_7c71764e60b83d2f903e49350ef3da7e965b8cd61123a9783ec2b7839bba410d",
    ),
    FactoryCase(
        "ledger-event-digest",
        lambda: sqlite._event_digest(_LEDGER_EVENT, None, sqlite._event_bytes(_LEDGER_EVENT)),
        "c92f732a739f0bf2aaa42872b768ab8dab26aa00d217c5154bb1761c4cf6ebee",
    ),
)


@pytest.mark.parametrize("factory_case", GOLDEN_FACTORY_CASES, ids=lambda case: case.name)
def test_stable_identifiers_remain_byte_compatible(factory_case: FactoryCase) -> None:
    # Given / When
    actual = factory_case.build()

    # Then
    assert actual == factory_case.expected_v01_value


def test_framed_sha256_domain_separates_length_delimited_components() -> None:
    # Given
    components = (_SAGA_ID.encode(), b"proposal_01234567")

    # When
    actual = identity.framed_sha256(b"compatibility/v0.1", *components)

    # Then
    assert actual == "30372a56e9cc8348ff67dbe36f97e604aeb1af3f8d3f65d6d85a98467a4990ae"
