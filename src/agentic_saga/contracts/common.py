from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator, Mapping
from enum import StrEnum
from hashlib import sha256
from math import isfinite
from types import MappingProxyType
from typing import Annotated, Final, cast

from pydantic import Field, GetCoreSchemaHandler, InstanceOf, StringConstraints
from pydantic_core import CoreSchema, core_schema

type _JsonString = Annotated[str, StringConstraints(strict=True)]
type _JsonInteger = Annotated[int, Field(strict=True)]
type _JsonFloat = Annotated[InstanceOf[float], Field(allow_inf_nan=False)]
type _JsonBoolean = Annotated[bool, Field(strict=True)]
type JsonScalar = _JsonString | _JsonInteger | _JsonFloat | _JsonBoolean | None

_MAX_JSON_DEPTH: Final[int] = 16
_MAX_JSON_NODES: Final[int] = 4_096
_MAX_JSON_CONTAINER_ITEMS: Final[int] = 256
_MAX_JSON_STRING_BYTES: Final[int] = 16_384
_MAX_JSON_ENCODED_BYTES: Final[int] = 65_536
_JSON_NODE_TYPES: Final[frozenset[type[object]]] = frozenset(
    {type(None), str, int, float, bool, list, dict}
)


class JsonLimit(StrEnum):
    """Stable, input-free reason a JSON value exceeded its ingress budget."""

    DEPTH = "depth"
    NODES = "nodes"
    CONTAINER_ITEMS = "container_items"
    STRING_BYTES = "string_bytes"
    ENCODED_BYTES = "encoded_bytes"


class JsonPayloadError(RuntimeError):
    """Sanitized rejection that Pydantic cannot wrap with the hostile input."""

    def __init__(self, limit: JsonLimit) -> None:
        self.limit = limit
        super().__init__(f"JSON payload exceeds the {limit.value} safety limit")


class _ImmutableJsonObject(Mapping[str, object]):
    """Recursively immutable, strictly validated JSON object."""

    __slots__ = ("_values",)
    _values: Mapping[str, object]

    def __init__(self, value: object) -> None:
        object.__setattr__(self, "_values", MappingProxyType(_freeze_members(value)))

    def __getitem__(self, key: str) -> object:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise TypeError("JSON object is immutable")

    def __delattr__(self, name: str) -> None:
        del name
        raise TypeError("JSON object is immutable")

    def __hash__(self) -> int:
        return hash(tuple(self.items()))

    def __repr__(self) -> str:
        return repr(dict(self.items()))


type _FrozenJsonValue = JsonScalar | tuple[_FrozenJsonValue, ...] | _ImmutableJsonObject
type SerializableJson = JsonScalar | list[SerializableJson] | dict[str, SerializableJson]


def _freeze_json(value: object) -> _FrozenJsonValue:
    if type(value) is _ImmutableJsonObject:
        return value
    require_bounded_json(value)
    return _freeze_validated(value)


def _freeze_validated(value: object) -> _FrozenJsonValue:
    if type(value) is _ImmutableJsonObject:
        return value
    freezer = _JSON_FREEZERS.get(type(value))
    if freezer is None:
        raise ValueError("value is not strict JSON")
    return freezer(value)


def _freeze_scalar(value: object) -> _FrozenJsonValue:
    if type(value) is float:
        return _finite_float(value)
    return cast(JsonScalar, value)


def _freeze_array(value: object) -> _FrozenJsonValue:
    return tuple(_freeze_validated(item) for item in cast(list[object], value))


def _freeze_mapping(value: object) -> _FrozenJsonValue:
    return _ImmutableJsonObject(value)


type _JsonFreezer = Callable[[object], _FrozenJsonValue]
_JSON_FREEZERS: Mapping[type[object], _JsonFreezer] = {
    type(None): _freeze_scalar,
    str: _freeze_scalar,
    int: _freeze_scalar,
    float: _freeze_scalar,
    bool: _freeze_scalar,
    list: _freeze_array,
    dict: _freeze_mapping,
}


def _finite_float(value: float) -> float:
    if not isfinite(value):
        raise ValueError("JSON numbers must be finite")
    return value


def _freeze_members(value: object) -> dict[str, _FrozenJsonValue]:
    if type(value) is not dict:
        raise ValueError("JSON objects require an exact dict")
    raw = cast(dict[object, object], value)
    if any(type(key) is not str for key in raw):
        raise ValueError("JSON object keys must be strings")
    return {cast(str, key): _freeze_validated(item) for key, item in sorted(raw.items())}


def require_bounded_json(value: object) -> None:
    """Bound JSON to depth 16, 4096 nodes, 256 items, 16 KiB strings, and 64 KiB."""
    if type(value) is _ImmutableJsonObject:
        return
    pending = [(value, 0)]
    nodes = 0
    encoded_bytes = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        _require_node_limits(current, depth, nodes)
        _require_json_node(current)
        encoded_bytes += _encoded_node_bytes(current)
        _require_encoded_bytes(encoded_bytes)
        pending.extend((item, depth + 1) for item in _json_children(current))


