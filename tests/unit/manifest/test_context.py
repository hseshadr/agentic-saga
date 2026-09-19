from __future__ import annotations

import hashlib
import json
import tracemalloc
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError
from ruamel.yaml import YAML

from agentic_saga import SagaManifest, load_saga_context
from agentic_saga.contracts.runtime import ToolDescriptor
from agentic_saga.contracts.tools import ReadToolDefinition, ToolRegistry
from agentic_saga.manifest import ManifestInputError

_FAKE_PROVIDER_TOKEN = "_".join(("sk", "live", "abcdefghijklmnopqrstuv"))


class LookupCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    query: str


class LookupResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    available: bool


class CredentialDefaultCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    api_key: str = _FAKE_PROVIDER_TOKEN


class LookupAdapter:
    async def read(self, command: LookupCommand) -> LookupResult:
        return LookupResult(available=bool(command.query))


class CredentialDefaultAdapter:
    async def read(self, command: CredentialDefaultCommand) -> LookupResult:
        return LookupResult(available=bool(command.api_key))


def _registry(*names: str) -> ToolRegistry:
    definitions = tuple(
        ReadToolDefinition(name, LookupCommand, LookupResult, LookupAdapter()) for name in names
    )
    return ToolRegistry(definitions)


def _digest(registry: ToolRegistry) -> str:
    tools = [
        item.model_dump(mode="json")
        for item in sorted(
            (ToolDescriptor.from_definition(item) for item in registry.definitions()),
            key=lambda item: item.name,
        )
    ]
    encoded = json.dumps({"tools": tools}, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _manifest_text(digest: str, tools: tuple[str, ...] = ("search_options",)) -> str:
    allowed = "\n".join(f"    - {name}" for name in tools)
    return f"""\
schema_version: "1.0"
name: trip_booking
version: "1.0"
objective: Book a valid itinerary or restore a clean state.
instructions:
  - Inspect authoritative availability before proposing an effect.
success_criteria:
  - A ticket is issued and payment is confirmed.
autonomy:
  mode: guarded
  instructions:
    - Escalate instead of guessing when evidence is ambiguous.
budgets:
  turn_limit: 8
  tool_call_limit: 6
  elapsed_ms_limit: 30000
  token_limit: 4000
tools:
  catalog_sha256: "{digest}"
  allowed:
{allowed}
checks:
  policy:
    - traveler_authorized
  success:
    - ticket_issued
  compensation:
    - no_active_hold
  clean_abort:
    - no_charge
escalation:
  conditions:
    - Provider outcome remains unknown after reconciliation.
  instructions:
    - Present receipts and the unresolved decision to an operator.
example_paths:
  - name: available itinerary
    kind: happy_path
    narrative:
      - Inspect options, reserve the selected itinerary, then issue after payment.
  - name: payment rejected
    kind: compensation_path
    narrative:
      - Release any confirmed hold and verify that no charge remains.
"""


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def test_should_resolve_authoritative_tools_and_budget_when_manifest_is_valid(
    tmp_path: Path,
) -> None:
    # Given
    registry = _registry("search_options")
    path = _write(tmp_path / "saga.yaml", _manifest_text(_digest(registry)))

    # When
    context = load_saga_context(
        path,
        registry=registry,
        policy_checks=("traveler_authorized",),
        invariant_checks=("ticket_issued", "no_active_hold", "no_charge"),
    )

    # Then
    assert tuple(item.name for item in context.tool_descriptors) == ("search_options",)
    assert context.budget.turn_limit == 8
    assert json.loads(context.agent_context)["manifest"]["objective"].startswith("Book")


def test_should_reject_unenforceable_provider_cost_budget() -> None:
    # Given
    raw = YAML(typ="safe").load(_manifest_text("0" * 64))
    raw["budgets"]["cost_microusd_limit"] = 50_000

    # When / Then
    with pytest.raises(ValidationError, match="cost_microusd_limit"):
        SagaManifest.model_validate(raw)


@pytest.mark.parametrize("field", ["elapsed_ms_limit", "token_limit"])
def test_should_reject_manifest_without_one_planning_unit_per_turn(field: str) -> None:
    # Given
    raw = YAML(typ="safe").load(_manifest_text("0" * 64))
    raw["budgets"][field] = 7

    # When / Then
    with pytest.raises(ValidationError, match=field):
        SagaManifest.model_validate(raw)


@pytest.mark.parametrize("field", ["turn_limit", "tool_call_limit"])
def test_should_reject_manifest_budget_above_temporal_bound(field: str) -> None:
    raw = YAML(typ="safe").load(_manifest_text("0" * 64))
    raw["budgets"][field] = 101

    with pytest.raises(ValidationError, match=field):
        SagaManifest.model_validate(raw)


def test_should_keep_resolved_manifest_immutable(tmp_path: Path) -> None:
    # Given
    registry = _registry("search_options")
    path = _write(tmp_path / "saga.yaml", _manifest_text(_digest(registry)))
    context = load_saga_context(
        path,
        registry=registry,
        policy_checks=("traveler_authorized",),
        invariant_checks=("ticket_issued", "no_active_hold", "no_charge"),
    )

    # When / Then
    with pytest.raises(ValidationError, match="frozen"):
        context.manifest.__setattr__("name", "changed")


def test_should_reject_unknown_fields_without_coercing_manifest_values() -> None:
    # Given
    raw = {
        "schema_version": "1.0",
        "name": "invalid",
        "version": "1.0",
        "objective": "Do a thing.",
        "instructions": ["Use evidence."],
        "success_criteria": ["Evidence passes."],
        "autonomy": {"mode": "guarded", "instructions": ["Stop safely."]},
        "budgets": {
            "turn_limit": "8",
            "tool_call_limit": 6,
            "elapsed_ms_limit": 30_000,
            "token_limit": 4_000,
        },
        "tools": {"catalog_sha256": "0" * 64, "allowed": ["inspect"]},
        "checks": {
            "policy": [],
            "success": ["done"],
            "compensation": ["restored"],
            "clean_abort": ["clean"],
        },
        "escalation": {"conditions": ["Unknown."], "instructions": ["Escalate."]},
        "example_paths": [],
        "workflow": {"then": "execute"},
    }

    # When / Then
    with pytest.raises(ValueError):
        SagaManifest.model_validate(raw)


def test_should_reject_duplicate_manifest_references() -> None:
    # Given
    raw = _manifest_text("0" * 64).replace(
        "    - traveler_authorized", "    - traveler_authorized\n    - traveler_authorized"
    )

    # When / Then
    with pytest.raises(ValueError, match="unique names"):
        SagaManifest.model_validate(YAML(typ="safe").load(raw))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda text: text + "name: duplicate\n", "safe valid YAML"),
        (
            lambda text: text.replace(
                "objective: Book a valid itinerary or restore a clean state.",
                "objective: !!python/object/apply:os.system ['false']",
            ),
            "safe valid YAML",
        ),
        (lambda text: text + "---\nname: second\n", "exactly one"),
    ],
)
def test_should_reject_ambiguous_or_unsafe_yaml(
    tmp_path: Path,
    mutation: Callable[[str], str],
    message: str,
) -> None:
    # Given
    registry = _registry("search_options")
    path = _write(tmp_path / "saga.yaml", mutation(_manifest_text(_digest(registry))))

    # When / Then
    with pytest.raises(ValueError, match=message):
        load_saga_context(path, registry=registry)


