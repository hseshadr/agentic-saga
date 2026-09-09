from __future__ import annotations

import asyncio
import sqlite3
import tempfile
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import cast

from pydantic import TypeAdapter

from agentic_saga.contracts.actions import AgentProposal, Escalate, Finish, ToolCall
from agentic_saga.contracts.common import JsonObject, canonical_json
from agentic_saga.contracts.runtime import (
    AgentDriver,
    SagaObservation,
    ToolDescriptor,
)

_OBSERVATION = TypeAdapter(SagaObservation)
_TOOL_SET = TypeAdapter(tuple[ToolDescriptor, ...])
_ACTIONS = TypeAdapter(tuple[str, ...])
_JSON_OBJECT: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
_CALL_QUERIES = {
    "observation_json": "SELECT observation_json FROM calls ORDER BY call_number",
    "descriptors_json": "SELECT descriptors_json FROM calls ORDER BY call_number",
}
_SCHEMA = """
CREATE TABLE configuration (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    actions_json BLOB NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_json BLOB NOT NULL,
    mode TEXT NOT NULL
);
CREATE TABLE calls (
    call_number INTEGER PRIMARY KEY,
    observation_json BLOB NOT NULL,
    descriptors_json BLOB NOT NULL
);
"""


@dataclass(frozen=True)
class _AgentConfiguration:
    actions: tuple[str, ...]
    tool_name: str
    arguments: JsonObject
    mode: str


@dataclass(frozen=True)
class DurableFakeAgent(AgentDriver):
    path: Path

    @classmethod
    def initialize(
        cls,
        actions: Sequence[str],
        *,
        tool_name: str = "observe_generic",
        arguments: JsonObject | None = None,
        mode: str = "scripted",
    ) -> DurableFakeAgent:
        root = Path(tempfile.mkdtemp(prefix="agentic-saga-agent-"))
        instance = cls(root / "agent.db")
        _initialize_database(instance.path, tuple(actions), tool_name, arguments or {}, mode)
        return instance

    async def next_action(
        self, observation: SagaObservation, available_tools: Sequence[ToolDescriptor]
    ) -> AgentProposal:
        return await _DurableDriver(self.path).next_action(observation, available_tools)

    @property
    def calls(self) -> int:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            row = connection.execute("SELECT COUNT(*) FROM calls").fetchone()
        return cast(int, row[0])

    @property
    def maximum_concurrent_calls(self) -> int:
        return min(self.calls, 1)

    @property
    def observations(self) -> list[SagaObservation]:
        payloads = _call_payloads(self.path, "observation_json")
        return [_OBSERVATION.validate_json(item, strict=True) for item in payloads]

    @property
    def tool_sets(self) -> list[tuple[ToolDescriptor, ...]]:
        payloads = _call_payloads(self.path, "descriptors_json")
        return [_TOOL_SET.validate_json(item, strict=True) for item in payloads]


def _initialize_database(
    path: Path,
    actions: tuple[str, ...],
    tool_name: str,
    arguments: JsonObject,
    mode: str,
) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(_SCHEMA)
        connection.execute(
            "INSERT INTO configuration VALUES (1, ?, ?, ?, ?)",
            (canonical_json(list(actions)), tool_name, canonical_json(arguments), mode),
        )


def _call_payloads(path: Path, column: str) -> tuple[bytes, ...]:
    query = _CALL_QUERIES.get(column)
    if query is None:
        raise ValueError("invalid durable agent column")
    with closing(sqlite3.connect(path)) as connection, connection:
        rows = connection.execute(query).fetchall()
    return tuple(cast(bytes, row[0]) for row in rows)


@dataclass(frozen=True)
class _DurableDriver(AgentDriver):
    path: Path

    async def next_action(
        self, observation: SagaObservation, available_tools: Sequence[ToolDescriptor]
    ) -> AgentProposal:
        config, call_number = _record_call(self.path, observation, available_tools)
        if config.mode == "block":
            return await _block_forever()
        if config.mode == "malformed":
            return cast(AgentProposal, {"authorization": "Bearer raw-secret"})
        if config.mode == "exception":
            raise KeyError("raw-provider-secret")
        return _scripted_action(config, call_number, observation.saga_seq, self.path)


def _record_call(
    path: Path,
    observation: SagaObservation,
    descriptors: Sequence[ToolDescriptor],
) -> tuple[_AgentConfiguration, int]:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        config = _load_config(connection)
        call_number = _next_call_number(connection)
        _insert_call(connection, call_number, observation, descriptors)
    return config, call_number


def _load_config(connection: sqlite3.Connection) -> _AgentConfiguration:
    row = connection.execute(
        "SELECT actions_json, tool_name, arguments_json, mode FROM configuration"
    ).fetchone()
    return _AgentConfiguration(
        actions=_ACTIONS.validate_json(cast(bytes, row[0]), strict=True),
        tool_name=cast(str, row[1]),
        arguments=_JSON_OBJECT.validate_json(cast(bytes, row[2]), strict=True),
        mode=cast(str, row[3]),
    )


def _next_call_number(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT COALESCE(MAX(call_number), 0) + 1 FROM calls").fetchone()
    return cast(int, row[0])


def _insert_call(
    connection: sqlite3.Connection,
    call_number: int,
    observation: SagaObservation,
    descriptors: Sequence[ToolDescriptor],
) -> None:
    connection.execute(
        "INSERT INTO calls VALUES (?, ?, ?)",
        (
            call_number,
            canonical_json(observation.model_dump(mode="json")),
            canonical_json([item.model_dump(mode="json") for item in descriptors]),
        ),
    )


def _scripted_action(
    config: _AgentConfiguration, call_number: int, saga_seq: int, path: Path
) -> AgentProposal:
    action = _selected_action(config.actions, call_number)
    proposal_id = _proposal_id(path, call_number)
    if action in {"read", "effect", "invalid"}:
        return _tool_call(config, action, proposal_id, saga_seq)
    if action == "finish":
        return Finish(
            proposal_id=proposal_id,
            based_on_saga_seq=saga_seq,
            rationale="Request verified completion.",
            target_status="succeeded_verified",
        )
    if action == "escalate":
        return _escalation(proposal_id, saga_seq)
    raise AssertionError("agent must not be called")


def _proposal_id(path: Path, call_number: int) -> str:
    identity = sha256(str(path).encode()).hexdigest()[:24]
    return f"proposal_runtime_{identity}_{call_number:08d}"


def _selected_action(actions: tuple[str, ...], call_number: int) -> str:
    if call_number > len(actions):
        return "forbidden"
    return actions[call_number - 1]


def _tool_call(
    config: _AgentConfiguration, action: str, proposal_id: str, saga_seq: int
) -> ToolCall:
    tool_name = "unregistered_generic_tool" if action == "invalid" else config.tool_name
    return ToolCall(
        proposal_id=proposal_id,
        tool_name=tool_name,
        arguments=config.arguments,
        based_on_saga_seq=saga_seq,
        rationale="Request one bounded tool operation.",
    )


def _escalation(proposal_id: str, saga_seq: int) -> Escalate:
    return Escalate(
        proposal_id=proposal_id,
        based_on_saga_seq=saga_seq,
        reason_code="operator_requested",
        rationale="A human decision is required.",
    )


async def _block_forever() -> AgentProposal:
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


__all__ = ["DurableFakeAgent"]
