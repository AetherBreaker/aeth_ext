"""Tests for `aeth_ext.central_log_server.client.history`.

`RecordHistoryBuffer.history_dir` and `EmergencyHistoryWriter`'s constructor
argument are both resolved through a real filesystem directory, so every test
redirects them into `tmp_path` via monkeypatching the real class attribute
(`RecordHistoryBuffer.history_dir` is bound once at class-definition time from
`settings.persisted_dir_loc`, so patching the `settings` object after import
would not reach it -- the class attribute itself must be patched directly).
"""

# Standard library imports
import logging
from datetime import datetime
from typing import TYPE_CHECKING

# Third party imports
import pytest

# First party imports
from aeth_ext.central_log_server.client import history as history_module
from aeth_ext.central_log_server.client.history import (
  EmergencyHistoryWriter,
  HistoryEntry,
  RecordHistoryBuffer,
  iter_entries,
)
from aeth_ext.logging.bases import TaggedLogRecord

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path

_NOON_UTC_2026_01_15 = 1768478400.0  # 2026-01-15T12:00:00+00:00
_TWO_ENTRIES = 2


def _make_record(message: str = "hello") -> TaggedLogRecord:
  return TaggedLogRecord("prog.module", logging.INFO, __file__, 1, message, None, None)


def _entry(entry_id: int, created: float = _NOON_UTC_2026_01_15, message: str = "hello") -> HistoryEntry:
  return HistoryEntry(id=entry_id, created=created, record=_make_record(message))


def _ids(entries: tuple[HistoryEntry, ...] | None) -> tuple[int, ...] | None:
  """Compare by id rather than full equality: `LogRecord` has no `__eq__`, so
  two independently-constructed `HistoryEntry`s (or one round-tripped through
  disk) are never `==` even with identical content."""
  return None if entries is None else tuple(e.id for e in entries)


@pytest.fixture
def history_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
  directory = tmp_path / "hist"
  monkeypatch.setattr(RecordHistoryBuffer, "history_dir", directory)
  return directory


class TestRecordHistoryBufferFlushing:
  def test_stays_in_memory_below_every_threshold(self, history_dir: Path):
    buffer = RecordHistoryBuffer(max_records=10, max_bytes=10**9, max_age=10**9)

    buffer.append(_entry(1))

    assert list(history_dir.glob("*.jsonl")) == []
    assert _ids(buffer.find_after(None, None)) == (1,)

  def test_flushes_to_disk_once_max_records_is_reached(self, history_dir: Path):
    buffer = RecordHistoryBuffer(max_records=2, max_bytes=10**9, max_age=10**9)

    buffer.append(_entry(1))
    buffer.append(_entry(2))

    assert buffer.find_after(None, None) == ()
    (history_file,) = history_dir.glob("*.jsonl")
    lines = history_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == _TWO_ENTRIES

  def test_flushes_to_disk_once_max_bytes_is_reached(self, history_dir: Path):
    buffer = RecordHistoryBuffer(max_records=10**9, max_bytes=1, max_age=10**9)

    buffer.append(_entry(1))

    assert buffer.find_after(None, None) == ()
    assert list(history_dir.glob("*.jsonl"))

  def test_flushes_to_disk_once_max_age_is_reached(self, history_dir: Path):
    buffer = RecordHistoryBuffer(max_records=10**9, max_bytes=10**9, max_age=-1.0)

    buffer.append(_entry(1))

    assert buffer.find_after(None, None) == ()
    assert list(history_dir.glob("*.jsonl"))

  def test_already_persisted_entries_are_not_written_again(self, history_dir: Path):
    buffer = RecordHistoryBuffer(max_records=1, max_bytes=10**9, max_age=10**9)
    persisted = _entry(1)
    persisted.persisted = True

    buffer.append(persisted)

    assert list(history_dir.glob("*.jsonl")) == []


