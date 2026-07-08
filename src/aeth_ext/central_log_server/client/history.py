# Standard library imports
import logging
import threading
from collections import deque
from datetime import date, datetime, timedelta
from queue import Empty, Queue
from time import monotonic
from typing import TYPE_CHECKING, Any

# Third party imports
from orjson import JSONDecodeError, dumps, loads
from pydantic import ConfigDict
from pydantic.dataclasses import dataclass

# First party imports
from aeth_ext.central_log_server.protocol import TaggedLogRecord, payload_to_record, record_to_payload
from aeth_ext.errors import handle_fatal_exc_sync
from aeth_ext.settings import BaseSettings
from aeth_ext.types import IsPydanticSlots

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Iterator
  from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["EmergencyHistoryWriter", "HistoryEntry", "RecordHistoryBuffer"]

settings = BaseSettings.get_settings()

# How long EmergencyHistoryWriter's run loop waits for the next entry before
# treating the queue as idle and closing the open file handle.
_IDLE_CLOSE_TIMEOUT = 2.0

pyd_config = ConfigDict(arbitrary_types_allowed=True)


@dataclass(config=pyd_config, slots=True)
class HistoryEntry(IsPydanticSlots):
  """A single emitted record, kept around so it can be replayed on reconnect.

  ``persisted`` tracks whether this entry has already been written to a
  history file (e.g. by :class:`EmergencyHistoryWriter`) so that
  :class:`RecordHistoryBuffer`'s own lazy spill does not write it a second
  time once it is evicted from memory.
  """

  id: int
  created: float
  record: TaggedLogRecord
  persisted: bool = False


def _history_file_for_date(history_dir: Path, day: date) -> Path:
  return history_dir / f"{day:%Y-%m-%d}.jsonl"


def _format_entry_line(entry: HistoryEntry) -> str:
  """Serialise *entry* to the JSON line written to a history file."""
  return dumps(
    {
      "id": entry.id,
      "created": entry.created,
      "record": record_to_payload(entry.record),
    },
    default=str,
  ).decode()


def iter_entries(path: Path) -> Iterator[HistoryEntry]:
  """Yield every :class:`HistoryEntry` in the JSONL file at *path*, in order.

  Malformed lines (e.g. a truncated final line left by a crash mid-write) are
  logged and skipped rather than aborting the whole read.
  """
  if not path.exists():
    return
  with path.open("r", encoding="utf-8") as fh:
    for line in fh:
      stripped_line = line.strip()
      if not stripped_line:
        continue
      try:
        data: dict[str, object] = loads(stripped_line)
        record = payload_to_record(data["record"])  # pyright: ignore[reportArgumentType]
      except JSONDecodeError, KeyError, ValueError, TypeError:
        logger.warning("Skipping malformed history line in %s", path, exc_info=True)
        continue
      yield HistoryEntry(id=int(data["id"]), created=float(data["created"]), record=record, persisted=True)  # pyright: ignore[reportArgumentType]


def _approx_entry_size(record: TaggedLogRecord) -> int:
  """A cheap size estimate used only to bound memory, not an exact size.

  Avoids pickling every record just to measure it; a fixed per-record
  overhead plus the message length is close enough for a memory cap.
  """
  try:
    message_len = len(record.getMessage())
  except Exception:
    message_len = 0
  return message_len + 512


