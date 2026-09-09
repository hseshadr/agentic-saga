from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from hashlib import sha256
from typing import Annotated, Final, Literal, cast

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from agentic_saga.contracts.common import JsonObject
from agentic_saga.contracts.redaction import RedactionPolicy, redact_json

type _BoundedText = Annotated[str, Field(min_length=1, max_length=500)]
SAFE_NO_EFFECT_REASON = "provider_confirmed_no_effect"
SAFE_RECONCILE_NO_EFFECT_REASON = "provider_confirmed_no_effect"
SAFE_RECONCILE_CONFLICT_REASON = "provider_evidence_conflict"
SAFE_RECONCILE_UNSUPPORTED_REASON = "provider_reconciliation_unsupported"
_OPAQUE_REFERENCE = re.compile(
    r"^opaque://[a-z][a-z0-9.-]{0,63}/[A-Za-z0-9_-]{8,128}/v[1-9][0-9]*$"
)
_JSON_OBJECT_ADAPTER: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
_MAX_PARTIAL_RECEIPTS: Final[int] = 256


class EffectConfirmed(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    kind: Literal["effect_confirmed"] = "effect_confirmed"
    receipt: JsonObject


class NoEffectConfirmed(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    kind: Literal["no_effect_confirmed"] = "no_effect_confirmed"
    reason: _BoundedText


class PartialEffectConfirmed(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    kind: Literal["partial_effect_confirmed"] = "partial_effect_confirmed"
    receipts: tuple[JsonObject, ...] = Field(min_length=1, max_length=_MAX_PARTIAL_RECEIPTS)


class OutcomeUnknown(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    kind: Literal["outcome_unknown"] = "outcome_unknown"
    correlation: _BoundedText


type EffectOutcome = Annotated[
    EffectConfirmed | NoEffectConfirmed | PartialEffectConfirmed | OutcomeUnknown,
    Field(discriminator="kind"),
]


def is_safe_outcome_correlation(value: str) -> bool:
    """Return whether a provider reference is opaque and safe to persist."""
    return _OPAQUE_REFERENCE.fullmatch(value) is not None


def generated_outcome_correlation(
    saga_id: str, operation_id: str, delivery_attempt: int, category: str
) -> str:
    """Build a deterministic opaque reference from stable public execution identity."""
    material = f"{saga_id}\0{operation_id}\0{delivery_attempt}\0{category}".encode()
    return f"opaque://agentic-saga/{sha256(material).hexdigest()}/v1"


def _safe_receipt(receipt: JsonObject, policy: RedactionPolicy) -> JsonObject:
    redacted = redact_json(receipt, policy)
    return _JSON_OBJECT_ADAPTER.validate_python(redacted)


def _normalize_confirmed(
    outcome: EffectOutcome, policy: RedactionPolicy, fallback: str
) -> EffectOutcome:
    del fallback
    confirmed = cast(EffectConfirmed, outcome)
    return EffectConfirmed(receipt=_safe_receipt(confirmed.receipt, policy))


def _normalize_partial(
    outcome: EffectOutcome, policy: RedactionPolicy, fallback: str
) -> EffectOutcome:
    del fallback
    partial = cast(PartialEffectConfirmed, outcome)
    receipts = tuple(_safe_receipt(receipt, policy) for receipt in partial.receipts)
    return PartialEffectConfirmed(receipts=receipts)


def _normalize_no_effect(
    outcome: EffectOutcome, policy: RedactionPolicy, fallback: str
) -> EffectOutcome:
    del outcome, policy, fallback
    return NoEffectConfirmed(reason=SAFE_NO_EFFECT_REASON)


def _normalize_unknown(
    outcome: EffectOutcome, policy: RedactionPolicy, fallback: str
) -> EffectOutcome:
    del policy
    correlation = cast(OutcomeUnknown, outcome).correlation
    return OutcomeUnknown(
        correlation=correlation if is_safe_outcome_correlation(correlation) else fallback
    )


type _OutcomeNormalizer = Callable[[EffectOutcome, RedactionPolicy, str], EffectOutcome]
_OUTCOME_NORMALIZERS: Mapping[str, _OutcomeNormalizer] = {
    "effect_confirmed": _normalize_confirmed,
    "no_effect_confirmed": _normalize_no_effect,
    "partial_effect_confirmed": _normalize_partial,
    "outcome_unknown": _normalize_unknown,
}


def normalize_effect_outcome(
    outcome: EffectOutcome,
    *,
    fallback_correlation: str,
    redaction_policy: RedactionPolicy | None = None,
) -> EffectOutcome:
    """Return the sole safe, canonical outcome form accepted by durable events."""
    policy = redaction_policy or RedactionPolicy()
    return _OUTCOME_NORMALIZERS[outcome.kind](outcome, policy, fallback_correlation)


def safe_outcome_json(outcome: EffectOutcome) -> JsonObject:
    """Return the canonical public JSON representation stored for an outcome."""
    return _JSON_OBJECT_ADAPTER.validate_python(outcome.model_dump(mode="json"))


def effect_outcome_is_safe(outcome: EffectOutcome) -> bool:
    fallback = "opaque://agentic-saga/0000000000000000/v1"
    return normalize_effect_outcome(outcome, fallback_correlation=fallback) == outcome


class ReconcileEffectConfirmed(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    kind: Literal["reconcile_effect_confirmed"] = "reconcile_effect_confirmed"
    receipt: JsonObject


class ReconcileNoEffectConfirmed(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    kind: Literal["reconcile_no_effect_confirmed"] = "reconcile_no_effect_confirmed"
    reason: _BoundedText


class ReconcilePending(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    kind: Literal["reconcile_pending"] = "reconcile_pending"
    correlation: _BoundedText
    check_after: AwareDatetime

    @field_validator("check_after")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("check_after must use UTC")
        return value


class ReconcileConflict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    kind: Literal["reconcile_conflict"] = "reconcile_conflict"
    reason: _BoundedText


class ReconcileUnsupported(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    kind: Literal["reconcile_unsupported"] = "reconcile_unsupported"
    reason: _BoundedText


type ReconciliationOutcome = Annotated[
    ReconcileEffectConfirmed
    | ReconcileNoEffectConfirmed
    | ReconcilePending
    | ReconcileConflict
    | ReconcileUnsupported,
    Field(discriminator="kind"),
]


def _normalize_reconcile_confirmed(
    outcome: ReconciliationOutcome, policy: RedactionPolicy, fallback: str
) -> ReconciliationOutcome:
    del fallback
    confirmed = cast(ReconcileEffectConfirmed, outcome)
    return ReconcileEffectConfirmed(receipt=_safe_receipt(confirmed.receipt, policy))


def _normalize_reconcile_absent(
    outcome: ReconciliationOutcome, policy: RedactionPolicy, fallback: str
) -> ReconciliationOutcome:
    del outcome, policy, fallback
    return ReconcileNoEffectConfirmed(reason=SAFE_RECONCILE_NO_EFFECT_REASON)


def _normalize_reconcile_pending(
    outcome: ReconciliationOutcome, policy: RedactionPolicy, fallback: str
) -> ReconciliationOutcome:
    del policy
    pending = cast(ReconcilePending, outcome)
    correlation = pending.correlation
    if not is_safe_outcome_correlation(correlation):
        correlation = fallback
    return ReconcilePending(correlation=correlation, check_after=pending.check_after)


def _normalize_reconcile_conflict(
    outcome: ReconciliationOutcome, policy: RedactionPolicy, fallback: str
) -> ReconciliationOutcome:
    del outcome, policy, fallback
    return ReconcileConflict(reason=SAFE_RECONCILE_CONFLICT_REASON)


def _normalize_reconcile_unsupported(
    outcome: ReconciliationOutcome, policy: RedactionPolicy, fallback: str
) -> ReconciliationOutcome:
    del outcome, policy, fallback
    return ReconcileUnsupported(reason=SAFE_RECONCILE_UNSUPPORTED_REASON)


type _ReconciliationNormalizer = Callable[
    [ReconciliationOutcome, RedactionPolicy, str], ReconciliationOutcome
]
_RECONCILIATION_NORMALIZERS: Mapping[str, _ReconciliationNormalizer] = {
    "reconcile_effect_confirmed": _normalize_reconcile_confirmed,
    "reconcile_no_effect_confirmed": _normalize_reconcile_absent,
    "reconcile_pending": _normalize_reconcile_pending,
    "reconcile_conflict": _normalize_reconcile_conflict,
    "reconcile_unsupported": _normalize_reconcile_unsupported,
}


def normalize_reconciliation_outcome(
    outcome: ReconciliationOutcome,
    *,
    fallback_correlation: str,
    redaction_policy: RedactionPolicy | None = None,
) -> ReconciliationOutcome:
    """Return the canonical public reconciliation evidence safe for persistence."""
    policy = redaction_policy or RedactionPolicy()
    return _RECONCILIATION_NORMALIZERS[outcome.kind](outcome, policy, fallback_correlation)


def safe_reconciliation_json(outcome: ReconciliationOutcome) -> JsonObject:
    """Return the canonical public JSON representation for reconciliation evidence."""
    return _JSON_OBJECT_ADAPTER.validate_python(outcome.model_dump(mode="json"))


def reconciliation_outcome_is_safe(outcome: ReconciliationOutcome) -> bool:
    fallback = "opaque://agentic-saga/0000000000000000/v1"
    return normalize_reconciliation_outcome(outcome, fallback_correlation=fallback) == outcome
