from __future__ import annotations

import os
import secrets
import sqlite3
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from hashlib import sha256
from pathlib import Path
from typing import cast

from pydantic import TypeAdapter, ValidationError

from agentic_saga.contracts.actions import HumanDecision
from agentic_saga.contracts.clock import Clock
from agentic_saga.contracts.common import (
    Direction,
    JsonObject,
    OperationId,
    SagaId,
    canonical_json,
    thaw_json_object,
)
from agentic_saga.contracts.events import (
    CompensationIntentRecorded,
    CompensationStarted,
    DispatchAbortedBeforeEntry,
    DispatchStarted,
    EffectIntentRecorded,
    EffectOutcomeRecorded,
    HumanRequired,
    HumanResolutionRecorded,
    InvariantEvaluated,
    LedgerEvent,
    ReconciliationRecorded,
    TerminalAssigned,
)
from agentic_saga.contracts.outcomes import OutcomeUnknown, ReconcileUnsupported
from agentic_saga.contracts.tools import ToolCapabilities
from agentic_saga.kernel.failpoints import (
    DurabilityFailpoint,
    DurabilityPoint,
    NoOpDurabilityFailpoint,
)
from agentic_saga.kernel.identity import framed_sha256
from agentic_saga.kernel.policy import human_resolution_digest
from agentic_saga.kernel.ports import (
    ClaimedCommand,
    ClaimIdFactory,
    ClaimMetadata,
    ConnectionSettings,
    EffectCapabilityProof,
    Failpoint,
    HumanResolutionAuthenticationFailed,
    HumanResolutionInapplicable,
    HumanResolutionVerifier,
    Lease,
    LeaseLost,
    LeaseUnavailable,
    NoOpFailpoint,
    OutboxCommand,
    OutboxState,
    ReconciliationJob,
    ReconciliationJobState,
    RecoveryPolicy,
    RecoveryProofExpired,
    StaleFence,
    StoreConflict,
    StoreCorruption,
    StoreFailpoint,
    TransitionBatch,
    TransitionKind,
    TransitionReceipt,
    UnwindQuiescence,
    UnwindToolEvidence,
)
from agentic_saga.kernel.reducer import rebuild_projection, reduce_event
from agentic_saga.kernel.state import OperationRecord, OperationStatus, SagaSnapshot

_SCHEMA_VERSION = 2
_BUSY_TIMEOUT_MS = 5_000
_MAX_OWNER_LENGTH = 200
_MAX_LEASE_DURATION = timedelta(days=1)
_MAX_FENCE_TOKEN = 9_223_372_036_854_775_807
_PRIVATE_FILE_MODE = 0o600
_SHARED_WRITE_MASK = 0o022
_HASH_DOMAIN = b"agentic-saga-ledger-v1"
_RECONCILIATION_HUMAN_REASON = "reconciliation_unsafe"
_POST_RECONCILIATION_HUMAN_REASON = "forward_work_requires_approval_after_reconciliation"
_EVENT_ADAPTER: TypeAdapter[LedgerEvent] = TypeAdapter(LedgerEvent)
_JSON_OBJECT_ADAPTER: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
_TRANSITION_ADAPTER: TypeAdapter[TransitionBatch] = TypeAdapter(TransitionBatch)
_REQUIRED_SCHEMA_OBJECTS = frozenset(
    {
        ("table", "sagas"),
        ("table", "ledger_events"),
        ("table", "outbox_commands"),
        ("table", "transition_receipts"),
        ("table", "reconciliation_jobs"),
        ("index", "ledger_events_replay"),
        ("index", "outbox_runnable_scan"),
        ("index", "outbox_expired_claim_scan"),
        ("index", "reconciliation_due_scan"),
        ("index", "reconciliation_expired_claim_scan"),
        ("trigger", "ledger_events_no_update"),
        ("trigger", "ledger_events_no_delete"),
        ("trigger", "transition_receipts_no_update"),
        ("trigger", "transition_receipts_no_delete"),
        ("trigger", "outbox_claim_fence_guard"),
        ("trigger", "sagas_fence_never_decreases"),
    }
)
_OUTBOX_LIFECYCLE: Mapping[OperationStatus, frozenset[OutboxState]] = {
    OperationStatus.INTENT_DURABLE: frozenset(
        {OutboxState.RUNNABLE, OutboxState.CLAIMED, OutboxState.SUSPENDED_FOR_HUMAN}
    ),
    OperationStatus.DISPATCHED: frozenset({OutboxState.CLAIMED}),
    OperationStatus.EFFECT_CONFIRMED: frozenset({OutboxState.COMPLETED}),
    OperationStatus.NO_EFFECT_CONFIRMED: frozenset({OutboxState.COMPLETED}),
    OperationStatus.PARTIAL_EFFECT_CONFIRMED: frozenset({OutboxState.COMPLETED}),
    OperationStatus.OUTCOME_UNKNOWN: frozenset({OutboxState.PARKED}),
}
_OUTBOX_SELECT = (
    "SELECT o.command_id, o.saga_id, o.operation_id, o.tool_name, "
    "o.definition_version, o.command_schema_version, o.step_instance_id, o.direction, "
    "o.semantic_generation, o.command_json, o.command_hash, o.available_at, o.state, "
    "o.claim_id, o.claim_owner, o.claim_expires_at, o.claim_generation, "
    "o.claim_fence_token, o.delivery_attempt, o.suspended_at_seq, s.fence_token "
    "FROM outbox_commands o "
    "JOIN sagas s ON s.saga_id = o.saga_id "
)
_CLAIMABLE_OUTBOX_SELECT = (
    _OUTBOX_SELECT + "WHERE s.status != 'human_required' AND "
    "((o.state = 'runnable' AND o.available_at <= ?) "
    "OR (o.state = 'claimed' AND o.claim_expires_at <= ?)) "
    "ORDER BY o.available_at, o.command_id LIMIT 1"
)
_CLAIMABLE_SAGA_OUTBOX_SELECT = (
    _OUTBOX_SELECT + "WHERE o.saga_id = ? AND s.status != 'human_required' AND "
    "((o.state = 'runnable' AND o.available_at <= ?) "
    "OR (o.state = 'claimed' AND o.claim_expires_at <= ?)) "
    "ORDER BY o.available_at, o.command_id LIMIT 1"
)
_RECONCILIATION_SELECT = (
    "SELECT job_id, command_id, saga_id, operation_id, state, due_at, "
    "first_dispatch_at, claim_id, claim_owner, claim_expires_at, claim_generation, "
    "claim_fence_token, recovery_policy_json, recovery_policy_digest, claimed_at, "
    "lookup_attempt, lookup_started_at "
    "FROM reconciliation_jobs "
)
_CLAIMABLE_RECONCILIATION_SELECT = (
    "SELECT job_id, command_id, saga_id, operation_id, state, due_at, "
    "first_dispatch_at, claim_id, claim_owner, claim_expires_at, claim_generation, "
    "claim_fence_token, recovery_policy_json, recovery_policy_digest, claimed_at, "
    "lookup_attempt, lookup_started_at "
    "FROM reconciliation_jobs "
    "WHERE (((state IN ('due', 'waiting')) AND due_at <= ?) "
    "OR (state = 'claimed' AND claim_expires_at <= ?)) "
    "AND EXISTS (SELECT 1 FROM outbox_commands o JOIN sagas s ON s.saga_id = o.saga_id "
    "WHERE o.command_id = reconciliation_jobs.command_id AND o.state = 'parked' "
    "AND s.status = 'reconciling_unknown') ORDER BY due_at, job_id LIMIT 1"
)


@dataclass(frozen=True)
class _SagaRow:
    snapshot: SagaSnapshot
    seq: int
    fence_token: int
    lease_owner: str | None
    lease_expires_at: datetime | None


@dataclass(frozen=True)
class _EventRow:
    event_id: str
    saga_id: str
    saga_seq: int
    event_type: str
    event_bytes: bytes
    event_hash: str
    prior_hash: str | None


@dataclass(frozen=True)
class _Receipt:
    transition_id: str
    saga_id: str
    expected_seq: int
    resulting_seq: int
    payload_digest: str
    request_digest: str | None
    kind: TransitionKind
    projection_bytes: bytes
    transition_bytes: bytes


@dataclass(frozen=True)
class _OutboxRow:
    command_id: str
    saga_id: str
    operation_id: str
    tool_name: str
    definition_version: str
    command_schema_version: str
    step_instance_id: str
    direction: Direction
    semantic_generation: int
    command: JsonObject
    command_hash: str
    available_at: datetime
    state: OutboxState
    claim_id: str | None
    claim_owner: str | None
    claim_expires_at: datetime | None
    claim_generation: int
    claim_fence_token: int
    delivery_attempt: int
    suspended_at_seq: int | None
    saga_fence_token: int


@dataclass(frozen=True)
class _CommitResult:
    snapshot: SagaSnapshot
    created: bool


@dataclass(frozen=True)
class _TransitionWrite:
    batch: TransitionBatch
    digest: str
    now: datetime
    request_digest: str | None = None
    kind: TransitionKind = TransitionKind.STANDARD
    claimed_lifecycle: bool = False
    human_decision: HumanDecision | None = None


@dataclass(frozen=True)
class _CommitRequest:
    batch: TransitionBatch
    digest: str
    request_digest: str | None
    kind: TransitionKind
    human_decision: HumanDecision | None


def _semantic_precommit_point(request: _CommitRequest) -> DurabilityPoint | None:
    if request.kind is TransitionKind.TERMINAL:
        return DurabilityPoint.BEFORE_TERMINAL_COMMIT
    if any(isinstance(event, EffectIntentRecorded) for event in request.batch.events):
        return DurabilityPoint.BEFORE_INTENT_COMMIT
    return None


@dataclass(frozen=True)
class _ReceiptRetry:
    batch: TransitionBatch
    digest: str
    request_digest: str | None
    kind: TransitionKind


@dataclass(frozen=True)
class _Claim:
    claim_id: str
    owner: str
    expires: str
    generation: int
    attempt: int


@dataclass(frozen=True)
class _Watermark:
    saga_id: str
    saga_seq: int
    projection_digest: str
    event_hash: str


@dataclass(frozen=True)
class _FileIdentity:
    device: int
    inode: int


@dataclass(frozen=True)
class _LeaseRequest:
    saga_id: SagaId
    owner: str
    now: datetime
    duration: timedelta


@dataclass(frozen=True)
class _RequiredColumns:
    table: str
    names: tuple[str, ...]


_REQUIRED_COLUMNS = (
    _RequiredColumns(
        "sagas",
        (
            "saga_id",
            "saga_seq",
            "status",
            "definition_version",
            "projection_json",
            "fence_token",
            "lease_owner",
            "lease_expires_at",
        ),
    ),
    _RequiredColumns(
        "ledger_events",
        (
            "event_id",
            "saga_id",
            "saga_seq",
            "event_type",
            "event_json",
            "event_hash",
            "prior_hash",
        ),
    ),
    _RequiredColumns(
        "outbox_commands",
        (
            "command_id",
            "saga_id",
            "operation_id",
            "tool_name",
            "definition_version",
            "command_schema_version",
            "step_instance_id",
            "direction",
            "semantic_generation",
            "command_json",
            "command_hash",
            "capabilities_json",
            "capability_digest",
            "state",
            "available_at",
            "claim_id",
            "claim_owner",
            "claim_expires_at",
            "claim_generation",
            "claim_fence_token",
            "delivery_attempt",
            "suspended_at_seq",
        ),
    ),
    _RequiredColumns(
        "transition_receipts",
        (
            "transition_id",
            "saga_id",
            "expected_seq",
            "resulting_seq",
            "payload_digest",
            "request_digest",
            "transition_kind",
            "projection_json",
            "transition_json",
        ),
    ),
    _RequiredColumns(
        "reconciliation_jobs",
        (
            "job_id",
            "command_id",
            "saga_id",
            "operation_id",
            "state",
            "due_at",
            "first_dispatch_at",
            "claim_id",
            "claim_owner",
            "claim_expires_at",
            "claim_generation",
            "claim_fence_token",
            "recovery_policy_json",
            "recovery_policy_digest",
            "claimed_at",
            "lookup_attempt",
            "lookup_started_at",
        ),
    ),
)


def _canonical_bytes(value: object) -> bytes:
    return canonical_json(value)


def _event_digest(event: LedgerEvent, prior_hash: str | None, event_bytes: bytes) -> str:
    components = (
        event.saga_id.encode(),
        str(event.saga_seq).encode(),
        (prior_hash or "").encode(),
        event_bytes,
    )
    return framed_sha256(_HASH_DOMAIN, *components)


def _json_digest(value: object) -> str:
    return sha256(_canonical_bytes(value)).hexdigest()


def _event_bytes(event: LedgerEvent) -> bytes:
    return _canonical_bytes(event.model_dump(mode="json"))


def _snapshot_bytes(snapshot: SagaSnapshot) -> bytes:
    return _canonical_bytes(snapshot.model_dump(mode="json"))


def _transition_bytes(batch: TransitionBatch) -> bytes:
    return _canonical_bytes(batch.model_dump(mode="json"))


def _batch_digest(batch: TransitionBatch) -> str:
    return sha256(_transition_bytes(batch)).hexdigest()