class RecordHistoryBuffer:
  """An in-memory, append-only record of everything emitted (sent or not).

  Bounded by three independent thresholds - record count, approximate byte
  size, and time since the last flush - so a crash can lose at most the last
  ``max_age`` seconds of records rather than an unbounded amount. Whichever
  threshold trips first flushes *every* currently-buffered entry to its
  date-segregated JSONL history file and clears the in-memory buffer, so
  :meth:`find_after` only ever needs to search memory for the most recent
  contiguous tail and disk for anything older.
  """

  history_dir = settings.log_loc_folder / "client_log_history"

  def __init__(
    self,
    max_records: int = 50_000,
    max_bytes: int = 64 * 1024 * 1024,
    max_age: float = 300.0,
  ) -> None:
    self.history_dir.mkdir(parents=True, exist_ok=True)
    self._max_records = max_records
    self._max_bytes = max_bytes
    self._max_age = max_age
    self._entries: deque[HistoryEntry] = deque()
    self._approx_bytes = 0
    self._last_flush_monotonic = monotonic()

  def append(self, entry: HistoryEntry) -> None:
    self._entries.append(entry)
    self._approx_bytes += _approx_entry_size(entry.record)
    self._maybe_flush()

  def _maybe_flush(self) -> None:
    now = monotonic()
    if (
      len(self._entries) < self._max_records
      and self._approx_bytes < self._max_bytes
      and (now - self._last_flush_monotonic) < self._max_age
    ):
      return
    self._flush_to_disk()
    self._last_flush_monotonic = now

  def _flush_to_disk(self) -> None:
    open_files: dict[Path, Any] = {}
    try:
      while self._entries:
        entry = self._entries.popleft()
        if entry.persisted:
          continue
        day = datetime.fromtimestamp(entry.created, tz=settings.tz).date()
        path = _history_file_for_date(self.history_dir, day)
        if path not in open_files:
          try:
            open_files[path] = path.open("a", encoding="utf-8")
          except OSError:
            logger.exception("Failed to open history file %s for entry %s", path, entry.id)
            continue
        try:
          open_files[path].write(_format_entry_line(entry) + "\n")
        except Exception:
          logger.exception("Failed to spill history entry %s to disk", entry.id)
    finally:
      for fh in open_files.values():
        try:
          fh.close()
        except OSError:
          pass
    self._approx_bytes = 0

  def find_after(self, last_id: int | None, hint_created: float | None) -> tuple[HistoryEntry, ...] | None:
    """Return every entry with ``id > last_id``, or ``None`` if unrecoverable.

    ``last_id`` of ``None`` means the server has never seen this program
    before, so everything currently retained (memory and, in principle, disk)
    is replayed. ``None`` is returned only when ``last_id`` is a real id that
    cannot be located anywhere (memory or the probed history files) - a
    genuine, unrecoverable gap rather than "nothing to resend".
    """
    if last_id is None:
      return tuple(self._entries)

    if self._entries and self._entries[0].id <= last_id:
      return tuple(e for e in self._entries if e.id > last_id)

    if self._entries and last_id > self._entries[-1].id:
      # Already caught up (or the ack is stale); nothing to resend.
      return ()

    disk_entries = self._search_disk(last_id, hint_created)
    if disk_entries is None:
      return None
    return (*disk_entries, *self._entries)

  def _search_disk(self, last_id: int, hint_created: float | None) -> tuple[HistoryEntry, ...] | None:
    """Scan history files near *hint_created* for everything after *last_id*.

    Probes the hinted date plus the adjacent day on either side to absorb
    midnight-rollover ambiguity between when a record was created and which
    date file it landed in. This is a bounded, practical window intended for
    the short reconnect gaps this mechanism targets, not an exhaustive scan of
    all retained history.
    """
    hint_date = (
      datetime.fromtimestamp(hint_created, tz=settings.tz).date() if hint_created is not None else datetime.now(tz=settings.tz).date()
    )
    candidate_dates = (hint_date - timedelta(days=1), hint_date, hint_date + timedelta(days=1))

    found = False
    collected: list[HistoryEntry] = []
    for day in candidate_dates:
      for entry in iter_entries(_history_file_for_date(self.history_dir, day)):
        if found:
          collected.append(entry)
        elif entry.id == last_id:
          found = True
        elif entry.id > last_id:
          # last_id itself wasn't found verbatim (e.g. it belongs to a file
          # outside the probed window); treat the first newer entry seen as
          # the resume point so nothing after it is missed.
          found = True
          collected.append(entry)

    return tuple(collected) if found else None


class EmergencyHistoryWriter:
  """Eagerly persists every submitted record while the server is unreachable.

  Started only once the caller's send-failure heuristic trips, and stopped as
  soon as the connection recovers, so the extra standing thread is paid for
  only while genuinely needed. Runs on its own daemon thread with a simple
  FIFO queue so submitting a record from :meth:`~logging.Handler.emit` never
  blocks on disk IO.

  The file handle for the active date's history file is held open across
  consecutive writes and only closed once the queue has been idle for
  ``_IDLE_CLOSE_TIMEOUT`` seconds or the date rolls over, so a burst of
  records pays for one ``open`` rather than one per record.
  """

  def __init__(self, history_dir: Path) -> None:
    self._history_dir = history_dir
    self._history_dir.mkdir(parents=True, exist_ok=True)
    self._queue: Queue[HistoryEntry | None] = Queue()
    self._thread = threading.Thread(target=self._run, name="log-emergency-writer", daemon=True)
    self._thread.start()

  def submit(self, entry: HistoryEntry) -> None:
    self._queue.put(entry)

  def _close_handle(self, fh: Any) -> None:
    """Close *fh*, silently swallowing any ``OSError``."""
    if fh is not None:
      try:
        fh.close()
      except OSError:
        pass

  def _switch_file(self, current_fh: Any, new_path: Path) -> Any:
    """Close *current_fh* (if open) and open *new_path* for append.

    Returns the new file handle, or ``None`` if the open fails.
    """
    self._close_handle(current_fh)
    try:
      return new_path.open("a", encoding="utf-8")
    except OSError:
      logger.exception("Emergency history writer failed to open %s", new_path)
      return None

  # TODO needs a detector for FATAL_EVENT being set so it can drain the queue and exit promptly

  @handle_fatal_exc_sync
  def _run(self) -> None:
    current_fh = None
    current_path = None

    while True:
      try:
        entry = self._queue.get(timeout=_IDLE_CLOSE_TIMEOUT)
      except Empty:
        # Queue went idle: release the handle so it is not held open
        # indefinitely between bursts, then block until the next entry.
        self._close_handle(current_fh)
        current_fh = None
        current_path = None
        entry = self._queue.get()

      if entry is None:
        break

      day = datetime.fromtimestamp(entry.created, tz=settings.tz).date()
      path = _history_file_for_date(self._history_dir, day)

      if path != current_path:
        current_fh = self._switch_file(current_fh, path)
        current_path = path if current_fh is not None else None

      if current_fh is None:
        continue

      try:
        current_fh.write(_format_entry_line(entry) + "\n")
        current_fh.flush()
      except OSError:
        logger.exception("Emergency history writer failed to persist record %s", entry.id)

    self._close_handle(current_fh)

  def close(self) -> None:
    self._queue.put(None)
    self._thread.join(timeout=5.0)
