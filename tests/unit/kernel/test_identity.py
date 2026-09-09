import inspect
import re

import pytest

from agentic_saga.contracts.common import Direction
from agentic_saga.kernel.identity import OperationIdentityFactory

SAGA_ID = "saga_0123456789abcdef"
OTHER_SAGA_ID = "saga_fedcba9876543210"
STEP_ID = "step_01234567"
OTHER_STEP_ID = "step_89abcdef"


def test_should_reproduce_stable_operation_identity_for_delivery_retry() -> None:
    # Given
    factory = OperationIdentityFactory(namespace=b"test-namespace")

    # When
    first = factory.create(SAGA_ID, STEP_ID, Direction.FORWARD, 0)
    retry = factory.create(SAGA_ID, STEP_ID, Direction.FORWARD, 0)

    # Then
    assert first == retry
    assert re.fullmatch(r"op_[a-f0-9]{64}", first)


def test_should_preserve_the_versioned_identity_vector() -> None:
    factory = OperationIdentityFactory(namespace=b"test-namespace")
    operation_id = factory.create(SAGA_ID, STEP_ID, Direction.FORWARD, 0)
    assert operation_id == "op_0665037d09501fba25d9b4c7015ce20c6a71ffc322765648a3daa12b13308026"


@pytest.mark.parametrize(
    ("factory", "saga_id", "step_id", "direction", "generation"),
    [
        (
            OperationIdentityFactory(namespace=b"other-namespace"),
            SAGA_ID,
            STEP_ID,
            Direction.FORWARD,
            0,
        ),
        (
            OperationIdentityFactory(namespace=b"test-namespace"),
            OTHER_SAGA_ID,
            STEP_ID,
            Direction.FORWARD,
            0,
        ),
        (
            OperationIdentityFactory(namespace=b"test-namespace"),
            SAGA_ID,
            OTHER_STEP_ID,
            Direction.FORWARD,
            0,
        ),
        (
            OperationIdentityFactory(namespace=b"test-namespace"),
            SAGA_ID,
            STEP_ID,
            Direction.COMPENSATION,
            0,
        ),
        (
            OperationIdentityFactory(namespace=b"test-namespace"),
            SAGA_ID,
            STEP_ID,
            Direction.FORWARD,
            1,
        ),
    ],
)
def test_should_change_identity_when_any_semantic_component_changes(
    factory: OperationIdentityFactory,
    saga_id: str,
    step_id: str,
    direction: Direction,
    generation: int,
) -> None:
    # Given
    baseline = OperationIdentityFactory(namespace=b"test-namespace").create(
        SAGA_ID, STEP_ID, Direction.FORWARD, 0
    )

    # When
    changed = factory.create(saga_id, step_id, direction, generation)

    # Then
    assert changed != baseline


def test_should_resist_ambiguous_component_concatenation() -> None:
    # Given
    first = OperationIdentityFactory(namespace=b"a")
    second = OperationIdentityFactory(namespace=b"ab")

    # When
    first_id = first.create("bc", "step", Direction.FORWARD, 0)
    second_id = second.create("c", "step", Direction.FORWARD, 0)

    # Then
    assert first_id != second_id


@pytest.mark.parametrize("generation", [-1, -100])
def test_should_reject_negative_semantic_generation(generation: int) -> None:
    # Given
    factory = OperationIdentityFactory(namespace=b"test-namespace")

    # When / Then
    with pytest.raises(ValueError, match="semantic_generation"):
        factory.create(SAGA_ID, STEP_ID, Direction.FORWARD, generation)


@pytest.mark.parametrize("generation", [True, "0"])
def test_should_reject_coercive_semantic_generation(generation: object) -> None:
    # Given
    factory = OperationIdentityFactory(namespace=b"test-namespace")

    # When / Then
    with pytest.raises(TypeError, match="semantic_generation"):
        factory.create(SAGA_ID, STEP_ID, Direction.FORWARD, generation)  # type: ignore[arg-type]


def test_should_reject_empty_namespace() -> None:
    # Given / When / Then
    with pytest.raises(ValueError, match="namespace"):
        OperationIdentityFactory(namespace=b"")


def test_should_reject_nonbytes_namespace() -> None:
    # Given / When / Then
    with pytest.raises(TypeError, match="namespace"):
        OperationIdentityFactory(namespace="test")  # type: ignore[arg-type]


def test_should_exclude_delivery_attempt_from_identity_interface() -> None:
    # Given / When
    parameters = inspect.signature(OperationIdentityFactory.create).parameters

    # Then
    assert tuple(parameters) == (
        "self",
        "saga_id",
        "step_instance_id",
        "direction",
        "semantic_generation",
    )
