from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterable
from itertools import chain
from pathlib import Path
from typing import Annotated, Final, Literal, Self, cast

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from agentic_saga.contracts.common import JsonPayloadError, canonical_json, require_bounded_json
from agentic_saga.contracts.redaction import RedactionPolicy, redact_json
from agentic_saga.contracts.runtime import ExecutionBudget, ToolDescriptor
from agentic_saga.contracts.tools import ToolRegistry, UnknownToolError

type _Name = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_-]{0,99}$")]
type _Version = Annotated[
    str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
]
type _Text = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4_000)]
type _Digest = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-f0-9]{64}$")]


def _tuple_from_yaml(value: object) -> object:
    if type(value) is list:
        return tuple(cast(list[object], value))
    return value


type _TextItems = Annotated[
    tuple[_Text, ...], BeforeValidator(_tuple_from_yaml), Field(min_length=1, max_length=50)
]
type _Names = Annotated[tuple[_Name, ...], BeforeValidator(_tuple_from_yaml), Field(max_length=100)]

_MAX_SOURCE_BYTES: Final[int] = 65_536
_MAX_DOCUMENT_DEPTH: Final[int] = 16
_MAX_DOCUMENT_NODES: Final[int] = 4_096
_SCALAR_TYPES: Final[frozenset[type[object]]] = frozenset({type(None), str, int, float, bool})
_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"\b(?:password|passwd|secret|api[_ -]?key|access[_ -]?token|client[_ -]?secret)"
        r"\s*[:=]\s*[^\s,;]+",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:sk_(?:live|test)_[A-Za-z0-9]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|"
        r"AKIA[0-9A-Z]{16})\b"
    ),
)


class ManifestInputError(ValueError):
    """Sanitized manifest rejection that never retains author-controlled input."""


class _ManifestModel(BaseModel):
    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )


def _require_unique_sorted(values: tuple[str, ...], role: str) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{role} must contain unique names")
    return tuple(sorted(values))


class _Autonomy(_ManifestModel):
    mode: Literal["supervised", "guarded", "autonomous"]
    instructions: _TextItems


class _Budgets(_ManifestModel):
    turn_limit: int = Field(strict=True, ge=1, le=100)
    tool_call_limit: int = Field(strict=True, ge=1, le=100)
    elapsed_ms_limit: int = Field(strict=True, ge=1, le=86_400_000)
    token_limit: int = Field(strict=True, ge=1, le=100_000_000)

    @model_validator(mode="after")
    def require_planning_capacity(self) -> Self:
        if self.elapsed_ms_limit < self.turn_limit:
            raise ValueError("elapsed_ms_limit must permit every configured turn")
        if self.token_limit < self.turn_limit:
            raise ValueError("token_limit must permit every configured turn")
        return self


class _Tools(_ManifestModel):
    catalog_sha256: _Digest
    allowed: Annotated[
        tuple[_Name, ...], BeforeValidator(_tuple_from_yaml), Field(min_length=1, max_length=100)
    ]

    @field_validator("allowed")
    @classmethod
    def sort_unique_tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _require_unique_sorted(value, "allowed tools")


class _Checks(_ManifestModel):
    policy: _Names = ()
    success: Annotated[
        tuple[_Name, ...], BeforeValidator(_tuple_from_yaml), Field(min_length=1, max_length=100)
    ]
    compensation: Annotated[
        tuple[_Name, ...], BeforeValidator(_tuple_from_yaml), Field(min_length=1, max_length=100)
    ]
    clean_abort: Annotated[
        tuple[_Name, ...], BeforeValidator(_tuple_from_yaml), Field(min_length=1, max_length=100)
    ]

    @field_validator("policy", "success", "compensation", "clean_abort")
    @classmethod
    def sort_unique_checks(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _require_unique_sorted(value, "checks")

    def invariant_names(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.success + self.compensation + self.clean_abort)))


class _Escalation(_ManifestModel):
    conditions: _TextItems
    instructions: _TextItems


class _ExamplePath(_ManifestModel):
    name: _Text
    kind: Literal["happy_path", "compensation_path"]
    narrative: _TextItems


