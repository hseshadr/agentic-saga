from __future__ import annotations

import asyncio
from collections.abc import Callable
from hashlib import sha256
from itertools import count
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from examples.ecommerce.demo import run_scenario
from examples.ecommerce.domain import ScenarioName, StrictModel

TRACE_ROOT = Path(__file__).with_name("flight-recorder") / "traces"


class CatalogEntry(StrictModel):
    id: ScenarioName
    name: str
    summary: str
    mode: Literal["scripted"]
    trace_ref: str
    trace_sha256: str


class Catalog(StrictModel):
    schema_version: Literal["1.0"]
    runs: tuple[CatalogEntry, ...]


async def export_catalog(destination: Path, source: Path = TRACE_ROOT) -> None:
    source_json = await asyncio.to_thread((source / "index.json").read_text)
    catalog = Catalog.model_validate_json(source_json)
    await asyncio.to_thread(destination.mkdir, parents=True, exist_ok=True)
    entries = [await _export_entry(entry, destination) for entry in catalog.runs]
    rendered = catalog.model_copy(update={"runs": tuple(entries)}).model_dump_json(indent=2)
    await asyncio.to_thread((destination / "index.json").write_text, f"{rendered}\n")


async def _export_entry(entry: CatalogEntry, destination: Path) -> CatalogEntry:
    with TemporaryDirectory() as workspace:
        run = await run_scenario(
            entry.id, Path(workspace), claim_id_factory=_deterministic_claim_ids()
        )
    body = f"{run.trace.model_dump_json(indent=2)}\n".encode()
    trace_ref = f"{entry.id.value}.json"
    await asyncio.to_thread((destination / trace_ref).write_bytes, body)
    return entry.model_copy(
        update={"trace_ref": trace_ref, "trace_sha256": sha256(body).hexdigest()}
    )


def _deterministic_claim_ids() -> Callable[[], str]:
    sequence = count(1)
    return lambda: f"claim_{next(sequence):032x}"


def main() -> None:
    asyncio.run(export_catalog(TRACE_ROOT))


if __name__ == "__main__":
    main()
