"""On-disk JSONL history of emitted records, enabling replay-by-id after a reconnect."""

# Standard library imports
import threading
from collections import deque
from datetime import date, datetime, timedelta
from itertools import takewhile
from queue import Empty, Queue
from time import monotonic
from typing import TYPE_CHECKING, Any, TextIO

# Third party imports
from orjson import JSONDecodeError, dumps, loads
from pydantic import ConfigDict
from pydantic.dataclasses import dataclass

# First party imports
from aeth_ext.central_log_server.protocol import payload_to_record, record_to_payload
from aeth_ext.errors import alert, handle_fatal_exc_sync
from aeth_ext.logging.bases import TaggedLogRecord
from aeth_ext.logging.emergency_diagnostics import emergency_diagnostic
from aeth_ext.settings import BaseSettings
from aeth_ext.types import IsPydanticSlots

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Iterator
  from pathlib import Path

# No `logging` anywhere in this module: every path here runs inside a client transport's delivery
# path (history append/spill under emit, replay reads, the emergency writer thread whose records
# come from emit), where a logged failure is re-delivered by the failing transport -- see
# `aeth_ext.logging.emergency_diagnostics`.

__all__ = ["EmergencyHistoryWriter", "HistoryEntry", "RecordHistoryBuffer"]

settings = BaseSettings.get_settings()

# How long EmergencyHistoryWriter's run loop waits for the next entry before
# treating the queue as idle and closing the open file handle.
_IDLE_CLOSE_TIMEOUT = 2.0

# How far back from the end of a history file `_repair_truncated_tail` will look
# for the last newline. History files are unbounded, but only the final line can
# ever be in question, so reading the whole file to inspect its last byte would
# be wasteful. Generous enough that a realistic record always fits.
_MAX_TAIL_SCAN_BYTES = 1024 * 1024

pyd_config = ConfigDict(arbitrary_types_allowed=True)


@dataclass(config=pyd_config, slots=True)
class HistoryEntry(IsPydanticSlots):
  """A single emitted record, kept around so it can be replayed on reconnect.

  ``persisted`` means the entry's line is on disk. It is set only by whoever wrote that line: a spill,
  write-through, or ``EmergencyHistoryWriter``, which also skips entries already marked. So an entry
  handed to a writer that never writes it (one closed under it) is still written by the next spill. A spill
  and the writer racing on one entry write it twice; ``_search_disk`` dedupes by id.
  """

  id: int
  created: float
  record: TaggedLogRecord
  persisted: bool = False


def _history_file_for_date(history_dir: Path, day: date) -> Path:
  # The program name (the directory's own name) is baked into the file name so a history file
  # copied out of its directory still says which program it came from.
  return history_dir / f"{history_dir.name}_{day:%Y-%m-%d}.jsonl"