class SagaManifest(_ManifestModel):
    """Domain-neutral author intent and deterministic Saga configuration."""

    schema_version: Literal["1.0"]
    name: _Name
    version: _Version
    objective: _Text
    instructions: _TextItems
    success_criteria: _TextItems
    autonomy: _Autonomy
    budgets: _Budgets
    tools: _Tools
    checks: _Checks
    escalation: _Escalation
    example_paths: Annotated[
        tuple[_ExamplePath, ...], BeforeValidator(_tuple_from_yaml), Field(max_length=20)
    ] = ()

    @field_validator("example_paths")
    @classmethod
    def sort_unique_examples(cls, value: tuple[_ExamplePath, ...]) -> tuple[_ExamplePath, ...]:
        names = tuple(item.name for item in value)
        _require_unique_sorted(names, "example paths")
        return tuple(sorted(value, key=lambda item: item.name))

    @model_validator(mode="after")
    def require_public_content(self) -> Self:
        values = _manifest_strings(self)
        if _redacts(values) or _contains_high_confidence_secret(values):
            raise ValueError("manifest contains private material")
        return self

    @classmethod
    def tool_catalog_sha256(cls, registry: ToolRegistry, names: Iterable[str]) -> str:
        """Return the digest to pin after registering the manifest's selected tools."""
        del cls
        selected = _require_unique_sorted(tuple(names), "allowed tools")
        return _catalog_digest(_descriptors_for(selected, registry))


class SagaContext(_ManifestModel):
    """Validated manifest plus authoritative context for an agent/runtime assembly."""

    manifest: SagaManifest
    agent_context: str
    tool_descriptors: tuple[ToolDescriptor, ...]
    budget: ExecutionBudget


def _manifest_strings(manifest: SagaManifest) -> list[str]:
    examples = [text for item in manifest.example_paths for text in (item.name, *item.narrative)]
    return [
        manifest.name,
        manifest.version,
        manifest.objective,
        *manifest.instructions,
        *manifest.success_criteria,
        *manifest.autonomy.instructions,
        *manifest.tools.allowed,
        *manifest.checks.policy,
        *manifest.checks.invariant_names(),
        *manifest.escalation.conditions,
        *manifest.escalation.instructions,
        *examples,
    ]


def _redacts(values: list[str]) -> bool:
    raw = {"content": values}
    return redact_json(raw, RedactionPolicy()) != raw


def _contains_high_confidence_secret(values: list[str]) -> bool:
    return any(pattern.search(value) for value in values for pattern in _SECRET_PATTERNS)


def _safe_yaml() -> YAML:
    parser = YAML(typ="safe", pure=True)
    parser.version = (1, 2)
    parser.allow_duplicate_keys = False
    parser.max_depth = _MAX_DOCUMENT_DEPTH
    return parser


def _parse_yaml(source: bytes) -> object:
    if len(source) > _MAX_SOURCE_BYTES:
        raise ManifestInputError("manifest exceeds 65536 bytes")
    documents = _load_yaml_documents(source)
    if len(documents) != 1:
        raise ManifestInputError("manifest must contain exactly one YAML document")
    _require_bounded_graph(documents[0])
    _require_json_bounds(documents[0])
    return documents[0]


def _load_yaml_documents(source: bytes) -> list[object]:
    try:
        documents: list[object] = list(_safe_yaml().load_all(source.decode("utf-8")))
    except (YAMLError, RecursionError, ValueError):
        failure = ManifestInputError("manifest is not safe valid YAML")
    else:
        return documents
    raise failure


def _read_source(path: Path) -> bytes:
    with path.open("rb") as stream:
        return stream.read(_MAX_SOURCE_BYTES + 1)


def _require_json_bounds(value: object) -> None:
    try:
        require_bounded_json(value)
    except (JsonPayloadError, OverflowError, TypeError, ValueError):
        failure = ManifestInputError("manifest structure exceeds safety limits")
    else:
        return
    raise failure


def _require_bounded_graph(root: object) -> None:
    pending = [(root, 0)]
    seen: set[int] = set()
    nodes = 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        _require_node_bounds(value, depth, nodes, seen)
        pending.extend(_children(value, depth))


def _require_node_bounds(value: object, depth: int, nodes: int, seen: set[int]) -> None:
    _require_graph_limits(depth, nodes)
    if type(value) in _SCALAR_TYPES:
        return
    _require_new_container(value, seen)


def _require_graph_limits(depth: int, nodes: int) -> None:
    if depth > _MAX_DOCUMENT_DEPTH or nodes > _MAX_DOCUMENT_NODES:
        raise ManifestInputError("manifest structure exceeds safety limits")


def _require_new_container(value: object, seen: set[int]) -> None:
    if type(value) not in {dict, list} or id(value) in seen:
        raise ManifestInputError("manifest contains an unsupported or aliased value")
    seen.add(id(value))


def _children(value: object, depth: int) -> list[tuple[object, int]]:
    return [(item, depth + 1) for item in _container_items(value)]


