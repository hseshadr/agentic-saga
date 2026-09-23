from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import TypeAdapter, ValidationError

from agentic_saga.contracts.runtime import SagaStatus
from agentic_saga.contracts.trace import RunTrace, TraceAuthority

_BUSINESS_FAILURE = Path("examples/ecommerce/flight-recorder/traces/business-failure.json")
_PAYLOAD_ADAPTER: TypeAdapter[dict[str, object]] = TypeAdapter(dict[str, object])
_OTHER_DIGEST = "0" * 64


def _payload() -> dict[str, object]:
    return _PAYLOAD_ADAPTER.validate_json(_BUSINESS_FAILURE.read_bytes(), strict=True)


def _events(payload: dict[str, object]) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], payload["events"])


def _proofs(payload: dict[str, object]) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], payload["proofs"])


def _validate(payload: dict[str, object]) -> RunTrace:
    return RunTrace.model_validate_json(json.dumps(payload), strict=True)


def _assert_invalid(payload: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _validate(payload)


def test_should_strictly_validate_real_business_failure_trace() -> None:
    trace = RunTrace.model_validate_json(_BUSINESS_FAILURE.read_bytes(), strict=True)

    assert trace.outcome is SagaStatus.COMPENSATED_VERIFIED
    assert trace.events[-1].after_status is trace.outcome
    assert [(proof.rule_id, proof.result) for proof in trace.proofs] == [
        ("verify_order", "invalid")
    ]
    completed = [event for event in trace.events if event.event_type == "compensation_completed"]
    assert len(completed) == 1
    assert completed[0].authority is TraceAuthority.WORKFLOW
    assert "all_passed" not in completed[0].rationale


def test_trace_authority_exactly_matches_current_temporal_emitters() -> None:
    assert {authority.value for authority in TraceAuthority} == {
        "agent",
        "workflow",
        "effect",
        "compensation",
        "proof",
        "human",
    }


def test_should_reject_retired_kernel_authority() -> None:
    payload = _payload()
    _events(payload)[0]["authority"] = "kernel"

    _assert_invalid(payload, "Input should be")


def test_should_reject_non_utc_event_time() -> None:
    payload = _payload()
    _events(payload)[1]["recorded_at"] = "2026-01-01T01:00:02+01:00"

    _assert_invalid(payload, "trace event time must use UTC")


def test_should_reject_non_utc_header_time() -> None:
    payload = _payload()
    payload["started_at"] = "2026-01-01T01:00:01+01:00"

    _assert_invalid(payload, "trace time must use UTC")


@pytest.mark.parametrize(
    ("value_field", "hash_field"),
    [("redacted_input", "input_hash"), ("redacted_output", "output_hash")],
)
def test_should_reject_hash_without_public_value(value_field: str, hash_field: str) -> None:
    payload = _payload()
    event = _events(payload)[0]
    assert event[value_field] is None
    event[hash_field] = _OTHER_DIGEST

    _assert_invalid(payload, f"{hash_field.removesuffix('_hash')} hash requires a redacted value")


@pytest.mark.parametrize(
    ("hash_field", "role"), [("input_hash", "input"), ("output_hash", "output")]
)
def test_should_reject_hash_that_does_not_bind_public_value(hash_field: str, role: str) -> None:
    payload = _payload()
    event = _events(payload)[2]
    assert event[hash_field] != _OTHER_DIGEST
    event[hash_field] = _OTHER_DIGEST

    _assert_invalid(payload, f"{role} hash must bind the canonical redacted value")


@pytest.mark.parametrize("hash_field", ["input_hash", "output_hash"])
def test_should_reject_malformed_public_value_hash(hash_field: str) -> None:
    payload = _payload()
    _events(payload)[2][hash_field] = "not-a-sha256-digest"

    _assert_invalid(payload, "String should match pattern")


def test_should_reject_non_contiguous_event_sequence() -> None:
    payload = _payload()
    _events(payload)[1]["saga_seq"] = 3

    _assert_invalid(payload, "trace events must be contiguous and ordered")


def test_should_reject_header_identity_that_differs_from_first_event() -> None:
    payload = _payload()
    payload["run_id"] = f"trace_{'0' * 16}"

    _assert_invalid(payload, "trace header does not match its first ledger event")


def test_should_reject_definition_drift_within_run() -> None:
    payload = _payload()
    _events(payload)[1]["definition_version"] = "ecommerce-temporal-v2"

    _assert_invalid(payload, "trace definition changed within one run")


def test_should_reject_outcome_that_differs_from_final_projection() -> None:
    payload = _payload()
    payload["outcome"] = "succeeded_verified"

    _assert_invalid(payload, "trace outcome does not match its final projection")


def test_should_reject_non_monotonic_event_time() -> None:
    payload = _payload()
    events = _events(payload)
    events[1]["recorded_at"], events[2]["recorded_at"] = (
        events[2]["recorded_at"],
        events[1]["recorded_at"],
    )

    _assert_invalid(payload, "trace event timestamps must be monotonic")


def test_should_reject_finish_time_without_matching_terminal_evidence() -> None:
    payload = _payload()
    payload["finished_at"] = None

    _assert_invalid(payload, "trace finish time does not match terminal evidence")


def test_should_reject_first_event_with_prior_status() -> None:
    payload = _payload()
    _events(payload)[0]["before_status"] = "running"

    _assert_invalid(payload, "first trace event must start without a prior status")


def test_should_reject_broken_status_chain() -> None:
    payload = _payload()
    _events(payload)[1]["before_status"] = "compensating"

    _assert_invalid(payload, "before and after statuses do not form one causal chain")


@pytest.mark.parametrize("source_mutation", ["missing_sequence", "wrong_identity"])
def test_should_reject_absent_proof_source(source_mutation: str) -> None:
    payload = _payload()
    proof = _proofs(payload)[0]
    if source_mutation == "missing_sequence":
        proof["source_event_seq"] = len(_events(payload)) + 1
    else:
        proof["source_event_id"] = f"evt_{'0' * 16}"

    _assert_invalid(payload, "trace proof source event is absent")


def test_should_reject_proof_source_without_invariant_evidence() -> None:
    payload = _payload()
    _events(payload)[8]["event_type"] = "effect_outcome_recorded"

    _assert_invalid(payload, "trace proof source is not invariant evidence")


def test_should_reject_proof_that_does_not_bind_prior_sequence() -> None:
    payload = _payload()
    _proofs(payload)[0]["evaluated_at_seq"] = 9

    _assert_invalid(payload, "trace proof does not bind the prior Saga sequence")
