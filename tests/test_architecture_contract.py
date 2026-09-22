from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

ROOT = Path(__file__).parents[1]
ARCHITECTURE = ROOT / "docs" / "architecture"
SPEC = ARCHITECTURE / "agentic-saga.architecture.json"
PAGE = ARCHITECTURE / "index.html"


def _spec() -> dict[str, object]:
    return cast(dict[str, object], json.loads(SPEC.read_text()))


def _items(name: str) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], _spec()[name])


def test_architecture_is_one_showcase_temporal_specification() -> None:
    specification = _spec()
    meta = cast(dict[str, object], specification["meta"])

    assert specification["schema_version"] == 1
    assert specification["diagram_type"] == "architecture"
    assert meta["quality_profile"] == "showcase"
    assert "Temporal Safety" in cast(str, meta["title"])


def test_architecture_keeps_the_public_system_small_and_unique() -> None:
    components = _items("components")
    identifiers = [item["id"] for item in components]

    assert len(components) == 12
    assert len(identifiers) == len(set(identifiers))
    assert {
        "temporal",
        "workflow",
        "agent-activity",
        "decision-adapters",
        "business-activity",
        "human-verifier",
        "trace-projector",
        "flight-recorder",
    } <= set(identifiers)


def test_architecture_states_the_agent_and_workflow_authority_split() -> None:
    source = SPEC.read_text()

    assert "one currently eligible forward tool" in source
    assert "Never executes, compensates, or self-escalates" in source
    assert "Deterministic prerequisites, budgets, proof, and rollback" in source
    assert "Automatic reverse compensation before human review" in source


def test_architecture_states_provider_and_private_history_contracts() -> None:
    source = SPEC.read_text()

    assert "At-least-once Activities use stable operation identity" in source
    assert "Lost responses reconcile before progress" in source
    assert "Private history requires a KMS-backed payload codec" in source
    assert "opaque authorization reference" in source


def test_architecture_has_no_retired_runtime_surface() -> None:
    lower = SPEC.read_text().lower()

    assert "sqlite" not in lower
    assert "saga kernel" not in lower
    assert "lease" not in lower
    assert "begin_compensation" not in lower
    assert "escalate_to_human" not in lower


def test_every_connection_and_view_references_a_real_component() -> None:
    identifiers = {cast(str, item["id"]) for item in _items("components")}
    connections = _items("connections")
    meta = cast(dict[str, object], _spec()["meta"])
    views = cast(list[dict[str, object]], meta["views"])

    assert all(item["from"] in identifiers and item["to"] in identifiers for item in connections)
    assert all(set(cast(list[str], item["focus"])) <= identifiers for item in views)


def test_delivered_architecture_contains_the_exact_semantic_nodes() -> None:
    page = PAGE.read_text()

    for label in (
        "Temporal Service",
        "Deterministic Workflow",
        "Agent Decision Activity",
        "Pydantic AI · Jev",
        "Human Verification Activity",
        "Flight Recorder",
    ):
        assert label in page


def test_delivered_architecture_is_standalone_and_accessible() -> None:
    page = PAGE.read_text()
    tags = tuple(re.findall(r"<(?:script|link|img)[^>]*>", page, re.IGNORECASE))
    external = re.compile(r'(?:src|href)=["\'](?:https?:)?//', re.IGNORECASE)

    assert "<svg" in page
    assert 'role="button"' in page
    assert 'tabindex="0"' in page
    assert not any(external.search(tag) for tag in tags)
    assert '<script src="' not in page.lower()
    assert '<link rel="stylesheet"' not in page.lower()