def _container_items(value: object) -> Iterable[object]:
    if type(value) is dict:
        mapping = cast(dict[object, object], value)
        return chain.from_iterable(mapping.items())
    if type(value) is list:
        return cast(list[object], value)
    return ()


def _descriptors(manifest: SagaManifest, registry: ToolRegistry) -> tuple[ToolDescriptor, ...]:
    return _descriptors_for(manifest.tools.allowed, registry)


def _descriptors_for(names: tuple[str, ...], registry: ToolRegistry) -> tuple[ToolDescriptor, ...]:
    values = _lookup_descriptors(names, registry)
    descriptors = tuple(sorted(values, key=lambda item: item.name))
    _require_public_descriptors(descriptors)
    return descriptors


def _lookup_descriptors(
    names: tuple[str, ...], registry: ToolRegistry
) -> tuple[ToolDescriptor, ...]:
    try:
        return tuple(ToolDescriptor.from_definition(registry.definition(name)) for name in names)
    except UnknownToolError:
        failure = ManifestInputError("manifest references an unknown tool")
    raise failure


def _require_public_descriptors(descriptors: tuple[ToolDescriptor, ...]) -> None:
    payload = {"tools": [item.model_dump(mode="json") for item in descriptors]}
    try:
        require_public_agent_payload(payload)
    except (JsonPayloadError, ValueError):
        failure = ManifestInputError("tool descriptor contains private material")
    else:
        return
    raise failure


def require_public_agent_payload(value: object) -> None:
    """Reject secret-like content before it crosses the model boundary."""
    encoded = _canonical_json(value)
    was_redacted = redact_json(value, RedactionPolicy()) != value
    if was_redacted or _contains_high_confidence_secret([encoded]):
        raise ValueError("agent payload contains private material")


def _canonical_json(value: object) -> str:
    return canonical_json(value).decode("utf-8")


def _catalog_digest(descriptors: tuple[ToolDescriptor, ...]) -> str:
    payload = {"tools": [item.model_dump(mode="json") for item in descriptors]}
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def _require_catalog_digest(
    manifest: SagaManifest, descriptors: tuple[ToolDescriptor, ...]
) -> None:
    actual = _catalog_digest(descriptors)
    if not hmac.compare_digest(manifest.tools.catalog_sha256, actual):
        raise ValueError("manifest tool catalog digest does not match registered descriptors")


def _require_registered(required: tuple[str, ...], available: Iterable[str], role: str) -> None:
    registered = frozenset(available)
    missing = tuple(sorted(set(required) - registered))
    if missing:
        raise ManifestInputError(f"manifest references an unknown {role}")


def _agent_context(manifest: SagaManifest, descriptors: tuple[ToolDescriptor, ...]) -> str:
    payload = {
        "manifest": manifest.model_dump(mode="json"),
        "tools": [item.model_dump(mode="json") for item in descriptors],
    }
    return _canonical_json(payload)


def _resolve(
    manifest: SagaManifest,
    registry: ToolRegistry,
    policy_checks: Iterable[str],
    invariant_checks: Iterable[str],
) -> SagaContext:
    descriptors = _descriptors(manifest, registry)
    _require_catalog_digest(manifest, descriptors)
    _require_registered(manifest.checks.policy, policy_checks, "policy check")
    _require_registered(manifest.checks.invariant_names(), invariant_checks, "invariant check")
    budget = ExecutionBudget.model_validate(manifest.budgets.model_dump())
    return SagaContext(
        manifest=manifest,
        agent_context=_agent_context(manifest, descriptors),
        tool_descriptors=descriptors,
        budget=budget,
    )


def load_saga_context(
    path: Path,
    *,
    registry: ToolRegistry,
    policy_checks: Iterable[str] = (),
    invariant_checks: Iterable[str] = (),
) -> SagaContext:
    """Load, validate, bind, and canonically render one Saga Context Manifest."""
    manifest = _validate_manifest(_parse_yaml(_read_source(path)))
    return _resolve(manifest, registry, policy_checks, invariant_checks)


def _validate_manifest(value: object) -> SagaManifest:
    try:
        return SagaManifest.model_validate(value)
    except ValidationError as error:
        failure = ManifestInputError(_manifest_validation_message(error))
    raise failure


def _manifest_validation_message(error: ValidationError) -> str:
    if "private material" in str(error):
        return "manifest contains private material"
    return "manifest does not match the required schema"


__all__ = ["ManifestInputError", "SagaContext", "SagaManifest", "load_saga_context"]
