"""Time source contract shared by deterministic core components."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Protocol, runtime_checkable


def _require_utc(value: datetime) -> datetime:
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("clock time must use UTC")
    return value


@runtime_checkable
class Clock(Protocol):
    """Supply authoritative UTC timestamps to deterministic kernel services."""

    def now(self) -> datetime: ...


@dataclass(frozen=True)
class SystemClock:
    """UTC wall clock used outside durable SQLite transactions."""

    def now(self) -> datetime:
        return datetime.now(tz=UTC)


@dataclass(frozen=True)
class FakeClock:
    """Thread-safe deterministic UTC clock for tests."""

    initial: datetime
    _current: datetime = field(init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_current", _require_utc(self.initial))

    def now(self) -> datetime:
        with self._lock:
            return self._current

    def advance(self, duration: timedelta) -> None:
        if duration < timedelta(0):
            raise ValueError("clock advance must not be negative")
        with self._lock:
            object.__setattr__(self, "_current", self._current + duration)


__all__ = ["Clock", "FakeClock", "SystemClock"]
