import tomllib
from pathlib import Path

import agentic_saga


def test_package_exposes_initial_version() -> None:
    assert agentic_saga.__version__ == "0.1.0"


def test_should_define_poe_lint_task_when_inspecting_project_metadata() -> None:
    project_root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((project_root / "pyproject.toml").read_text())
    tasks = pyproject["tool"]["poe"]["tasks"]
    assert tasks["lint"]["shell"] == "ruff check --fix . && ruff format ."
