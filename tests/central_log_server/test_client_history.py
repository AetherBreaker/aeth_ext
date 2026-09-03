# Standard library imports
import logging
from datetime import date, datetime, timedelta
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
_TEST_PROGRAM = "test-program"


def _make_record(message: str = "hello") -> TaggedLogRecord:
  return TaggedLogRecord("prog.module", logging.INFO, __file__, 1, message, None, None)


def _entry(entry_id: int, created: float = _NOON_UTC_2026_01_15, message: str = "hello") -> HistoryEntry:
  return HistoryEntry(id=entry_id, created=created, record=_make_record(message))


def _ids(entries: tuple[HistoryEntry, ...] | None) -> tuple[int, ...] | None:
  return None if entries is None else tuple(e.id for e in entries)


@pytest.fixture
def history_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
  root = tmp_path / "hist"
  monkeypatch.setattr(RecordHistoryBuffer, "history_root", root)
  return root / _TEST_PROGRAM


class TestRecordHistoryBufferFlushing:
  def test_stays_in_memory_below_every_threshold(self, history_dir: Path) -> None:
    buffer = RecordHistoryBuffer(_TEST_PROGRAM, max_records=10, max_bytes=10**9, max_age=10**9)

    buffer.append(_entry(1))

    assert list(history_dir.glob("*.jsonl")) == []
    assert _ids(buffer.find_after(None, None)) == (1,)

  def test_flushes_to_disk_once_max_records_is_reached(self, history_dir: Path) -> None:
    buffer = RecordHistoryBuffer(_TEST_PROGRAM, max_records=2, max_bytes=10**9, max_age=10**9)

    buffer.append(_entry(1))
    buffer.append(_entry(2))

    assert buffer.find_after(None, None) == ()
    (history_file,) = history_dir.glob("*.jsonl")
    lines = history_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == _TWO_ENTRIES

  def test_flushes_to_disk_once_max_bytes_is_reached(self, history_dir: Path) -> None:
    buffer = RecordHistoryBuffer(_TEST_PROGRAM, max_records=10**9, max_bytes=1, max_age=10**9)

    buffer.append(_entry(1))

    assert buffer.find_after(None, None) == ()
    assert list(history_dir.glob("*.jsonl"))

  def test_flushes_to_disk_once_max_age_is_reached(self, history_dir: Path) -> None:
    buffer = RecordHistoryBuffer(_TEST_PROGRAM, max_records=10**9, max_bytes=10**9, max_age=-1.0)

    buffer.append(_entry(1))

    assert buffer.find_after(None, None) == ()
    assert list(history_dir.glob("*.jsonl"))

  def test_already_persisted_entries_are_not_written_again(self, history_dir: Path) -> None:
    buffer = RecordHistoryBuffer(_TEST_PROGRAM, max_records=1, max_bytes=10**9, max_age=10**9)
    persisted = _entry(1)
    persisted.persisted = True

    buffer.append(persisted)

    assert list(history_dir.glob("*.jsonl")) == []


