"""Keep the README short, plain, and true.

String and regex checks only: this guards structure, facts, and wording, not style.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
# The Archify map in this repository is generated from this spec file.
ARCHITECTURE_SPEC = "docs/architecture/runtime.architecture.json"
MAX_TAGLINE = 120
MAX_BADGES = 3
SECTION_ORDER = (
    "## Try it",
    "## How it works",
    "## What it does not do",
    "## When to use something else",
    "## Install",
    "## Develop",
    "## More detail",
    "## License",
)
# Internal vocabulary, hype, and the retired first-screen template. Checked outside code.
BANNED = (
    "northstar",
    "seam",
    "lego",
    "trust envelope",
    "receipt",
    "fail-closed",
    "fail closed",
    "gate",
    "fleet",
    "portfolio",
    "production-ready",
    "robust",
    "blazing",
    "enterprise-grade",
    "seamless",
    "at a glance",
    "try it in 60 seconds",
)
# Real output of the Try it steps, captured from the ecommerce example.
TRY_IT_FACTS = (
    "Agent chose: reserve_inventory -> charge_payment -> schedule_fulfillment -> verify_order",
    "Undo order:  cancel_fulfillment -> refund_payment -> release_inventory",
    "Outcome:     compensated_verified",
    "uv run python -m examples.ecommerce.run lost-response",
    "Outcome: succeeded_verified · provider effects: 3",
)


def _lines() -> list[str]:
    return [line.strip() for line in README.splitlines()[1:] if line.strip()]


def _intro() -> str:
    return README.split("\n## ", 1)[0]


def _section(heading: str) -> str:
    after = README.split(f"\n{heading}\n", 1)[1]
    return after.split("\n## ", 1)[0]


def _prose() -> str:
    without_blocks = re.sub(r"```.*?```", "", README, flags=re.DOTALL)
    without_code = re.sub(r"`[^`]*`", "", without_blocks)
    return re.sub(r"\]\([^)]*\)", "]", without_code).lower()


def _package_description() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return str(project["description"])


def test_title_and_first_line_match_the_package_description() -> None:
    assert README.splitlines()[0] == "# Agentic Saga"
    tagline = _lines()[0]
    assert len(tagline) <= MAX_TAGLINE
    assert tagline == _package_description()


def test_fastest_way_to_try_it_is_bold_right_under_the_first_line() -> None:
    try_line = _lines()[1]
    assert try_line.startswith("**")
    assert "git clone https://github.com/hseshadr/agentic-saga" in try_line


def test_intro_has_few_badges_and_a_technical_docs_line() -> None:
    intro = _intro()
    assert intro.count("[![") <= MAX_BADGES
    docs_line = next(line for line in intro.splitlines() if line.startswith("**Technical docs:**"))
    assert "(docs/ARCHITECTURE.md)" in docs_line
    assert "(docs/GETTING_STARTED.md)" in docs_line


def test_sections_appear_in_the_standard_order() -> None:
    positions = [README.index(f"\n{heading}\n") for heading in SECTION_ORDER]
    assert positions == sorted(positions)


def test_try_it_shows_real_output_of_the_example() -> None:
    try_it = _section("## Try it")
    assert all(fact in try_it for fact in TRY_IT_FACTS)


def test_prose_avoids_internal_jargon_and_hype() -> None:
    prose = _prose()
    found = [word for word in BANNED if re.search(rf"\b{re.escape(word)}\b", prose)]
    assert not found


def test_develop_links_the_getting_started_guide_and_full_check() -> None:
    develop = _section("## Develop")
    assert "(docs/GETTING_STARTED.md)" in develop
    assert "uv run poe gate" in develop


def test_more_detail_links_every_technical_doc() -> None:
    more = _section("## More detail")
    docs = sorted(path.relative_to(ROOT).as_posix() for path in (ROOT / "docs").rglob("*.md"))
    missing = [doc for doc in docs if f"({doc})" not in more]
    assert not missing


def test_architecture_map_is_linked_and_its_spec_exists() -> None:
    assert "(docs/architecture/index.html)" in _section("## More detail")
    assert (ROOT / ARCHITECTURE_SPEC).is_file()


def test_every_relative_link_resolves() -> None:
    links = re.findall(r"\]\(([^)\s]+)\)", README)
    relative = [link.split("#", 1)[0] for link in links if not _external(link)]
    missing = [link for link in relative if link and not (ROOT / link).exists()]
    assert not missing


def _external(link: str) -> bool:
    return link.startswith(("http://", "https://", "mailto:", "#"))
