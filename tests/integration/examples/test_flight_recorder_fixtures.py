from pathlib import Path

import pytest

from examples.ecommerce.export_flight_recorder import export_catalog

ROOT = Path(__file__).parents[3]
CHECKED_IN = ROOT / "examples" / "ecommerce" / "flight-recorder" / "traces"


@pytest.mark.asyncio
async def test_exported_flight_recorder_catalog_is_byte_reproducible(tmp_path: Path) -> None:
    await export_catalog(tmp_path)

    expected = sorted(path.name for path in CHECKED_IN.glob("*.json"))
    assert expected == [
        "business-failure.json",
        "compensation-failure.json",
        "happy-path.json",
        "index.json",
        "lost-response.json",
    ]
    for name in expected:
        assert (tmp_path / name).read_bytes() == (CHECKED_IN / name).read_bytes()