class TestRecordHistoryBufferFindAfter:
  def test_none_last_id_returns_everything_currently_in_memory(self, history_dir: Path) -> None:
    buffer = RecordHistoryBuffer(_TEST_PROGRAM, max_records=10**9, max_bytes=10**9, max_age=10**9)
    buffer.append(_entry(1))
    buffer.append(_entry(2))

    assert _ids(buffer.find_after(None, None)) == (1, 2)

  def test_in_memory_last_id_returns_only_the_newer_entries(self, history_dir: Path) -> None:
    buffer = RecordHistoryBuffer(_TEST_PROGRAM, max_records=10**9, max_bytes=10**9, max_age=10**9)
    for entry_id in (1, 2, 3):
      buffer.append(_entry(entry_id))

    assert _ids(buffer.find_after(1, None)) == (2, 3)

  def test_in_memory_last_id_at_the_newest_entry_returns_empty_tuple(self, history_dir: Path) -> None:
    buffer = RecordHistoryBuffer(_TEST_PROGRAM, max_records=10**9, max_bytes=10**9, max_age=10**9)
    buffer.append(_entry(1))
    buffer.append(_entry(2))

    assert buffer.find_after(2, None) == ()

  def test_empty_memory_with_last_id_present_on_disk_replays_from_disk(self, history_dir: Path) -> None:
    buffer = RecordHistoryBuffer(_TEST_PROGRAM, max_records=1, max_bytes=10**9, max_age=10**9)
    buffer.append(_entry(1))  # flushes id 1 to disk
    buffer.append(_entry(2))  # flushes id 2 to disk (memory empty again afterward)

    result = buffer.find_after(1, _NOON_UTC_2026_01_15)

    assert _ids(result) == (2,)

  def test_last_id_not_found_anywhere_returns_none(self, history_dir: Path) -> None:
    buffer = RecordHistoryBuffer(_TEST_PROGRAM, max_records=10**9, max_bytes=10**9, max_age=10**9)

    assert buffer.find_after(999, _NOON_UTC_2026_01_15) is None

  def test_gap_between_disk_and_memory_is_bridged_by_a_disk_search(self, history_dir: Path) -> None:
    buffer = RecordHistoryBuffer(_TEST_PROGRAM, max_records=1, max_bytes=10**9, max_age=10**9)
    buffer.append(_entry(1))  # flushes id 1 to disk, memory now empty
    buffer._max_records = 10**9  # pyright: ignore[reportPrivateUsage]
    buffer.append(_entry(2))  # stays in memory (entries[0].id == 2 > last_id == 1)
    buffer.append(_entry(3))

    result = buffer.find_after(1, _NOON_UTC_2026_01_15)

    assert _ids(result) == (2, 3)


class TestRecordHistoryBufferWriteThrough:
  def test_write_through_entries_are_persisted(self, history_dir: Path) -> None:
    buffer = RecordHistoryBuffer(_TEST_PROGRAM, max_records=10**9, max_bytes=10**9, max_age=10**9)
    buffer.begin_shutdown()

    buffer.append(_entry(1))  # first write-through append: runs the catch-up drain
    buffer.append(_entry(2))  # fast path: reuses the held-open handle

    (history_file,) = history_dir.glob("*.jsonl")
    assert [e.id for e in iter_entries(history_file)] == [1, 2]

  def test_write_through_reuses_the_same_handle_across_entries(self, history_dir: Path) -> None:
    buffer = RecordHistoryBuffer(_TEST_PROGRAM, max_records=10**9, max_bytes=10**9, max_age=10**9)
    buffer.begin_shutdown()
    buffer.append(_entry(1))  # catch-up drain; `_write_through_fh` stays unset until the next append
    buffer.append(_entry(2))  # first real write-through call: opens the handle
    first_fh = buffer._write_through_fh  # pyright: ignore[reportPrivateUsage]

    buffer.append(_entry(3))  # same-day fast path: must reuse `first_fh` rather than reopen

    assert buffer._write_through_fh is first_fh  # pyright: ignore[reportPrivateUsage]

  def test_write_through_switches_handle_when_the_date_rolls_over(self, history_dir: Path) -> None:
    buffer = RecordHistoryBuffer(_TEST_PROGRAM, max_records=10**9, max_bytes=10**9, max_age=10**9)
    buffer.begin_shutdown()
    later = _NOON_UTC_2026_01_15 + 86400 * 3
    buffer.append(_entry(1, created=_NOON_UTC_2026_01_15))

    buffer.append(_entry(2, created=later))

    files = sorted(history_dir.glob("*.jsonl"))
    assert len(files) == _TWO_ENTRIES
    assert [e.id for f in files for e in iter_entries(f)] == [1, 2]

  def test_close_releases_the_write_through_handle(self, history_dir: Path) -> None:
    buffer = RecordHistoryBuffer(_TEST_PROGRAM, max_records=10**9, max_bytes=10**9, max_age=10**9)
    buffer.begin_shutdown()
    buffer.append(_entry(1))
    (history_file,) = history_dir.glob("*.jsonl")

    buffer.close()

    assert buffer._write_through_fh is None  # pyright: ignore[reportPrivateUsage]
    history_file.unlink()  # would raise PermissionError on Windows if the handle were still open

  def test_close_before_write_through_mode_is_a_no_op(self, history_dir: Path) -> None:
    buffer = RecordHistoryBuffer(_TEST_PROGRAM, max_records=10**9, max_bytes=10**9, max_age=10**9)

    buffer.close()