def test_should_sanitize_yaml_parser_failure_without_retaining_source(tmp_path: Path) -> None:
    # Given
    private = "private-parser-material"
    path = _write(tmp_path / "saga.yaml", f"objective: [{private}\n")

    # When / Then
    with pytest.raises(ManifestInputError) as captured:
        load_saga_context(path, registry=ToolRegistry())
    assert private not in repr(captured.value.args)
    assert private not in repr(vars(captured.value))
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_should_sanitize_yaml_integer_conversion_failure(tmp_path: Path) -> None:
    # Given
    private = "7" * 6_000
    path = _write(tmp_path / "saga.yaml", f"objective: {private}\n")

    # When / Then
    with pytest.raises(ManifestInputError) as captured:
        load_saga_context(path, registry=ToolRegistry())
    assert private not in repr(captured.value.args)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize(
    "source",
    [
        "[" + ",".join("0" for _ in range(257)) + "]\n",
        "value: " + ("x" * 16_385) + "\n",
    ],
)
def test_should_apply_generic_json_limits_before_manifest_validation(
    tmp_path: Path, source: str
) -> None:
    # Given
    path = _write(tmp_path / "saga.yaml", source)

    # When / Then
    with pytest.raises(ManifestInputError, match="safety limits") as captured:
        load_saga_context(path, registry=ToolRegistry())
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_should_sanitize_manifest_validation_failure_without_retaining_input(
    tmp_path: Path,
) -> None:
    # Given
    private = "private-invalid-objective"
    registry = _registry("search_options")
    text = _manifest_text(_digest(registry)).replace(
        "Book a valid itinerary or restore a clean state.", private * 500
    )
    path = _write(tmp_path / private, text)

    # When / Then
    with pytest.raises(ManifestInputError) as captured:
        load_saga_context(path, registry=registry)
    assert private not in repr(captured.value.args)
    assert private not in repr(vars(captured.value))
    assert not hasattr(captured.value, "errors")
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_should_reject_oversized_yaml_before_parsing(tmp_path: Path) -> None:
    # Given
    path = _write(tmp_path / "saga.yaml", "x" * 65_537)

    # When / Then
    with pytest.raises(ValueError, match="exceeds 65536 bytes"):
        load_saga_context(path, registry=ToolRegistry())


