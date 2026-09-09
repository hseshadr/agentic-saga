from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from agentic_saga.contracts.common import JsonObject
from agentic_saga.contracts.outcomes import (
    EffectConfirmed,
    EffectOutcome,
    NoEffectConfirmed,
    OutcomeUnknown,
    PartialEffectConfirmed,
    ReconcileConflict,
    ReconcileEffectConfirmed,
    ReconcileNoEffectConfirmed,
    ReconcilePending,
    ReconcileUnsupported,
    ReconciliationOutcome,
)


def test_should_require_correlation_when_outcome_is_unknown() -> None:
    # Given / When
    outcome = OutcomeUnknown(correlation="provider-request-7")

    # Then
    assert outcome.kind == "outcome_unknown"


def test_should_reject_empty_receipts_when_effect_is_partial() -> None:
    # Given / When / Then
    with pytest.raises(ValidationError):
        PartialEffectConfirmed(receipts=())


def test_should_keep_partial_receipts_when_effect_is_partial() -> None:
    # Given
    receipts: tuple[JsonObject, ...] = (
        {"reservation_id": "r1"},
        {"reservation_id": "r2"},
    )

    # When
    outcome = PartialEffectConfirmed(receipts=receipts)

    # Then
    assert outcome.receipts == receipts


def test_should_reject_partial_effect_when_receipt_count_exceeds_ingress_limit() -> None:
    # Given
    receipts = tuple({"receipt_id": str(index)} for index in range(257))

    # When / Then
    with pytest.raises(ValidationError):
        PartialEffectConfirmed(receipts=receipts)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_should_reject_non_finite_float_when_effect_receipt_is_validated(value: float) -> None:
    # Given
    payload = {"receipt": {"provider_amount": value}}

    # When / Then
    with pytest.raises(ValidationError):
        EffectConfirmed.model_validate(payload)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_should_reject_non_finite_float_when_partial_receipt_is_validated(value: float) -> None:
    # Given
    payload = {"receipts": ({"provider_amount": value},)}

    # When / Then
    with pytest.raises(ValidationError):
        PartialEffectConfirmed.model_validate(payload)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_should_reject_non_finite_float_when_reconcile_receipt_is_validated(value: float) -> None:
    # Given
    payload = {"receipt": {"provider_amount": value}}

    # When / Then
    with pytest.raises(ValidationError):
        ReconcileEffectConfirmed.model_validate(payload)


def test_should_distinguish_conflict_when_reconciliation_conflicts() -> None:
    # Given / When
    outcome = ReconcileConflict(reason="two provider records")

    # Then
    assert outcome.kind == "reconcile_conflict"


@pytest.mark.parametrize(
    ("payload", "expected_type"),
    [
        ({"kind": "effect_confirmed", "receipt": {"payment_id": "pay_1"}}, EffectConfirmed),
        ({"kind": "no_effect_confirmed", "reason": "not found"}, NoEffectConfirmed),
        (
            {"kind": "partial_effect_confirmed", "receipts": ({"item_id": "i1"},)},
            PartialEffectConfirmed,
        ),
        ({"kind": "outcome_unknown", "correlation": "request_1"}, OutcomeUnknown),
    ],
)
def test_should_select_variant_when_validating_effect_outcome(
    payload: dict[str, object], expected_type: type[BaseModel]
) -> None:
    # Given
    adapter: TypeAdapter[EffectOutcome] = TypeAdapter(EffectOutcome)

    # When
    outcome = adapter.validate_python(payload)

    # Then
    assert isinstance(outcome, expected_type)


@pytest.mark.parametrize(
    ("payload", "expected_type"),
    [
        (
            {"kind": "reconcile_effect_confirmed", "receipt": {"payment_id": "pay_1"}},
            ReconcileEffectConfirmed,
        ),
        (
            {"kind": "reconcile_no_effect_confirmed", "reason": "not found"},
            ReconcileNoEffectConfirmed,
        ),
        (
            {
                "kind": "reconcile_pending",
                "correlation": "request_1",
                "check_after": datetime(2026, 9, 6, tzinfo=UTC),
            },
            ReconcilePending,
        ),
        ({"kind": "reconcile_conflict", "reason": "conflict"}, ReconcileConflict),
        ({"kind": "reconcile_unsupported", "reason": "no lookup"}, ReconcileUnsupported),
    ],
)
def test_should_select_variant_when_validating_reconciliation_outcome(
    payload: dict[str, object], expected_type: type[BaseModel]
) -> None:
    # Given
    adapter: TypeAdapter[ReconciliationOutcome] = TypeAdapter(ReconciliationOutcome)

    # When
    outcome = adapter.validate_python(payload)

    # Then
    assert isinstance(outcome, expected_type)


def test_should_reject_unknown_kind_when_validating_effect_outcome() -> None:
    # Given
    payload = {"kind": "assumed_failure", "reason": "timeout"}

    # When / Then
    with pytest.raises(ValidationError):
        TypeAdapter(EffectOutcome).validate_python(payload)


def test_should_reject_extra_field_when_validating_effect_outcome() -> None:
    # Given
    payload = {"receipt": {"payment_id": "pay_1"}, "secret": "token"}

    # When / Then
    with pytest.raises(ValidationError):
        EffectConfirmed.model_validate(payload)


@pytest.mark.parametrize("reason", ["", "x" * 501])
def test_should_reject_reason_when_outside_bounds(reason: str) -> None:
    # Given / When / Then
    with pytest.raises(ValidationError):
        NoEffectConfirmed(reason=reason)


@pytest.mark.parametrize("correlation", ["", "x" * 501])
def test_should_reject_correlation_when_outside_bounds(correlation: str) -> None:
    # Given / When / Then
    with pytest.raises(ValidationError):
        OutcomeUnknown(correlation=correlation)


def test_should_reject_list_when_partial_receipts_require_tuple() -> None:
    # Given
    payload = {"receipts": [{"reservation_id": "r1"}]}

    # When / Then
    with pytest.raises(ValidationError):
        PartialEffectConfirmed.model_validate(payload)


def test_should_reject_naive_check_time_when_reconciliation_is_pending() -> None:
    # Given
    payload = {"correlation": "request_1", "check_after": datetime(2026, 9, 6)}

    # When / Then
    with pytest.raises(ValidationError):
        ReconcilePending.model_validate(payload)


def test_should_reject_non_utc_check_time_when_reconciliation_is_pending() -> None:
    # Given
    non_utc = timezone(timedelta(hours=1))
    payload = {"correlation": "request_1", "check_after": datetime(2026, 9, 6, tzinfo=non_utc)}

    # When / Then
    with pytest.raises(ValidationError):
        ReconcilePending.model_validate(payload)


def test_should_prevent_assignment_when_outcome_is_frozen() -> None:
    # Given
    outcome = OutcomeUnknown(correlation="request_1")

    # When / Then
    with pytest.raises(ValidationError):
        outcome.correlation = "request_2"