class TestIterEntries:
  def test_missing_file_yields_nothing(self, tmp_path: Path) -> None:
    assert list(iter_entries(tmp_path / "does_not_exist.jsonl")) == []

  def test_malformed_lines_are_skipped_not_raised(self, tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    good = history_module._format_entry_line(_entry(1))  # pyright: ignore[reportPrivateUsage]
    path.write_text(f"{good}\nnot json at all\n\n", encoding="utf-8")

    entries = list(iter_entries(path))

    assert [e.id for e in entries] == [1]


class TestEmergencyHistoryWriter:
  def test_submitted_entries_are_persisted_and_marked_persisted(self, tmp_path: Path) -> None:
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

  def test_close_stops_the_background_thread(self, tmp_path: Path) -> None:
    writer = EmergencyHistoryWriter(tmp_path)

    writer.close()

    assert not writer._thread.is_alive()  # pyright: ignore[reportPrivateUsage]

  def test_entries_on_different_dates_land_in_different_files(self, tmp_path: Path) -> None:
    writer = EmergencyHistoryWriter(tmp_path)
    later = _NOON_UTC_2026_01_15 + 86400 * 3
    try:
      writer.submit(_entry(1, created=_NOON_UTC_2026_01_15))
      writer.submit(_entry(2, created=later))
    finally:
      writer.close()

    files = sorted(tmp_path.glob("*.jsonl"))
    assert len(files) == _TWO_ENTRIES


class TestRecordHistoryBufferHousekeeping:
  @staticmethod
  def _today() -> date:
    return datetime.now(tz=history_module.settings.tz).date()

  @staticmethod
  def _managed(history_dir: Path, day: date, size: int = 10) -> Path:
    path = history_module._history_file_for_date(history_dir, day)  # pyright: ignore[reportPrivateUsage]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path

  @pytest.fixture
  def alerts(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, object]]]:
    calls: list[tuple[str, str, dict[str, object]]] = []
    monkeypatch.setattr(history_module, "alert", lambda reason, details, **kw: calls.append((reason, details, kw)))
    return calls

  def test_history_file_name_carries_the_program_name(self, history_dir: Path) -> None:
    buffer = RecordHistoryBuffer(_TEST_PROGRAM, max_records=1, max_bytes=10**9, max_age=10**9)

    buffer.append(_entry(1))

    (history_file,) = history_dir.glob("*.jsonl")
    assert history_file.name == f"{_TEST_PROGRAM}_2026-01-15.jsonl"

  @pytest.mark.parametrize("bad_name", ["", "a/b", "a" + chr(92) + "b", ".", ".."])
  def test_rejects_program_names_that_are_not_a_plain_directory_name(self, history_dir: Path, bad_name: str) -> None:
    with pytest.raises(ValueError, match="program_name"):
      RecordHistoryBuffer(bad_name)

  def test_deletes_managed_files_strictly_older_than_the_retention_window(self, history_dir: Path) -> None:
    today = self._today()
    expired = self._managed(history_dir, today - timedelta(days=8))
    on_cutoff = self._managed(history_dir, today - timedelta(days=7))
    recent = self._managed(history_dir, today - timedelta(days=1))

    RecordHistoryBuffer(_TEST_PROGRAM, retention_days=7)

    assert not expired.exists()
    assert on_cutoff.exists()
    assert recent.exists()

  def test_never_touches_files_it_did_not_write(self, history_dir: Path) -> None:
    history_dir.mkdir(parents=True)
    legacy = history_dir / "2020-01-01.jsonl"  # pre-rename layout
    other_program = history_dir / "other-program_2020-01-01.jsonl"
    not_a_date = history_dir / f"{_TEST_PROGRAM}_notes.jsonl"
    wrong_suffix = history_dir / f"{_TEST_PROGRAM}_2020-01-01.jsonl.bak"
    root_level = history_dir.parent / "2020-01-01.jsonl"  # legacy interleaved layout at the root
    for path in (legacy, other_program, not_a_date, wrong_suffix, root_level):
      path.write_bytes(b"keep me")
    nested = history_dir / f"{_TEST_PROGRAM}_2020-01-02.jsonl"
    nested.mkdir()

    RecordHistoryBuffer(_TEST_PROGRAM, retention_days=1)

    for path in (legacy, other_program, not_a_date, wrong_suffix, root_level, nested):
      assert path.exists(), path

  def test_retention_reruns_only_when_the_date_rolls(self, history_dir: Path) -> None:
    buffer = RecordHistoryBuffer(_TEST_PROGRAM, max_records=1, max_bytes=10**9, max_age=10**9, retention_days=1)
    expired = self._managed(history_dir, self._today() - timedelta(days=30))

    buffer.append(_entry(1))  # flushes -> housekeeping, but retention already ran today
    assert expired.exists()

    buffer._last_retention_date = self._today() - timedelta(days=1)  # pyright: ignore[reportPrivateUsage]
    buffer.append(_entry(2))
    assert not expired.exists()

  def test_over_budget_alerts_once_per_excursion_and_deletes_nothing(
    self, history_dir: Path, alerts: list[tuple[str, str, dict[str, object]]]
  ) -> None:
    big = self._managed(history_dir, self._today() - timedelta(days=1), size=100_000)

    buffer = RecordHistoryBuffer(_TEST_PROGRAM, max_records=1, max_bytes=10**9, max_age=10**9, max_dir_bytes=50_000)
    assert big.exists()
    assert len(alerts) == 1
    reason, details, kwargs = alerts[0]
    assert "over size budget" in reason
    assert big.name in details
    assert kwargs == {"priority": -1, "in_except_block": False}

    buffer.append(_entry(1))  # still over budget: no second alert
    assert len(alerts) == 1

    big.unlink()
    buffer.append(_entry(2))  # back under budget: re-armed, still no alert
    assert len(alerts) == 1

    self._managed(history_dir, self._today() - timedelta(days=2), size=100_000)
    buffer.append(_entry(3))
    assert len(alerts) == _TWO_ENTRIES

  def test_under_budget_never_alerts(self, history_dir: Path, alerts: list[tuple[str, str, dict[str, object]]]) -> None:
    self._managed(history_dir, self._today(), size=100)

    buffer = RecordHistoryBuffer(_TEST_PROGRAM, max_records=1, max_bytes=10**9, max_age=10**9, max_dir_bytes=10**6)
    buffer.append(_entry(1))

    assert alerts == []

  def test_housekeeping_failure_is_reported_and_swallowed(self, history_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # First party imports
    from aeth_ext.logging import emergency_diagnostics

    def _boom(_history_dir: Path) -> list[tuple[date, Path, int]]:
      raise OSError("scan failed")

    monkeypatch.setattr(history_module, "_managed_history_files", _boom)

    buffer = RecordHistoryBuffer(_TEST_PROGRAM, max_records=1, max_bytes=10**9, max_age=10**9)
    buffer.append(_entry(1))  # the flush still lands even though housekeeping keeps failing

    assert list(history_dir.glob("*.jsonl"))
    assert "history housekeeping" in emergency_diagnostics.emergency_diagnostics_path.read_text(encoding="utf-8")
