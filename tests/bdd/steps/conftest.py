from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from examples.ecommerce.domain import DemoRun


@dataclass
class BddWorld:
    workdir: Path
    scenario: str | None = None
    run: DemoRun | None = None


@pytest.fixture
def world(tmp_path: Path) -> BddWorld:
    return BddWorld(workdir=tmp_path)
