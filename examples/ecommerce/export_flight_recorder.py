from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Literal, cast

from agentic_saga.contracts.common import JsonObject, sha256_json
from agentic_saga.contracts.trace import RunTrace
from examples.ecommerce.demo import run_fixture_scenario
from examples.ecommerce.domain import ScenarioName, StrictModel

TRACE_ROOT = Path(__file__).with_name("flight-recorder") / "traces"
_FIXTURE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


class CatalogEntry(StrictModel):
    id: ScenarioName
    name: str
    summary: str
    mode: Literal["scripted"]
    presentation: Literal["ecommerce"] | None = None
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
    run = await run_fixture_scenario(entry.id)
    trace = _stable_trace(run.trace)
    body = f"{trace.model_dump_json(indent=2)}\n".encode()
    trace_ref = f"{entry.id.value}.json"
    await asyncio.to_thread((destination / trace_ref).write_bytes, body)
    return entry.model_copy(
        update={"trace_ref": trace_ref, "trace_sha256": sha256(body).hexdigest()}
    )


def _stable_trace(trace: RunTrace) -> RunTrace:
    events = tuple(
        event.model_copy(update={"recorded_at": _FIXTURE_TIME + timedelta(seconds=event.saga_seq)})
        for event in trace.events
    )
    finished = None if trace.finished_at is None else events[-1].recorded_at
    return trace.model_copy(
        update={
            "events": events,
            "started_at": events[0].recorded_at,
            "finished_at": finished,
            "final_projection_hash": _stable_projection_hash(trace),
        }
    )


def _stable_projection_hash(trace: RunTrace) -> str:
    material = {
        "event_ids": [event.event_id for event in trace.events],
        "outcome": trace.outcome.value,
        "proofs": [proof.model_dump(mode="json") for proof in trace.proofs],
    }
    return sha256_json(cast(JsonObject, material))


def main() -> None:
    asyncio.run(export_catalog(TRACE_ROOT))


if __name__ == "__main__":
    main()
