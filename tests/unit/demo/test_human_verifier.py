from __future__ import annotations

import pytest

from agentic_saga.temporal.contracts import HumanResolutionVerificationRequest
from examples.ecommerce import demo


@pytest.mark.asyncio
async def test_demo_authorization_is_bound_to_one_human_resolution() -> None:
    # Given
    saga_id = "saga_1234567890abcdef"
    operation_id = f"op_{'a' * 64}"
    sequence = 7
    authorization = demo._demo_authorization(saga_id, operation_id, sequence)
    request = HumanResolutionVerificationRequest(
        saga_id=saga_id,
        operation_id=operation_id,
        based_on_event_seq=sequence,
        authorization_reference=authorization,
        receipt={"operator_confirmation": "refund_verified"},
    )

    # When
    accepted = await demo.verify_demo_human_resolution(request)
    replayed = await demo.verify_demo_human_resolution(
        request.model_copy(update={"operation_id": f"op_{'b' * 64}"})
    )

    # Then
    assert accepted.verified is True
    assert replayed.verified is False
