# Standard library imports
import logging
from typing import TYPE_CHECKING

# Third party imports
import pytest

# First party imports
from aeth_ext.logging import bases
from aeth_ext.logging.bases import CustomTimedRotatingFileHandler, TaggedLogRecord

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Generator
  from pathlib import Path

_ONE_KIB = 1024


def _record(message: str) -> TaggedLogRecord:
  return TaggedLogRecord("prog", logging.INFO, __file__, 1, message, None, None)


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, object]]]:
  calls: list[tuple[str, str, dict[str, object]]] = []
  monkeypatch.setattr(bases, "alert", lambda reason, details, **kw: calls.append((reason, details, kw)))
  return calls


@pytest.fixture
def handler(tmp_path: Path) -> Generator[CustomTimedRotatingFileHandler]:
  h = CustomTimedRotatingFileHandler(str(tmp_path / "prog.log"), when="midnight", backupCount=2, size_warn_bytes=_ONE_KIB)
  h.setFormatter(logging.Formatter("%(message)s"))
  yield h
  h.close()


class TestSizeWatchdog:
  def test_alerts_once_at_the_threshold_then_at_each_doubling(
    self, handler: CustomTimedRotatingFileHandler, alerts: list[tuple[str, str, dict[str, object]]]
  ) -> None:
    line = "x" * 99  # 100 bytes with the newline

    for _ in range(9):  # 900 bytes: under the 1 KiB threshold
      handler.emit(_record(line))
    assert alerts == []

    for _ in range(2):  # 1100 bytes: crosses 1 KiB
      handler.emit(_record(line))
    assert len(alerts) == 1
    reason, details, kwargs = alerts[0]
    assert "unusually large" in reason
    assert handler.baseFilename in details
    assert kwargs == {"priority": -1, "in_except_block": False}

    for _ in range(9):  # 2000 bytes: still under the 2 KiB ladder step
      handler.emit(_record(line))
    assert len(alerts) == 1

    handler.emit(_record(line))  # 2100 bytes: crosses 2 KiB
    assert len(alerts) == 2  # noqa: PLR2004

  def test_a_single_huge_write_skips_straight_up_the_ladder(
    self, handler: CustomTimedRotatingFileHandler, alerts: list[tuple[str, str, dict[str, object]]]
  ) -> None:
    handler.emit(_record("x" * (5 * _ONE_KIB)))  # past 1, 2 and 4 KiB in one go

    assert len(alerts) == 1
    assert handler._next_size_warn == 8 * _ONE_KIB  # pyright: ignore[reportPrivateUsage]

  def test_rollover_resets_the_ladder(
    self, handler: CustomTimedRotatingFileHandler, alerts: list[tuple[str, str, dict[str, object]]]
  ) -> None:
    handler.emit(_record("x" * (3 * _ONE_KIB)))
    assert len(alerts) == 1

    handler.doRollover()
    handler.emit(_record("x" * (3 * _ONE_KIB)))

    assert len(alerts) == 2  # noqa: PLR2004 -- the fresh file crossed the base threshold again

  def test_zero_disables_the_watchdog(self, tmp_path: Path, alerts: list[tuple[str, str, dict[str, object]]]) -> None:
    h = CustomTimedRotatingFileHandler(str(tmp_path / "prog.log"), when="midnight", size_warn_bytes=0)
    try:
      h.emit(_record("x" * (64 * _ONE_KIB)))
    finally:
      h.close()

    assert alerts == []

  def test_defaults_to_the_setting(self, tmp_path: Path) -> None:
    h = CustomTimedRotatingFileHandler(str(tmp_path / "prog.log"), when="midnight")
    try:
      assert h._size_warn_bytes == bases.BaseSettings.get_settings().log_file_size_warn_bytes  # pyright: ignore[reportPrivateUsage]
    finally:
      h.close()

  def test_alert_failure_never_escapes_emit(self, handler: CustomTimedRotatingFileHandler, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_kw: object) -> None:
      raise RuntimeError("smtp down")

    monkeypatch.setattr(bases, "alert", _boom)

    handler.emit(_record("x" * (3 * _ONE_KIB)))  # must not raise


class TestGetFilesToDelete:
  @staticmethod
  def _touch(directory: Path, *names: str) -> None:
    for name in names:
      (directory / name).write_text("x", encoding="utf-8")

  def test_matches_this_projects_rotated_naming_and_keeps_backup_count(
    self, tmp_path: Path, handler: CustomTimedRotatingFileHandler
  ) -> None:
    self._touch(tmp_path, "prog.2026-07-19.log", "prog.2026-07-20.log", "prog.2026-08-01.log", "prog.2026-08-02.log")

    doomed = [bases.Path(p).name for p in handler.getFilesToDelete()]

    assert doomed == ["prog.2026-07-19.log", "prog.2026-07-20.log"]  # backupCount=2 keeps the newest two

  def test_ignores_files_that_are_not_this_handlers_rotations(self, tmp_path: Path, handler: CustomTimedRotatingFileHandler) -> None:
    self._touch(
      tmp_path,
      "prog_debug.2026-07-19.log",  # sibling handler with a different stem
      "prog_debug.2026-07-20.log",
      "prog_debug.2026-07-21.log",
      "prog.2026-07-19.txt",  # wrong suffix
      "prog.notes.log",  # not a date
      "prog.log",  # the live file itself
      "other.2026-07-19.log",
    )
    (tmp_path / "prog.2026-07-18.log").mkdir()  # a directory with a matching name

    assert handler.getFilesToDelete() == []

  def test_fewer_rotations_than_backup_count_deletes_nothing(self, tmp_path: Path, handler: CustomTimedRotatingFileHandler) -> None:
    self._touch(tmp_path, "prog.2026-07-19.log")

    assert handler.getFilesToDelete() == []

  def test_do_rollover_actually_prunes(self, tmp_path: Path, handler: CustomTimedRotatingFileHandler) -> None:
    self._touch(tmp_path, "prog.2026-07-19.log", "prog.2026-07-20.log")
    handler.emit(_record("hello"))

    handler.doRollover()

    names = sorted(p.name for p in tmp_path.iterdir() if not p.name.startswith("."))
    # The rollover added one dated file; backupCount=2 then prunes the oldest so exactly two remain.
    assert len([n for n in names if n != "prog.log"]) == 2  # noqa: PLR2004
    assert "prog.2026-07-19.log" not in names
