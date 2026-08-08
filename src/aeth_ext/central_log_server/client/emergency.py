# Standard library imports
import logging
from time import monotonic
from typing import TYPE_CHECKING

# First party imports
from aeth_ext.central_log_server.client.history import EmergencyHistoryWriter

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["EmergencyModeTracker"]


class EmergencyModeTracker:
  """Tracks whether a client transport should be writing straight to its emergency history file.

  Once both *time_threshold* (seconds since the last successful send) and *attempt_threshold*
  (consecutive failed attempts) are exceeded, :meth:`maybe_enter` spins up an
  :class:`~aeth_ext.central_log_server.client.history.EmergencyHistoryWriter` so new records survive a
  process crash even while the server is unreachable. Deliberately decoupled from any
  :class:`~aeth_ext.central_log_server.client.durability.RecordDurability` instance - it only needs a
  directory and a program name, not a whole durability object, so it can be constructed and tested
  independently.
  """

  def __init__(self, history_dir: Path, program_name: str, *, time_threshold: float, attempt_threshold: int) -> None:
    self._history_dir = history_dir
    self._program_name = program_name
    self._time_threshold = time_threshold
    self._attempt_threshold = attempt_threshold
    self._consecutive_failures = 0
    self._last_success_monotonic = monotonic()
    self._writer: EmergencyHistoryWriter | None = None

  @property
  def writer(self) -> EmergencyHistoryWriter | None:
    """The active emergency writer, or ``None`` if not currently in emergency mode."""
    return self._writer

  def record_failure(self) -> None:
    """Count one more consecutive failed send/connect attempt."""
    self._consecutive_failures += 1

  def record_success(self) -> None:
    """Reset the failure counter and stamp the last-success time; exit emergency mode if it was active."""
    self._consecutive_failures = 0
    self._last_success_monotonic = monotonic()
    writer = self._writer
    if writer is None:
      return
    self._writer = None
    writer.close()
    logger.info("Log server reachable again for %r; stopped emergency history writer", self._program_name)

  def maybe_enter(self) -> None:
    """Spin up an emergency writer if both thresholds have tripped and one isn't already active."""
    if self._writer is not None:
      return
    elapsed = monotonic() - self._last_success_monotonic
    if elapsed >= self._time_threshold and self._consecutive_failures >= self._attempt_threshold:
      self._writer = EmergencyHistoryWriter(self._history_dir)
      logger.warning(
        "Log server unreachable for %.0fs after %d attempts for %r; writing new records directly to history file",
        elapsed,
        self._consecutive_failures,
        self._program_name,
      )

  def close(self) -> None:
    """Close the active writer, if any. Idempotent."""
    if self._writer is not None:
      self._writer.close()
      self._writer = None
