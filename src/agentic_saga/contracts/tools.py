from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Annotated, Final, Protocol, TypeGuard, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from agentic_saga.contracts.common import (
    FenceToken,
    JsonObject,
    OperationId,
    Reversibility,
    SagaId,
    StepInstanceId,
)
from agentic_saga.contracts.outcomes import (
    EffectOutcome,
    ReconciliationOutcome,
    is_safe_outcome_correlation,
)

type _Correlation = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]
_MAX_VERSION_LENGTH: Final[int] = 200
_MAX_DESCRIPTION_LENGTH: Final[int] = 2_000
_DEFAULT_READ_DESCRIPTION: Final[str] = "Read current authoritative state."
_DEFAULT_EFFECT_DESCRIPTION: Final[str] = "Request one durable external effect."


class EffectContext(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    saga_id: SagaId
    step_instance_id: StepInstanceId
    operation_id: OperationId
    fence_token: FenceToken
    delivery_attempt: int = Field(strict=True, ge=1)
    forward_receipts: tuple[JsonObject, ...] = ()


class ReconcileContext(EffectContext):
    correlation: _Correlation

    @field_validator("correlation")
    @classmethod
    def require_opaque_correlation(cls, value: str) -> str:
        if not is_safe_outcome_correlation(value):
            raise ValueError("correlation must be an opaque validated reference")
        return value


@runtime_checkable
class EffectAdapter[CommandT: BaseModel](Protocol):
    async def execute(self, command: CommandT, context: EffectContext) -> EffectOutcome: ...

    async def reconcile(
        self,
        command: CommandT,
        context: ReconcileContext,
    ) -> ReconciliationOutcome: ...


@runtime_checkable
class ReadAdapter[CommandT: BaseModel, ResultT: BaseModel](Protocol):
    async def read(self, command: CommandT) -> ResultT: ...


class ToolCapabilities(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    idempotency_retention_seconds: int | None = Field(default=None, strict=True, gt=0)
    reconciliation_supported: bool
    cancellation_supported: bool
    fencing_supported: bool
    reversibility: Reversibility
    partial_effects_possible: bool


class InvalidToolSchemaError(Exception):
    """Raised when a registered tool schema is not strict and immutable."""


class UnsupportedToolDefinitionError(Exception):
    """Raised when an object is not a supported tool definition."""


class DependencyCycle(ValueError):
    """Raised when registered compensation dependencies contain a cycle."""


class InvalidCompensationDependency(ValueError):
    """Raised when compensation dependency metadata is incomplete or malformed."""


def _require_version(value: str, role: str) -> None:
    if type(value) is not str or not 1 <= len(value) <= _MAX_VERSION_LENGTH:
        raise ValueError(f"{role} must contain between 1 and 200 characters")


def _require_description(value: str, role: str) -> None:
    if type(value) is not str or not value.strip() or len(value) > _MAX_DESCRIPTION_LENGTH:
        raise ValueError(f"{role} description must contain between 1 and 2000 characters")


def _missing_schema_requirement(model: type[BaseModel]) -> str | None:
    if model.model_config.get("strict") is not True:
        return "strict=True"
    if model.model_config.get("extra") != "forbid":
        return "extra='forbid'"
    if model.model_config.get("frozen") is not True:
        return "frozen=True"
    return None


def _require_safe_schema(tool_name: str, role: str, model: type[BaseModel]) -> None:
    requirement = _missing_schema_requirement(model)
    if requirement is None:
        return
    raise InvalidToolSchemaError(f"tool {tool_name!r} {role} schema must configure {requirement}")


def _require_unique_names(values: tuple[str, ...], role: str) -> None:
    if any(type(value) is not str or not value for value in values):
        raise InvalidCompensationDependency(f"{role} must contain non-empty names")
    if len(values) != len(set(values)):
        raise InvalidCompensationDependency(f"{role} must contain unique names")


@dataclass(frozen=True)
class ReadToolDefinition[CommandT: BaseModel, ResultT: BaseModel]:
    name: str
    input_model: type[CommandT]
    result_model: type[ResultT]
    adapter: ReadAdapter[CommandT, ResultT]
    description: str = _DEFAULT_READ_DESCRIPTION

    def __post_init__(self) -> None:
        _require_description(self.description, "read")
        _require_safe_schema(self.name, "read input", self.input_model)
        _require_safe_schema(self.name, "read result", self.result_model)


@dataclass(frozen=True)
class EffectToolDefinition[CommandT: BaseModel]:
    name: str
    definition_version: str
    command_schema_version: str
    input_model: type[CommandT]
    adapter: EffectAdapter[CommandT]
    capabilities: ToolCapabilities
    compensate_with: str | None
    compensation_dependencies: tuple[str, ...] = ()
    compensation_independent_with: tuple[str, ...] = ()
    compensation_resource_selector: tuple[str, ...] = ()
    description: str = _DEFAULT_EFFECT_DESCRIPTION

    def __post_init__(self) -> None:
        _require_description(self.description, "effect")
        _require_version(self.definition_version, "effect definition version")
        _require_version(self.command_schema_version, "effect command schema version")
        _require_safe_schema(self.name, "effect input", self.input_model)
        _require_unique_names(self.compensation_dependencies, "compensation dependencies")
        _require_unique_names(self.compensation_independent_with, "independent compensations")
        _require_unique_names(self.compensation_resource_selector, "resource selector")
        if self.name in self.compensation_independent_with:
            raise InvalidCompensationDependency("an effect cannot be independent with itself")


type _PublicDefinition = ReadToolDefinition[BaseModel, BaseModel] | EffectToolDefinition[BaseModel]


def _is_public_definition(value: object) -> TypeGuard[_PublicDefinition]:
    return isinstance(value, (ReadToolDefinition, EffectToolDefinition))


def _require_definition(value: object) -> _PublicDefinition:
    if not _is_public_definition(value):
        raise UnsupportedToolDefinitionError(type(value).__name__)
    return value


class UnknownToolError(LookupError):
    """Raised when a tool name is not present in the registry."""


class DuplicateToolError(ValueError):
    """Raised when two definitions use the same tool name."""


class ToolRegistryFrozen(ValueError):
    """Raised when version-pinned definitions attempt registry mutation."""


class ToolRegistry:
    def __init__(self, definitions: Iterable[object] = ()) -> None:
        self._definitions: dict[str, _PublicDefinition] = {}
        self._frozen = False
        for definition in definitions:
            self._register(definition)
        self._validate_compensation_dependencies(require_complete=True)

    def register_read[CommandT: BaseModel, ResultT: BaseModel](
        self, definition: ReadToolDefinition[CommandT, ResultT]
    ) -> None:
        self._require_mutable()
        self._register(definition)

    def register_effect[CommandT: BaseModel](
        self, definition: EffectToolDefinition[CommandT]
    ) -> None:
        self._require_mutable()
        self._register(definition)
        try:
            self._validate_compensation_dependencies(require_complete=False)
        except ValueError:
            del self._definitions[definition.name]
            raise

    def definition(self, name: str) -> _PublicDefinition:
        if name not in self._definitions:
            raise UnknownToolError(name)
        return self._definitions[name]

    def validate_command(self, name: str, raw: JsonObject) -> BaseModel:
        definition = self.definition(name)
        return definition.input_model.model_validate(raw)

    def definitions(self) -> tuple[_PublicDefinition, ...]:
        return tuple(sorted(self._definitions.values(), key=lambda item: item.name))

    def freeze(self) -> None:
        self._frozen = True

    def effect_definitions(self) -> tuple[EffectToolDefinition[BaseModel], ...]:
        effects = (
            definition
            for definition in self._definitions.values()
            if isinstance(definition, EffectToolDefinition)
        )
        return tuple(sorted(effects, key=lambda item: item.name))

    def _register(self, definition: object) -> None:
        registered = _require_definition(definition)
        if registered.name in self._definitions:
            raise DuplicateToolError(registered.name)
        self._definitions[registered.name] = registered

    def _require_mutable(self) -> None:
        if self._frozen:
            raise ToolRegistryFrozen("version-pinned tool registry is immutable")

    def _validate_compensation_dependencies(self, *, require_complete: bool) -> None:
        definitions = self.effect_definitions()
        names = frozenset(item.name for item in definitions)
        graph = {item.name: item.compensation_dependencies for item in definitions}
        unknown = _unknown_dependencies(graph, names)
        _require_known_dependencies(unknown, require_complete)
        if _graph_has_cycle(graph, names):
            raise DependencyCycle("compensation dependency graph contains a cycle")


def _unknown_dependencies(
    graph: dict[str, tuple[str, ...]], names: frozenset[str]
) -> tuple[str, ...]:
    values = {dependency for dependencies in graph.values() for dependency in dependencies}
    return tuple(sorted(values - names))


def _require_known_dependencies(unknown: tuple[str, ...], require_complete: bool) -> None:
    if require_complete and unknown:
        raise InvalidCompensationDependency(f"unknown compensation dependency: {unknown[0]}")


def _graph_has_cycle(graph: dict[str, tuple[str, ...]], names: frozenset[str]) -> bool:
    pending = {name: set(graph[name]) & names for name in names}
    while ready := _ready_dependencies(pending):
        _remove_dependencies(pending, ready)
    return bool(pending)


def _ready_dependencies(pending: dict[str, set[str]]) -> tuple[str, ...]:
    return tuple(sorted(name for name, dependencies in pending.items() if not dependencies))


def _remove_dependencies(pending: dict[str, set[str]], ready: tuple[str, ...]) -> None:
    for name in ready:
        del pending[name]
    for dependencies in pending.values():
        dependencies.difference_update(ready)
