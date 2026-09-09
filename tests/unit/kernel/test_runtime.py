import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from agentic_saga.contracts.actions import Finish, ToolCall
from agentic_saga.contracts.common import canonical_json, sha256_json
from agentic_saga.contracts.events import (
    ApprovalConsumed,
    LedgerEvent,
    ProposalRejected,
    SagaCreated,
    SagaStarted,
    TerminalDenied,
)
from agentic_saga.kernel.reducer import reduce_event

SAGA_ID = "saga_0000000000000001"


def saga_created() -> SagaCreated:
    return SagaCreated.model_validate(
        event_fields(1)
        | {
            "definition_name": "checkout",
            "definition_fingerprint": "f" * 64,
            "redacted_goal": {"order_id": "order-1"},
        }
    )


def saga_started() -> SagaStarted:
    return SagaStarted.model_validate(event_fields(2))


def test_should_match_existing_compact_canonical_encoding_byte_for_byte() -> None:
    value = {"z": "cafe\u0301", "a": [True, 1, None]}
    expected = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")

    assert canonical_json(value) == expected
    assert len(sha256_json(value)) == 64


@pytest.mark.parametrize("value", [float("nan"), ("not", "json"), {1: "bad-key"}])
def test_should_reject_non_strict_canonical_json(value: object) -> None:
    with pytest.raises((TypeError, ValueError, ValidationError)):
        canonical_json(value)


def test_should_require_caller_proposal_identity() -> None:
    with pytest.raises(ValidationError):
        ToolCall.model_validate(
            {
                "tool_name": "charge_payment",
                "arguments": {"amount_minor": 14900},
                "based_on_saga_seq": 2,
                "rationale": "Capture payment.",
            }
        )


def test_should_require_explicit_finish_target() -> None:
    finish = Finish(
        proposal_id="proposal_finish_01",
        based_on_saga_seq=2,
        rationale="All invariants appear satisfied.",
        target_status="succeeded_verified",
    )

    assert finish.target_status == "succeeded_verified"


def event_fields(seq: int) -> dict[str, object]:
    return {
        "event_id": f"evt_{seq:016d}",
        "saga_id": SAGA_ID,
        "saga_seq": seq,
        "definition_version": "checkout-v1",
        "fence_token": None,
        "actor": "kernel",
        "trace_id": "trace_0000000000000001",
        "recorded_at": datetime(2026, 9, 6, 12, tzinfo=UTC),
    }


def approval_consumed() -> ApprovalConsumed:
    return ApprovalConsumed.model_validate(
        event_fields(3)
        | {
            "decision_id": "decision_01",
            "proposal_hash": "a" * 64,
            "verification_result": True,
        }
    )


def test_should_replay_consumed_approval_without_authentication_material() -> None:
    created = saga_created()
    started = saga_started()
    snapshot = reduce_event(reduce_event(None, created), started)
    event = approval_consumed()

    after = reduce_event(snapshot, event)

    assert after.consumed_approval_ids == ("decision_01",)
    assert "auth_proof" not in event.model_dump_json()


@pytest.mark.parametrize(
    "event",
    [
        ProposalRejected.model_validate(
            event_fields(3)
            | {
                "proposal_id": "proposal_00000001",
                "proposal_hash": "a" * 64,
                "reason_code": "unknown_tool",
            }
        ),
        TerminalDenied.model_validate(
            event_fields(3)
            | {
                "proposal_id": "proposal_00000001",
                "proposal_hash": "a" * 64,
                "target_status": "succeeded_verified",
                "reason_code": "invariant_failed",
            }
        ),
    ],
)
def test_should_replay_denial_evidence_without_changing_material_state(
    event: LedgerEvent,
) -> None:
    before = reduce_event(reduce_event(None, saga_created()), saga_started())

    after = reduce_event(before, event)

    assert after.seq == 3
    assert after.status is before.status
    assert after.operations == before.operations
