from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from hashlib import sha256
from inspect import getsource
from textwrap import dedent
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, TypeAdapter, ValidationError

from agentic_saga.contracts.common import JsonObject, sha256_json
from agentic_saga.contracts.redaction import RedactionPolicy, contains_sensitive_json
from agentic_saga.contracts.runtime import ExecutionBudget, TerminalRequirement
from agentic_saga.contracts.tools import EffectToolDefinition, ReadToolDefinition, ToolRegistry
from agentic_saga.kernel.policy import PolicyEngine

_MAX_IDENTIFIER_LENGTH = 200
_JSON_OBJECT: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)


@runtime_checkable
class _DefinitionIdentityProvider(Protocol):
    def definition_identity(self) -> JsonObject: ...


@dataclass(frozen=True)
class SagaDefinition:
    """Pin a Saga's executable safety contract across starts and resumes.

    Applications must bump ``version``, tool/policy behavior versions, or the relevant
    ``invariant_version`` whenever transitive host behavior changes.
    """

    name: str
    version: str
    registry: ToolRegistry
    policy: PolicyEngine
    success_invariants: TerminalRequirement
    compensation_invariants: TerminalRequirement
    clean_abort_invariants: TerminalRequirement
    exception_invariants: TerminalRequirement
    budget: ExecutionBudget
    redaction_policy: RedactionPolicy = field(default_factory=RedactionPolicy)
    _fingerprint: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        _require_identifier(self.name, "definition name")
        _require_identifier(self.version, "definition version")
        if not isinstance(self.registry, ToolRegistry):
            raise TypeError("registry must be a ToolRegistry")
        if not isinstance(self.policy, PolicyEngine):
            raise TypeError("policy must be a PolicyEngine")
        if self.policy.registry is not self.registry:
            raise ValueError("policy must be bound to the same ToolRegistry")
        _revalidate_redaction_policy(self)
        self.registry.freeze()
        object.__setattr__(self, "_fingerprint", sha256_json(_definition_identity(self)))

    @property
    def fingerprint(self) -> str:
        """Return a deterministic digest of the executable safety contract."""
        if sha256_json(_definition_identity(self)) != self._fingerprint:
            raise DefinitionConflict("definition dependencies mutated after pinning")
        return self._fingerprint


class DefinitionUnavailable(LookupError):
    """Raised when exact historical Saga code is unavailable."""


class DefinitionConflict(ValueError):
    """Raised when an immutable definition key is reused."""


class DefinitionCatalog:
    """Register and resolve immutable saga definitions by name and version."""

    def __init__(self, definitions: Iterable[SagaDefinition] = ()) -> None:
        self._definitions: dict[tuple[str, str], SagaDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: SagaDefinition) -> None:
        key = (definition.name, definition.version)
        existing = self._definitions.get(key)
        if existing is not None and existing is not definition:
            raise DefinitionConflict("definition key is already pinned")
        self._definitions[key] = definition

    def resolve(self, name: str, version: str) -> SagaDefinition:
        try:
            return self._definitions[(name, version)]
        except KeyError as error:
            raise DefinitionUnavailable("exact historical definition is unavailable") from error


def _require_identifier(value: str, role: str) -> None:
    if type(value) is not str or not 1 <= len(value) <= _MAX_IDENTIFIER_LENGTH:
        raise ValueError(f"{role} must contain between 1 and 200 characters")


def _revalidate_redaction_policy(definition: SagaDefinition) -> None:
    if type(definition.redaction_policy) is not RedactionPolicy:
        raise TypeError("redaction_policy must be a RedactionPolicy")
    validated = RedactionPolicy(sensitive_keys=definition.redaction_policy.sensitive_keys)
    object.__setattr__(definition, "redaction_policy", validated)


def _qualified_type(value: type[object]) -> str:
    return f"{value.__module__}.{value.__qualname__}"


def _source_hash(value: type[object], role: str) -> str:
    try:
        source = dedent(getsource(value)).strip().encode("utf-8")
    except (OSError, TypeError) as error:
        raise ValueError(f"{role} must expose stable source") from error
    return sha256(source).hexdigest()


def _implementation_identity(value: object) -> JsonObject:
    implementation = type(value)
    payload = {
        "qualified_name": _qualified_type(implementation),
        "source_hash": _source_hash(implementation, "tool adapters"),
        "config": _adapter_config_identity(value),
    }
    return _JSON_OBJECT.validate_python(payload, strict=True)


