"""Tests for `aeth_ext.central_log_server.client.emergency.EmergencyModeTracker`."""

# Standard library imports
from time import monotonic
from typing import TYPE_CHECKING

# First party imports
from aeth_ext.central_log_server.client.emergency import EmergencyModeTracker

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path

  # Third party imports
  import pytest


class TestMaybeEnter:
  def test_enters_emergency_mode_once_both_thresholds_are_exceeded(self, tmp_path: Path) -> None:
    tracker = EmergencyModeTracker(tmp_path, "prog", time_threshold=0.0, attempt_threshold=1)
    tracker.record_failure()
    tracker._last_success_monotonic = monotonic() - 1000  # pyright: ignore[reportPrivateUsage]

    tracker.maybe_enter()

    assert tracker.writer is not None
    tracker.close()

  def test_stays_out_of_emergency_mode_below_thresholds(self, tmp_path: Path) -> None:
    tracker = EmergencyModeTracker(tmp_path, "prog", time_threshold=999.0, attempt_threshold=999)
    tracker.record_failure()

    tracker.maybe_enter()

    assert tracker.writer is None

  def test_does_not_spin_up_a_second_writer_once_already_active(self, tmp_path: Path) -> None:
    tracker = EmergencyModeTracker(tmp_path, "prog", time_threshold=0.0, attempt_threshold=1)
    tracker.record_failure()
    tracker._last_success_monotonic = monotonic() - 1000  # pyright: ignore[reportPrivateUsage]
    tracker.maybe_enter()
    first_writer = tracker.writer

    tracker.maybe_enter()

    assert tracker.writer is first_writer
    tracker.close()


class TestRecordFailure:
  def test_increments_consecutive_failures_towards_the_attempt_threshold(self, tmp_path: Path) -> None:
    tracker = EmergencyModeTracker(tmp_path, "prog", time_threshold=0.0, attempt_threshold=2)
    tracker._last_success_monotonic = monotonic() - 1000  # pyright: ignore[reportPrivateUsage]

    tracker.record_failure()
    tracker.maybe_enter()
    assert tracker.writer is None

    tracker.record_failure()
    tracker.maybe_enter()
    assert tracker.writer is not None
    tracker.close()


class TestRecordSuccess:
  def test_closes_the_writer_and_resets_failures(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = EmergencyModeTracker(tmp_path, "prog", time_threshold=0.0, attempt_threshold=1)
    tracker.record_failure()
    tracker._last_success_monotonic = monotonic() - 1000  # pyright: ignore[reportPrivateUsage]
    tracker.maybe_enter()
    writer = tracker.writer
    assert writer is not None
    writer_closed: list[bool] = []
    monkeypatch.setattr(writer, "close", lambda: writer_closed.append(True))

    tracker.record_success()

    assert tracker.writer is None
    assert writer_closed == [True]
    assert tracker._consecutive_failures == 0  # pyright: ignore[reportPrivateUsage]

  def test_is_a_noop_when_not_in_emergency_mode(self, tmp_path: Path) -> None:
    tracker = EmergencyModeTracker(tmp_path, "prog", time_threshold=0.0, attempt_threshold=1)

    tracker.record_success()  # must not raise

    assert tracker.writer is None


class TestClose:
  def test_closes_an_active_writer(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = EmergencyModeTracker(tmp_path, "prog", time_threshold=0.0, attempt_threshold=1)
    tracker.record_failure()
    tracker._last_success_monotonic = monotonic() - 1000  # pyright: ignore[reportPrivateUsage]
    tracker.maybe_enter()
    writer = tracker.writer
    assert writer is not None
    writer_closed: list[bool] = []
    monkeypatch.setattr(writer, "close", lambda: writer_closed.append(True))

    tracker.close()

    assert tracker.writer is None
    assert writer_closed == [True]

  def test_is_a_noop_when_not_in_emergency_mode(self, tmp_path: Path) -> None:
    tracker = EmergencyModeTracker(tmp_path, "prog", time_threshold=0.0, attempt_threshold=1)

    tracker.close()  # must not raise