def _managed_history_files(history_dir: Path) -> list[tuple[date, Path, int]]:
  """``(day, path, size)`` for every file in *history_dir* this module wrote, oldest first.

  "Managed" means the name is exactly what ``_history_file_for_date`` produces for this
  directory. Anything else -- pre-rename ``YYYY-MM-DD.jsonl`` files, files from another
  program, stray copies -- is invisible here, so housekeeping can never touch it.
  """
  prefix = f"{history_dir.name}_"
  found: list[tuple[date, Path, int]] = []
  for path in history_dir.iterdir():
    name = path.name
    if not (name.startswith(prefix) and name.endswith(".jsonl")):
      continue
    try:
      day = date.fromisoformat(name[len(prefix) : -len(".jsonl")])
    except ValueError:
      continue
    if _history_file_for_date(history_dir, day) != path or not path.is_file():
      continue
    try:
      size = path.stat().st_size
    except FileNotFoundError:
      continue  # unlinked by retention since ``is_file``: ``find_after`` lists outside ``_lock``
    found.append((day, path, size))
  found.sort()
  return found


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
  logged and skipped rather than aborting the whole read. Read as bytes, so a line torn inside a
  multi-byte character is one more ``JSONDecodeError`` instead of a ``UnicodeDecodeError`` that
  aborts the file. A missing file yields nothing; opening rather than checking first means a file
  retention unlinks after ``find_after`` listed it does too.
  """
  try:
    fh = path.open("rb")
  except FileNotFoundError:
    return
  with fh:
    for line in fh:
      stripped_line = line.strip()
      if not stripped_line:
        continue
      try:
        data: dict[str, object] = loads(stripped_line)
        record = payload_to_record(data["record"])  # pyright: ignore[reportArgumentType]
      except (JSONDecodeError, KeyError, ValueError, TypeError) as e:
        emergency_diagnostic(f"skipping malformed history line in {path}", exc=e)
        continue
      yield HistoryEntry(id=int(data["id"]), created=float(data["created"]), record=record, persisted=True)  # pyright: ignore[reportArgumentType]


def _approx_entry_size(record: TaggedLogRecord) -> int:
  """A cheap size estimate used only to bound memory, not an exact size.

  Avoids pickling every record just to measure it; a fixed per-record
  overhead plus the message length is close enough for a memory cap.
  """
  try:
    message_len = len(record.getMessage())
  except Exception:  # noqa: BLE001
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

  Each instance gets its own subdirectory under :attr:`history_root`, keyed
  by *program_name*: the drainer classes are primarily used to submit records
  generated by a separate program (a subprocess, a different package) to the
  log server, so that program's history must stay in its own files rather
  than interleaving with whatever the current process's own logging is
  already writing under the same date-keyed name.
  """

  history_root = settings.persisted_dir_loc / "client_log_history"

  def __init__(
    self,
    program_name: str,
    max_records: int = 50_000,
    max_bytes: int = 64 * 1024 * 1024,
    max_age: float = 300.0,
    *,
    retention_days: int = settings.log_history_retention_days,
    max_dir_bytes: int = settings.log_history_max_bytes,
  ) -> None:
    """Create the per-program history directory, then repair any truncated tail and run housekeeping.

    Raises:
        ValueError: if *program_name* cannot serve as a directory name and file-name prefix.
    """
    if not program_name or any(sep in program_name for sep in ("/", "\\")) or program_name in {".", ".."}:
      # The name becomes both a directory and a file-name prefix; a separator would silently
      # nest the history somewhere `_managed_history_files` never looks.
      raise ValueError(f"program_name is not a valid history directory name: {program_name!r}")
    self.history_dir = self.history_root / program_name
    self.history_dir.mkdir(parents=True, exist_ok=True)
    self._max_records = max_records
    self._max_bytes = max_bytes
    self._max_age = max_age
    self._retention_days = retention_days
    self._max_dir_bytes = max_dir_bytes
    # Housekeeping state (see `_maintain`): the day retention last ran, and whether the
    # over-budget alert has fired for the current excursion.
    self._last_retention_date: date | None = None
    self._over_budget = False
    # The newest id `append` has taken, whether it went to `_entries` or straight to disk; bounds `find_after`.
    self._newest_id: int | None = None
    self._entries: deque[HistoryEntry] = deque()
    self._approx_bytes = 0
    self._last_flush_monotonic = monotonic()
    # Guards `_entries` and the whole write path. All three client classes call
    # `append` from arbitrary threads, so `_flush_to_disk`'s drain loop could
    # already interleave today; write-through mode (below) would turn that from
    # rare into per-record, and two threads appending to the same file at once
    # interleave partial JSON lines *mid-file* -- damage `_repair_truncated_tail`
    # cannot fix, since it only ever inspects the trailing segment.
    # Reentrant because `append` holds it across `_maybe_flush` -> `_flush_to_disk`.
    self._lock = threading.RLock()
    # Flipped by `begin_shutdown`. Never read without also consulting the lock
    # *except* in `begin_shutdown` itself -- see there for why.
    self._shutting_down = False
    # Flipped once, by whichever of `append`/`flush` is first to observe
    # `_shutting_down` after it goes true. Marks that the one-time catch-up
    # drain (see `append`) has happened and `_entries`/`_flush_to_disk` are
    # retired for the rest of this buffer's life -- every later write-through
    # entry goes straight to disk via `_write_entry_through` instead.
    self._shutdown_flushed = False
    # The file handle `_write_entry_through` is currently writing through to,
    # held open across calls for as long as it keeps targeting the same date
    # file -- graceful shutdown is exactly when execution time is scarcest, so
    # paying one `open` per write-through entry (as opposed to one per burst)
    # would be spending that budget in the worst possible place. Closed and
    # cleared by `close`.
    self._write_through_fh: TextIO | None = None
    self._write_through_path: Path | None = None
    self._repair_truncated_tail()
    self._maintain()

  def _maintain(self) -> None:
    """Age out old history files and watch the directory's size. Caller must hold ``_lock``.

    Runs at construction and after every buffer flush, so the size check lags a runaway by at
    most one flush interval (64 MB / 50k records / 5 min, whichever trips first) rather than a
    day. Cheap: with retention active the directory holds about ``retention_days`` files, so
    this is one ``iterdir`` plus a ``stat`` each.

    Retention deletes only files ``_managed_history_files`` recognises and only those strictly
    older than the retention window; it runs once per calendar day since the outcome cannot
    change until the date rolls. Over-budget never deletes anything -- the data is exactly what
    is needed to triage why it grew -- it raises one low-priority alert per excursion, re-armed
    once the directory drops back under budget (so a day's normal growth cannot re-fire it).

    Best-effort: any failure is reported through the emergency-diagnostics channel and the
    buffer carries on; housekeeping must never take the history path down with it.
    """
    try:
      today = datetime.now(tz=settings.tz).date()
      files = _managed_history_files(self.history_dir)
      if self._last_retention_date != today:
        cutoff = today - timedelta(days=self._retention_days)
        for day, path, _ in files:
          if day < cutoff:
            path.unlink()
        files = [f for f in files if f[0] >= cutoff]
        self._last_retention_date = today

      total = sum(size for _, _, size in files)
      if total > self._max_dir_bytes:
        if not self._over_budget:
          self._over_budget = True
          largest = sorted(files, key=lambda f: f[2], reverse=True)[:5]
          alert(
            "Client log history over size budget",
            f"{self.history_dir} holds {total / 2**20:,.0f} MiB across {len(files)} managed files; "
            f"budget is {self._max_dir_bytes / 2**20:,.0f} MiB (retention {self._retention_days} days).\n"
            "Nothing was deleted. Largest files:\n"
            + "\n".join(f"  {path.name}: {size / 2**20:,.0f} MiB" for _, path, size in largest),
            priority=-1,
            in_except_block=False,
            wait=False,  # this runs under `_lock` inside `emit`; never hold it across a network send
          )
      else:
        self._over_budget = False
    except Exception as e:  # noqa: BLE001 -- housekeeping is best-effort; see docstring
      emergency_diagnostic(f"history housekeeping for {self.history_dir} failed", exc=e)

  def _repair_truncated_tail(self) -> None:
    r"""Repair a final record left half-written by a previous run (D-I7).

    Only today's file is checked -- it is the only one a fresh instance is about
    to append to; other dates are read-only lookup targets for
    :meth:`find_after`.

    Every successful write appends a complete JSON line *plus* a trailing
    newline, so a file not ending in ``\n`` is by construction evidence that
    something was cut short. The trailing segment is then parsed to decide which
    of two very different repairs applies -- deleting a record that turns out to
    be intact would itself be data loss.

    Reads a bounded tail rather than the whole file: these can grow without
    limit, and only the last line is ever in question.

    Best-effort throughout. Any :exc:`OSError` is logged and swallowed, since
    construction succeeding matters more than the repair.
    """
    path = _history_file_for_date(self.history_dir, datetime.now(tz=settings.tz).date())
    try:
      size = path.stat().st_size if path.exists() else 0
      if size == 0:
        return

      read_size = min(size, _MAX_TAIL_SCAN_BYTES)
      with path.open("rb") as fh:
        fh.seek(size - read_size)
        tail = fh.read(read_size)

      if tail.endswith(b"\n"):
        return  # well-formed; nothing to do

      newline_at = tail.rfind(b"\n")
      if newline_at == -1 and read_size < size:
        # The final line is longer than the scan window, so its true start is
        # somewhere we did not read. Truncating would need that boundary, and
        # guessing it risks discarding good records -- leave the file alone.
        emergency_diagnostic(f"history file {path} has an unterminated final line longer than {read_size} bytes; leaving as-is")
        return

      suspect = tail[newline_at + 1 :]
      try:
        loads(suspect)
      except JSONDecodeError:
        # A genuine partial write. Report the length rather than the content:
        # a truncated write need not even be valid UTF-8.
        truncate_at = size - read_size + newline_at + 1
        with path.open("r+b") as fh:
          fh.truncate(truncate_at)
        emergency_diagnostic(f"discarded a {len(suspect)}-byte partial record at the end of {path}")
      else:
        # A complete record that only lost its newline. Should not happen given
        # the write pattern, but appending one byte is far cheaper than assuming
        # it cannot and dropping a valid record.
        with path.open("ab") as fh:
          fh.write(b"\n")
        emergency_diagnostic(f"restored the missing trailing newline on {path}")
    except OSError as e:
      emergency_diagnostic(f"could not repair the trailing record in {path}", exc=e)

  def begin_shutdown(self) -> None:
    """Switch to write-through mode; safe to call from a signal handler (D-I6).

    Registered for :attr:`~aeth_ext.errors.ShutdownPhase.INTERRUPT`, so this runs
    on the main thread between bytecodes and must obey that phase's rules: it
    does exactly one atomic attribute store, and **nothing else**.

    In particular it deliberately does *not* flush, and does not take
    :attr:`_lock`. Flushing here would re-enter :meth:`_flush_to_disk`'s drain
    loop if the interrupted thread was already inside it, losing or duplicating
    entries; taking the lock would deadlock outright for the same reason. The
    backlog is not stranded by that choice -- whichever of :meth:`append` or
    :meth:`flush` next observes :attr:`_shutting_down` performs the one-time
    catch-up drain itself (see :meth:`append`).
    """
    self._shutting_down = True

  def flush(self) -> None:
    """Force every buffered entry to disk, ignoring the usual thresholds.

    A no-op once the shutdown catch-up drain has already run (see
    :meth:`append`) -- at that point :attr:`_entries` is retired for good and
    there is nothing left in it to flush.
    """
    with self._lock:
      if self._shutting_down and self._shutdown_flushed:
        return
      self._flush_to_disk()
      self._last_flush_monotonic = monotonic()
      if self._shutting_down:
        self._shutdown_flushed = True

  def append(self, entry: HistoryEntry) -> None:
    """Buffer *entry*, flushing when a threshold trips; during shutdown, write it straight through to disk."""
    with self._lock:
      self._newest_id = entry.id
      if self._shutting_down:
        if self._shutdown_flushed:
          # Fast path: the one-time catch-up drain below already ran, so
          # `_entries` is retired -- write straight to disk without spinning
          # up `_flush_to_disk`'s multi-entry drain loop for what is, from
          # here on, always exactly one entry.
          self._write_entry_through(entry)
        else:
          # First write-through append: catch up whatever `begin_shutdown`
          # left buffered, plus this entry, in one drain, then retire
          # `_entries` for the rest of this buffer's life.
          self._entries.append(entry)
          self._approx_bytes += _approx_entry_size(entry.record)
          self._flush_to_disk()
          self._last_flush_monotonic = monotonic()
          self._shutdown_flushed = True
      else:
        self._entries.append(entry)
        self._approx_bytes += _approx_entry_size(entry.record)
        self._maybe_flush()

  def _write_entry_through(self, entry: HistoryEntry) -> None:
    """Persist a single entry directly, bypassing :attr:`_entries` entirely.

    Caller must hold :attr:`_lock`. Only ever reached once the shutdown
    catch-up drain has run (:attr:`_shutdown_flushed` is set), so there is
    never more than one entry to write here.

    :attr:`_write_through_fh` is reused across calls rather than reopened per
    entry -- write-through mode exists for graceful shutdown, where CPU time
    is the scarcest resource, so paying an ``open`` per record would be
    spending that budget on exactly the wrong thing. Each write is still
    flushed individually so no record is stranded in a buffer if the process
    is killed before :meth:`close` runs.
    """
    day = datetime.fromtimestamp(entry.created, tz=settings.tz).date()
    path = _history_file_for_date(self.history_dir, day)
    if path != self._write_through_path:
      self._close_write_through()
      try:
        self._write_through_fh = path.open("a", encoding="utf-8")
      except OSError as e:
        emergency_diagnostic(f"failed to open history file {path} for entry {entry.id}", exc=e)
        return
      self._write_through_path = path
    try:
      assert self._write_through_fh is not None
      self._write_through_fh.write(_format_entry_line(entry) + "\n")
      self._write_through_fh.flush()
    except OSError as e:
      emergency_diagnostic(f"failed to spill history entry {entry.id} to disk", exc=e)
    else:
      entry.persisted = True

  def _close_write_through(self) -> None:
    """Close and clear :attr:`_write_through_fh`, if open. Caller must hold :attr:`_lock`."""
    if self._write_through_fh is not None:
      try:
        self._write_through_fh.close()
      except OSError:
        pass
      self._write_through_fh = None
      self._write_through_path = None

  def close(self) -> None:
    """Release the write-through file handle held open by :meth:`_write_entry_through`.

    A no-op if write-through mode was never entered. Meant to run at final
    teardown, after the last possible :meth:`append`/:meth:`flush` call --
    there is no way back into non-write-through mode, so nothing needs this
    handle again once it is closed.
    """
    with self._lock:
      self._close_write_through()

  def _maybe_flush(self) -> None:
    """Flush if any threshold has tripped. Caller must hold :attr:`_lock`."""
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
    """Drain the buffer to date-segregated files. Caller must hold :attr:`_lock`."""
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
          except OSError as e:
            emergency_diagnostic(f"failed to open history file {path} for entry {entry.id}", exc=e)
            continue
        try:
          open_files[path].write(_format_entry_line(entry) + "\n")
        except Exception as e:  # noqa: BLE001 -- one bad entry must not strand the rest of the drain
          emergency_diagnostic(f"failed to spill history entry {entry.id} to disk", exc=e)
        else:
          entry.persisted = True  # an emergency writer still holding it skips it
    finally:
      for fh in open_files.values():
        try:
          fh.close()
        except OSError:
          pass
    self._approx_bytes = 0
    if not self._shutting_down:
      # Skipped during shutdown: that drain runs where execution time is scarcest, and the
      # directory has been checked within the last flush interval anyway.
      self._maintain()

  def find_after(self, last_id: int | None, hint_created: float | None) -> tuple[HistoryEntry, ...] | None:
    """Every retained entry with ``id > last_id``, memory and disk, in id order and without duplicates.

    Safe to call from any thread while others ``append``: memory is read under ``_lock`` and disk outside it, since
    ``append`` runs inside a handler's ``emit``, which must never wait on file IO. Ids are contiguous, so memory
    starting at or before ``last_id + 1`` holds the whole answer: its tail, taken from the right end in O(k), since
    the sender asks on every pass. Otherwise ``_entries`` is copied and disk is read only below the copy's first id
    or, with the copy empty, up to the newest id appended by then, so an entry appended mid-read is left for the
    next call instead of overtaking an older one in memory.

    Args:
        last_id: The newest id the receiver holds; ``None`` if it has never seen this program. Ids start at 1; pass
            ``None``, not 0.
        hint_created: ``created`` of the entry at ``last_id``, if known. Disk files dated over a day before it are
            skipped; without it every retained file is read.

    Returns:
        The entries, ``()`` if there is nothing newer, or ``None`` if ``last_id`` is a real id neither memory nor
        disk can place: an unrecoverable gap, typically history that aged out.
    """
    with self._lock:
      if last_id is not None and self._entries and self._entries[0].id <= last_id + 1:
        return tuple(reversed(tuple(takewhile(lambda e: e.id > last_id, reversed(self._entries)))))
      snapshot = tuple(self._entries)
      newest_id = self._newest_id
    if snapshot:
      stop_before = snapshot[0].id
    else:
      # Nothing appended yet: disk holds only earlier processes' entries, which need no bound.
      stop_before = None if newest_id is None else newest_id + 1
    disk_entries = self._search_disk(last_id, hint_created, stop_before=stop_before)
    if disk_entries is None:
      return None
    return (*disk_entries, *snapshot)  # every snapshot entry is past `last_id + 1` here

  def _search_disk(self, last_id: int | None, hint_created: float | None, stop_before: int | None) -> tuple[HistoryEntry, ...] | None:
    """Every entry on disk with ``last_id < id < stop_before``, in id order, or ``None`` if *last_id* can't be placed.

    Reads every managed file dated from the day before *hint_created* onward (the day absorbs a record's
    ``created`` and its file's date straddling midnight), or every file when there's no hint or *last_id* is
    ``None``. Entries are selected by id rather than by file position because file order is not id order: a
    write racing the caller appends ids ``>= stop_before``, which the caller holds or leaves for its next call, and
    the emergency writer lands its entries ahead of older ones a later spill writes.

    Args:
        last_id: Return entries after this id; ``None`` for all of them.
        hint_created: ``created`` of the entry at *last_id*, if known.
        stop_before: Exclusive upper bound on the ids returned (see ``find_after``), or ``None`` for none.

    Returns:
        The entries, or ``None`` if *last_id* is a real id and no entry from it up to *stop_before* exists. Either
        *last_id* itself or, if it aged out, any newer entry places it.
    """
    first_day = (
      datetime.fromtimestamp(hint_created, tz=settings.tz).date() - timedelta(days=1)
      if last_id is not None and hint_created is not None
      else date.min
    )
    found = last_id is None
    # Keyed by id: a spill or write-through racing the emergency writer can write one entry twice (see HistoryEntry).
    collected: dict[int, HistoryEntry] = {}
    for day, path, _ in _managed_history_files(self.history_dir):
      if day < first_day:
        continue
      for entry in iter_entries(path):
        if stop_before is not None and entry.id >= stop_before:
          continue
        if last_id is None or entry.id > last_id:
          collected[entry.id] = entry
        elif entry.id == last_id:
          found = True

    if not (found or collected):
      return None
    return tuple(sorted(collected.values(), key=lambda e: e.id))


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
    """Create *history_dir* if needed and start the daemon writer thread."""
    self._history_dir = history_dir
    self._history_dir.mkdir(parents=True, exist_ok=True)
    self._queue: Queue[HistoryEntry | None] = Queue()
    self._thread = threading.Thread(target=self._run, name="log-emergency-writer", daemon=True)
    self._thread.start()

  def submit(self, entry: HistoryEntry) -> None:
    """Queue *entry* for the writer thread; never blocks on disk IO."""
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
    except OSError as e:
      emergency_diagnostic(f"emergency history writer failed to open {new_path}", exc=e)
      return None

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
      if entry.persisted:  # a spill got there first
        continue

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
      except OSError as e:
        # Via `logging` this was a self-sustaining loop: the failure record is itself submitted to
        # this writer, fails the same way, and reports again.
        emergency_diagnostic(f"emergency history writer failed to persist record {entry.id}", exc=e)
      else:
        entry.persisted = True

    self._close_handle(current_fh)

  def close(self) -> None:
    """Signal the writer thread to exit and wait up to 5s for it."""
    self._queue.put(None)
    self._thread.join(timeout=5.0)