def _require_node_limits(value: object, depth: int, nodes: int) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise JsonPayloadError(JsonLimit.DEPTH)
    if nodes > _MAX_JSON_NODES:
        raise JsonPayloadError(JsonLimit.NODES)
    if type(value) is str:
        _require_string_bytes(value)


def _require_string_bytes(value: str) -> None:
    if len(value) > _MAX_JSON_STRING_BYTES:
        raise JsonPayloadError(JsonLimit.STRING_BYTES)
    if len(value.encode("utf-8")) > _MAX_JSON_STRING_BYTES:
        raise JsonPayloadError(JsonLimit.STRING_BYTES)


def _require_json_node(value: object) -> None:
    if type(value) is not _ImmutableJsonObject and type(value) not in _JSON_NODE_TYPES:
        raise ValueError("value is not strict JSON")


def _json_children(value: object) -> Iterable[object]:
    if type(value) is _ImmutableJsonObject:
        return ()
    if type(value) is list:
        items = cast(list[object], value)
        _require_container_width(items)
        return items
    if type(value) is dict:
        mapping = cast(dict[object, object], value)
        _require_container_width(mapping)
        _require_string_keys(mapping)
        return (*mapping.keys(), *mapping.values())
    return ()


def _require_container_width(value: list[object] | dict[object, object]) -> None:
    if len(value) > _MAX_JSON_CONTAINER_ITEMS:
        raise JsonPayloadError(JsonLimit.CONTAINER_ITEMS)


def _require_string_keys(value: dict[object, object]) -> None:
    if any(type(key) is not str for key in value):
        raise ValueError("JSON object keys must be strings")


def _require_encoded_bytes(value: int) -> None:
    if value > _MAX_JSON_ENCODED_BYTES:
        raise JsonPayloadError(JsonLimit.ENCODED_BYTES)


def _encoded_node_bytes(value: object) -> int:
    if type(value) is _ImmutableJsonObject:
        return len(_encode_json(thaw_json(value)))
    if type(value) is list:
        length = len(cast(list[object], value))
        return 2 + max(0, length - 1)
    if type(value) is dict:
        length = len(cast(dict[object, object], value))
        return 2 + length + max(0, length - 1)
    return len(_encode_json(value))


def _encode_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def thaw_json(value: JsonValue) -> SerializableJson:
    if type(value) is _ImmutableJsonObject:
        mapping = cast(Mapping[str, _FrozenJsonValue], value)
        return {key: thaw_json(item) for key, item in mapping.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return cast(SerializableJson, value)


def thaw_json_object(value: JsonObject) -> dict[str, SerializableJson]:
    """Return a detached JSON object with exact mutable dict/list containers."""
    return {key: thaw_json(item) for key, item in value.items()}


def _validate_json_object(value: object) -> _ImmutableJsonObject:
    if type(value) is _ImmutableJsonObject:
        return value
    require_bounded_json(value)
    return _ImmutableJsonObject(value)


def _json_object_input_schema() -> CoreSchema:
    return core_schema.dict_schema(
        core_schema.str_schema(strict=True), core_schema.any_schema(), strict=True
    )


class _JsonObjectMarker:
    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: object, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        del cls, source_type, handler
        serializer = core_schema.plain_serializer_function_ser_schema(thaw_json)
        return core_schema.no_info_plain_validator_function(
            _validate_json_object,
            json_schema_input_schema=_json_object_input_schema(),
            serialization=serializer,
        )


class _JsonValueMarker:
    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: object, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        del cls, source_type, handler
        serializer = core_schema.plain_serializer_function_ser_schema(thaw_json)
        return core_schema.no_info_plain_validator_function(
            _freeze_json,
            json_schema_input_schema=core_schema.any_schema(),
            serialization=serializer,
        )


type JsonObject = Annotated[Mapping[str, _FrozenJsonValue], _JsonObjectMarker]
type JsonValue = Annotated[_FrozenJsonValue, _JsonValueMarker]


def canonical_json(value: object) -> bytes:
    """Encode strict JSON using the kernel's byte-stable representation."""
    validated = _freeze_json(value)
    return _encode_json(thaw_json(validated))


def sha256_json(value: object) -> str:
    """Hash the exact canonical JSON bytes used by durable contracts."""
    return sha256(canonical_json(value)).hexdigest()


type SagaId = Annotated[str, StringConstraints(strict=True, pattern=r"^saga_[a-z0-9]{16,64}$")]
type StepInstanceId = Annotated[
    str, StringConstraints(strict=True, pattern=r"^step_[a-z0-9]{8,64}$")
]
type OperationId = Annotated[str, StringConstraints(strict=True, pattern=r"^op_[a-f0-9]{64}$")]
type FenceToken = Annotated[int, Field(strict=True, ge=1)]


class Direction(StrEnum):
    FORWARD = "forward"
    COMPENSATION = "compensation"


class Reversibility(StrEnum):
    EXACT = "exact"
    SEMANTIC = "semantic"
    IRREVERSIBLE = "irreversible"
