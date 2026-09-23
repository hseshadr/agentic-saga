"""Keep the README's first screen on the portfolio template.

String and regex checks only: this guards structure, not prose.
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
MAX_BADGES = 4
REQUIRED_LABELS = (
    "**What it does**",
    "**Who it's for**",
    "**What stays on your device / what leaves it**",
    "**Runs on**",
    "**Not for**",
    "**Status**",
)


def _first_screen() -> str:
    return README.split("## Try it in 60 seconds", 1)[0]


def _tagline() -> str:
    lines = (line.strip() for line in README.splitlines()[1:])
    return next(line for line in lines if line and not line.startswith("[!["))


def _package_description() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return str(project["description"])


def test_title_and_tagline_match_the_package_description() -> None:
    assert README.splitlines()[0] == "# Agentic Saga"
    tagline = _tagline()
    assert len(tagline) <= MAX_TAGLINE
    assert tagline == _package_description()


def test_first_screen_has_few_badges_and_every_label() -> None:
    before_glance = README.split("## At a glance", 1)[0]
    assert before_glance.count("[![") <= MAX_BADGES
    first_screen = _first_screen()
    assert all(label in first_screen for label in REQUIRED_LABELS)


def test_example_heading_and_hero_caption_are_in_order() -> None:
    assert "## Try it in 60 seconds" in README
    assert README.index("## Try it in 60 seconds") < README.index("## How it works")
    assert "Real output of the example below" in _first_screen()


def test_architecture_map_is_linked_and_its_spec_exists() -> None:
    assert re.search(
        r"\[Explore the interactive architecture map[^\]]*\]\(docs/architecture/index\.html\)",
        README,
    )
    assert (ROOT / ARCHITECTURE_SPEC).is_file()


def test_every_relative_link_resolves() -> None:
    links = re.findall(r"\]\(([^)\s]+)\)", README)
    relative = [link.split("#", 1)[0] for link in links if not _external(link)]
    missing = [link for link in relative if link and not (ROOT / link).exists()]
    assert not missing


def _external(link: str) -> bool:
    return link.startswith(("http://", "https://", "mailto:", "#"))