class TestRecordHistoryBufferFindAfter:
  def test_none_last_id_returns_everything_currently_in_memory(self, history_dir: Path):
    buffer = RecordHistoryBuffer(max_records=10**9, max_bytes=10**9, max_age=10**9)
    buffer.append(_entry(1))
    buffer.append(_entry(2))

    assert _ids(buffer.find_after(None, None)) == (1, 2)

  def test_in_memory_last_id_returns_only_the_newer_entries(self, history_dir: Path):
    buffer = RecordHistoryBuffer(max_records=10**9, max_bytes=10**9, max_age=10**9)
    for entry_id in (1, 2, 3):
      buffer.append(_entry(entry_id))

    assert _ids(buffer.find_after(1, None)) == (2, 3)

  def test_in_memory_last_id_at_the_newest_entry_returns_empty_tuple(self, history_dir: Path):
    buffer = RecordHistoryBuffer(max_records=10**9, max_bytes=10**9, max_age=10**9)
    buffer.append(_entry(1))
    buffer.append(_entry(2))

    assert buffer.find_after(2, None) == ()

  def test_empty_memory_with_last_id_present_on_disk_replays_from_disk(self, history_dir: Path):
    buffer = RecordHistoryBuffer(max_records=1, max_bytes=10**9, max_age=10**9)
    buffer.append(_entry(1))  # flushes id 1 to disk
    buffer.append(_entry(2))  # flushes id 2 to disk (memory empty again afterward)

    result = buffer.find_after(1, _NOON_UTC_2026_01_15)

    assert _ids(result) == (2,)

  def test_last_id_not_found_anywhere_returns_none(self, history_dir: Path):
    buffer = RecordHistoryBuffer(max_records=10**9, max_bytes=10**9, max_age=10**9)

    assert buffer.find_after(999, _NOON_UTC_2026_01_15) is None

  def test_gap_between_disk_and_memory_is_bridged_by_a_disk_search(self, history_dir: Path):
    buffer = RecordHistoryBuffer(max_records=1, max_bytes=10**9, max_age=10**9)
    buffer.append(_entry(1))  # flushes id 1 to disk, memory now empty
    buffer._max_records = 10**9  # pyright: ignore[reportPrivateUsage]
    buffer.append(_entry(2))  # stays in memory (entries[0].id == 2 > last_id == 1)
    buffer.append(_entry(3))

    result = buffer.find_after(1, _NOON_UTC_2026_01_15)

    assert _ids(result) == (2, 3)


class TestIterEntries:
  def test_missing_file_yields_nothing(self, tmp_path: Path):
    assert list(iter_entries(tmp_path / "does_not_exist.jsonl")) == []

  def test_malformed_lines_are_skipped_not_raised(self, tmp_path: Path):
    path = tmp_path / "history.jsonl"
    good = history_module._format_entry_line(_entry(1))  # pyright: ignore[reportPrivateUsage]
    path.write_text(f"{good}\nnot json at all\n\n", encoding="utf-8")

    entries = list(iter_entries(path))

    assert [e.id for e in entries] == [1]


class TestEmergencyHistoryWriter:
  def test_submitted_entries_are_persisted_and_marked_persisted(self, tmp_path: Path):
    writer = EmergencyHistoryWriter(tmp_path)
    entry = _entry(1)
    try:
      writer.submit(entry)
    finally:
      writer.close()

    day = datetime.fromtimestamp(_NOON_UTC_2026_01_15, tz=history_module.settings.tz).date()
    history_file = history_module._history_file_for_date(tmp_path, day)  # pyright: ignore[reportPrivateUsage]
    written = list(iter_entries(history_file))
    assert [e.id for e in written] == [1]

  def test_close_stops_the_background_thread(self, tmp_path: Path):
    writer = EmergencyHistoryWriter(tmp_path)

    writer.close()

    assert not writer._thread.is_alive()  # pyright: ignore[reportPrivateUsage]

  def test_entries_on_different_dates_land_in_different_files(self, tmp_path: Path):
    writer = EmergencyHistoryWriter(tmp_path)
    later = _NOON_UTC_2026_01_15 + 86400 * 3
    try:
      writer.submit(_entry(1, created=_NOON_UTC_2026_01_15))
      writer.submit(_entry(2, created=later))
    finally:
      writer.close()

    files = sorted(tmp_path.glob("*.jsonl"))
    assert len(files) == _TWO_ENTRIES
