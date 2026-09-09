from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agentic_saga.contracts.clock import FakeClock, SystemClock


def test_should_return_aware_utc_time_from_system_clock() -> None:
    # Given
    clock = SystemClock()

    # When
    value = clock.now()

    # Then
    assert value.tzinfo is UTC


def test_should_advance_fake_clock_by_exact_duration() -> None:
    # Given
    initial = datetime(2026, 9, 6, 12, tzinfo=UTC)
    clock = FakeClock(initial)

    # When
    clock.advance(timedelta(milliseconds=125))

    # Then
    assert clock.now() == datetime(2026, 9, 6, 12, 0, 0, 125_000, tzinfo=UTC)


def test_should_reject_non_utc_fake_clock_time() -> None:
    # Given / When / Then
    with pytest.raises(ValueError, match="UTC"):
        FakeClock(datetime(2026, 9, 6, 12))