def test_should_bound_memory_before_reading_oversized_file(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "huge-sparse.yaml"
    with path.open("wb") as stream:
        stream.seek(10_000_000)
        stream.write(b"x")

    # When
    tracemalloc.start()
    with pytest.raises(ValueError, match="exceeds 65536 bytes"):
        load_saga_context(path, registry=ToolRegistry())
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # Then
    assert peak < 1_000_000


def test_should_reject_deep_yaml_structure(tmp_path: Path) -> None:
    # Given
    nested = "value"
    for _ in range(20):
        nested = f"[{nested}]"
    path = _write(tmp_path / "saga.yaml", f"root: {nested}\n")

    # When / Then
    with pytest.raises(ValueError, match=r"safe valid YAML|safety limits"):
        load_saga_context(path, registry=ToolRegistry())


def test_should_reject_yaml_aliases(tmp_path: Path) -> None:
    # Given
    registry = _registry("search_options")
    text = _manifest_text(_digest(registry)).replace(
        "instructions:\n  - Inspect authoritative availability before proposing an effect.\n"
        "success_criteria:\n  - A ticket is issued and payment is confirmed.",
        "instructions: &guidance\n"
        "  - Inspect authoritative availability before proposing an effect.\n"
        "success_criteria: *guidance",
    )
    path = _write(tmp_path / "saga.yaml", text)

    # When / Then
    with pytest.raises(ValueError, match="aliased value"):
        load_saga_context(path, registry=registry)


def test_should_reject_secret_like_text(tmp_path: Path) -> None:
    # Given
    registry = _registry("search_options")
    text = _manifest_text(_digest(registry)).replace(
        "Book a valid itinerary or restore a clean state.", "Bearer raw-production-secret"
    )
    path = _write(tmp_path / "saga.yaml", text)

    # When / Then
    with pytest.raises(ValueError, match="private material"):
        load_saga_context(
            path,
            registry=registry,
            policy_checks=("traveler_authorized",),
            invariant_checks=("ticket_issued", "no_active_hold", "no_charge"),
        )


@pytest.mark.parametrize(
    "replacement",
    ["password=hunter2", _FAKE_PROVIDER_TOKEN],
)
def test_should_reject_high_confidence_secret_patterns(tmp_path: Path, replacement: str) -> None:
    # Given
    registry = _registry("search_options")
    text = _manifest_text(_digest(registry)).replace(
        "Book a valid itinerary or restore a clean state.", replacement
    )
    path = _write(tmp_path / "saga.yaml", text)

    # When / Then
    with pytest.raises(ValueError, match="private material"):
        load_saga_context(
            path,
            registry=registry,
            policy_checks=("traveler_authorized",),
            invariant_checks=("ticket_issued", "no_active_hold", "no_charge"),
        )


def test_should_reject_provider_token_pattern_in_manifest_identity(tmp_path: Path) -> None:
    # Given
    registry = _registry("search_options")
    text = _manifest_text(_digest(registry)).replace(
        '\nversion: "1.0"', f"\nversion: {_FAKE_PROVIDER_TOKEN}"
    )
    path = _write(tmp_path / "saga.yaml", text)

    # When / Then
    with pytest.raises(ValueError, match="private material"):
        load_saga_context(
            path,
            registry=registry,
            policy_checks=("traveler_authorized",),
            invariant_checks=("ticket_issued", "no_active_hold", "no_charge"),
        )


def test_should_reject_missing_tool_before_agent_context_is_built(tmp_path: Path) -> None:
    # Given
    registry = _registry("search_options")
    path = _write(tmp_path / "saga.yaml", _manifest_text("0" * 64, ("missing_tool",)))

    # When / Then
    with pytest.raises(ValueError, match="unknown tool") as captured:
        load_saga_context(path, registry=registry)
    assert "missing_tool" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_should_reject_descriptor_schema_drift(tmp_path: Path) -> None:
    # Given
    original = _registry("search_options")
    changed = _registry("search_options", "new_tool")
    path = _write(tmp_path / "saga.yaml", _manifest_text(_digest(original), ("new_tool",)))

    # When / Then
    with pytest.raises(ValueError, match="catalog digest"):
        load_saga_context(path, registry=changed)


def test_should_reject_private_material_in_registered_tool_descriptor(tmp_path: Path) -> None:
    # Given
    registry = ToolRegistry(
        (
            ReadToolDefinition(
                "search_options",
                CredentialDefaultCommand,
                LookupResult,
                CredentialDefaultAdapter(),
            ),
        )
    )
    path = _write(tmp_path / "saga.yaml", _manifest_text(_digest(registry)))

    # When / Then
    with pytest.raises(ValueError, match="tool descriptor contains private material"):
        load_saga_context(path, registry=registry)


@pytest.mark.parametrize(
    ("policy", "invariants", "message"),
    [
        ((), ("ticket_issued", "no_active_hold", "no_charge"), "policy check"),
        (("traveler_authorized",), ("ticket_issued",), "invariant check"),
    ],
)
def test_should_reject_missing_named_check(
    tmp_path: Path,
    policy: tuple[str, ...],
    invariants: tuple[str, ...],
    message: str,
) -> None:
    # Given
    registry = _registry("search_options")
    path = _write(tmp_path / "saga.yaml", _manifest_text(_digest(registry)))

    # When / Then
    with pytest.raises(ValueError, match=message):
        load_saga_context(
            path, registry=registry, policy_checks=policy, invariant_checks=invariants
        )


def test_should_render_same_context_when_reference_order_changes(tmp_path: Path) -> None:
    # Given
    registry = _registry("search_options", "read_rules")
    digest = _digest(registry)
    first_text = _manifest_text(digest, ("search_options", "read_rules"))
    second_text = _manifest_text(digest, ("read_rules", "search_options"))
    first = _write(tmp_path / "first.yaml", first_text)
    second = _write(tmp_path / "second.yaml", second_text)
    # When
    policy = ("traveler_authorized",)
    invariants = ("ticket_issued", "no_active_hold", "no_charge")
    first_context = load_saga_context(
        first, registry=registry, policy_checks=policy, invariant_checks=invariants
    )
    second_context = load_saga_context(
        second, registry=registry, policy_checks=policy, invariant_checks=invariants
    )

    # Then
    assert first_context.agent_context == second_context.agent_context


def test_should_compute_catalog_digest_for_manifest_authoring() -> None:
    # Given
    registry = _registry("search_options", "read_rules")

    # When
    digest = SagaManifest.tool_catalog_sha256(registry, ("search_options", "read_rules"))

    # Then
    assert digest == _digest(registry)
