from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from agentic_saga import load_saga_context
from agentic_saga.contracts.clock import FakeClock
from agentic_saga.contracts.common import Reversibility
from agentic_saga.contracts.outcomes import EffectConfirmed, ReconcileEffectConfirmed
from agentic_saga.contracts.runtime import ToolDescriptor
from agentic_saga.contracts.tools import (
    EffectContext,
    EffectToolDefinition,
    ReadToolDefinition,
    ReconcileContext,
    ToolCapabilities,
    ToolRegistry,
)
from examples.ecommerce.demo import build_registry
from examples.ecommerce.domain import ScenarioName
from examples.ecommerce.provider import EcommerceProvider

ROOT = Path(__file__).parents[3]


class ExampleCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    public_reference: str


class ExampleResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    status: str


class ExampleAdapter:
    async def read(self, command: ExampleCommand) -> ExampleResult:
        return ExampleResult(status=command.public_reference)


class ExampleEffectAdapter:
    async def execute(self, command: ExampleCommand, context: EffectContext) -> EffectConfirmed:
        del command, context
        return EffectConfirmed(receipt={})

    async def reconcile(
        self, command: ExampleCommand, context: ReconcileContext
    ) -> ReconcileEffectConfirmed:
        del command, context
        return ReconcileEffectConfirmed(receipt={})


def _capabilities() -> ToolCapabilities:
    return ToolCapabilities(
        idempotency_retention_seconds=86_400,
        reconciliation_supported=True,
        cancellation_supported=True,
        fencing_supported=True,
        reversibility=Reversibility.SEMANTIC,
        partial_effects_possible=False,
    )


def _read(name: str) -> ReadToolDefinition[ExampleCommand, ExampleResult]:
    return ReadToolDefinition(name, ExampleCommand, ExampleResult, ExampleAdapter())


def _effect(name: str, compensate_with: str | None) -> EffectToolDefinition[ExampleCommand]:
    return EffectToolDefinition(
        name,
        "example-v1",
        "command-v1",
        ExampleCommand,
        ExampleEffectAdapter(),
        _capabilities(),
        compensate_with,
    )


def _tickets_registry() -> ToolRegistry:
    return ToolRegistry(
        (
            _read("search_itineraries"),
            _effect("hold_itinerary", "release_hold"),
            _effect("release_hold", None),
            _effect("charge_payment", "refund_payment"),
            _effect("refund_payment", None),
            _effect("issue_ticket", "void_ticket"),
            _effect("void_ticket", None),
        )
    )


def _digest(registry: ToolRegistry) -> str:
    descriptors = [
        ToolDescriptor.from_definition(item).model_dump(mode="json")
        for item in registry.definitions()
    ]
    payload = json.dumps({"tools": descriptors}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def test_should_load_ecommerce_example_against_registered_capabilities(
    tmp_path: Path,
) -> None:
    # Given
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    provider = EcommerceProvider.initialize(
        tmp_path / "provider.db", clock, ScenarioName.HAPPY_PATH
    )
    registry = build_registry(provider)

    # When
    context = load_saga_context(
        ROOT / "examples/ecommerce/saga.yaml",
        registry=registry,
        policy_checks=("amount_within_limit", "customer_authorized"),
        invariant_checks=(
            "inventory_released",
            "inventory_reserved",
            "no_external_effects",
            "order_cancelled",
            "order_fulfilled",
            "payment_captured",
            "payment_refunded",
        ),
    )

    # Then
    assert context.manifest.tools.catalog_sha256 == _digest(registry)
    assert context.manifest.name == "ecommerce_order"
    assert len(context.tool_descriptors) == 8


def test_should_load_ticket_example_with_same_domain_neutral_schema() -> None:
    # Given
    registry = _tickets_registry()

    # When
    context = load_saga_context(
        ROOT / "examples/ticket-booking/saga.yaml",
        registry=registry,
        policy_checks=("fare_within_limit", "traveler_authorized"),
        invariant_checks=(
            "hold_released",
            "no_active_booking",
            "payment_captured",
            "payment_refunded",
            "ticket_issued",
        ),
    )

    # Then
    assert context.manifest.tools.catalog_sha256 == _digest(registry)
    assert context.manifest.name == "ticket_booking"
    assert len(context.tool_descriptors) == 7
