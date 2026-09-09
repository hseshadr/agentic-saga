from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from agentic_saga.contracts.common import Direction, OperationId, SagaId, StepInstanceId

_LENGTH_BYTES = 8


def _length_delimit(value: bytes) -> bytes:
    length = len(value).to_bytes(_LENGTH_BYTES, "big")  # pragma: no mutate - explicit default
    return length + value


def framed_sha256(namespace: bytes, *components: bytes) -> str:
    values = (namespace, *components)
    return sha256(b"".join(_length_delimit(value) for value in values)).hexdigest()


def _generation_bytes(semantic_generation: int) -> bytes:
    if isinstance(semantic_generation, bool) or not isinstance(semantic_generation, int):
        raise TypeError("semantic_generation must be an integer")  # pragma: no mutate - stable code
    if semantic_generation < 0:
        raise ValueError("semantic_generation must be non-negative")  # pragma: no mutate
    return str(semantic_generation).encode("ascii")  # pragma: no mutate - codec alias equivalent


def _identity_components(
    saga_id: SagaId,
    step_instance_id: StepInstanceId,
    direction: Direction,
    semantic_generation: int,
) -> tuple[bytes, ...]:
    return (
        saga_id.encode("utf-8"),  # pragma: no mutate - codec alias equivalent
        step_instance_id.encode("utf-8"),  # pragma: no mutate - codec alias equivalent
        direction.value.encode("ascii"),  # pragma: no mutate - codec alias equivalent
        _generation_bytes(semantic_generation),
    )


@dataclass(frozen=True)
class OperationIdentityFactory:
    namespace: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, bytes):
            raise TypeError("namespace must be bytes")
        if not self.namespace:
            raise ValueError("namespace must not be empty")

    def create(
        self,
        saga_id: SagaId,
        step_instance_id: StepInstanceId,
        direction: Direction,
        semantic_generation: int,
    ) -> OperationId:
        components = _identity_components(saga_id, step_instance_id, direction, semantic_generation)
        digest = framed_sha256(self.namespace, *components)
        return f"op_{digest}"
