from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
OUTCOME_TITLES = (
    "Happy path — verified finish",
    "Business failure — verified compensation",
    "Compensation failure or unknown — reconcile, then stop",
    "Human resolution — authenticated transaction",
)
CORE_BOUNDS = (
    "16 levels deep",
    "4,096 total nodes",
    "256 items per container",
    "16,384 characters and 16,384 UTF-8 bytes per string",
    "65,536 canonical UTF-8 bytes",
)
SOURCE_PATHS = (
    "src/agentic_saga/kernel/definitions.py",
    "src/agentic_saga/contracts/runtime.py",
    "src/agentic_saga/contracts/common.py",
    "src/agentic_saga/contracts/redaction.py",
    "src/agentic_saga/kernel/ports.py",
    "src/agentic_saga/storage/sqlite.py",
    "examples/ecommerce/demo.py",
    "src/agentic_saga/demo/server.py",
    "src/agentic_saga/cli/",
)
NETWORK_TOKENS = ("@import", "fetch(", "xmlhttprequest", "websocket", "eventsource")
URL_ATTRIBUTES = ("href", "src", "srcset", "action", "formaction", "poster", "cite")
METERING_CLAUSE = "input tokens, actual token usage, end-to-end Saga time, money, or provider spend"
STORAGE_LIMITS = (
    "POSIX-only",
    "fresh destination only",
    "0600",
    "ACL",
    "same-UID",
    "local filesystem",
)
RECORDER_LIMITS = (
    "POSIX descriptor support",
    "fresh, empty destination",
    "at most 200 static files and 32 MiB total",
    "127.0.0.1:&lt;actual port&gt;",
    "GET and HEAD only",
    "240-character request path",
    "8 MiB served-file limit",
    "1-second header deadline",
    "8 concurrent handlers",
    "Host",
    "Origin",
    "Sec-Fetch-Site",
    "independently optional",
)


def _architecture() -> str:
    return (ROOT / "docs" / "architecture.html").read_text()


def _attributes(source: str, name: str) -> tuple[str, ...]:
    return tuple(re.findall(rf'\b{name}="([^"]+)"', source))


def _tags(page: str, selector: str) -> tuple[str, ...]:
    return tuple(re.findall(rf"<{selector}[^>]*>", page))


def test_architecture_states_exact_truth_and_only_one_worked_example() -> None:
    page = _architecture()
    required = (
        CORE_BOUNDS
        + OUTCOME_TITLES
        + (
            "SagaDefinition-owned",
            "application-classified sensitive keys",
            "built-in floor",
            "public manifest",
            "four-field deterministic planning quota",
            "floor(total / turns)",
            "remainder stays unused",
        )
    )
    assert all(value in page for value in required)
    assert "ecommerce" in page.lower()
    assert "ticket" not in page.lower()


def test_architecture_uses_canonical_definition_and_ecommerce_terms() -> None:
    page = _architecture()
    manifest = (ROOT / "examples" / "ecommerce" / "saga.yaml").read_text()
    assert "pinned tools + policy" in page
    assert "typed goal + policy" not in page
    assert "charge_payment" in page and "charge_payment" in manifest
    assert "capture_payment" not in page
    assert "Every generic JSON boundary" in page
    assert "Every core JSON value" not in page


def test_architecture_states_planning_quota_and_provider_limits() -> None:
    page = _architecture()
    quota = ("turn_limit", "tool_call_limit", "elapsed_ms_limit", "token_limit")
    assert all(name in page for name in quota)
    assert "Counts durable read starts, effect intents, and compensation intents" in page
    assert "OpenRouter adapter applies the output-token cap and planning-call deadline" in page
    assert METERING_CLAUSE in page
    assert "cost_microusd" not in page


def test_architecture_states_storage_and_recorder_boundaries() -> None:
    page = _architecture()
    assert all(value in page for value in STORAGE_LIMITS + RECORDER_LIMITS)
    assert "database, WAL, SHM, temporary, backup, and restored files" in page


def test_architecture_names_restart_and_authority_safety_contracts() -> None:
    page = _architecture()
    required = (
        "definition fingerprint",
        "same ToolRegistry",
        "renewed around awaited work",
        "released before returning",
        "ReadUnavailable",
    )
    assert all(value in page for value in required)


def test_displayed_source_paths_are_canonical_and_exist() -> None:
    page = _architecture()
    displayed = _attributes(page, "data-source-path")
    assert set(SOURCE_PATHS) <= set(displayed)
    assert all((ROOT / path).exists() for path in displayed)
    assert "src/agentic_saga/cli.py" not in page
    assert "src/agentic_saga/contracts/</code>" not in page


def test_lifecycle_ids_controls_and_scroll_hint_are_complete() -> None:
    page = _architecture()
    stages = _tags(page, 'button class="stage"')
    details = _tags(page, 'div class="detail"')
    ids = _attributes(page, "id")
    controls = tuple(_attributes(stage, "aria-controls")[0] for stage in stages)
    assert len(ids) == len(set(ids))
    assert len(stages) == len(details) == len(controls) == 7
    assert set(controls) == set(_attributes("".join(details), "id"))
    assert all('aria-describedby="lifecycle-scroll-hint"' in stage for stage in stages)


def test_lifecycle_defaults_to_one_selection_but_all_details_without_javascript() -> None:
    page = _architecture()
    stages = _tags(page, 'button class="stage"')
    details = _tags(page, 'div class="detail"')
    noscript = re.findall(r"<noscript>(.*?)</noscript>", page, re.DOTALL)
    assert sum('aria-pressed="true"' in stage for stage in stages) == 1
    assert all(" hidden" not in detail for detail in details)
    assert ".js-ready .detail[hidden]" in page
    assert len(noscript) == 1 and "every station remains expanded" in noscript[0]


def test_lifecycle_script_enforces_one_pressed_detail_and_native_activation() -> None:
    page = _architecture()
    stages = _tags(page, 'button class="stage"')
    assert 'stage.addEventListener("click", () => select(stage))' in page
    assert 'item.setAttribute("aria-pressed", String(item === stage))' in page
    assert "item.hidden = item.id !== stage.dataset.detail" in page
    assert "stages.length === 7 && details.length === 7" in page
    assert "select(stages[0])" in page
    assert all('type="button"' in stage for stage in stages)


def test_architecture_is_a_standalone_zero_egress_document() -> None:
    page = _architecture()
    lower = page.lower()
    urls = tuple(value for name in URL_ATTRIBUTES for value in _attributes(page, name))
    absolute = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//)", re.IGNORECASE)
    assert re.search(r"(?:https?:)?//", page, re.IGNORECASE) is None
    assert re.search(r"url\(\s*['\"]?(?:https?:)?//", page, re.IGNORECASE) is None
    assert all(token not in lower for token in NETWORK_TOKENS)
    assert all(absolute.match(value) is None or value.startswith("data:") for value in urls)
    assert '<script src="' not in lower
    assert '<link rel="stylesheet"' not in lower
