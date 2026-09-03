"""Per-transport record durability: id assignment, history buffering, and checkpointed resume state."""

# Standard library imports
from typing import TYPE_CHECKING, Literal

# First party imports
from aeth_ext.central_log_server.client.history import EmergencyHistoryWriter, HistoryEntry, RecordHistoryBuffer
from aeth_ext.central_log_server.client.id_checkpoint import (
  AsyncioIdCheckpointBackend,
  IdCheckpointBackend,
  ThreadedIdCheckpointBackend,
)
from aeth_ext.logging.emergency_diagnostics import emergency_diagnostic
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Standard library imports
  import logging
  from asyncio import AbstractEventLoop
  from pathlib import Path

  # First party imports
  from aeth_ext.central_log_server.protocol import HandshakeAck
  from aeth_ext.logging.bases import TaggedLogRecord

settings = BaseSettings.get_settings()

__all__ = ["RecordDurability"]


class RecordDurability:
  """Owns everything about making one client transport's records durable and replayable.

  Wraps a :class:`~aeth_ext.central_log_server.client.history.RecordHistoryBuffer` and an
  :class:`~aeth_ext.central_log_server.client.id_checkpoint.IdCheckpointBackend`, and provides the
  "assign an id, wrap in a HistoryEntry, append to history, schedule checkpoint persistence, optionally
  hand to a local logger, optionally submit to an active emergency writer" sequence as a single
  :meth:`record` call - this exact sequence was previously duplicated 5 times across the three client
  transport classes.
  """

  def __init__(
    self,
    program_name: str,
    max_records: int = 50_000,
    max_bytes: int = 64 * 1024 * 1024,
    max_age: float = 300.0,
    *,
    id_checkpoint_backend: Literal["thread", "asyncio"] = "thread",
    event_loop: AbstractEventLoop | None = None,
  ) -> None:
    """Build the history buffer and id-checkpoint backend; ids resume after the last checkpointed one."""
    self._program_name = program_name
    self.history = RecordHistoryBuffer(program_name, max_records, max_bytes, max_age)

    checkpoint_path = settings.persisted_dir_loc / "logging_ids.checkpoint"
    self._id_checkpoint: IdCheckpointBackend
    if id_checkpoint_backend == "asyncio":
      if event_loop is None:
        raise ValueError("event_loop is required when id_checkpoint_backend='asyncio'")
      self._id_checkpoint = AsyncioIdCheckpointBackend(checkpoint_path, event_loop)
    else:
      self._id_checkpoint = ThreadedIdCheckpointBackend(checkpoint_path)

    self._next_id = self._id_checkpoint.load() + 1
    self._last_sent_id = 0

  @property
  def history_dir(self) -> Path:
    """Directory the history buffer writes this program's files to."""
    return self.history.history_dir

  @property
  def last_sent_id(self) -> int:
    """Highest entry id confirmed on the wire via :meth:`mark_sent`."""
    return self._last_sent_id

  def record(
    self, record: TaggedLogRecord, *, local_root: logging.Logger | None, emergency_writer: EmergencyHistoryWriter | None
  ) -> HistoryEntry:
    """Assign a durable id to *record* and persist it. Returns the entry for the caller to transmit."""
    record_id = self._next_id
    self._next_id += 1
    record.record_id = record_id
    entry = HistoryEntry(id=record_id, created=record.created, record=record)
    if emergency_writer is not None:
      entry.persisted = True
      emergency_writer.submit(entry)
    self.history.append(entry)
    self._id_checkpoint.schedule_persist(record_id)
    if local_root is not None:
      local_root.handle(record)
    return entry

  def mark_sent(self, entry_id: int) -> None:
    """Record that *entry_id* has been confirmed on the wire."""
    self._last_sent_id = entry_id

  def resolve_backlog(self, ack: HandshakeAck) -> tuple[HistoryEntry, ...]:
    """Whatever *ack* says the server is missing, in order; ``()`` if there's nothing to replay.

    An unrecoverable gap (the acked id can't be located in memory or on disk) also returns ``()`` after
    logging a warning - the caller resumes live either way.
    """
    backlog = self.history.find_after(ack.last_record_id, ack.last_received_at)
    if backlog is None:
      # Runs inside the handler's emit (reconnect -> handshake -> replay), so not via `logging`.
      emergency_diagnostic(
        f"log server last confirmed record id {ack.last_record_id} for {self._program_name!r}, but it could not be "
        "located in history; some records may already have aged out. Resuming live."
      )
      return ()
    return backlog

  def arm_shutdown(self) -> None:
    """Interrupt-phase arm (D-I8): switch history and checkpoint to write-through. Atomic stores only."""
    self.history.begin_shutdown()
    self._id_checkpoint.begin_shutdown()

  def flush(self) -> None:
    """Force every buffered history entry to disk, ignoring the usual thresholds."""
    self.history.flush()

  def close(self) -> None:
    """Stop the id-checkpoint backend's background work, and release the history write-through handle.

    Both are one-shot teardown, safe to call after the last :meth:`flush`.
    """
    self._id_checkpoint.close()
    self.history.close()