def _adapter_config_identity(value: object) -> JsonObject:
    if not isinstance(value, _DefinitionIdentityProvider):
        if _instance_has_state(value):
            raise ValueError("stateful tool adapters must expose definition_identity")
        return _JSON_OBJECT.validate_python({}, strict=True)
    first = _public_adapter_identity(value)
    if first != _public_adapter_identity(value):
        raise ValueError("adapter definition_identity must be deterministic")
    return first


def _public_adapter_identity(value: _DefinitionIdentityProvider) -> JsonObject:
    try:
        identity = _JSON_OBJECT.validate_python(value.definition_identity(), strict=True)
    except (TypeError, ValueError, ValidationError):
        raise ValueError("adapter definition_identity must be stable public JSON") from None
    if contains_sensitive_json(identity, RedactionPolicy()):
        raise ValueError("adapter definition_identity must contain only public values")
    return identity


def _instance_has_state(value: object) -> bool:
    try:
        if bool(vars(value)):
            return True
    except TypeError:
        pass
    return any(_populated_slots(value, implementation) for implementation in type(value).__mro__)


def _populated_slots(value: object, implementation: type[object]) -> bool:
    slots = implementation.__dict__.get("__slots__", ())
    names = (slots,) if isinstance(slots, str) else slots
    return any(name not in {"__dict__", "__weakref__"} and hasattr(value, name) for name in names)


def _schema_identity(model: type[BaseModel]) -> JsonObject:
    payload = {
        "model": _qualified_type(model),
        "source_hash": _source_hash(model, "tool models"),
        "schema": model.model_json_schema(),
    }
    return _JSON_OBJECT.validate_python(payload, strict=True)


def _read_identity(definition: ReadToolDefinition[BaseModel, BaseModel]) -> JsonObject:
    payload = {
        "kind": "read",
        "name": definition.name,
        "input": _schema_identity(definition.input_model),
        "result": _schema_identity(definition.result_model),
        "adapter": _implementation_identity(definition.adapter),
    }
    return _JSON_OBJECT.validate_python(payload, strict=True)


def _effect_identity(definition: EffectToolDefinition[BaseModel]) -> JsonObject:
    payload = {
        "kind": "effect",
        "name": definition.name,
        "definition_version": definition.definition_version,
        "command_schema_version": definition.command_schema_version,
        "input": _schema_identity(definition.input_model),
        "adapter": _implementation_identity(definition.adapter),
        "capabilities": definition.capabilities.model_dump(mode="json"),
        "compensate_with": definition.compensate_with,
        "compensation_dependencies": list(definition.compensation_dependencies),
        "compensation_independent_with": list(definition.compensation_independent_with),
        "compensation_resource_selector": list(definition.compensation_resource_selector),
    }
    return _JSON_OBJECT.validate_python(payload, strict=True)


def _tool_identity(
    definition: ReadToolDefinition[BaseModel, BaseModel] | EffectToolDefinition[BaseModel],
) -> JsonObject:
    if isinstance(definition, ReadToolDefinition):
        return _read_identity(definition)
    return _effect_identity(definition)


def _invariant_identity(definition: SagaDefinition) -> JsonObject:
    payload = {
        "success": definition.success_invariants.model_dump(mode="json"),
        "compensation": definition.compensation_invariants.model_dump(mode="json"),
        "clean_abort": definition.clean_abort_invariants.model_dump(mode="json"),
        "exception": definition.exception_invariants.model_dump(mode="json"),
    }
    return _JSON_OBJECT.validate_python(payload, strict=True)


def _definition_identity(definition: SagaDefinition) -> JsonObject:
    tools = [_tool_identity(item) for item in definition.registry.definitions()]
    payload = {
        "format": "agentic-saga-definition-v2",
        "name": definition.name,
        "version": definition.version,
        "tools": tools,
        "policy": definition.policy.definition_identity(),
        "invariants": _invariant_identity(definition),
        "budget": definition.budget.model_dump(mode="json"),
        "redaction": definition.redaction_policy.model_dump(mode="json"),
    }
    return _JSON_OBJECT.validate_python(payload, strict=True)


__all__ = [
    "DefinitionCatalog",
    "DefinitionConflict",
    "DefinitionUnavailable",
    "SagaDefinition",
]