def _as_str(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise StoreCorruption(f"{field} is not text")
    return value


def _as_optional_str(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _as_str(value, field)


def _as_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StoreCorruption(f"{field} is not an integer")
    return value


def _as_optional_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    return _as_int(value, field)


def _as_bytes(value: object, field: str) -> bytes:
    if not isinstance(value, bytes):
        raise StoreCorruption(f"{field} is not bytes")
    return value


def _utc_text(value: datetime) -> str:
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("time must use UTC")
    return value.isoformat()


def _require_owner(owner: str) -> None:
    if not 1 <= len(owner) <= _MAX_OWNER_LENGTH:
        raise ValueError("owner must contain between 1 and 200 characters")


def _require_lease_duration(value: timedelta) -> None:
    if value <= timedelta(0):
        raise ValueError("lease duration must be positive")
    if value > _MAX_LEASE_DURATION:
        raise ValueError("lease duration must be bounded to one day")
    if value.microseconds % 1_000:
        raise ValueError("lease duration must use whole milliseconds")


def _secure_claim_id() -> str:
    return f"claim_{secrets.token_hex(16)}"


def _is_writer_contention(error: sqlite3.OperationalError) -> bool:
    code: object = getattr(error, "sqlite_errorcode", None)
    return code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}


def _begin_immediate(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as error:
        if _is_writer_contention(error):
            raise StoreConflict("SQLite writer is busy; retry with bounded backoff") from error
        raise StoreCorruption("SQLite write transaction could not begin") from error


@contextmanager
def _immediate(connection: sqlite3.Connection) -> Iterator[None]:
    _begin_immediate(connection)
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


@contextmanager
def _stable_read(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN")
    try:
        yield
    finally:
        connection.rollback()


def _schema_sql() -> str:
    return Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")


def _parent_details(path: Path) -> os.stat_result:
    if os.name != "posix":
        raise StoreConflict("SQLite storage requires POSIX file security")
    try:
        return path.parent.stat(follow_symlinks=False)
    except OSError as error:
        raise StoreConflict("database parent directory cannot be inspected") from error


def _validate_parent_details(details: os.stat_result) -> None:
    if not stat.S_ISDIR(details.st_mode):
        raise StoreConflict("database parent directory does not exist")
    if details.st_uid != os.geteuid() or details.st_mode & _SHARED_WRITE_MASK:
        raise StoreConflict("database parent directory must be owned and not shared-writable")


def _require_secure_parent(path: Path) -> None:
    _validate_parent_details(_parent_details(path))


def _file_identity(details: os.stat_result) -> _FileIdentity:
    return _FileIdentity(details.st_dev, details.st_ino)


def _harden_file_descriptor(descriptor: int, path: Path) -> _FileIdentity:
    details = os.fstat(descriptor)
    if not stat.S_ISREG(details.st_mode):
        raise StoreConflict(f"storage path is not a regular file: {path}")
    if details.st_uid != os.geteuid():
        raise StoreConflict(f"storage file is not owned by this user: {path}")
    os.fchmod(descriptor, _PRIVATE_FILE_MODE)
    secured = os.fstat(descriptor)
    if stat.S_IMODE(secured.st_mode) != _PRIVATE_FILE_MODE:
        raise StoreConflict(f"storage file permissions cannot be made private: {path}")
    return _file_identity(secured)


def _open_new_file(path: Path) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        return os.open(path, flags, _PRIVATE_FILE_MODE)
    except FileExistsError as error:
        raise StoreConflict(f"database already exists: {path}") from error
    except OSError as error:
        raise StoreConflict(f"database cannot be created safely: {path}") from error


def _open_existing_file(path: Path) -> int:
    flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        return os.open(path, flags)
    except FileNotFoundError as error:
        raise StoreConflict(f"database does not exist: {path}") from error
    except OSError as error:
        raise StoreConflict(f"storage path is not a safe regular file: {path}") from error


def _secure_descriptor(descriptor: int, path: Path) -> _FileIdentity:
    try:
        return _harden_file_descriptor(descriptor, path)
    except OSError as error:
        raise StoreConflict(f"storage file cannot be secured: {path}") from error
    finally:
        os.close(descriptor)


def _claim_private_file(path: Path) -> _FileIdentity:
    _require_secure_parent(path)
    return _secure_descriptor(_open_new_file(path), path)


def _secure_existing_file(path: Path) -> None:
    _require_secure_parent(path)
    _secure_descriptor(_open_existing_file(path), path)


def _secure_optional_file(path: Path) -> None:
    _require_secure_parent(path)
    flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return
    except OSError as error:
        raise StoreConflict(f"storage path is not a safe regular file: {path}") from error
    _secure_descriptor(descriptor, path)


def _leaf_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise StoreConflict(f"storage path cannot be inspected: {path}") from error
    return True


def _sidecar_paths(path: Path) -> tuple[Path, Path]:
    return Path(f"{path}-wal"), Path(f"{path}-shm")


def _secure_database_files(path: Path) -> None:
    _secure_existing_file(path)
    for sidecar in _sidecar_paths(path):
        _secure_optional_file(sidecar)


def _configure(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA busy_timeout = 5000")


def _prepare_connection(connection: sqlite3.Connection, path: Path, require_schema: bool) -> None:
    try:
        _configure(connection)
        _check_schema_version(connection, require_schema)
        _secure_database_files(path)
    except StoreConflict:
        connection.close()
        raise
    except sqlite3.DatabaseError as error:
        connection.close()
        raise StoreCorruption("database cannot be opened safely") from error


def _sqlite_rw_uri(path: Path) -> str:
    return f"{path.as_uri()}?mode=rw"


def _connect(path: Path, *, require_schema: bool) -> sqlite3.Connection:
    _secure_database_files(path)
    try:
        connection = sqlite3.connect(
            _sqlite_rw_uri(path), timeout=5.0, isolation_level=None, uri=True
        )
    except sqlite3.DatabaseError as error:
        raise StoreCorruption("database cannot be opened safely") from error
    _prepare_connection(connection, path, require_schema)
    return connection


def _check_schema_version(connection: sqlite3.Connection, required: bool) -> None:
    version = _pragma_int(connection, "PRAGMA user_version", "schema version")
    if required and version != _SCHEMA_VERSION:
        connection.close()
        raise StoreCorruption(f"unsupported schema version {version}")


def _pragma_int(connection: sqlite3.Connection, sql: str, field: str) -> int:
    row = connection.execute(sql).fetchone()
    if row is None:
        raise StoreCorruption(f"missing {field}")
    return _as_int(cast(tuple[object, ...], row)[0], field)


def _pragma_str(connection: sqlite3.Connection, sql: str, field: str) -> str:
    row = connection.execute(sql).fetchone()
    if row is None:
        raise StoreCorruption(f"missing {field}")
    return _as_str(cast(tuple[object, ...], row)[0], field)


def _integrity_status(connection: sqlite3.Connection) -> str:
    return _pragma_str(connection, "PRAGMA integrity_check", "integrity check")


def _foreign_key_errors(connection: sqlite3.Connection) -> tuple[str, ...]:
    rows = connection.execute("PRAGMA foreign_key_check").fetchall()
    return tuple(":".join(str(item) for item in row) for row in rows)


def _verify_integrity(connection: sqlite3.Connection) -> None:
    if _integrity_status(connection) != "ok":
        raise StoreCorruption("SQLite integrity check failed")
    if _foreign_key_errors(connection):
        raise StoreCorruption("SQLite foreign key check failed")


def _verify_required_schema(connection: sqlite3.Connection) -> None:
    if _schema_objects(connection) != _REQUIRED_SCHEMA_OBJECTS:
        raise StoreCorruption("database schema objects do not match required version")
    if _schema_definitions(connection) != _required_schema_definitions():
        raise StoreCorruption("database schema definitions do not match required version")
    for required in _REQUIRED_COLUMNS:
        if _column_names(connection, required.table) != required.names:
            raise StoreCorruption("database schema columns do not match required version")


def _schema_objects(connection: sqlite3.Connection) -> frozenset[tuple[str, str]]:
    rows = connection.execute(
        "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return frozenset(
        (_as_str(raw[0], "schema object type"), _as_str(raw[1], "schema object name"))
        for raw in cast(Sequence[Sequence[object]], rows)
    )


def _schema_definitions(connection: sqlite3.Connection) -> frozenset[tuple[str, str, str]]:
    rows = connection.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return frozenset(
        (
            _as_str(raw[0], "schema object type"),
            _as_str(raw[1], "schema object name"),
            _as_str(raw[2], "schema object SQL"),
        )
        for raw in cast(Sequence[Sequence[object]], rows)
    )


@cache
def _required_schema_definitions() -> frozenset[tuple[str, str, str]]:
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
        _configure(connection)
        connection.executescript(_schema_sql())
        return _schema_definitions(connection)


def _column_names(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    rows = connection.execute(
        "SELECT name FROM pragma_table_info(?) ORDER BY cid", (table,)
    ).fetchall()
    return tuple(_as_str(cast(tuple[object, ...], raw)[0], "column name") for raw in rows)


def _saga_ids(connection: sqlite3.Connection) -> tuple[str, ...]:
    rows = connection.execute("SELECT saga_id FROM sagas ORDER BY saga_id").fetchall()
    return tuple(_as_str(cast(tuple[object, ...], row)[0], "Saga ID") for row in rows)


def _parse_snapshot(value: bytes) -> SagaSnapshot:
    try:
        return SagaSnapshot.model_validate_json(value, strict=True)
    except ValidationError as error:
        raise StoreCorruption("stored projection is invalid") from error


def _parse_transition(value: bytes) -> TransitionBatch:
    try:
        batch = _TRANSITION_ADAPTER.validate_json(value, strict=True)
    except ValidationError as error:
        raise StoreCorruption("stored receipt transition is invalid") from error
    if _transition_bytes(batch) != value:
        raise StoreCorruption("stored receipt transition JSON is not canonical")
    return batch


def _parse_event(row: _EventRow) -> LedgerEvent:
    try:
        event = _EVENT_ADAPTER.validate_json(row.event_bytes, strict=True)
    except ValidationError as error:
        raise StoreCorruption("stored ledger event is invalid") from error
    _verify_event_row(row, event)
    return event


def _verify_event_row(row: _EventRow, event: LedgerEvent) -> None:
    actual = (event.event_id, event.saga_id, event.saga_seq, event.event_type)
    stored = (row.event_id, row.saga_id, row.saga_seq, row.event_type)
    if actual != stored:
        raise StoreCorruption("ledger row does not match event payload")
    if _event_bytes(event) != row.event_bytes:
        raise StoreCorruption("ledger event JSON is not canonical")


def _event_row(raw: Sequence[object]) -> _EventRow:
    return _EventRow(
        _as_str(raw[0], "event ID"),
        _as_str(raw[1], "Saga ID"),
        _as_int(raw[2], "Saga sequence"),
        _as_str(raw[3], "event type"),
        _as_bytes(raw[4], "event JSON"),
        _as_str(raw[5], "event hash"),
        _as_optional_str(raw[6], "prior hash"),
    )


def _verify_hash(row: _EventRow, event: LedgerEvent, prior_hash: str | None) -> None:
    if row.prior_hash != prior_hash:
        raise StoreCorruption("ledger prior hash does not match chain")
    if row.event_hash != _event_digest(event, prior_hash, row.event_bytes):
        raise StoreCorruption("ledger event hash does not match exact bytes")


def _decode_events(rows: Sequence[Sequence[object]]) -> tuple[LedgerEvent, ...]:
    events: list[LedgerEvent] = []
    prior_hash: str | None = None
    for raw in rows:
        row = _event_row(raw)
        event = _parse_event(row)
        _verify_hash(row, event, prior_hash)
        events.append(event)
        prior_hash = row.event_hash
    return tuple(events)


def _read_events(connection: sqlite3.Connection, saga_id: SagaId) -> tuple[LedgerEvent, ...]:
    rows = connection.execute(
        "SELECT event_id, saga_id, saga_seq, event_type, event_json, event_hash, prior_hash "
        "FROM ledger_events WHERE saga_id = ? ORDER BY saga_seq",
        (saga_id,),
    ).fetchall()
    return _decode_events(cast(Sequence[Sequence[object]], rows))


def _rebuild_events(events: tuple[LedgerEvent, ...]) -> SagaSnapshot:
    try:
        return rebuild_projection(events)
    except (TypeError, ValueError) as error:
        raise StoreCorruption("ledger replay failed") from error


def _verify_current_projection(
    connection: sqlite3.Connection, saga_id: SagaId, stored: SagaSnapshot
) -> None:
    replayed = _rebuild_events(_read_events(connection, saga_id))
    if replayed != stored:
        raise StoreCorruption("current projection does not match ledger replay")


def _contains_redacted(value: object) -> bool:
    checker = _REDACTION_CHECKERS.get(type(value), _is_redaction_scalar)
    return checker(value)


def _mapping_contains_redacted(value: object) -> bool:
    mapping = cast(Mapping[object, object], value)
    return any(_contains_redacted(item) for item in mapping.values())


def _sequence_contains_redacted(value: object) -> bool:
    return any(_contains_redacted(item) for item in cast(list[object], value))


def _is_redaction_scalar(value: object) -> bool:
    return value == "[REDACTED]"


type _RedactionChecker = Callable[[object], bool]
_REDACTION_CHECKERS: Mapping[type[object], _RedactionChecker] = {
    dict: _mapping_contains_redacted,
    list: _sequence_contains_redacted,
}


def _deny_human_resolution(decision: HumanDecision, snapshot: SagaSnapshot) -> bool:
    del decision, snapshot
    return False


class SQLiteKernelStore:
    """Single-host durable SQLite ledger requiring a private, owner-controlled parent."""

    def __init__(  # noqa: PLR0913, PLR0917
        self,
        path: Path,
        failpoint: Failpoint | None = None,
        claim_id_factory: ClaimIdFactory | None = None,
        clock: Clock | None = None,
        human_resolution_verifier: HumanResolutionVerifier | None = None,
        durability_failpoint: DurabilityFailpoint | None = None,
    ) -> None:
        self._path = path.absolute()
        self._failpoint: Failpoint = failpoint or NoOpFailpoint()
        self._claim_id_factory = claim_id_factory or _secure_claim_id
        self._clock = clock
        self._human_resolution_verifier = human_resolution_verifier or _deny_human_resolution
        self._durability_failpoint = durability_failpoint or NoOpDurabilityFailpoint()

    @property
    def path(self) -> Path:
        return self._path

    @classmethod
    def initialize(  # noqa: PLR0913
        cls,
        path: Path,
        *,
        failpoint: Failpoint | None = None,
        claim_id_factory: ClaimIdFactory | None = None,
        clock: Clock | None = None,
        human_resolution_verifier: HumanResolutionVerifier | None = None,
        durability_failpoint: DurabilityFailpoint | None = None,
    ) -> SQLiteKernelStore:
        target = path.absolute()
        _claim_private_file(target)
        cls._install_schema(target)
        return cls.open(
            target,
            failpoint=failpoint,
            claim_id_factory=claim_id_factory,
            clock=clock,
            human_resolution_verifier=human_resolution_verifier,
            durability_failpoint=durability_failpoint,
        )

    @staticmethod
    def _install_schema(path: Path) -> None:
        if not path.parent.is_dir():
            raise StoreConflict("database parent directory does not exist")
        with closing(_connect(path, require_schema=False)) as connection:
            try:
                connection.executescript(_schema_sql())
            except sqlite3.DatabaseError as error:
                raise StoreCorruption("database schema initialization failed") from error

    @classmethod
    def open(  # noqa: PLR0913
        cls,
        path: Path,
        *,
        failpoint: Failpoint | None = None,
        claim_id_factory: ClaimIdFactory | None = None,
        clock: Clock | None = None,
        human_resolution_verifier: HumanResolutionVerifier | None = None,
        durability_failpoint: DurabilityFailpoint | None = None,
    ) -> SQLiteKernelStore:
        target = path.absolute()
        _secure_database_files(target)
        store = cls(
            target,
            failpoint,
            claim_id_factory,
            clock,
            human_resolution_verifier,
            durability_failpoint,
        )
        store._verify_database()
        return store

    @classmethod
    def restore_from(
        cls,
        source: Path,
        destination: Path,
        *,
        human_resolution_verifier: HumanResolutionVerifier | None = None,
    ) -> SQLiteKernelStore:
        source_store = cls.open(source)
        source_store.backup_to(destination)
        return cls.open(destination, human_resolution_verifier=human_resolution_verifier)

    def _connection(self) -> sqlite3.Connection:
        return _connect(self._path, require_schema=True)

    def connection_settings(self) -> ConnectionSettings:
        with closing(self._connection()) as connection:
            return ConnectionSettings(
                foreign_keys=_pragma_int(connection, "PRAGMA foreign_keys", "foreign keys") == 1,
                journal_mode=_pragma_str(connection, "PRAGMA journal_mode", "journal mode"),
                synchronous=_pragma_int(connection, "PRAGMA synchronous", "synchronous"),
                busy_timeout_ms=_pragma_int(connection, "PRAGMA busy_timeout", "busy timeout"),
                schema_version=_pragma_int(connection, "PRAGMA user_version", "schema version"),
            )

    def create_saga(self, first_event: LedgerEvent) -> SagaSnapshot:
        if first_event.fence_token is not None:
            raise StaleFence("Saga creation requires an empty bootstrap fence")
        snapshot = reduce_event(None, first_event)
        event_bytes = _event_bytes(first_event)
        try:
            self._create_saga_transaction(snapshot, first_event, event_bytes)
        except sqlite3.IntegrityError as error:
            raise StoreConflict(f"Saga already exists: {first_event.saga_id}") from error
        return snapshot

    def _create_saga_transaction(
        self, snapshot: SagaSnapshot, event: LedgerEvent, event_bytes: bytes
    ) -> None:
        with closing(self._connection()) as connection, _immediate(connection):
            self._insert_saga(connection, snapshot)
            digest = _event_digest(event, None, event_bytes)
            _execute_event_insert(connection, event, event_bytes, digest, None)

    @staticmethod
    def _insert_saga(connection: sqlite3.Connection, snapshot: SagaSnapshot) -> None:
        connection.execute(
            "INSERT INTO sagas "
            "(saga_id, saga_seq, status, definition_version, projection_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                snapshot.saga_id,
                snapshot.seq,
                snapshot.status.value,
                snapshot.definition_version,
                _snapshot_bytes(snapshot),
            ),
        )

    def commit_transition(self, batch: TransitionBatch) -> SagaSnapshot:
        return self._commit(batch, None, TransitionKind.STANDARD)

    def commit_proposal(self, batch: TransitionBatch, request_digest: str) -> SagaSnapshot:
        return self._commit(batch, request_digest, TransitionKind.PROPOSAL)

    def suspend_for_human(self, batch: TransitionBatch, request_digest: str) -> SagaSnapshot:
        return self._commit(batch, request_digest, TransitionKind.HUMAN_SUSPENSION)

    def commit_human_resolution(
        self, batch: TransitionBatch, decision: HumanDecision
    ) -> SagaSnapshot:
        digest = _canonical_human_resolution_digest(decision)
        return self._commit(
            batch,
            digest,
            TransitionKind.HUMAN_RESOLUTION,
            human_decision=decision,
        )

    def commit_terminal(self, batch: TransitionBatch, request_digest: str) -> SagaSnapshot:
        return self._commit(batch, request_digest, TransitionKind.TERMINAL)

    def _commit(
        self,
        batch: TransitionBatch,
        request_digest: str | None,
        kind: TransitionKind,
        *,
        human_decision: HumanDecision | None = None,
    ) -> SagaSnapshot:
        _verify_claimed_lifecycle_access(batch, allowed=False)
        request = _CommitRequest(batch, _batch_digest(batch), request_digest, kind, human_decision)
        return self._perform_commit(request)

    def _perform_commit(self, request: _CommitRequest) -> SagaSnapshot:
        try:
            result = self._commit_with_connection(request)
        except sqlite3.IntegrityError as error:
            raise StoreConflict("transition violated a durable identity constraint") from error
        if result.created:
            self._failpoint.hit(StoreFailpoint.AFTER_COMMIT_BEFORE_RETURN)
        return result.snapshot

    def _commit_with_connection(self, request: _CommitRequest) -> _CommitResult:
        with closing(self._connection()) as connection, _immediate(connection):
            receipt = self._read_receipt(connection, request.batch.transition_id)
            if receipt is not None:
                retry = _ReceiptRetry(
                    request.batch, request.digest, request.request_digest, request.kind
                )
                return self._retry_commit(connection, receipt, retry)
            write = self._transition_write(connection, request)
            snapshot = self._apply_transition(connection, write)
            self._hit_semantic_precommit(request)
            self._failpoint.hit(StoreFailpoint.BEFORE_COMMIT)
            return _CommitResult(snapshot, True)

    def _hit_semantic_precommit(self, request: _CommitRequest) -> None:
        point = _semantic_precommit_point(request)
        if point is not None:
            self._durability_failpoint.hit(point)

    def _retry_commit(
        self, connection: sqlite3.Connection, receipt: _Receipt, retry: _ReceiptRetry
    ) -> _CommitResult:
        snapshot = self._retry_snapshot(
            connection,
            receipt,
            retry.batch,
            digest=retry.digest,
            request_digest=retry.request_digest,
            kind=retry.kind,
        )
        return _CommitResult(snapshot, False)

    def _transition_write(
        self, connection: sqlite3.Connection, request: _CommitRequest
    ) -> _TransitionWrite:
        return _TransitionWrite(
            request.batch,
            request.digest,
            self._transaction_now(connection),
            request.request_digest,
            request.kind,
            human_decision=request.human_decision,
        )

    def _retry_snapshot(  # noqa: PLR0913
        self,
        connection: sqlite3.Connection,
        receipt: _Receipt,
        batch: TransitionBatch,
        *,
        digest: str,
        request_digest: str | None,
        kind: TransitionKind,
    ) -> SagaSnapshot:
        identity = (receipt.transition_id, receipt.saga_id, receipt.request_digest, receipt.kind)
        expected = (batch.transition_id, batch.saga_id, request_digest, kind)
        if identity != expected:
            raise StoreConflict("transition identity was reused with a different request")
        if kind is TransitionKind.STANDARD:
            return self._verify_receipt(receipt, batch, digest, request_digest, kind)
        _verify_stored_receipt(connection, receipt)
        return _parse_snapshot(receipt.projection_bytes)

    def _apply_transition(
        self,
        connection: sqlite3.Connection,
        write: _TransitionWrite,
    ) -> SagaSnapshot:
        batch = write.batch
        _verify_claimed_lifecycle_access(batch, allowed=write.claimed_lifecycle)
        row = self._load_saga_row(connection, batch.saga_id)
        self._verify_compare_and_swap(row, batch, write.now)
        _verify_current_projection(connection, batch.saga_id, row.snapshot)
        _verify_specialized_transition(
            connection, row.snapshot, write, self._human_resolution_verifier
        )
        projection = self._reduce_and_verify(row.snapshot, batch)
        self._persist_transition(connection, write, projection)
        return projection

    def _persist_transition(
        self, connection: sqlite3.Connection, write: _TransitionWrite, projection: SagaSnapshot
    ) -> None:
        batch = write.batch
        self._verify_outbox_commands(batch, projection)
        self._append_events(connection, batch.events)
        self._insert_outbox_commands(connection, batch.outbox_commands)
        _apply_specialized_transition(connection, batch, write.kind)
        self._update_projection(connection, batch, projection)
        self._insert_receipt(
            connection, batch, write.digest, projection, write.request_digest, write.kind
        )

    @staticmethod
    def _reduce_and_verify(snapshot: SagaSnapshot, batch: TransitionBatch) -> SagaSnapshot:
        projected = snapshot
        for event in batch.events:
            projected = reduce_event(projected, event)
        if projected != batch.projection:
            raise StoreConflict("supplied projection does not match independent replay")
        return projected

    def _verify_compare_and_swap(
        self, row: _SagaRow, batch: TransitionBatch, now: datetime
    ) -> None:
        if row.fence_token != batch.expected_fence_token:
            message = f"expected fence {batch.expected_fence_token}, found {row.fence_token}"
            raise StaleFence(message)
        if row.seq != batch.expected_seq:
            raise StoreConflict(f"expected Saga sequence {batch.expected_seq}, found {row.seq}")
        self._verify_lease(row, batch, now)
        _verify_event_fences(batch)

    @staticmethod
    def _verify_lease(row: _SagaRow, batch: TransitionBatch, now: datetime) -> None:
        if row.fence_token == 0:
            _verify_bootstrap_lease(batch)
            return
        _verify_active_lease(row, batch, now)

    @staticmethod
    def _verify_outbox_commands(batch: TransitionBatch, projection: SagaSnapshot) -> None:
        _verify_intent_command_cardinality(batch)
        for command in batch.outbox_commands:
            if command.saga_id != batch.saga_id:
                raise StoreConflict("outbox command belongs to another Saga")
            operation = projection.operations.get(command.operation_id)
            if operation is None:
                raise StoreConflict("outbox command has no durable operation")
            _verify_command(operation, command)

    def _append_events(
        self, connection: sqlite3.Connection, events: tuple[LedgerEvent, ...]
    ) -> None:
        prior_hash = self._last_event_hash(connection, events[0].saga_id)
        for event in events:
            event_bytes = _event_bytes(event)
            prior_hash = self._insert_ledger_event(connection, event, event_bytes, prior_hash)

    def _insert_ledger_event(
        self,
        connection: sqlite3.Connection,
        event: LedgerEvent,
        event_bytes: bytes,
        prior_hash: str | None,
    ) -> str:
        digest = _event_digest(event, prior_hash, event_bytes)
        self._failpoint.hit(StoreFailpoint.BEFORE_EVENT_INSERT)
        _execute_event_insert(connection, event, event_bytes, digest, prior_hash)
        self._failpoint.hit(StoreFailpoint.AFTER_EVENT_INSERT)
        return digest

    @staticmethod
    def _last_event_hash(connection: sqlite3.Connection, saga_id: SagaId) -> str | None:
        row = connection.execute(
            "SELECT event_hash FROM ledger_events WHERE saga_id = ? ORDER BY saga_seq DESC LIMIT 1",
            (saga_id,),
        ).fetchone()
        if row is None:
            return None
        return _as_str(cast(tuple[object, ...], row)[0], "event hash")

    def _insert_outbox_commands(
        self, connection: sqlite3.Connection, commands: tuple[OutboxCommand, ...]
    ) -> None:
        for command in commands:
            self._failpoint.hit(StoreFailpoint.BEFORE_OUTBOX_INSERT)
            _execute_outbox_insert(connection, command)
            self._failpoint.hit(StoreFailpoint.AFTER_OUTBOX_INSERT)

    def _update_projection(
        self, connection: sqlite3.Connection, batch: TransitionBatch, snapshot: SagaSnapshot
    ) -> None:
        self._failpoint.hit(StoreFailpoint.BEFORE_PROJECTION_UPDATE)
        cursor = _execute_projection_update(connection, batch, snapshot)
        if cursor.rowcount != 1:
            raise StoreConflict("projection compare-and-swap lost during transition")
        self._failpoint.hit(StoreFailpoint.AFTER_PROJECTION_UPDATE)

    def _insert_receipt(  # noqa: PLR0913, PLR0917
        self,
        connection: sqlite3.Connection,
        batch: TransitionBatch,
        digest: str,
        snapshot: SagaSnapshot,
        request_digest: str | None,
        kind: TransitionKind,
    ) -> None:
        self._failpoint.hit(StoreFailpoint.BEFORE_RECEIPT_INSERT)
        _execute_receipt_insert(connection, batch, digest, snapshot, request_digest, kind)
        self._failpoint.hit(StoreFailpoint.AFTER_RECEIPT_INSERT)

    @staticmethod
    def _read_receipt(connection: sqlite3.Connection, transition_id: str) -> _Receipt | None:
        row = connection.execute(
            "SELECT transition_id, saga_id, expected_seq, resulting_seq, payload_digest, "
            "request_digest, transition_kind, projection_json, transition_json "
            "FROM transition_receipts WHERE transition_id = ?",
            (transition_id,),
        ).fetchone()
        if row is None:
            return None
        return _receipt_row(cast(Sequence[object], row))

    @staticmethod
    def _verify_receipt(
        receipt: _Receipt,
        batch: TransitionBatch,
        digest: str,
        request_digest: str | None = None,
        kind: TransitionKind = TransitionKind.STANDARD,
    ) -> SagaSnapshot:
        if (receipt.request_digest, receipt.kind) != (request_digest, kind):
            raise StoreConflict("transition identity was reused with a different request")
        if _receipt_retry_identity(receipt) != _batch_retry_identity(batch, digest):
            raise StoreConflict("transition identity was reused with a different payload")
        snapshot = _parse_snapshot(receipt.projection_bytes)
        if snapshot != batch.projection or receipt.resulting_seq != snapshot.seq:
            raise StoreCorruption("transition receipt projection does not match payload")
        return snapshot

    def lookup_transition_receipt(self, transition_id: str) -> TransitionReceipt | None:
        with closing(self._connection()) as connection, _stable_read(connection):
            receipt = self._read_receipt(connection, transition_id)
            if receipt is None:
                return None
            _verify_stored_receipt(connection, receipt)
            return _public_receipt(receipt)

    def load_snapshot(self, saga_id: SagaId) -> SagaSnapshot:
        with closing(self._connection()) as connection:
            return self._load_saga_row(connection, saga_id).snapshot

    @staticmethod
    def _load_saga_row(connection: sqlite3.Connection, saga_id: SagaId) -> _SagaRow:
        row = connection.execute(
            "SELECT projection_json, saga_seq, fence_token, lease_owner, lease_expires_at, "
            "status, definition_version FROM sagas WHERE saga_id = ?",
            (saga_id,),
        ).fetchone()
        if row is None:
            raise StoreConflict(f"Saga not found: {saga_id}")
        return _build_saga_row(cast(tuple[object, ...], row), saga_id)

    def read_events(self, saga_id: SagaId) -> tuple[LedgerEvent, ...]:
        with closing(self._connection()) as connection:
            return _read_events(connection, saga_id)

    def rebuild_and_verify(self, saga_id: SagaId) -> SagaSnapshot:
        with closing(self._connection()) as connection, _stable_read(connection):
            return self._rebuild_on_connection(connection, saga_id)

    def _rebuild_on_connection(
        self, connection: sqlite3.Connection, saga_id: SagaId
    ) -> SagaSnapshot:
        rebuilt = _rebuild_events(_read_events(connection, saga_id))
        if rebuilt != self._load_saga_row(connection, saga_id).snapshot:
            raise StoreCorruption("stored projection does not match ledger replay")
        return rebuilt

    def claim_outbox(self, owner: str, lease_duration: timedelta) -> ClaimedCommand | None:
        return self._claim_outbox(owner, lease_duration, None)

    def claim_outbox_for_saga(
        self, saga_id: SagaId, owner: str, lease_duration: timedelta
    ) -> ClaimedCommand | None:
        return self._claim_outbox(owner, lease_duration, saga_id)

    def _claim_outbox(
        self, owner: str, lease_duration: timedelta, saga_id: SagaId | None
    ) -> ClaimedCommand | None:
        _require_owner(owner)
        _require_lease_duration(lease_duration)
        with closing(self._connection()) as connection, _immediate(connection):
            now = self._transaction_now(connection)
            row = _select_claimable(connection, _utc_text(now), saga_id)
            if row is None:
                return None
            expires = _utc_text(_lease_expiry(now, lease_duration))
            return self._claim_row(connection, row, owner, expires)

    def _claim_row(
        self, connection: sqlite3.Connection, row: _OutboxRow, owner: str, expires: str
    ) -> ClaimedCommand:
        claim_id = self._claim_id_factory()
        generation = row.claim_generation + 1
        attempt = row.delivery_attempt + int(row.state is OutboxState.RUNNABLE)
        claim = _Claim(claim_id, owner, expires, generation, attempt)
        cursor = _update_claim(connection, row, claim)
        if cursor.rowcount != 1:
            raise StoreConflict("outbox claim compare-and-swap failed")
        return _claimed_with_proof(connection, row, claim, row.saga_fence_token)

    def complete_outbox(self, claimed: ClaimedCommand, batch: TransitionBatch) -> SagaSnapshot:
        return self._finish_outbox(claimed, OutboxState.COMPLETED, batch)

    def park_outbox(self, claimed: ClaimedCommand, batch: TransitionBatch) -> SagaSnapshot:
        return self._finish_outbox(claimed, OutboxState.PARKED, batch)

    def release_outbox(self, claimed: ClaimedCommand) -> None:
        with closing(self._connection()) as connection, _immediate(connection):
            now = self._transaction_now(connection)
            durable = _durable_claim(connection, claimed)
            self._release_claim(connection, durable, now)

    def validate_dispatch_authority(self, claimed: ClaimedCommand, lease: Lease) -> None:
        with closing(self._connection()) as connection, _immediate(connection):
            now = self._transaction_now(connection)
            durable = _durable_claim(connection, claimed)
            _verify_claim_owner(durable, lease.owner)
            _require_live_claim(connection, durable, now)
            _require_current_lease(self._load_saga_row(connection, claimed.saga_id), lease, now)

    def start_dispatch(self, claimed: ClaimedCommand, batch: TransitionBatch) -> SagaSnapshot:
        result = self._claimed_transition(claimed, batch, _verify_dispatch_batch)
        if result.created:
            self._failpoint.hit(StoreFailpoint.AFTER_COMMIT_BEFORE_RETURN)
        return result.snapshot

    def abort_dispatch(self, claimed: ClaimedCommand, batch: TransitionBatch) -> SagaSnapshot:
        result = self._abort_transition(claimed, batch)
        if result.created:
            self._failpoint.hit(StoreFailpoint.AFTER_COMMIT_BEFORE_RETURN)
        return result.snapshot

    def _claimed_transition(
        self,
        claimed: ClaimedCommand,
        batch: TransitionBatch,
        verify: Callable[[ClaimedCommand, TransitionBatch], None],
    ) -> _CommitResult:
        with closing(self._connection()) as connection, _immediate(connection):
            durable = _durable_claim(connection, claimed)
            verify(durable, batch)
            digest = _batch_digest(batch)
            receipt = self._read_receipt(connection, batch.transition_id)
            if receipt is not None:
                snapshot = self._verify_receipt(receipt, batch, digest)
                return _CommitResult(snapshot, False)
            now = self._transaction_now(connection)
            _require_live_claim(connection, durable, now)
            return self._apply_claimed_batch(connection, batch, now)

    def _abort_transition(self, claimed: ClaimedCommand, batch: TransitionBatch) -> _CommitResult:
        with closing(self._connection()) as connection, _immediate(connection):
            receipt = self._read_receipt(connection, batch.transition_id)
            if receipt is not None:
                return _retry_aborted_transition(receipt, claimed, batch)
            durable = _durable_claim(connection, claimed)
            _verify_abort_batch(durable, batch)
            now = self._transaction_now(connection)
            _require_live_claim(connection, durable, now)
            result = self._apply_claimed_batch(connection, batch, now)
            self._release_claim(connection, durable, now)
            return result

    def _apply_claimed_batch(
        self, connection: sqlite3.Connection, batch: TransitionBatch, now: datetime
    ) -> _CommitResult:
        digest = _batch_digest(batch)
        receipt = self._read_receipt(connection, batch.transition_id)
        if receipt is not None:
            return _CommitResult(self._verify_receipt(receipt, batch, digest), False)
        write = _TransitionWrite(batch, digest, now, claimed_lifecycle=True)
        snapshot = self._apply_transition(connection, write)
        self._failpoint.hit(StoreFailpoint.BEFORE_COMMIT)
        return _CommitResult(snapshot, True)

    def _release_claim(
        self, connection: sqlite3.Connection, claimed: ClaimedCommand, now: datetime
    ) -> None:
        self._failpoint.hit(StoreFailpoint.BEFORE_OUTBOX_UPDATE)
        cursor = _execute_outbox_release(connection, claimed, now)
        if cursor.rowcount != 1:
            self._raise_claim_conflict(connection, claimed, now)
        self._failpoint.hit(StoreFailpoint.AFTER_OUTBOX_UPDATE)

    def _finish_outbox(
        self, claimed: ClaimedCommand, state: OutboxState, batch: TransitionBatch
    ) -> SagaSnapshot:
        try:
            result = self._finish_with_connection(claimed, state, batch)
        except sqlite3.IntegrityError as error:
            raise StoreConflict("outbox finalization violated a durable constraint") from error
        if result.created:
            self._failpoint.hit(StoreFailpoint.AFTER_COMMIT_BEFORE_RETURN)
        return result.snapshot

    def _finish_with_connection(
        self, claimed: ClaimedCommand, state: OutboxState, batch: TransitionBatch
    ) -> _CommitResult:
        with closing(self._connection()) as connection, _immediate(connection):
            durable = _durable_claim(connection, claimed)
            now = self._transaction_now(connection)
            result = self._disposition_transition(connection, durable, state, batch, now)
            if not result.created:
                _verify_finished_claim(connection, durable, state)
                return result
            self._update_outbox_state(connection, durable, state, now)
            if state is OutboxState.PARKED:
                self._failpoint.hit(StoreFailpoint.BEFORE_RECONCILIATION_JOB_UPDATE)
                _ensure_reconciliation_job(connection, durable, now)
                self._failpoint.hit(StoreFailpoint.AFTER_RECONCILIATION_JOB_UPDATE)
            self._failpoint.hit(StoreFailpoint.BEFORE_COMMIT)
            return result

    def claim_reconciliation(
        self,
        owner: str,
        lease_duration: timedelta,
        recovery_policy: RecoveryPolicy | None = None,
        recovery_policy_digest: str | None = None,
    ) -> ReconciliationJob | None:
        _require_owner(owner)
        _require_lease_duration(lease_duration)
        policy, digest = _require_policy_pair(recovery_policy, recovery_policy_digest)
        with closing(self._connection()) as connection, _immediate(connection):
            now = self._transaction_now(connection)
            job = _select_reconciliation_job(connection, now)
            if job is None:
                return None
            _require_matching_policy(job, policy, digest)
            return self._claim_reconciliation_job(
                connection, job, owner, lease_duration, policy, digest
            )

    def _claim_reconciliation_job(  # noqa: PLR0913, PLR0917
        self,
        connection: sqlite3.Connection,
        job: ReconciliationJob,
        owner: str,
        duration: timedelta,
        policy: RecoveryPolicy,
        digest: str,
    ) -> ReconciliationJob:
        now = self._transaction_now(connection)
        claimed = _claimed_reconciliation(job, owner, now, duration, self._claim_id_factory())
        cursor = _update_reconciliation_claim(connection, job, claimed, policy, digest)
        if cursor.rowcount != 1:
            raise StoreConflict("reconciliation claim compare-and-swap failed")
        return claimed.model_copy(
            update={
                "recovery_policy": job.recovery_policy or policy,
                "recovery_policy_digest": job.recovery_policy_digest or digest,
            }
        )

    def claim_reconciliation_frozen(
        self, owner: str, lease_duration: timedelta
    ) -> ReconciliationJob | None:
        _require_owner(owner)
        _require_lease_duration(lease_duration)
        with closing(self._connection()) as connection, _immediate(connection):
            job = _select_reconciliation_job(connection, self._transaction_now(connection))
            if job is None:
                return None
            return self._claim_frozen_reconciliation(connection, job, owner, lease_duration)

    def _claim_frozen_reconciliation(
        self,
        connection: sqlite3.Connection,
        job: ReconciliationJob,
        owner: str,
        lease_duration: timedelta,
    ) -> ReconciliationJob:
        if job.recovery_policy is None or job.recovery_policy_digest is None:
            raise StoreConflict("reconciliation job has no frozen recovery policy")
        return self._claim_reconciliation_job(
            connection, job, owner, lease_duration, job.recovery_policy, job.recovery_policy_digest
        )

    def reconciliation_job(self, operation_id: str) -> ReconciliationJob | None:
        with closing(self._connection()) as connection:
            row = connection.execute(
                _RECONCILIATION_SELECT + "WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        return None if row is None else _reconciliation_job(cast(Sequence[object], row))

    def bind_reconciliation_claim(self, job: ReconciliationJob, lease: Lease) -> ReconciliationJob:
        with closing(self._connection()) as connection, _immediate(connection):
            now = self._transaction_now(connection)
            durable = _durable_reconciliation_claim(connection, job)
            _require_current_lease(self._load_saga_row(connection, job.saga_id), lease, now)
            if durable.claim_owner != lease.owner:
                raise StoreConflict("reconciliation claim owner does not match Saga lease")
            cursor = connection.execute(
                "UPDATE reconciliation_jobs SET claim_fence_token = ? WHERE job_id = ? "
                "AND state = 'claimed' AND claim_id = ? AND claim_generation = ?",
                (lease.fence_token, job.job_id, job.claim_id, job.claim_generation),
            )
            if cursor.rowcount != 1:
                raise StoreConflict("reconciliation claim changed before fence binding")
            return durable.model_copy(update={"claim_fence_token": lease.fence_token})

    def validate_reconciliation_authority(self, job: ReconciliationJob, lease: Lease) -> None:
        with closing(self._connection()) as connection, _immediate(connection):
            now = self._transaction_now(connection)
            durable = _durable_reconciliation_claim(connection, job)
            _require_reconciliation_authority(connection, durable, lease, now)

    def reconciliation_requires_human_followup(self, job: ReconciliationJob, lease: Lease) -> bool:
        with closing(self._connection()) as connection, _immediate(connection):
            now = self._transaction_now(connection)
            durable = _durable_reconciliation_claim(connection, job)
            _require_reconciliation_authority(connection, durable, lease, now)
            return bool(_suspended_outbox_count(connection, job.saga_id))

    def begin_reconciliation_lookup(
        self, job: ReconciliationJob, lease: Lease
    ) -> ReconciliationJob:
        with closing(self._connection()) as connection, _immediate(connection):
            now = self._transaction_now(connection)
            durable = _durable_reconciliation_claim(connection, job)
            _require_reconciliation_authority(connection, durable, lease, now)
            started = _started_reconciliation_lookup(durable, now)
            cursor = _record_reconciliation_lookup(connection, durable, started, now)
            if cursor.rowcount != 1:
                raise StoreConflict("reconciliation claim changed before provider entry")
            return started

    def release_reconciliation(self, job: ReconciliationJob) -> None:
        with closing(self._connection()) as connection, _immediate(connection):
            now = self._transaction_now(connection)
            durable = _durable_reconciliation_claim(connection, job)
            if durable.claim_expires_at is None or durable.claim_expires_at <= now:
                raise StoreConflict("reconciliation claim expired before release")
            cursor = _release_reconciliation_claim(
                connection, durable, _reconciliation_retry_due(now, durable)
            )
            if cursor.rowcount != 1:
                raise StoreConflict("reconciliation claim changed before release")

    def commit_reconciliation(
        self,
        job: ReconciliationJob,
        lease: Lease,
        batch: TransitionBatch,
        action: str,
        check_after: datetime | None,
    ) -> SagaSnapshot:
        try:
            result = self._commit_reconciliation_transaction(job, lease, batch, action, check_after)
        except sqlite3.IntegrityError as error:
            raise StoreConflict("reconciliation violated a durable constraint") from error
        if result.created:
            self._failpoint.hit(StoreFailpoint.AFTER_COMMIT_BEFORE_RETURN)
        return result.snapshot

    def _commit_reconciliation_transaction(
        self,
        job: ReconciliationJob,
        lease: Lease,
        batch: TransitionBatch,
        action: str,
        check_after: datetime | None,
    ) -> _CommitResult:
        with closing(self._connection()) as connection, _immediate(connection):
            receipt = self._read_receipt(connection, batch.transition_id)
            if receipt is not None:
                return _retry_reconciliation(connection, receipt, job, batch, action)
            return self._apply_reconciliation_commit(
                connection, job, lease, batch, action, check_after
            )

    def _apply_reconciliation_commit(  # noqa: PLR0913, PLR0917
        self,
        connection: sqlite3.Connection,
        job: ReconciliationJob,
        lease: Lease,
        batch: TransitionBatch,
        action: str,
        check_after: datetime | None,
    ) -> _CommitResult:
        now = self._transaction_now(connection)
        durable = _durable_reconciliation_claim(connection, job)
        _require_reconciliation_authority(connection, durable, lease, now)
        _verify_reconciliation_batch(connection, durable, batch, (action, check_after), now)
        write = _reconciliation_write(batch, durable, now)
        snapshot = self._apply_transition(connection, write)
        self._failpoint.hit(StoreFailpoint.BEFORE_RECONCILIATION_JOB_UPDATE)
        _apply_reconciliation_disposition(connection, durable, batch, action, check_after)
        self._failpoint.hit(StoreFailpoint.AFTER_RECONCILIATION_JOB_UPDATE)
        self._failpoint.hit(StoreFailpoint.BEFORE_COMMIT)
        return _CommitResult(snapshot, True)

    def _disposition_transition(
        self,
        connection: sqlite3.Connection,
        claimed: ClaimedCommand,
        state: OutboxState,
        batch: TransitionBatch,
        now: datetime,
    ) -> _CommitResult:
        digest = _batch_digest(batch)
        receipt = self._read_receipt(connection, batch.transition_id)
        if receipt is not None:
            snapshot = self._verify_receipt(receipt, batch, digest)
            _verify_claim_batch(claimed, batch, state)
            return _CommitResult(snapshot, False)
        _verify_claim_batch(claimed, batch, state)
        write = _TransitionWrite(batch, digest, now, claimed_lifecycle=True)
        return _CommitResult(self._apply_transition(connection, write), True)

    def _update_outbox_state(
        self,
        connection: sqlite3.Connection,
        claimed: ClaimedCommand,
        state: OutboxState,
        now: datetime,
    ) -> None:
        self._failpoint.hit(StoreFailpoint.BEFORE_OUTBOX_UPDATE)
        cursor = _execute_outbox_update(connection, claimed, state, now)
        if cursor.rowcount != 1:
            self._raise_claim_conflict(connection, claimed, now)
        self._failpoint.hit(StoreFailpoint.AFTER_OUTBOX_UPDATE)

    @staticmethod
    def _raise_claim_conflict(
        connection: sqlite3.Connection, claimed: ClaimedCommand, now: datetime
    ) -> None:
        row = connection.execute(
            "SELECT fence_token FROM sagas WHERE saga_id = ?", (claimed.saga_id,)
        ).fetchone()
        if row is not None and _as_int(cast(tuple[object, ...], row)[0], "fence") != (
            claimed.saga_fence_token
        ):
            raise StoreConflict("outbox claim has a stale Saga fence")
        if _claim_is_expired(connection, claimed, now):
            raise StoreConflict("outbox claim expired before finalization")
        raise StoreConflict("outbox claim identity or owner is stale")

    def runnable_count(self, saga_id: SagaId) -> int:
        with closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM outbox_commands WHERE saga_id = ? AND state = 'runnable'",
                (saga_id,),
            ).fetchone()
        if row is None:
            raise StoreCorruption("runnable count query returned no row")
        return _as_int(cast(tuple[object, ...], row)[0], "runnable count")

    def outbox_state(self, command_id: str) -> OutboxState:
        with closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT state FROM outbox_commands WHERE command_id = ?", (command_id,)
            ).fetchone()
        if row is None:
            raise StoreConflict(f"outbox command not found: {command_id}")
        return OutboxState(_as_str(cast(tuple[object, ...], row)[0], "outbox state"))

    def load_outbox(self, command_id: str) -> OutboxCommand:
        with closing(self._connection()) as connection:
            row = connection.execute(
                _OUTBOX_SELECT + "WHERE o.command_id = ?", (command_id,)
            ).fetchone()
            proof = _load_capability_proof(connection, command_id)
        if row is None:
            raise StoreConflict(f"outbox command not found: {command_id}")
        return _outbox_envelope(_outbox_row(cast(Sequence[object], row))).model_copy(
            update={"capability_proof": proof}
        )

    def acquire_lease(self, saga_id: SagaId, owner: str, duration: timedelta) -> Lease:
        _require_owner(owner)
        _require_lease_duration(duration)
        with closing(self._connection()) as connection, _immediate(connection):
            row = self._load_saga_row(connection, saga_id)
            now = self._transaction_now(connection)
            request = _LeaseRequest(saga_id, owner, now, duration)
            return self._acquire_lease_row(connection, row, request)

    def _acquire_lease_row(
        self,
        connection: sqlite3.Connection,
        row: _SagaRow,
        request: _LeaseRequest,
    ) -> Lease:
        if _lease_is_live(row, request.now):
            return self._renew_or_reject(connection, row, request)
        return self._take_lease(connection, row, request)

    def _renew_or_reject(
        self,
        connection: sqlite3.Connection,
        row: _SagaRow,
        request: _LeaseRequest,
    ) -> Lease:
        if row.lease_owner != request.owner:
            raise LeaseUnavailable("Saga lease is held by another owner")
        return self._renew_live_lease(connection, row, request)

    def _take_lease(
        self,
        connection: sqlite3.Connection,
        row: _SagaRow,
        request: _LeaseRequest,
    ) -> Lease:
        fence = _next_fence(row.fence_token)
        expires = _lease_expiry(request.now, request.duration)
        lease = Lease(
            saga_id=request.saga_id, owner=request.owner, fence_token=fence, expires_at=expires
        )
        return self._write_lease(connection, row, lease)

    def renew_lease(self, lease: Lease, duration: timedelta) -> Lease:
        _require_lease_duration(duration)
        with closing(self._connection()) as connection, _immediate(connection):
            row = self._load_saga_row(connection, lease.saga_id)
            now = self._transaction_now(connection)
            _require_current_lease(row, lease, now)
            request = _LeaseRequest(lease.saga_id, lease.owner, now, duration)
            return self._renew_live_lease(connection, row, request)

    def _renew_live_lease(
        self,
        connection: sqlite3.Connection,
        row: _SagaRow,
        request: _LeaseRequest,
    ) -> Lease:
        expires = _extended_expiry(row, request.now, request.duration)
        lease = Lease(
            saga_id=request.saga_id,
            owner=request.owner,
            fence_token=row.fence_token,
            expires_at=expires,
        )
        return self._write_live_lease(connection, row, lease, request.now)

    def _write_live_lease(
        self, connection: sqlite3.Connection, row: _SagaRow, lease: Lease, now: datetime
    ) -> Lease:
        self._failpoint.hit(StoreFailpoint.BEFORE_LEASE_UPDATE)
        cursor = _execute_live_lease_write(connection, row, lease, now)
        if cursor.rowcount != 1:
            raise LeaseLost("Saga lease changed before renewal")
        self._failpoint.hit(StoreFailpoint.AFTER_LEASE_UPDATE)
        return lease

    def release_lease(self, lease: Lease) -> None:
        with closing(self._connection()) as connection, _immediate(connection):
            row = self._load_saga_row(connection, lease.saga_id)
            now = self._transaction_now(connection)
            _require_current_lease(row, lease, now)
            self._clear_lease(connection, row, lease, now)

    def _clear_lease(
        self, connection: sqlite3.Connection, row: _SagaRow, lease: Lease, now: datetime
    ) -> None:
        self._failpoint.hit(StoreFailpoint.BEFORE_LEASE_UPDATE)
        cursor = _execute_lease_release(connection, row, lease, now)
        if cursor.rowcount != 1:
            raise LeaseLost("Saga lease identity is stale")
        self._failpoint.hit(StoreFailpoint.AFTER_LEASE_UPDATE)

    def _write_lease(
        self,
        connection: sqlite3.Connection,
        row: _SagaRow,
        lease: Lease,
    ) -> Lease:
        self._failpoint.hit(StoreFailpoint.BEFORE_LEASE_UPDATE)
        cursor = _execute_lease_write(connection, row, lease)
        if cursor.rowcount != 1:
            raise LeaseLost("Saga lease changed before renewal")
        self._failpoint.hit(StoreFailpoint.AFTER_LEASE_UPDATE)
        return lease

    def lease_state(self, saga_id: SagaId) -> Lease | None:
        with closing(self._connection()) as connection:
            row = self._load_saga_row(connection, saga_id)
        return _lease_state_from_row(saga_id, row)

    def _transaction_now(self, connection: sqlite3.Connection) -> datetime:
        if self._clock is None:
            return _database_now(connection)
        return _clock_time(self._clock.now())

    def current_fence(self, saga_id: SagaId) -> int:
        with closing(self._connection()) as connection:
            return self._load_saga_row(connection, saga_id).fence_token

    def inspect_unwind_quiescence(self, saga_id: SagaId, lease: Lease) -> UnwindQuiescence:
        if lease.saga_id != saga_id:
            raise LeaseLost("Saga lease identity is stale")
        with closing(self._connection()) as connection, _immediate(connection):
            row = self._load_saga_row(connection, saga_id)
            _require_current_lease(row, lease, self._transaction_now(connection))
            return _unwind_quiescence(connection, row, lease)

    def integrity_check(self) -> str:
        with closing(self._connection()) as connection:
            return _integrity_status(connection)

    def foreign_key_check(self) -> tuple[str, ...]:
        with closing(self._connection()) as connection:
            return _foreign_key_errors(connection)

    def _verify_database(self) -> None:
        with closing(self._connection()) as connection, _stable_read(connection):
            _verify_integrity(connection)
            _verify_required_schema(connection)
            self._verify_all_sagas(connection)
            self._verify_stored_outbox(connection)
            _verify_stored_receipts(connection)
            _verify_historical_claim_fences(connection)
            _verify_reconciliation_jobs(connection)

    def _verify_all_sagas(self, connection: sqlite3.Connection) -> None:
        for saga_id in _saga_ids(connection):
            self._rebuild_on_connection(connection, saga_id)

    @staticmethod
    def _verify_stored_outbox(connection: sqlite3.Connection) -> None:
        for raw in _stored_outbox_rows(connection):
            _verify_stored_outbox_row(connection, raw)
        _verify_all_outbox_cardinality(connection)

    def backup_to(self, destination: Path) -> None:
        target = destination.absolute()
        _validate_backup_destination(self._path, target)
        temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
        with _claimed_backup_file(temporary) as claimed:
            marks = self._backup_into(temporary)
            restored = SQLiteKernelStore.open(temporary)
            _verify_watermarks(restored, marks)
            _publish_new_backup(temporary, target, claimed)

    def _backup_into(self, destination: Path) -> tuple[_Watermark, ...]:
        with closing(self._connection()) as source:
            source.execute("BEGIN")
            marks = _watermarks(source)
            with closing(_connect(destination, require_schema=False)) as target:
                source.backup(target)
            source.rollback()
        return marks


def _execute_projection_update(
    connection: sqlite3.Connection, batch: TransitionBatch, snapshot: SagaSnapshot
) -> sqlite3.Cursor:
    return connection.execute(
        "UPDATE sagas SET saga_seq = ?, status = ?, projection_json = ? "
        "WHERE saga_id = ? AND saga_seq = ? AND fence_token = ?",
        (
            snapshot.seq,
            snapshot.status.value,
            _snapshot_bytes(snapshot),
            batch.saga_id,
            batch.expected_seq,
            batch.expected_fence_token,
        ),
    )


def _execute_lease_write(
    connection: sqlite3.Connection,
    row: _SagaRow,
    lease: Lease,
) -> sqlite3.Cursor:
    return connection.execute(
        "UPDATE sagas SET fence_token = ?, lease_owner = ?, lease_expires_at = ? "
        "WHERE saga_id = ? AND fence_token = ? AND lease_owner IS ? AND lease_expires_at IS ?",
        _lease_write_values(row, lease),
    )


def _execute_live_lease_write(
    connection: sqlite3.Connection, row: _SagaRow, lease: Lease, now: datetime
) -> sqlite3.Cursor:
    return connection.execute(
        "UPDATE sagas SET lease_expires_at = ? WHERE saga_id = ? AND fence_token = ? "
        "AND lease_owner = ? AND lease_expires_at = ? "
        "AND julianday(lease_expires_at) > julianday(?)",
        _live_lease_write_values(row, lease, now),
    )


def _lease_write_values(row: _SagaRow, lease: Lease) -> tuple[object, ...]:
    return (
        lease.fence_token,
        lease.owner,
        _utc_text(lease.expires_at),
        lease.saga_id,
        row.fence_token,
        row.lease_owner,
        _utc_text_or_none(row.lease_expires_at),
    )


def _live_lease_write_values(row: _SagaRow, lease: Lease, now: datetime) -> tuple[object, ...]:
    return (
        _utc_text(lease.expires_at),
        lease.saga_id,
        lease.fence_token,
        lease.owner,
        _utc_text_or_none(row.lease_expires_at),
        _utc_text(now),
    )


def _execute_lease_release(
    connection: sqlite3.Connection, row: _SagaRow, lease: Lease, now: datetime
) -> sqlite3.Cursor:
    return connection.execute(
        "UPDATE sagas SET lease_owner = NULL, lease_expires_at = NULL "
        "WHERE saga_id = ? AND fence_token = ? AND lease_owner = ? "
        "AND lease_expires_at = ? AND julianday(lease_expires_at) > julianday(?)",
        _lease_release_values(row, lease, now),
    )


def _lease_release_values(row: _SagaRow, lease: Lease, now: datetime) -> tuple[object, ...]:
    return (
        lease.saga_id,
        lease.fence_token,
        lease.owner,
        _utc_text_or_none(row.lease_expires_at),
        _utc_text(now),
    )


def _execute_receipt_insert(  # noqa: PLR0913, PLR0917
    connection: sqlite3.Connection,
    batch: TransitionBatch,
    digest: str,
    snapshot: SagaSnapshot,
    request_digest: str | None,
    kind: TransitionKind,
) -> None:
    connection.execute(
        "INSERT INTO transition_receipts "
        "(transition_id, saga_id, expected_seq, resulting_seq, payload_digest, request_digest, "
        "transition_kind, projection_json, transition_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        _receipt_insert_values(batch, digest, snapshot, request_digest, kind),
    )


def _receipt_insert_values(
    batch: TransitionBatch,
    digest: str,
    snapshot: SagaSnapshot,
    request_digest: str | None,
    kind: TransitionKind,
) -> tuple[object, ...]:
    return (
        batch.transition_id,
        batch.saga_id,
        batch.expected_seq,
        snapshot.seq,
        digest,
        request_digest,
        kind.value,
        _snapshot_bytes(snapshot),
        _transition_bytes(batch),
    )


def _receipt_row(raw: Sequence[object]) -> _Receipt:
    return _Receipt(
        _as_str(raw[0], "receipt transition ID"),
        _as_str(raw[1], "receipt Saga ID"),
        _as_int(raw[2], "receipt expected sequence"),
        _as_int(raw[3], "receipt resulting sequence"),
        _as_str(raw[4], "receipt payload digest"),
        _as_optional_str(raw[5], "receipt request digest"),
        TransitionKind(_as_str(raw[6], "receipt transition kind")),
        _as_bytes(raw[7], "receipt projection"),
        _as_bytes(raw[8], "receipt transition"),
    )


def _receipt_retry_identity(receipt: _Receipt) -> tuple[object, ...]:
    return (
        receipt.transition_id,
        receipt.saga_id,
        receipt.expected_seq,
        receipt.payload_digest,
        receipt.transition_bytes,
    )


def _batch_retry_identity(batch: TransitionBatch, digest: str) -> tuple[object, ...]:
    return (
        batch.transition_id,
        batch.saga_id,
        batch.expected_seq,
        digest,
        _transition_bytes(batch),
    )


def _verify_stored_receipts(connection: sqlite3.Connection) -> None:
    for saga_id in _saga_ids(connection):
        _verify_saga_receipts(connection, saga_id)


def _verify_saga_receipts(connection: sqlite3.Connection, saga_id: SagaId) -> None:
    rows = _receipt_rows(connection, saga_id)
    expected_seq = 1
    for raw in rows:
        receipt = _receipt_row(raw)
        if receipt.expected_seq != expected_seq:
            raise StoreCorruption("stored receipt coverage is overlapping or gapped")
        _verify_stored_receipt(connection, receipt)
        expected_seq = receipt.resulting_seq
    if expected_seq != SQLiteKernelStore._load_saga_row(connection, saga_id).seq:
        raise StoreCorruption("stored receipt coverage does not reach current Saga sequence")


def _receipt_rows(connection: sqlite3.Connection, saga_id: SagaId) -> Sequence[Sequence[object]]:
    rows = connection.execute(
        "SELECT transition_id, saga_id, expected_seq, resulting_seq, payload_digest, "
        "request_digest, transition_kind, projection_json, transition_json "
        "FROM transition_receipts WHERE saga_id = ? ORDER BY expected_seq, resulting_seq",
        (saga_id,),
    ).fetchall()
    return cast(Sequence[Sequence[object]], rows)


def _public_receipt(receipt: _Receipt) -> TransitionReceipt:
    batch = _parse_transition(receipt.transition_bytes)
    return TransitionReceipt(
        transition_id=receipt.transition_id,
        saga_id=receipt.saga_id,
        resulting_seq=receipt.resulting_seq,
        request_digest=receipt.request_digest,
        kind=receipt.kind,
        events=batch.events,
        projection=_parse_snapshot(receipt.projection_bytes),
    )


def _verify_stored_receipt(connection: sqlite3.Connection, receipt: _Receipt) -> None:
    batch = _parse_transition(receipt.transition_bytes)
    snapshot = _parse_snapshot(receipt.projection_bytes)
    events = _read_events(connection, receipt.saga_id)
    _verify_receipt_kind(receipt, batch)
    _verify_receipt_boundary(receipt, batch, snapshot)
    _verify_receipt_fence_evidence(batch)
    _verify_receipt_events(receipt, batch, snapshot, events)
    _verify_receipt_commands(connection, batch)


def _verify_receipt_kind(receipt: _Receipt, batch: TransitionBatch) -> None:
    if receipt.kind is TransitionKind.STANDARD:
        if receipt.request_digest is not None:
            raise StoreCorruption("standard receipt carries a request digest")
        return
    if receipt.request_digest is None:
        raise StoreCorruption("specialized receipt lacks a request digest")
    _verify_specialized_receipt_kind(receipt.kind, batch)
    _verify_human_resolution_receipt_digest(receipt, batch)


def _verify_human_resolution_receipt_digest(receipt: _Receipt, batch: TransitionBatch) -> None:
    if receipt.kind is not TransitionKind.HUMAN_RESOLUTION:
        return
    event = batch.events[0]
    if not isinstance(event, HumanResolutionRecorded):
        raise StoreCorruption("human resolution receipt lacks its event")
    if receipt.request_digest != event.proposal_hash:
        raise StoreCorruption("human resolution receipt digest differs from ledger evidence")


def _verify_specialized_receipt_kind(kind: TransitionKind, batch: TransitionBatch) -> None:
    if kind is TransitionKind.HUMAN_SUSPENSION:
        _require_human_suspension_receipt(batch)
    if kind is TransitionKind.HUMAN_RESOLUTION:
        _require_human_resolution_receipt(batch)
    if kind is TransitionKind.TERMINAL:
        _require_terminal_receipt(batch)
    if kind is TransitionKind.RECONCILIATION:
        _verify_reconciliation_receipt(batch)


def _require_human_suspension_receipt(batch: TransitionBatch) -> None:
    if not _is_human_suspension(batch):
        raise StoreCorruption("human suspension receipt lacks HumanRequired evidence")


def _require_human_resolution_receipt(batch: TransitionBatch) -> None:
    if not _is_human_resolution(batch):
        raise StoreCorruption("human resolution receipt lacks exact authenticated evidence")


def _require_terminal_receipt(batch: TransitionBatch) -> None:
    if not _is_terminal_transition(batch):
        raise StoreCorruption("terminal receipt lacks proof and assignment")


def _is_human_suspension(batch: TransitionBatch) -> bool:
    return len(batch.events) == 1 and isinstance(batch.events[0], HumanRequired)


def _is_human_resolution(batch: TransitionBatch) -> bool:
    return len(batch.events) == 1 and isinstance(batch.events[0], HumanResolutionRecorded)


def _is_terminal_transition(batch: TransitionBatch) -> bool:
    return tuple(type(event) for event in batch.events) == (InvariantEvaluated, TerminalAssigned)


def _verify_receipt_fence_evidence(batch: TransitionBatch) -> None:
    try:
        _verify_event_fences(batch)
    except StaleFence as error:
        raise StoreCorruption("stored receipt event fence does not match transition") from error


def _verify_receipt_boundary(
    receipt: _Receipt, batch: TransitionBatch, snapshot: SagaSnapshot
) -> None:
    stored = (receipt.transition_id, receipt.saga_id, receipt.expected_seq, receipt.resulting_seq)
    proven = (batch.transition_id, batch.saga_id, batch.expected_seq, batch.projection.seq)
    if stored != proven or receipt.resulting_seq != receipt.expected_seq + len(batch.events):
        raise StoreCorruption("stored receipt does not match its transition boundary")
    if snapshot != batch.projection or _batch_digest(batch) != receipt.payload_digest:
        raise StoreCorruption("stored receipt does not match its transition proof")


def _verify_receipt_events(
    receipt: _Receipt,
    batch: TransitionBatch,
    snapshot: SagaSnapshot,
    events: tuple[LedgerEvent, ...],
) -> None:
    event_slice = events[receipt.expected_seq : receipt.resulting_seq]
    if event_slice != batch.events or receipt.resulting_seq > len(events):
        raise StoreCorruption("stored receipt events do not match authoritative ledger")
    if _rebuild_events(events[: receipt.resulting_seq]) != snapshot:
        raise StoreCorruption("stored receipt projection does not match authoritative replay")


def _verify_receipt_commands(connection: sqlite3.Connection, batch: TransitionBatch) -> None:
    try:
        _verify_intent_command_cardinality(batch)
        for command in batch.outbox_commands:
            row = _load_outbox_row(connection, command.command_id, command.saga_id)
            durable = _outbox_envelope(row).model_copy(
                update={"capability_proof": _load_capability_proof(connection, command.command_id)}
            )
            if durable != command:
                raise StoreConflict("outbox envelope differs from transition proof")
    except StoreConflict as error:
        raise StoreCorruption("stored receipt outbox does not match transition proof") from error


def _execute_outbox_update(
    connection: sqlite3.Connection,
    claimed: ClaimedCommand,
    state: OutboxState,
    now: datetime,
) -> sqlite3.Cursor:
    return connection.execute(
        "UPDATE outbox_commands SET state = ? WHERE command_id = ? AND saga_id = ? "
        "AND state = 'claimed' AND claim_id = ? AND claim_owner = ? "
        "AND claim_expires_at = ? AND claim_generation = ? AND claim_fence_token = ? "
        "AND delivery_attempt = ? "
        "AND julianday(claim_expires_at) > julianday(?) AND EXISTS "
        "(SELECT 1 FROM sagas WHERE saga_id = ? AND fence_token = ?)",
        _outbox_update_values(claimed, state, now),
    )


def _outbox_update_values(
    claimed: ClaimedCommand, state: OutboxState, now: datetime
) -> tuple[object, ...]:
    return (
        state.value,
        claimed.command_id,
        claimed.saga_id,
        claimed.claim_id,
        claimed.claim_owner,
        _utc_text(claimed.claim_expires_at),
        claimed.claim_generation,
        claimed.saga_fence_token,
        claimed.delivery_attempt,
        _utc_text(now),
        claimed.saga_id,
        claimed.saga_fence_token,
    )


def _execute_outbox_release(
    connection: sqlite3.Connection, claimed: ClaimedCommand, now: datetime
) -> sqlite3.Cursor:
    generation, fence, attempt = _released_claim_history(claimed)
    return connection.execute(
        "UPDATE outbox_commands SET state = 'runnable', claim_id = NULL, "
        "claim_owner = NULL, claim_expires_at = NULL, claim_generation = ?, "
        "claim_fence_token = ?, delivery_attempt = ? "
        "WHERE command_id = ? AND saga_id = ? AND state = 'claimed' AND claim_id = ? "
        "AND claim_owner = ? AND claim_expires_at = ? AND claim_generation = ? "
        "AND claim_fence_token = ? AND delivery_attempt = ? "
        "AND julianday(claim_expires_at) > julianday(?) AND EXISTS "
        "(SELECT 1 FROM sagas WHERE saga_id = ? AND fence_token = ?)",
        (generation, fence, attempt, *_outbox_release_values(claimed, now)),
    )


def _released_claim_history(claimed: ClaimedCommand) -> tuple[int, int, int]:
    prior_attempt = claimed.delivery_attempt - 1
    if prior_attempt == 0:
        return 0, 0, 0
    return claimed.claim_generation, claimed.saga_fence_token, prior_attempt


def _outbox_release_values(claimed: ClaimedCommand, now: datetime) -> tuple[object, ...]:
    return (
        claimed.command_id,
        claimed.saga_id,
        claimed.claim_id,
        claimed.claim_owner,
        _utc_text(claimed.claim_expires_at),
        claimed.claim_generation,
        claimed.saga_fence_token,
        claimed.delivery_attempt,
        _utc_text(now),
        claimed.saga_id,
        claimed.saga_fence_token,
    )


def _reconciliation_job(raw: Sequence[object]) -> ReconciliationJob:
    policy = _decode_recovery_policy(raw[12], raw[13])
    try:
        return ReconciliationJob.model_validate(
            _reconciliation_identity(raw)
            | _reconciliation_claim(raw)
            | {
                "recovery_policy": policy,
                "recovery_policy_digest": _as_optional_str(raw[13], "recovery policy digest"),
                "claimed_at": _optional_utc(raw[14], "reconciliation claim time"),
                "lookup_attempt": _as_int(raw[15], "reconciliation lookup attempt"),
                "lookup_started_at": _optional_utc(raw[16], "reconciliation lookup start"),
            }
        )
    except (ValueError, ValidationError) as error:
        raise StoreCorruption("stored reconciliation job is invalid") from error


def _reconciliation_identity(raw: Sequence[object]) -> dict[str, object]:
    return {
        "job_id": _as_str(raw[0], "reconciliation job ID"),
        "command_id": _as_str(raw[1], "reconciliation command ID"),
        "saga_id": _as_str(raw[2], "reconciliation Saga ID"),
        "operation_id": _as_str(raw[3], "reconciliation operation ID"),
        "state": ReconciliationJobState(_as_str(raw[4], "reconciliation state")),
        "due_at": _parse_utc(_as_str(raw[5], "reconciliation due time"), "due time"),
        "first_dispatch_at": _parse_utc(_as_str(raw[6], "first dispatch time"), "dispatch"),
    }


def _reconciliation_claim(raw: Sequence[object]) -> dict[str, object]:
    return {
        "claim_id": _as_optional_str(raw[7], "reconciliation claim ID"),
        "claim_owner": _as_optional_str(raw[8], "reconciliation claim owner"),
        "claim_expires_at": _optional_utc(raw[9], "reconciliation claim expiry"),
        "claim_generation": _as_int(raw[10], "reconciliation claim generation"),
        "claim_fence_token": _as_int(raw[11], "reconciliation claim fence"),
    }


def _decode_recovery_policy(raw: object, digest: object) -> RecoveryPolicy | None:
    proof = _recovery_policy_proof(raw, digest)
    if proof is None:
        return None
    encoded, expected_digest = proof
    policy = _parse_recovery_policy(encoded)
    _verify_recovery_policy(policy, encoded, expected_digest)
    return policy


def _recovery_policy_proof(raw: object, digest: object) -> tuple[bytes, str] | None:
    state = (raw is None, digest is None)
    if state == (True, True):
        return None
    if state != (False, False):
        raise StoreCorruption("stored recovery policy proof is incomplete")
    return _as_bytes(raw, "recovery policy JSON"), _as_str(digest, "digest")


def _parse_recovery_policy(encoded: bytes) -> RecoveryPolicy:
    try:
        return RecoveryPolicy.model_validate_json(encoded, strict=True)
    except ValidationError as error:
        raise StoreCorruption("stored recovery policy is invalid") from error


def _verify_recovery_policy(policy: RecoveryPolicy, encoded: bytes, digest: str) -> None:
    payload = policy.model_dump(mode="json")
    if _canonical_bytes(payload) != encoded:
        raise StoreCorruption("stored recovery policy proof does not match")
    if _json_digest(payload) != digest:
        raise StoreCorruption("stored recovery policy proof does not match")


def _reconciliation_job_id(operation_id: str) -> str:
    digest = sha256(b"agentic-saga-reconciliation-job-v1\0" + operation_id.encode()).hexdigest()
    return f"recon_{digest}"


def _first_dispatch_at(
    connection: sqlite3.Connection, saga_id: SagaId, operation_id: str
) -> datetime:
    events = _read_events(connection, saga_id)
    times = tuple(
        event.recorded_at
        for event in events
        if isinstance(event, DispatchStarted) and event.operation_id == operation_id
    )
    if not times:
        raise StoreConflict("unknown outcome lacks durable dispatch evidence")
    return times[0]


def _ensure_reconciliation_job(
    connection: sqlite3.Connection, claimed: ClaimedCommand, now: datetime
) -> None:
    values = _reconciliation_job_values(connection, claimed, now)
    row = connection.execute(
        _RECONCILIATION_SELECT + "WHERE operation_id = ?", (claimed.operation_id,)
    ).fetchone()
    if row is None:
        _insert_reconciliation_job(connection, values)
        return
    _rearm_reconciliation_job(connection, _reconciliation_job(row), values)


def _reconciliation_job_values(
    connection: sqlite3.Connection, claimed: ClaimedCommand, now: datetime
) -> tuple[str, str, SagaId, OperationId, str, str]:
    return (
        _reconciliation_job_id(claimed.operation_id),
        claimed.command_id,
        claimed.saga_id,
        claimed.operation_id,
        _utc_text(now),
        _utc_text(_first_dispatch_at(connection, claimed.saga_id, claimed.operation_id)),
    )


def _insert_reconciliation_job(
    connection: sqlite3.Connection,
    values: tuple[str, str, SagaId, OperationId, str, str],
) -> None:
    connection.execute(
        "INSERT INTO reconciliation_jobs "
        "(job_id, command_id, saga_id, operation_id, state, due_at, first_dispatch_at) "
        "VALUES (?, ?, ?, ?, 'due', ?, ?)",
        values,
    )


def _rearm_reconciliation_job(
    connection: sqlite3.Connection,
    job: ReconciliationJob,
    values: tuple[str, str, SagaId, OperationId, str, str],
) -> None:
    identity = (job.job_id, job.command_id, job.saga_id, job.operation_id)
    if job.state is not ReconciliationJobState.REQUEUED or identity != values[:4]:
        raise StoreConflict("existing reconciliation job cannot be rearmed")
    if _utc_text(job.first_dispatch_at) != values[5]:
        raise StoreConflict("reconciliation first dispatch anchor changed")
    cursor = connection.execute(
        "UPDATE reconciliation_jobs SET state = 'due', due_at = ? "
        "WHERE job_id = ? AND state = 'requeued' AND claim_generation = ?",
        (values[4], job.job_id, job.claim_generation),
    )
    if cursor.rowcount != 1:
        raise StoreConflict("reconciliation job changed before rearm")


def _select_reconciliation_job(
    connection: sqlite3.Connection, now: datetime
) -> ReconciliationJob | None:
    text = _utc_text(now)
    row = connection.execute(_CLAIMABLE_RECONCILIATION_SELECT, (text, text)).fetchone()
    return None if row is None else _reconciliation_job(cast(Sequence[object], row))


def _require_policy_pair(
    policy: RecoveryPolicy | None, digest: str | None
) -> tuple[RecoveryPolicy, str]:
    if policy is None or digest is None:
        raise ValueError("recovery policy and digest are required")
    if _json_digest(policy.model_dump(mode="json")) != digest:
        raise ValueError("recovery policy digest does not match canonical policy")
    return policy, digest


def _require_matching_policy(job: ReconciliationJob, policy: RecoveryPolicy, digest: str) -> None:
    if job.recovery_policy_digest is None:
        return
    if (job.recovery_policy, job.recovery_policy_digest) != (policy, digest):
        raise StoreConflict("recovery policy does not match the frozen first claim")


def _reconciliation_retry_due(now: datetime, job: ReconciliationJob) -> datetime:
    if job.recovery_policy is None:
        raise StoreConflict("reconciliation claim has no frozen recovery policy")
    delay = timedelta(microseconds=job.recovery_policy.maximum_retry_delay_microseconds)
    try:
        return now + delay
    except OverflowError as error:
        raise StoreConflict("frozen reconciliation retry delay is too large") from error


def _claimed_reconciliation(
    job: ReconciliationJob,
    owner: str,
    now: datetime,
    duration: timedelta,
    claim_id: str,
) -> ReconciliationJob:
    return job.model_copy(
        update={
            "state": ReconciliationJobState.CLAIMED,
            "claim_id": claim_id,
            "claim_owner": owner,
            "claim_expires_at": _lease_expiry(now, duration),
            "claim_generation": job.claim_generation + 1,
            "claimed_at": now,
        }
    )


def _update_reconciliation_claim(
    connection: sqlite3.Connection,
    previous: ReconciliationJob,
    claimed: ReconciliationJob,
    policy: RecoveryPolicy,
    digest: str,
) -> sqlite3.Cursor:
    return connection.execute(
        "UPDATE reconciliation_jobs SET state = 'claimed', claim_id = ?, claim_owner = ?, "
        "claim_expires_at = ?, claim_generation = ?, claimed_at = ?, recovery_policy_json = "
        "COALESCE(recovery_policy_json, ?), recovery_policy_digest = "
        "COALESCE(recovery_policy_digest, ?) WHERE job_id = ? AND state = ? "
        "AND claim_generation = ?",
        _reconciliation_claim_values(previous, claimed, policy, digest),
    )


def _reconciliation_claim_values(
    previous: ReconciliationJob,
    claimed: ReconciliationJob,
    policy: RecoveryPolicy,
    digest: str,
) -> tuple[object, ...]:
    return (
        claimed.claim_id,
        claimed.claim_owner,
        _utc_text(cast(datetime, claimed.claim_expires_at)),
        claimed.claim_generation,
        _utc_text(cast(datetime, claimed.claimed_at)),
        _canonical_bytes(policy.model_dump(mode="json")),
        digest,
        previous.job_id,
        previous.state.value,
        previous.claim_generation,
    )


def _durable_reconciliation_claim(
    connection: sqlite3.Connection, supplied: ReconciliationJob
) -> ReconciliationJob:
    row = connection.execute(
        _RECONCILIATION_SELECT + "WHERE job_id = ?", (supplied.job_id,)
    ).fetchone()
    if row is None:
        raise StoreConflict("durable reconciliation claim does not exist")
    durable = _reconciliation_job(cast(Sequence[object], row))
    if durable != supplied or durable.state is not ReconciliationJobState.CLAIMED:
        raise StoreConflict("supplied reconciliation claim is stale")
    return durable


def _require_reconciliation_authority(
    connection: sqlite3.Connection,
    job: ReconciliationJob,
    lease: Lease,
    now: datetime,
) -> None:
    if job.claim_expires_at is None or job.claim_expires_at <= now:
        raise StoreConflict("reconciliation claim expired before transition")
    if (job.claim_owner, job.claim_fence_token) != (lease.owner, lease.fence_token):
        raise StoreConflict("reconciliation claim lacks current Saga authority")
    saga = SQLiteKernelStore._load_saga_row(connection, job.saga_id)
    _require_current_lease(saga, lease, now)


def _started_reconciliation_lookup(job: ReconciliationJob, now: datetime) -> ReconciliationJob:
    return job.model_copy(
        update={"lookup_attempt": job.lookup_attempt + 1, "lookup_started_at": now}
    )


def _record_reconciliation_lookup(
    connection: sqlite3.Connection,
    job: ReconciliationJob,
    started: ReconciliationJob,
    now: datetime,
) -> sqlite3.Cursor:
    return connection.execute(
        "UPDATE reconciliation_jobs SET lookup_attempt = ?, lookup_started_at = ? "
        "WHERE job_id = ? AND state = 'claimed' AND claim_id = ? "
        "AND claim_generation = ? AND claim_fence_token = ? AND lookup_attempt = ?",
        (
            started.lookup_attempt,
            _utc_text(now),
            job.job_id,
            job.claim_id,
            job.claim_generation,
            job.claim_fence_token,
            job.lookup_attempt,
        ),
    )


def _release_reconciliation_claim(
    connection: sqlite3.Connection, job: ReconciliationJob, due_at: datetime
) -> sqlite3.Cursor:
    return connection.execute(
        "UPDATE reconciliation_jobs SET state = 'waiting', due_at = ?, claim_id = NULL, "
        "claim_owner = NULL, claim_expires_at = NULL WHERE job_id = ? AND state = 'claimed' "
        "AND claim_id = ? AND claim_generation = ? AND claim_fence_token = ?",
        (
            _utc_text(due_at),
            job.job_id,
            job.claim_id,
            job.claim_generation,
            job.claim_fence_token,
        ),
    )


def _reconciliation_event(batch: TransitionBatch) -> ReconciliationRecorded:
    if not batch.events or not isinstance(batch.events[0], ReconciliationRecorded):
        raise StoreConflict("reconciliation requires dedicated evidence")
    return batch.events[0]


def _reconciliation_write(
    batch: TransitionBatch, job: ReconciliationJob, now: datetime
) -> _TransitionWrite:
    return _TransitionWrite(
        batch,
        _batch_digest(batch),
        now,
        job.recovery_policy_digest,
        TransitionKind.RECONCILIATION,
        True,
    )


def _verify_reconciliation_event_shape(batch: TransitionBatch) -> None:
    event = _reconciliation_event(batch)
    actual = tuple(type(item) for item in batch.events)
    if actual not in _allowed_reconciliation_shapes(event):
        raise StoreConflict("reconciliation event sequence does not match its action")
    _verify_reconciliation_human_reason(batch, event)


def _allowed_reconciliation_shapes(
    event: ReconciliationRecorded,
) -> tuple[tuple[type[LedgerEvent], ...], ...]:
    with_human = (ReconciliationRecorded, HumanRequired)
    if event.action == "human":
        return (with_human,)
    if event.action in {"confirm", "retry_same_id"}:
        return ((ReconciliationRecorded,), with_human)
    return ((ReconciliationRecorded,),)


def _has_reconciliation_human_followup(batch: TransitionBatch) -> bool:
    return tuple(type(item) for item in batch.events) == (
        ReconciliationRecorded,
        HumanRequired,
    )


def _verify_reconciliation_human_reason(
    batch: TransitionBatch, event: ReconciliationRecorded
) -> None:
    if not _has_reconciliation_human_followup(batch):
        return
    human = batch.events[-1]
    if not isinstance(human, HumanRequired):
        raise StoreConflict("reconciliation lacks exact HumanRequired evidence")
    expected = (
        _RECONCILIATION_HUMAN_REASON
        if event.action == "human"
        else _POST_RECONCILIATION_HUMAN_REASON
    )
    if human.reason_code != expected:
        raise StoreConflict("reconciliation HumanRequired reason does not match its action")


def _verify_reconciliation_receipt(batch: TransitionBatch) -> None:
    try:
        _verify_reconciliation_event_shape(batch)
    except StoreConflict as error:
        raise StoreCorruption("reconciliation receipt lacks exact evidence") from error


def _verify_reconciliation_batch(
    connection: sqlite3.Connection,
    job: ReconciliationJob,
    batch: TransitionBatch,
    disposition: tuple[str, datetime | None],
    now: datetime,
) -> None:
    action, check_after = disposition
    _verify_reconciliation_event_shape(batch)
    event = _reconciliation_event(batch)
    if job.recovery_policy_digest is None:
        raise StoreConflict("reconciliation claim lacks frozen recovery policy")
    if event.recovery_policy_digest != job.recovery_policy_digest or event.action != action:
        raise StoreConflict("reconciliation evidence does not match claimed policy or action")
    _verify_reconciliation_identity(connection, job, batch, event)
    _verify_reconciliation_due_time(event, action, check_after)
    _verify_retry_horizon(connection, job, event, action, now)
    _verify_reconciliation_human_followup(connection, batch, event)
    if action == "human":
        _verify_reconciliation_quiescence(connection, job)


def _verify_reconciliation_human_followup(
    connection: sqlite3.Connection,
    batch: TransitionBatch,
    event: ReconciliationRecorded,
) -> None:
    has_human = _has_reconciliation_human_followup(batch)
    required = event.action in {"confirm", "retry_same_id"} and bool(
        _suspended_outbox_count(connection, batch.saga_id)
    )
    if event.action != "human" and has_human != required:
        raise StoreConflict("reconciliation lacks exact human follow-up evidence")


def _verify_retry_horizon(
    connection: sqlite3.Connection,
    job: ReconciliationJob,
    event: ReconciliationRecorded,
    action: str,
    now: datetime,
) -> None:
    if not _requires_retry_proof(action, event):
        return
    proof = _load_capability_proof(connection, job.command_id)
    retention = None if proof is None else proof.capabilities.idempotency_retention_seconds
    if not _policy_covers(now, job, retention):
        raise RecoveryProofExpired("idempotency retention expired before retry commit")


def _requires_retry_proof(action: str, event: ReconciliationRecorded) -> bool:
    return action == "retry_same_id" and isinstance(event.outcome, ReconcileUnsupported)


def _policy_covers(now: datetime, job: ReconciliationJob, retention: int | None) -> bool:
    if retention is None or job.recovery_policy is None:
        return False
    try:
        horizon = now + job.recovery_policy.total
        return horizon <= job.first_dispatch_at + timedelta(seconds=retention)
    except OverflowError:
        return False


def _verify_reconciliation_identity(
    connection: sqlite3.Connection,
    job: ReconciliationJob,
    batch: TransitionBatch,
    event: ReconciliationRecorded,
) -> None:
    row = _load_outbox_row(connection, job.command_id, job.saga_id)
    claimed = _outbox_envelope(row)
    identity = (batch.saga_id, batch.expected_fence_token, batch.lease_owner)
    if identity != (job.saga_id, job.claim_fence_token, job.claim_owner):
        raise StoreConflict("reconciliation transition has stale Saga authority")
    if event.reconciliation_attempt != job.claim_generation:
        raise StoreConflict("reconciliation attempt does not match job generation")
    if _event_claim_signature(event) != _reconciliation_signature(claimed, row.delivery_attempt):
        raise StoreConflict("reconciliation evidence does not match parked operation")


def _reconciliation_signature(command: OutboxCommand, delivery_attempt: int) -> tuple[object, ...]:
    return (
        command.operation_id,
        command.tool_name,
        command.step_instance_id,
        command.direction,
        command.semantic_generation,
        command.command_hash,
        delivery_attempt,
        command.command,
    )


def _verify_reconciliation_due_time(
    event: ReconciliationRecorded, action: str, check_after: datetime | None
) -> None:
    pending_time = getattr(event.outcome, "check_after", None)
    if action == "wait":
        _require_authoritative_check_after(pending_time, check_after)
    elif check_after is not None:
        raise StoreConflict("only wait reconciliation accepts check_after")


def _require_authoritative_check_after(pending_time: object, check_after: datetime | None) -> None:
    if check_after is None:
        raise StoreConflict("wait disposition must use authoritative check_after")
    if pending_time != check_after:
        raise StoreConflict("wait disposition must use authoritative check_after")


def _verify_reconciliation_quiescence(
    connection: sqlite3.Connection, job: ReconciliationJob
) -> None:
    if _claimed_count(connection, job.saga_id):
        raise StoreConflict("claimed command blocks reconciliation escalation")
    if _has_other_claimed_reconciliation(connection, job):
        raise StoreConflict("claimed reconciliation lookup blocks escalation")
    snapshot = SQLiteKernelStore._load_saga_row(connection, job.saga_id).snapshot
    if any(item.status is OperationStatus.DISPATCHED for item in snapshot.operations.values()):
        raise StoreConflict("in-flight command blocks reconciliation escalation")


def _has_other_claimed_reconciliation(
    connection: sqlite3.Connection, job: ReconciliationJob
) -> bool:
    row = connection.execute(
        "SELECT COUNT(*) FROM reconciliation_jobs WHERE saga_id = ? AND job_id != ? "
        "AND state = 'claimed'",
        (job.saga_id, job.job_id),
    ).fetchone()
    return row is not None and bool(_as_int(row[0], "claimed reconciliation count"))


def _apply_reconciliation_disposition(
    connection: sqlite3.Connection,
    job: ReconciliationJob,
    batch: TransitionBatch,
    action: str,
    check_after: datetime | None,
) -> None:
    states = {
        "confirm": ("completed", "completed"),
        "retry_same_id": ("requeued", "runnable"),
        "wait": ("waiting", "parked"),
        "human": ("human_required", "parked"),
    }
    job_state, outbox_state = states[action]
    due_at = check_after or job.due_at
    _update_reconciliation_job(connection, job, job_state, due_at)
    _update_reconciled_outbox(connection, job, outbox_state)
    if action == "human":
        _suspend_other_runnable(connection, job, batch.projection.seq)
    if action in {"confirm", "retry_same_id"} and _has_reconciliation_human_followup(batch):
        _apply_human_suspension(connection, batch)


def _update_reconciliation_job(
    connection: sqlite3.Connection,
    job: ReconciliationJob,
    state: str,
    due_at: datetime,
) -> None:
    cursor = connection.execute(
        "UPDATE reconciliation_jobs SET state = ?, due_at = ?, claim_id = NULL, "
        "claim_owner = NULL, claim_expires_at = NULL WHERE job_id = ? AND state = 'claimed' "
        "AND claim_id = ? AND claim_generation = ? AND claim_fence_token = ?",
        (
            state,
            _utc_text(due_at),
            job.job_id,
            job.claim_id,
            job.claim_generation,
            job.claim_fence_token,
        ),
    )
    if cursor.rowcount != 1:
        raise StoreConflict("reconciliation job changed before finalization")


def _update_reconciled_outbox(
    connection: sqlite3.Connection, job: ReconciliationJob, state: str
) -> None:
    sql, parameters = _reconciled_outbox_update(job, state)
    if connection.execute(sql, parameters).rowcount != 1:
        raise StoreConflict("parked outbox changed before reconciliation")


def _reconciled_outbox_update(job: ReconciliationJob, state: str) -> tuple[str, tuple[object, ...]]:
    identity = (job.command_id, job.saga_id, job.operation_id)
    if state == "runnable":
        sql = (
            "UPDATE outbox_commands SET state = 'runnable', claim_id = NULL, "
            "claim_owner = NULL, claim_expires_at = NULL WHERE command_id = ? "
            "AND saga_id = ? AND operation_id = ? AND state = 'parked'"
        )
        return sql, identity
    sql = "UPDATE outbox_commands SET state = ? WHERE command_id = ? AND saga_id = ? "
    return sql + "AND operation_id = ? AND state = 'parked'", (state, *identity)


def _suspend_other_runnable(
    connection: sqlite3.Connection, job: ReconciliationJob, sequence: int
) -> None:
    connection.execute(
        "UPDATE outbox_commands SET state = 'suspended_for_human', suspended_at_seq = ? "
        "WHERE saga_id = ? AND command_id != ? AND state = 'runnable'",
        (sequence, job.saga_id, job.command_id),
    )


def _retry_reconciliation(
    connection: sqlite3.Connection,
    receipt: _Receipt,
    job: ReconciliationJob,
    batch: TransitionBatch,
    action: str,
) -> _CommitResult:
    snapshot = SQLiteKernelStore._verify_receipt(
        receipt,
        batch,
        _batch_digest(batch),
        job.recovery_policy_digest,
        TransitionKind.RECONCILIATION,
    )
    if _stored_reconciliation_state(connection, job) != _final_job_state(action):
        raise StoreConflict("reconciliation receipt does not match finalized job")
    return _CommitResult(snapshot, False)


def _stored_reconciliation_state(
    connection: sqlite3.Connection, job: ReconciliationJob
) -> str | None:
    row = connection.execute(
        "SELECT state FROM reconciliation_jobs WHERE job_id = ?", (job.job_id,)
    ).fetchone()
    return None if row is None else _as_str(row[0], "reconciliation state")


def _final_job_state(action: str) -> str:
    return {
        "confirm": "completed",
        "retry_same_id": "requeued",
        "wait": "waiting",
        "human": "human_required",
    }[action]


def _verify_reconciliation_jobs(connection: sqlite3.Connection) -> None:
    rows = connection.execute(_RECONCILIATION_SELECT + "ORDER BY job_id").fetchall()
    jobs = tuple(_reconciliation_job(cast(Sequence[object], row)) for row in rows)
    _verify_reconciliation_cardinality(connection, jobs)
    for job in jobs:
        _verify_reconciliation_job(connection, job)


def _verify_reconciliation_job(connection: sqlite3.Connection, job: ReconciliationJob) -> None:
    try:
        row = _load_outbox_row(connection, job.command_id, job.saga_id)
        operation = _job_operation(connection, job)
        _verify_job_identity(connection, job, row)
        _verify_job_lifecycle(job, operation, row.state)
        _verify_job_policy(job)
        _verify_job_lookup_history(job)
        _verify_job_final_evidence(connection, job)
    except StoreConflict as error:
        raise StoreCorruption("reconciliation job identity is invalid") from error


def _job_operation(connection: sqlite3.Connection, job: ReconciliationJob) -> OperationRecord:
    snapshot = SQLiteKernelStore._load_saga_row(connection, job.saga_id).snapshot
    operation = snapshot.operations.get(job.operation_id)
    if operation is None:
        raise StoreConflict("reconciliation operation does not belong to Saga")
    return operation


def _verify_job_identity(
    connection: sqlite3.Connection, job: ReconciliationJob, row: _OutboxRow
) -> None:
    identity = (job.job_id, job.operation_id, job.saga_id)
    expected = (_reconciliation_job_id(row.operation_id), row.operation_id, row.saga_id)
    if identity != expected:
        raise StoreConflict("reconciliation identity differs from outbox")
    anchor = _first_dispatch_at(connection, job.saga_id, job.operation_id)
    if job.first_dispatch_at != anchor:
        raise StoreConflict("reconciliation dispatch anchor differs from ledger")


def _verify_job_lifecycle(
    job: ReconciliationJob, operation: OperationRecord, outbox: OutboxState
) -> None:
    expected = _expected_reconciliation_lifecycle(job.state)
    if (operation.status, outbox) not in expected:
        raise StoreConflict("reconciliation lifecycle differs from durable operation")


def _expected_reconciliation_lifecycle(
    state: ReconciliationJobState,
) -> frozenset[tuple[OperationStatus, OutboxState]]:
    if state is ReconciliationJobState.COMPLETED:
        return frozenset({(OperationStatus.EFFECT_CONFIRMED, OutboxState.COMPLETED)})
    if state is ReconciliationJobState.REQUEUED:
        return frozenset(
            {
                (OperationStatus.INTENT_DURABLE, OutboxState.RUNNABLE),
                (OperationStatus.INTENT_DURABLE, OutboxState.CLAIMED),
                (OperationStatus.INTENT_DURABLE, OutboxState.SUSPENDED_FOR_HUMAN),
                (OperationStatus.DISPATCHED, OutboxState.CLAIMED),
                (OperationStatus.EFFECT_CONFIRMED, OutboxState.COMPLETED),
                (OperationStatus.NO_EFFECT_CONFIRMED, OutboxState.COMPLETED),
                (OperationStatus.PARTIAL_EFFECT_CONFIRMED, OutboxState.COMPLETED),
            }
        )
    return frozenset({(OperationStatus.OUTCOME_UNKNOWN, OutboxState.PARKED)})


def _verify_job_policy(job: ReconciliationJob) -> None:
    has_policy = job.recovery_policy is not None and job.recovery_policy_digest is not None
    if has_policy != (job.claim_generation >= 1):
        raise StoreConflict("reconciliation policy is not bound to first claim")


def _verify_job_lookup_history(job: ReconciliationJob) -> None:
    has_start = job.lookup_started_at is not None
    if has_start != (job.lookup_attempt >= 1) or job.lookup_attempt > job.claim_generation:
        raise StoreConflict("reconciliation lookup history is invalid")


def _verify_job_final_evidence(connection: sqlite3.Connection, job: ReconciliationJob) -> None:
    expected = _expected_final_action(job.state)
    if expected is None or _has_expected_final_evidence(connection, job, expected):
        return
    raise StoreConflict("reconciliation final state lacks matching evidence")


def _has_expected_final_evidence(
    connection: sqlite3.Connection, job: ReconciliationJob, expected: str
) -> bool:
    actions = _job_reconciliation_actions(connection, job)
    if actions[-1:] == (expected,):
        return True
    return _has_emergency_human_evidence(connection, job)


def _has_emergency_human_evidence(connection: sqlite3.Connection, job: ReconciliationJob) -> bool:
    if job.state is not ReconciliationJobState.HUMAN_REQUIRED:
        return False
    return _has_later_human_event(connection, job)


def _has_later_human_event(connection: sqlite3.Connection, job: ReconciliationJob) -> bool:
    events = _read_events(connection, job.saga_id)
    unknown_seq = max(
        (event.saga_seq for event in events if _is_job_unknown_event(event, job)), default=0
    )
    return any(_is_later_human_event(event, unknown_seq) for event in events)


def _is_job_unknown_event(event: LedgerEvent, job: ReconciliationJob) -> bool:
    if not isinstance(event, EffectOutcomeRecorded):
        return False
    if event.operation_id != job.operation_id:
        return False
    return isinstance(event.outcome, OutcomeUnknown)


def _is_later_human_event(event: LedgerEvent, unknown_seq: int) -> bool:
    return isinstance(event, HumanRequired) and event.saga_seq > unknown_seq


def _expected_final_action(state: ReconciliationJobState) -> str | None:
    return {
        ReconciliationJobState.COMPLETED: "confirm",
        ReconciliationJobState.REQUEUED: "retry_same_id",
        ReconciliationJobState.HUMAN_REQUIRED: "human",
    }.get(state)


def _job_reconciliation_actions(
    connection: sqlite3.Connection, job: ReconciliationJob
) -> tuple[str, ...]:
    events = _read_events(connection, job.saga_id)
    return tuple(
        event.action
        for event in events
        if isinstance(event, ReconciliationRecorded) and event.operation_id == job.operation_id
    )


def _verify_reconciliation_cardinality(
    connection: sqlite3.Connection, jobs: tuple[ReconciliationJob, ...]
) -> None:
    expected = set().union(
        *(_unknown_operations(connection, saga_id) for saga_id in _saga_ids(connection))
    )
    excluded = {ReconciliationJobState.REQUEUED, ReconciliationJobState.COMPLETED}
    actual = {job.operation_id for job in jobs if job.state not in excluded}
    if actual != expected:
        raise StoreCorruption("reconciliation job cardinality does not match unknown operations")


def _unknown_operations(connection: sqlite3.Connection, saga_id: SagaId) -> set[OperationId]:
    snapshot = SQLiteKernelStore._load_saga_row(connection, saga_id).snapshot
    return {
        operation.operation_id
        for operation in snapshot.operations.values()
        if operation.status is OperationStatus.OUTCOME_UNKNOWN
    }


def _execute_event_insert(
    connection: sqlite3.Connection,
    event: LedgerEvent,
    event_bytes: bytes,
    digest: str,
    prior_hash: str | None,
) -> None:
    connection.execute(
        "INSERT INTO ledger_events "
        "(event_id, saga_id, saga_seq, event_type, event_json, event_hash, prior_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        _event_insert_values(event, event_bytes, digest, prior_hash),
    )


def _event_insert_values(
    event: LedgerEvent, event_bytes: bytes, digest: str, prior_hash: str | None
) -> tuple[object, ...]:
    return (
        event.event_id,
        event.saga_id,
        event.saga_seq,
        event.event_type,
        event_bytes,
        digest,
        prior_hash,
    )


def _execute_outbox_insert(connection: sqlite3.Connection, command: OutboxCommand) -> None:
    connection.execute(
        "INSERT INTO outbox_commands "
        "(command_id, saga_id, operation_id, tool_name, definition_version, "
        "command_schema_version, step_instance_id, direction, semantic_generation, "
        "command_json, command_hash, capabilities_json, capability_digest, state, available_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'runnable', ?)",
        _outbox_insert_values(command),
    )


def _outbox_insert_values(command: OutboxCommand) -> tuple[object, ...]:
    return (
        command.command_id,
        command.saga_id,
        command.operation_id,
        command.tool_name,
        command.definition_version,
        command.command_schema_version,
        command.step_instance_id,
        command.direction.value,
        command.semantic_generation,
        *_outbox_payload_values(command),
    )


def _outbox_payload_values(command: OutboxCommand) -> tuple[object, ...]:
    proof = command.capability_proof
    return (
        _canonical_bytes(thaw_json_object(command.command)),
        command.command_hash,
        None if proof is None else _canonical_bytes(proof.capabilities.model_dump(mode="json")),
        None if proof is None else proof.capability_digest,
        _utc_text(command.available_at),
    )


def _load_capability_proof(
    connection: sqlite3.Connection, command_id: str
) -> EffectCapabilityProof | None:
    row = connection.execute(
        "SELECT capabilities_json, capability_digest FROM outbox_commands WHERE command_id = ?",
        (command_id,),
    ).fetchone()
    if row is None:
        return None
    return _decode_capability_proof(*cast(tuple[object, object], row))


def _decode_capability_proof(raw: object, digest: object) -> EffectCapabilityProof | None:
    proof = _recovery_policy_proof(raw, digest)
    if proof is None:
        return None
    encoded, expected = proof
    try:
        capabilities = ToolCapabilities.model_validate_json(encoded, strict=True)
        if _canonical_bytes(capabilities.model_dump(mode="json")) != encoded:
            raise StoreCorruption("stored capability proof is not canonical")
        return EffectCapabilityProof(capabilities=capabilities, capability_digest=expected)
    except ValidationError as error:
        raise StoreCorruption("stored capability proof is invalid") from error


def _verify_command(operation: OperationRecord, command: OutboxCommand) -> None:
    if _operation_signature(operation) != _command_signature(command):
        raise StoreConflict("outbox command does not match durable operation intent")
    public_command = thaw_json_object(command.command)
    _verify_public_command(public_command, command.command_hash)


def _verify_intent_command_cardinality(batch: TransitionBatch) -> None:
    expected = tuple(
        event.operation_id
        for event in batch.events
        if isinstance(event, (EffectIntentRecorded, CompensationIntentRecorded))
    )
    actual = tuple(command.operation_id for command in batch.outbox_commands)
    if sorted(expected) != sorted(actual):
        raise StoreConflict("each effect intent requires exactly one outbox command")


def _operation_signature(operation: OperationRecord) -> tuple[object, ...]:
    return (
        operation.tool_name,
        operation.step_instance_id,
        operation.direction,
        operation.semantic_generation,
        operation.command_hash,
        operation.redacted_command,
    )


def _command_signature(command: OutboxCommand) -> tuple[object, ...]:
    return (
        command.tool_name,
        command.step_instance_id,
        command.direction,
        command.semantic_generation,
        command.command_hash,
        command.command,
    )


def _verify_public_command(public_command: object, command_hash: str) -> None:
    if _json_digest(public_command) != command_hash:
        raise StoreConflict("outbox command hash does not match canonical command")
    if _contains_redacted(public_command):
        raise StoreConflict("outbox command cannot execute a redaction placeholder")


def _verify_specialized_transition(
    connection: sqlite3.Connection,
    snapshot: SagaSnapshot,
    write: _TransitionWrite,
    verifier: HumanResolutionVerifier,
) -> None:
    _verify_human_transition_kind(connection, snapshot, write, verifier)
    _verify_terminal_transition_kind(connection, write.batch, write.kind)
    _verify_reconciliation_transition_kind(write.batch, write.kind)
    _verify_compensation_transition(connection, snapshot, write.batch)


def _verify_human_transition_kind(
    connection: sqlite3.Connection,
    snapshot: SagaSnapshot,
    write: _TransitionWrite,
    verifier: HumanResolutionVerifier,
) -> None:
    _verify_resolution_commit_kind(write.batch, write.kind)
    if write.kind is TransitionKind.HUMAN_SUSPENSION:
        _verify_human_suspension(connection, snapshot, write.batch)
    if write.kind is TransitionKind.HUMAN_RESOLUTION:
        _verify_human_resolution(connection, snapshot, write.batch, verifier, write.human_decision)


def _verify_resolution_commit_kind(batch: TransitionBatch, kind: TransitionKind) -> None:
    contains = any(isinstance(event, HumanResolutionRecorded) for event in batch.events)
    if contains and kind is not TransitionKind.HUMAN_RESOLUTION:
        raise StoreConflict("human resolution requires a specialized authenticated commit")


def _verify_human_decision_binding(batch: TransitionBatch, decision: HumanDecision) -> None:
    if not _is_human_resolution(batch):
        raise StoreConflict("authenticated human resolution requires exactly one event")
    event = batch.events[0]
    if not isinstance(event, HumanResolutionRecorded):
        raise StoreConflict("authenticated human resolution event is invalid")
    if _resolution_signature(batch, event) != _decision_signature(decision):
        raise StoreConflict("human resolution authorization does not match exact public evidence")


def _resolution_signature(
    batch: TransitionBatch, event: HumanResolutionRecorded
) -> tuple[object, ...]:
    return (
        batch.expected_seq,
        batch.saga_id,
        event.decision_id,
        event.action,
        event.proposal_hash,
        event.actor,
    )


def _decision_signature(decision: HumanDecision) -> tuple[object, ...]:
    return (
        decision.based_on_saga_seq,
        decision.saga_id,
        decision.decision_id,
        decision.action,
        _canonical_human_resolution_digest(decision),
        decision.actor,
    )


def _canonical_human_resolution_digest(decision: HumanDecision) -> str:
    digest = human_resolution_digest(decision)
    if decision.proposal_hash != digest:
        raise StoreConflict("human resolution lacks its canonical public digest")
    return digest


def _verify_human_resolution(
    connection: sqlite3.Connection,
    snapshot: SagaSnapshot,
    batch: TransitionBatch,
    verifier: HumanResolutionVerifier,
    decision: HumanDecision | None,
) -> None:
    event = _verified_human_resolution_event(batch)
    if decision is None:
        raise HumanResolutionAuthenticationFailed("trusted human decision is unavailable")
    _verify_human_decision_binding(batch, decision)
    _authenticate_human_resolution(verifier, decision, snapshot)
    if snapshot.status.value != "human_required" or not snapshot.pending_approval:
        raise HumanResolutionInapplicable("human decision has no pending durable request")
    _verify_human_action_targets(connection, batch.saga_id, event.action)


def _authenticate_human_resolution(
    verifier: HumanResolutionVerifier, decision: HumanDecision, snapshot: SagaSnapshot
) -> None:
    try:
        accepted = verifier(decision, snapshot) is True
    except Exception:
        accepted = False
    if not accepted:
        raise HumanResolutionAuthenticationFailed("trusted human resolution verification failed")


def _verified_human_resolution_event(batch: TransitionBatch) -> HumanResolutionRecorded:
    if not _is_human_resolution(batch):
        raise StoreConflict("human resolution requires exactly one authenticated event")
    event = batch.events[0]
    if not isinstance(event, HumanResolutionRecorded) or not event.verification_result:
        raise StoreConflict("human resolution requires verified public evidence")
    return event


def _verify_human_action_targets(
    connection: sqlite3.Connection, saga_id: SagaId, action: str
) -> None:
    if action == "reject":
        return
    reconciliation = _human_reconciliation_count(connection, saga_id)
    applicable = reconciliation == 0
    if action == "reconcile":
        applicable = reconciliation > 0
    if not applicable:
        raise HumanResolutionInapplicable("human decision action has no exact durable target")


def _suspended_outbox_count(connection: sqlite3.Connection, saga_id: SagaId) -> int:
    query = "SELECT COUNT(*) FROM outbox_commands WHERE saga_id = ? AND state = ?"
    return _human_target_count(connection, query, saga_id, "suspended_for_human")


def _human_reconciliation_count(connection: sqlite3.Connection, saga_id: SagaId) -> int:
    query = "SELECT COUNT(*) FROM reconciliation_jobs WHERE saga_id = ? AND state = ?"
    return _human_target_count(connection, query, saga_id, "human_required")


def _human_target_count(
    connection: sqlite3.Connection, query: str, saga_id: SagaId, state: str
) -> int:
    row = connection.execute(query, (saga_id, state)).fetchone()
    if row is None:
        raise StoreCorruption("human resolution target count is unavailable")
    return _as_int(row[0], "human resolution target count")


def _verify_terminal_transition_kind(
    connection: sqlite3.Connection, batch: TransitionBatch, kind: TransitionKind
) -> None:
    if kind is TransitionKind.TERMINAL:
        _verify_terminal_commit(connection, batch)


def _verify_reconciliation_transition_kind(batch: TransitionBatch, kind: TransitionKind) -> None:
    if kind is TransitionKind.RECONCILIATION:
        _verify_reconciliation_event_shape(batch)


def _verify_compensation_transition(
    connection: sqlite3.Connection, snapshot: SagaSnapshot, batch: TransitionBatch
) -> None:
    if any(isinstance(event, CompensationStarted) for event in batch.events):
        _verify_compensation_quiescence(connection, snapshot)


def _verify_claimed_lifecycle_access(batch: TransitionBatch, *, allowed: bool) -> None:
    lifecycle = (
        DispatchStarted,
        DispatchAbortedBeforeEntry,
        EffectOutcomeRecorded,
        ReconciliationRecorded,
    )
    if not allowed and any(isinstance(event, lifecycle) for event in batch.events):
        raise StoreConflict("dispatch lifecycle requires a specialized exact-claim operation")


def _verify_human_suspension(
    connection: sqlite3.Connection, snapshot: SagaSnapshot, batch: TransitionBatch
) -> None:
    if not _is_human_suspension(batch):
        raise StoreConflict("human suspension requires exactly one HumanRequired event")
    _verify_no_claimed_commands(connection, batch.saga_id)
    _verify_no_claimed_reconciliation_jobs(connection, batch.saga_id)
    _verify_no_dispatched_operations(snapshot)


def _verify_compensation_quiescence(connection: sqlite3.Connection, snapshot: SagaSnapshot) -> None:
    if _forward_outbox_blockers(connection, snapshot.saga_id):
        raise StoreConflict("active forward outbox work blocks compensation")
    if _unresolved_forward_blockers(snapshot):
        raise StoreConflict("unresolved forward operation blocks compensation")
    if _active_reconciliation_blockers(connection, snapshot.saga_id):
        raise StoreConflict("active reconciliation job blocks compensation")


def _verify_no_claimed_reconciliation_jobs(connection: sqlite3.Connection, saga_id: SagaId) -> None:
    row = connection.execute(
        "SELECT COUNT(*) FROM reconciliation_jobs WHERE saga_id = ? AND state = 'claimed'",
        (saga_id,),
    ).fetchone()
    if row is None or _as_int(row[0], "claimed reconciliation count"):
        raise StoreConflict("claimed reconciliation lookup blocks human suspension")


def _verify_no_claimed_commands(connection: sqlite3.Connection, saga_id: str) -> None:
    if _claimed_count(connection, saga_id):
        raise StoreConflict("claimed command blocks human suspension")


def _verify_no_dispatched_operations(snapshot: SagaSnapshot) -> None:
    if any(item.status is OperationStatus.DISPATCHED for item in snapshot.operations.values()):
        raise StoreConflict("in-flight command blocks human suspension")


def _verify_terminal_commit(connection: sqlite3.Connection, batch: TransitionBatch) -> None:
    event_types = tuple(type(event) for event in batch.events)
    if event_types != (InvariantEvaluated, TerminalAssigned):
        raise StoreConflict("terminal commit requires proof followed by assignment")
    row = connection.execute(
        "SELECT COUNT(*) FROM outbox_commands WHERE saga_id = ? "
        "AND state IN ('runnable', 'claimed')",
        (batch.saga_id,),
    ).fetchone()
    if row is None or _as_int(cast(tuple[object, ...], row)[0], "active outbox count"):
        raise StoreConflict("active outbox work blocks terminal assignment")


def _claimed_count(connection: sqlite3.Connection, saga_id: SagaId) -> int:
    row = connection.execute(
        "SELECT COUNT(*) FROM outbox_commands WHERE saga_id = ? AND state = 'claimed'",
        (saga_id,),
    ).fetchone()
    if row is None:
        raise StoreCorruption("claimed count query returned no row")
    return _as_int(cast(tuple[object, ...], row)[0], "claimed count")


def _unwind_quiescence(
    connection: sqlite3.Connection, row: _SagaRow, lease: Lease
) -> UnwindQuiescence:
    return UnwindQuiescence(
        saga_id=lease.saga_id,
        saga_seq=row.seq,
        fence_token=lease.fence_token,
        forward_outbox_blockers=_forward_outbox_blockers(connection, lease.saga_id),
        unresolved_forward_blockers=_unresolved_forward_blockers(row.snapshot),
        reconciliation_blockers=_active_reconciliation_blockers(connection, lease.saga_id),
        tool_evidence=_unwind_tool_evidence(connection, lease.saga_id),
    )


def _unwind_tool_evidence(
    connection: sqlite3.Connection, saga_id: SagaId
) -> tuple[UnwindToolEvidence, ...]:
    rows = connection.execute(
        "SELECT operation_id, tool_name, definition_version, command_schema_version, "
        "capability_digest FROM outbox_commands WHERE saga_id = ? AND direction = 'forward' "
        "ORDER BY operation_id",
        (saga_id,),
    ).fetchall()
    return tuple(_unwind_evidence_row(raw) for raw in rows)


def _unwind_evidence_row(raw: Sequence[object]) -> UnwindToolEvidence:
    digest = None if raw[4] is None else _as_str(raw[4], "unwind capability digest")
    return UnwindToolEvidence(
        operation_id=_as_str(raw[0], "unwind operation ID"),
        tool_name=_as_str(raw[1], "unwind tool name"),
        definition_version=_as_str(raw[2], "unwind definition version"),
        command_schema_version=_as_str(raw[3], "unwind command schema version"),
        capability_digest=digest,
    )


def _forward_outbox_blockers(
    connection: sqlite3.Connection, saga_id: SagaId
) -> tuple[OperationId, ...]:
    rows = connection.execute(
        "SELECT operation_id FROM outbox_commands WHERE saga_id = ? AND direction = 'forward' "
        "AND state IN ('runnable', 'claimed', 'parked') ORDER BY operation_id",
        (saga_id,),
    ).fetchall()
    return tuple(_as_str(row[0], "unwind outbox operation") for row in rows)


def _unresolved_forward_blockers(snapshot: SagaSnapshot) -> tuple[OperationId, ...]:
    unresolved = {
        OperationStatus.PLANNED,
        OperationStatus.INTENT_DURABLE,
        OperationStatus.DISPATCHED,
        OperationStatus.OUTCOME_UNKNOWN,
    }
    values = (
        item.operation_id
        for item in snapshot.operations.values()
        if item.direction is Direction.FORWARD and item.status in unresolved
    )
    return tuple(sorted(values))


def _active_reconciliation_blockers(
    connection: sqlite3.Connection, saga_id: SagaId
) -> tuple[OperationId, ...]:
    rows = connection.execute(
        "SELECT operation_id FROM reconciliation_jobs WHERE saga_id = ? "
        "AND state IN ('due', 'waiting', 'claimed', 'human_required') ORDER BY operation_id",
        (saga_id,),
    ).fetchall()
    return tuple(_as_str(row[0], "unwind reconciliation operation") for row in rows)


def _apply_specialized_transition(
    connection: sqlite3.Connection, batch: TransitionBatch, kind: TransitionKind
) -> None:
    if kind is TransitionKind.HUMAN_SUSPENSION:
        _apply_human_suspension(connection, batch)
        return
    if kind is TransitionKind.HUMAN_RESOLUTION:
        _apply_resolution_event(connection, batch)


def _apply_resolution_event(connection: sqlite3.Connection, batch: TransitionBatch) -> None:
    resolution = next(
        (event for event in batch.events if isinstance(event, HumanResolutionRecorded)), None
    )
    if resolution is not None and resolution.verification_result:
        _apply_human_resolution(connection, batch, resolution.action)


def _apply_human_suspension(connection: sqlite3.Connection, batch: TransitionBatch) -> None:
    connection.execute(
        "UPDATE outbox_commands SET state = 'suspended_for_human', suspended_at_seq = ? "
        "WHERE saga_id = ? AND state = 'runnable'",
        (batch.projection.seq, batch.saga_id),
    )
    connection.execute(
        "UPDATE reconciliation_jobs SET state = 'human_required' WHERE saga_id = ? "
        "AND state IN ('due', 'waiting')",
        (batch.saga_id,),
    )


def _apply_human_resolution(
    connection: sqlite3.Connection, batch: TransitionBatch, action: str
) -> None:
    if action == "approve":
        connection.execute(
            "UPDATE outbox_commands SET state = 'runnable', suspended_at_seq = NULL "
            "WHERE saga_id = ? AND state = 'suspended_for_human'",
            (batch.saga_id,),
        )
    if action == "reconcile":
        connection.execute(
            "UPDATE reconciliation_jobs SET state = 'due' WHERE saga_id = ? "
            "AND state = 'human_required'",
            (batch.saga_id,),
        )


def _build_saga_row(raw: Sequence[object], saga_id: SagaId) -> _SagaRow:
    snapshot = _parse_snapshot(_as_bytes(raw[0], "projection JSON"))
    seq = _as_int(raw[1], "Saga sequence")
    fence = _as_int(raw[2], "fence token")
    identity = (snapshot.saga_id, snapshot.seq, snapshot.status.value, snapshot.definition_version)
    stored = (saga_id, seq, _as_str(raw[5], "Saga status"), _as_str(raw[6], "definition"))
    if identity != stored:
        raise StoreCorruption("Saga row does not match stored projection")
    return _SagaRow(
        snapshot,
        seq,
        fence,
        _as_optional_str(raw[3], "lease owner"),
        _optional_utc(raw[4], "lease expiry"),
    )


def _database_now(connection: sqlite3.Connection) -> datetime:
    value = _pragma_str(
        connection,
        "SELECT strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')",
        "database time",
    )
    return _parse_utc(value, "database time")


def _clock_time(value: datetime) -> datetime:
    if value.utcoffset() != UTC.utcoffset(value):
        raise StoreCorruption("injected clock must use UTC")
    if value.microsecond % 1_000:
        raise StoreCorruption("injected clock must use millisecond precision")
    return value


def _utc_text_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _utc_text(value)


def _lease_is_live(row: _SagaRow, now: datetime) -> bool:
    return (
        row.lease_owner is not None
        and row.lease_expires_at is not None
        and row.lease_expires_at > now
    )


def _lease_expiry(now: datetime, duration: timedelta) -> datetime:
    try:
        return now + duration
    except OverflowError as error:
        raise ValueError("lease expiry is not representable") from error


def _extended_expiry(row: _SagaRow, now: datetime, duration: timedelta) -> datetime:
    if row.lease_expires_at is None:
        raise LeaseLost("Saga lease expiry is missing")
    return max(row.lease_expires_at, _lease_expiry(now, duration))


def _next_fence(value: int) -> int:
    if value >= _MAX_FENCE_TOKEN:
        raise LeaseUnavailable("Saga fence token is exhausted")
    return value + 1


def _require_current_lease(row: _SagaRow, lease: Lease, now: datetime) -> None:
    if row.fence_token != lease.fence_token or row.lease_owner != lease.owner:
        raise LeaseLost("Saga lease identity is stale")
    if not _lease_is_live(row, now):
        raise LeaseLost("Saga lease has expired")


def _lease_state_from_row(saga_id: SagaId, row: _SagaRow) -> Lease | None:
    if row.lease_owner is None or row.lease_expires_at is None:
        return None
    return Lease(
        saga_id=saga_id,
        owner=row.lease_owner,
        fence_token=row.fence_token,
        expires_at=row.lease_expires_at,
    )


def _parse_utc(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise StoreCorruption(f"stored {field} is not a valid UTC time") from error
    if parsed.utcoffset() != UTC.utcoffset(parsed):
        raise StoreCorruption(f"stored {field} is not UTC")
    return parsed


def _optional_utc(value: object, field: str) -> datetime | None:
    text = _as_optional_str(value, field)
    if text is None:
        return None
    return _parse_utc(text, field)


def _verify_bootstrap_lease(batch: TransitionBatch) -> None:
    if batch.lease_owner is not None:
        raise StaleFence("bootstrap fence cannot have a lease owner")


def _verify_event_fences(batch: TransitionBatch) -> None:
    expected = None if batch.expected_fence_token == 0 else batch.expected_fence_token
    if any(event.fence_token != expected for event in batch.events):
        raise StaleFence("event fence does not match transition fence")


def _verify_active_lease(row: _SagaRow, batch: TransitionBatch, now: datetime) -> None:
    if row.lease_owner != batch.lease_owner or row.lease_expires_at is None:
        raise StaleFence("lease owner does not hold the current fence")
    if row.lease_expires_at <= now:
        raise StaleFence("lease expired before transition")


def _select_claimable(
    connection: sqlite3.Connection, now: str, saga_id: SagaId | None
) -> _OutboxRow | None:
    query = _CLAIMABLE_OUTBOX_SELECT
    values: tuple[str, ...] = (now, now)
    if saga_id is not None:
        query = _CLAIMABLE_SAGA_OUTBOX_SELECT
        values = (saga_id, now, now)
    row = connection.execute(query, values).fetchone()
    if row is None:
        return None
    return _outbox_row(cast(Sequence[object], row))


type _OutboxIdentity = tuple[str, str, str, str, str, str, str, Direction]
type _OutboxClaimDetails = tuple[
    str | None, str | None, datetime | None, int, int, int, int | None, int
]
type _OutboxDetails = tuple[
    int,
    JsonObject,
    str,
    datetime,
    OutboxState,
    str | None,
    str | None,
    datetime | None,
    int,
    int,
    int,
    int | None,
    int,
]


def _outbox_row(raw: Sequence[object]) -> _OutboxRow:
    values = (*_outbox_identity(raw), *_outbox_details(raw))
    return _OutboxRow(*values)


def _outbox_identity(raw: Sequence[object]) -> _OutboxIdentity:
    return (
        _as_str(raw[0], "command ID"),
        _as_str(raw[1], "Saga ID"),
        _as_str(raw[2], "operation ID"),
        _as_str(raw[3], "tool name"),
        _as_str(raw[4], "definition version"),
        _as_str(raw[5], "command schema version"),
        _as_str(raw[6], "step instance ID"),
        Direction(_as_str(raw[7], "direction")),
    )


def _outbox_details(raw: Sequence[object]) -> _OutboxDetails:
    command_hash = _as_str(raw[10], "command hash")
    return (
        _as_int(raw[8], "semantic generation"),
        _decode_command_bytes(_as_bytes(raw[9], "command JSON"), command_hash),
        command_hash,
        _parse_utc(_as_str(raw[11], "available time"), "outbox available time"),
        _outbox_state(raw[12]),
        *_outbox_claim_details(raw),
    )


def _outbox_claim_details(raw: Sequence[object]) -> _OutboxClaimDetails:
    return (
        _as_optional_str(raw[13], "claim ID"),
        _as_optional_str(raw[14], "claim owner"),
        _optional_utc(raw[15], "claim expiry"),
        _as_int(raw[16], "claim generation"),
        _as_int(raw[17], "claim fence"),
        _as_int(raw[18], "delivery attempt"),
        _as_optional_int(raw[19], "suspension sequence"),
        _as_int(raw[20], "Saga fence"),
    )


def _outbox_state(value: object) -> OutboxState:
    try:
        return OutboxState(_as_str(value, "outbox state"))
    except ValueError as error:
        raise StoreCorruption("stored outbox state is invalid") from error


def _update_claim(
    connection: sqlite3.Connection,
    row: _OutboxRow,
    claim: _Claim,
) -> sqlite3.Cursor:
    return connection.execute(
        "UPDATE outbox_commands SET state = 'claimed', claim_id = ?, claim_owner = ?, "
        "claim_expires_at = ?, claim_generation = ?, claim_fence_token = ?, "
        "delivery_attempt = ? "
        "WHERE command_id = ? AND state = ? AND claim_generation = ?",
        _claim_update_values(row, claim),
    )


def _claim_update_values(row: _OutboxRow, claim: _Claim) -> tuple[object, ...]:
    return (
        claim.claim_id,
        claim.owner,
        claim.expires,
        claim.generation,
        row.saga_fence_token,
        claim.attempt,
        row.command_id,
        row.state.value,
        row.claim_generation,
    )


def _claimed_command(row: _OutboxRow, claim: _Claim, fence_token: int) -> ClaimedCommand:
    envelope = _outbox_envelope(row)
    metadata = ClaimMetadata(
        claim_id=claim.claim_id,
        claim_owner=claim.owner,
        claim_expires_at=_parse_utc(claim.expires, "claim expiry"),
        claim_generation=claim.generation,
        delivery_attempt=claim.attempt,
        saga_fence_token=fence_token,
    )
    return ClaimedCommand(envelope=envelope, claim=metadata)


def _claimed_with_proof(
    connection: sqlite3.Connection, row: _OutboxRow, claim: _Claim, fence_token: int
) -> ClaimedCommand:
    claimed = _claimed_command(row, claim, fence_token)
    proof = _load_capability_proof(connection, row.command_id)
    envelope = claimed.envelope.model_copy(update={"capability_proof": proof})
    return claimed.model_copy(update={"envelope": envelope})


def _outbox_envelope(row: _OutboxRow) -> OutboxCommand:
    try:
        return OutboxCommand.model_validate(row, from_attributes=True)
    except ValidationError as error:
        raise StoreCorruption("stored outbox command envelope is invalid") from error


def _decode_command_bytes(command_bytes: bytes, command_hash: str) -> JsonObject:
    try:
        command = _JSON_OBJECT_ADAPTER.validate_json(command_bytes, strict=True)
    except ValidationError as error:
        raise StoreCorruption("stored outbox command is invalid JSON") from error
    public_command = thaw_json_object(command)
    canonical = _canonical_bytes(public_command)
    if canonical != command_bytes or _json_digest(public_command) != command_hash:
        raise StoreCorruption("stored outbox command hash or canonical bytes do not match")
    return command


def _verify_stored_outbox_row(connection: sqlite3.Connection, raw: Sequence[object]) -> None:
    row = _outbox_row(raw)
    _load_capability_proof(connection, row.command_id)
    _verify_stored_claim(row)
    envelope = _outbox_envelope(row)
    saga = SQLiteKernelStore._load_saga_row(connection, envelope.saga_id)
    operation = saga.snapshot.operations.get(envelope.operation_id)
    if operation is None:
        raise StoreCorruption("stored outbox command has no durable operation")
    try:
        _verify_command(operation, envelope)
    except StoreConflict as error:
        raise StoreCorruption("stored outbox command does not match its intent") from error
    _verify_outbox_lifecycle(operation, row.state)
    _verify_suspension_binding(connection, row)


def _verify_suspension_binding(connection: sqlite3.Connection, row: _OutboxRow) -> None:
    if row.state is not OutboxState.SUSPENDED_FOR_HUMAN:
        return
    receipts = _suspension_receipts(connection, row)
    if len(receipts) != 1 or not _receipt_suspends_for_human(receipts[0]):
        raise StoreCorruption("suspended command lacks exact HumanRequired transition")
    batch = _parse_transition(receipts[0].transition_bytes)
    if not _batch_suspends_for_human(batch, receipts[0].kind):
        raise StoreCorruption("suspension receipt lacks exact HumanRequired event")


def _receipt_suspends_for_human(receipt: _Receipt) -> bool:
    return receipt.kind in {
        TransitionKind.HUMAN_SUSPENSION,
        TransitionKind.RECONCILIATION,
    }


def _batch_suspends_for_human(batch: TransitionBatch, kind: TransitionKind) -> bool:
    if kind is TransitionKind.HUMAN_SUSPENSION:
        return _is_human_suspension(batch)
    try:
        _verify_reconciliation_event_shape(batch)
    except StoreConflict:
        return False
    return any(isinstance(event, HumanRequired) for event in batch.events)


def _suspension_receipts(connection: sqlite3.Connection, row: _OutboxRow) -> tuple[_Receipt, ...]:
    receipts = (_receipt_row(raw) for raw in _receipt_rows(connection, row.saga_id))
    return tuple(item for item in receipts if item.resulting_seq == row.suspended_at_seq)


def _verify_historical_claim_fences(connection: sqlite3.Connection) -> None:
    for raw in _stored_outbox_rows(connection):
        _verify_historical_claim_fence(connection, _outbox_row(raw))


def _verify_historical_claim_fence(connection: sqlite3.Connection, row: _OutboxRow) -> None:
    if row.state not in {OutboxState.COMPLETED, OutboxState.PARKED}:
        return
    fences = _disposition_receipt_fences(connection, row)
    if not fences or fences[-1] != row.claim_fence_token:
        raise StoreCorruption("stored historical claim fence lacks matching disposition evidence")


def _disposition_receipt_fences(connection: sqlite3.Connection, row: _OutboxRow) -> tuple[int, ...]:
    return tuple(
        batch.expected_fence_token
        for batch in _receipt_batches(connection, row.saga_id)
        if _batch_has_outcome(batch, row.operation_id)
    )


def _receipt_batches(
    connection: sqlite3.Connection, saga_id: SagaId
) -> tuple[TransitionBatch, ...]:
    return tuple(
        _parse_transition(_receipt_row(raw).transition_bytes)
        for raw in _receipt_rows(connection, saga_id)
    )


def _batch_has_outcome(batch: TransitionBatch, operation_id: str) -> bool:
    return any(
        isinstance(event, EffectOutcomeRecorded) and event.operation_id == operation_id
        for event in batch.events
    )


def _verify_stored_claim(row: _OutboxRow) -> None:
    if row.state in {OutboxState.RUNNABLE, OutboxState.SUSPENDED_FOR_HUMAN}:
        _verify_runnable_claim_fields(row)
        return
    _verify_nonrunnable_claim(row)


def _verify_nonrunnable_claim(row: _OutboxRow) -> None:
    claim_id, owner, expiry = _required_claim_fields(row)
    try:
        ClaimMetadata(
            claim_id=claim_id,
            claim_owner=owner,
            claim_expires_at=expiry,
            claim_generation=row.claim_generation,
            delivery_attempt=row.delivery_attempt,
            saga_fence_token=row.claim_fence_token,
        )
    except ValidationError as error:
        raise StoreCorruption("stored claim metadata is invalid") from error


def _required_claim_fields(row: _OutboxRow) -> tuple[str, str, datetime]:
    claim_id, owner, expiry = row.claim_id, row.claim_owner, row.claim_expires_at
    if claim_id is None or owner is None or expiry is None:
        raise StoreCorruption("stored claim metadata is incomplete")
    return claim_id, owner, expiry


def _verify_runnable_claim_fields(row: _OutboxRow) -> None:
    identity = (row.claim_id, row.claim_owner, row.claim_expires_at)
    counters = (row.claim_generation, row.claim_fence_token, row.delivery_attempt)
    fresh = counters == (0, 0, 0)
    retried = row.claim_generation >= 1 and row.delivery_attempt >= 1
    if identity != (None, None, None) or not (fresh or retried):
        raise StoreCorruption("stored runnable command carries claim metadata")
    _verify_suspension_fields(row)


def _verify_suspension_fields(row: _OutboxRow) -> None:
    if row.state is OutboxState.RUNNABLE and row.suspended_at_seq is not None:
        raise StoreCorruption("runnable command carries suspension evidence")
    if row.state is OutboxState.SUSPENDED_FOR_HUMAN and row.suspended_at_seq is None:
        raise StoreCorruption("suspended command lacks suspension evidence")


def _verify_outbox_lifecycle(operation: OperationRecord, state: OutboxState) -> None:
    if state not in _OUTBOX_LIFECYCLE.get(operation.status, frozenset()):
        raise StoreCorruption("stored outbox lifecycle does not match durable operation")


def _durable_claim(connection: sqlite3.Connection, supplied: ClaimedCommand) -> ClaimedCommand:
    row = _load_outbox_row(connection, supplied.command_id, supplied.saga_id)
    durable = _claimed_with_proof(connection, row, _claim_from_row(row), row.claim_fence_token)
    if durable != supplied:
        raise StoreConflict("supplied claimed operation or fence does not match durable claim")
    return durable


def _require_live_claim(
    connection: sqlite3.Connection, claimed: ClaimedCommand, now: datetime
) -> None:
    saga = SQLiteKernelStore._load_saga_row(connection, claimed.saga_id)
    if saga.fence_token != claimed.saga_fence_token:
        raise StoreConflict("outbox claim has a stale Saga fence")
    if claimed.claim_expires_at <= now:
        raise StoreConflict("outbox claim expired before transition")


def _retry_aborted_transition(
    receipt: _Receipt, claimed: ClaimedCommand, batch: TransitionBatch
) -> _CommitResult:
    snapshot = SQLiteKernelStore._verify_receipt(receipt, batch, _batch_digest(batch))
    _verify_abort_batch(claimed, batch)
    return _CommitResult(snapshot, False)


def _claim_from_row(row: _OutboxRow) -> _Claim:
    claim_id, owner, expiry = _required_claim_fields(row)
    return _Claim(claim_id, owner, _utc_text(expiry), row.claim_generation, row.delivery_attempt)


def _load_outbox_row(
    connection: sqlite3.Connection, command_id: str, saga_id: SagaId
) -> _OutboxRow:
    row = connection.execute(
        _OUTBOX_SELECT + "WHERE o.command_id = ? AND o.saga_id = ?", (command_id, saga_id)
    ).fetchone()
    if row is None:
        raise StoreConflict("durable outbox claim does not exist")
    return _outbox_row(cast(Sequence[object], row))


def _stored_outbox_rows(connection: sqlite3.Connection) -> Sequence[Sequence[object]]:
    rows = connection.execute(_OUTBOX_SELECT + "ORDER BY o.command_id").fetchall()
    return cast(Sequence[Sequence[object]], rows)


def _verify_all_outbox_cardinality(connection: sqlite3.Connection) -> None:
    saga_rows = connection.execute("SELECT saga_id FROM sagas ORDER BY saga_id").fetchall()
    for raw in saga_rows:
        saga_id = _as_str(cast(tuple[object, ...], raw)[0], "Saga ID")
        snapshot = SQLiteKernelStore._load_saga_row(connection, saga_id).snapshot
        if _outbox_operation_ids(connection, saga_id) != frozenset(snapshot.operations):
            raise StoreCorruption("stored outbox cardinality does not match durable intents")


def _outbox_operation_ids(connection: sqlite3.Connection, saga_id: SagaId) -> frozenset[str]:
    rows = connection.execute(
        "SELECT operation_id FROM outbox_commands WHERE saga_id = ?", (saga_id,)
    ).fetchall()
    return frozenset(
        _as_str(cast(tuple[object, ...], raw)[0], "outbox operation ID") for raw in rows
    )


def _verify_claim_batch(
    claimed: ClaimedCommand, batch: TransitionBatch, state: OutboxState
) -> None:
    _verify_claim_owner(claimed, batch.lease_owner)
    if batch.saga_id != claimed.saga_id:
        raise StoreConflict("outbox completion transition belongs to another Saga")
    if batch.expected_fence_token != claimed.saga_fence_token:
        raise StoreConflict("outbox completion transition has a stale Saga fence")
    event = _disposition_event(batch)
    if _event_claim_signature(event) != _claimed_signature(claimed):
        raise StoreConflict("outcome does not match the claimed operation")
    _verify_disposition(state, event)


def _verify_dispatch_batch(claimed: ClaimedCommand, batch: TransitionBatch) -> None:
    event = _single_claim_event(batch, DispatchStarted, "dispatch start")
    _verify_claim_transition_identity(claimed, batch, event)


def _verify_abort_batch(claimed: ClaimedCommand, batch: TransitionBatch) -> None:
    event = _single_claim_event(batch, DispatchAbortedBeforeEntry, "dispatch abort")
    _verify_claim_transition_identity(claimed, batch, event)


def _single_claim_event[EventT: (DispatchStarted, DispatchAbortedBeforeEntry)](
    batch: TransitionBatch, expected: type[EventT], action: str
) -> EventT:
    if len(batch.events) != 1 or not isinstance(batch.events[0], expected):
        raise StoreConflict(f"{action} requires exactly one matching event")
    return batch.events[0]


def _verify_claim_transition_identity(
    claimed: ClaimedCommand,
    batch: TransitionBatch,
    event: DispatchStarted | DispatchAbortedBeforeEntry,
) -> None:
    _verify_claim_owner(claimed, batch.lease_owner)
    if batch.saga_id != claimed.saga_id:
        raise StoreConflict("claimed transition belongs to another Saga")
    if batch.expected_fence_token != claimed.saga_fence_token:
        raise StoreConflict("claimed transition has a stale Saga fence")
    if _event_claim_signature(event) != _claimed_signature(claimed):
        raise StoreConflict("transition does not match the claimed operation")


def _verify_claim_owner(claimed: ClaimedCommand, lease_owner: str | None) -> None:
    if lease_owner is not None and claimed.claim_owner != lease_owner:
        raise StoreConflict("outbox claim owner does not match the Saga lease owner")


def _disposition_event(batch: TransitionBatch) -> EffectOutcomeRecorded:
    if len(batch.events) != 1 or not isinstance(batch.events[0], EffectOutcomeRecorded):
        raise StoreConflict("outbox disposition requires exactly one outcome event")
    return batch.events[0]


def _event_claim_signature(
    event: DispatchStarted
    | DispatchAbortedBeforeEntry
    | EffectOutcomeRecorded
    | ReconciliationRecorded,
) -> tuple[object, ...]:
    return (
        event.operation_id,
        event.tool_name,
        event.step_instance_id,
        event.direction,
        event.semantic_generation,
        event.command_hash,
        event.delivery_attempt,
        event.redacted_command,
    )


def _claimed_signature(claimed: ClaimedCommand) -> tuple[object, ...]:
    return (
        claimed.operation_id,
        claimed.tool_name,
        claimed.step_instance_id,
        claimed.direction,
        claimed.semantic_generation,
        claimed.command_hash,
        claimed.delivery_attempt,
        claimed.command,
    )


def _verify_disposition(state: OutboxState, event: EffectOutcomeRecorded) -> None:
    is_unknown = event.outcome.kind == "outcome_unknown"
    if (state is OutboxState.PARKED) != is_unknown:
        raise StoreConflict("outbox disposition does not match outcome semantics")


def _claim_is_expired(
    connection: sqlite3.Connection, claimed: ClaimedCommand, now: datetime
) -> bool:
    row = connection.execute(
        "SELECT claim_expires_at FROM outbox_commands WHERE command_id = ? AND saga_id = ?",
        (claimed.command_id, claimed.saga_id),
    ).fetchone()
    if row is None:
        return False
    expiry = _parse_utc(_as_str(cast(tuple[object, ...], row)[0], "claim expiry"), "claim expiry")
    return expiry <= now


def _verify_finished_claim(
    connection: sqlite3.Connection, claimed: ClaimedCommand, state: OutboxState
) -> None:
    row = connection.execute(
        "SELECT state, claim_id, claim_owner, claim_generation, claim_fence_token "
        "FROM outbox_commands WHERE command_id = ? AND saga_id = ?",
        (claimed.command_id, claimed.saga_id),
    ).fetchone()
    if row is None:
        raise StoreConflict("transition receipt does not match finalized outbox claim")
    if _finished_claim_identity(cast(Sequence[object], row)) != _expected_claim(claimed, state):
        raise StoreConflict("transition receipt does not match finalized outbox claim")


def _expected_claim(claimed: ClaimedCommand, state: OutboxState) -> tuple[str, str, str, int, int]:
    return (
        state.value,
        claimed.claim_id,
        claimed.claim_owner,
        claimed.claim_generation,
        claimed.saga_fence_token,
    )


def _finished_claim_identity(row: Sequence[object]) -> tuple[str, str, str, int, int]:
    return (
        _as_str(row[0], "outbox state"),
        _as_str(row[1], "claim ID"),
        _as_str(row[2], "claim owner"),
        _as_int(row[3], "claim generation"),
        _as_int(row[4], "Saga fence"),
    )


def _validate_backup_destination(source: Path, destination: Path) -> None:
    _require_secure_parent(destination)
    if _same_file(source, destination):
        raise StoreConflict("backup destination is the source database")
    _require_backup_availability(destination)
    _require_no_sidecars(destination)


def _same_file(source: Path, destination: Path) -> bool:
    if source == destination:
        return True
    if not _leaf_exists(destination):
        return False
    return os.path.samestat(source.stat(follow_symlinks=False), destination.lstat())


def _require_backup_availability(destination: Path) -> None:
    if not _leaf_exists(destination):
        return
    details = destination.lstat()
    if not stat.S_ISREG(details.st_mode):
        raise StoreConflict("backup destination is not a regular file")
    raise StoreConflict(f"backup destination already exists: {destination}")


def _require_no_sidecars(destination: Path) -> None:
    if any(_leaf_exists(sidecar) for sidecar in _sidecar_paths(destination)):
        raise StoreConflict("backup destination has active or unsafe SQLite sidecars")


def _link_new_backup(temporary: Path, destination: Path) -> None:
    try:
        os.link(temporary, destination, follow_symlinks=False)
    except FileExistsError as error:
        raise StoreConflict(f"backup destination already exists: {destination}") from error
    except OSError as error:
        raise StoreConflict("backup cannot be published atomically") from error


def _observed_leaf_identity(path: Path) -> _FileIdentity | None:
    try:
        return _file_identity(path.lstat())
    except FileNotFoundError:
        return None
    except OSError as error:
        raise StoreConflict(f"storage path cannot be inspected: {path}") from error


def _require_leaf_identity(path: Path, expected: _FileIdentity) -> None:
    if _observed_leaf_identity(path) != expected:
        raise StoreConflict(f"storage path identity changed: {path}")


def _remove_owned_leaf(path: Path, expected: _FileIdentity) -> bool:
    try:
        if _observed_leaf_identity(path) != expected:
            return False
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _hold_claimed_leaf(path: Path, expected: _FileIdentity) -> int:
    descriptor: int | None = None
    try:
        descriptor = _open_published_file(path)
        if _file_identity(os.fstat(descriptor)) != expected:
            raise StoreConflict(f"storage path identity changed: {path}")
        return descriptor
    except BaseException:
        _remove_owned_leaf(path, expected)
        if descriptor is not None:
            os.close(descriptor)
        raise


@contextmanager
def _claimed_backup_file(path: Path) -> Iterator[_FileIdentity]:
    claimed = _claim_private_file(path)
    descriptor = _hold_claimed_leaf(path, claimed)
    try:
        yield claimed
    except BaseException:
        _remove_owned_leaf(path, claimed)
        raise
    finally:
        os.close(descriptor)


def _open_published_file(path: Path) -> int:
    try:
        return os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise StoreConflict("published backup cannot be opened safely") from error


def _verify_published_leaf(path: Path, expected: _FileIdentity) -> None:
    descriptor = _open_published_file(path)
    try:
        if _file_identity(os.fstat(descriptor)) != expected:
            raise StoreConflict("published backup identity does not match temporary file")
        if _harden_file_descriptor(descriptor, path) != expected:
            raise StoreConflict("published backup identity changed while securing it")
        _require_leaf_identity(path, expected)
    except OSError as error:
        raise StoreConflict("published backup cannot be verified safely") from error
    finally:
        os.close(descriptor)


def _verify_published_backup(destination: Path, expected: _FileIdentity) -> None:
    try:
        _require_no_sidecars(destination)
        _verify_published_leaf(destination, expected)
        _require_no_sidecars(destination)
    except BaseException:
        _remove_owned_leaf(destination, expected)
        raise


def _validate_final_backup(destination: Path, expected: _FileIdentity) -> None:
    try:
        _require_no_sidecars(destination)
        _require_leaf_identity(destination, expected)
    except BaseException:
        _remove_owned_leaf(destination, expected)
        raise


def _publish_new_backup(temporary: Path, destination: Path, expected: _FileIdentity) -> None:
    _require_leaf_identity(temporary, expected)
    _require_no_sidecars(temporary)
    _require_no_sidecars(destination)
    _link_new_backup(temporary, destination)
    _verify_published_backup(destination, expected)
    if not _remove_owned_leaf(temporary, expected):
        _remove_owned_leaf(destination, expected)
        raise StoreConflict("backup temporary file identity changed during publication")
    _validate_final_backup(destination, expected)


def _watermarks(connection: sqlite3.Connection) -> tuple[_Watermark, ...]:
    rows = connection.execute(
        "SELECT s.saga_id, s.saga_seq, s.projection_json, e.event_hash "
        "FROM sagas s JOIN ledger_events e "
        "ON e.saga_id = s.saga_id AND e.saga_seq = s.saga_seq ORDER BY s.saga_id"
    ).fetchall()
    return tuple(_watermark(cast(Sequence[object], row)) for row in rows)


def _watermark(row: Sequence[object]) -> _Watermark:
    projection = _as_bytes(row[2], "watermark projection")
    return _Watermark(
        _as_str(row[0], "watermark Saga ID"),
        _as_int(row[1], "watermark Saga sequence"),
        sha256(projection).hexdigest(),
        _as_str(row[3], "watermark event hash"),
    )


def _verify_watermarks(store: SQLiteKernelStore, marks: tuple[_Watermark, ...]) -> None:
    with closing(store._connection()) as connection:
        restored = _watermarks(connection)
    if restored != marks:
        raise StoreCorruption("backup does not match captured source watermark")


__all__ = ["SQLiteKernelStore"]
