"""Tests for `aeth_ext.ftp.AdaptedFTP` against a real local `pyftpdlib` server."""

# Standard library imports
from datetime import UTC, datetime
from typing import TYPE_CHECKING

# First party imports
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable

  # First party imports
  from aeth_ext.ftp import AdaptedFTP


class TestUploadDownloadRoundTrip:
  def test_byte_exact_round_trip(self, make_ftp_adapter: Callable[[], AdaptedFTP]) -> None:
    data = b"hello world, this is a real ftp transfer"
    with make_ftp_adapter() as ftp:
      chunks = iter([data, b""])
      ftp.upload_file("file.txt", lambda _size: next(chunks), file_size=len(data))

      received = bytearray()
      ftp.download_file("file.txt", lambda chunk: received.extend(bytes(chunk)))

    assert bytes(received) == data


class TestTestConnection:
  def test_returns_true_when_server_is_reachable(self, make_ftp_adapter: Callable[[], AdaptedFTP]) -> None:
    adapter = make_ftp_adapter()

    assert adapter.test_connection() is True

  def test_returns_false_when_server_is_unreachable(self, make_ftp_adapter: Callable[[], AdaptedFTP]) -> None:
    adapter = make_ftp_adapter()
    # Point the underlying provider at a port nothing is listening on.
    adapter._provider._port = 1  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage]

    assert adapter.test_connection() is False


class TestGetSizeRenameRemoveMakedir:
  def test_get_size_matches_uploaded_content(self, make_ftp_adapter: Callable[[], AdaptedFTP]) -> None:
    data = b"0123456789"
    with make_ftp_adapter() as ftp:
      chunks = iter([data, b""])
      ftp.upload_file("sized.bin", lambda _size: next(chunks), file_size=len(data))

      assert ftp.get_size("sized.bin") == len(data)

  def test_rename(self, make_ftp_adapter: Callable[[], AdaptedFTP]) -> None:
    with make_ftp_adapter() as ftp:
      chunks = iter([b"x", b""])
      ftp.upload_file("old_name.txt", lambda _size: next(chunks), file_size=1)

      ftp.rename("old_name.txt", "new_name.txt")

      assert ftp.get_size("new_name.txt") == 1

  def test_remove(self, make_ftp_adapter: Callable[[], AdaptedFTP]) -> None:
    with make_ftp_adapter() as ftp:
      chunks = iter([b"x", b""])
      ftp.upload_file("to_delete.txt", lambda _size: next(chunks), file_size=1)

      ftp.remove("to_delete.txt")

      assert list(ftp.listdir(".")) == []

  def test_makedir_then_listdir_sees_it_as_a_directory_entry(self, make_ftp_adapter: Callable[[], AdaptedFTP]) -> None:
    with make_ftp_adapter() as ftp:
      ftp.makedir("a_new_dir")

      names = {entry.filename for entry in ftp.listdir(".")}
      assert "a_new_dir" in names


class TestListdirModifiedTime:
  def test_modified_time_is_timezone_aware_and_matches_settings_tz_by_default(
    self, make_ftp_adapter: Callable[[], AdaptedFTP]
  ) -> None:
    with make_ftp_adapter() as ftp:
      chunks = iter([b"x", b""])
      ftp.upload_file("timed.txt", lambda _size: next(chunks), file_size=1)

      (entry,) = list(ftp.listdir("."))

    assert entry.filename == "timed.txt"
    assert entry.modified_time.tzinfo == BaseSettings.get_settings(caller_file=__file__).tz

  def test_modified_time_is_recent(self, make_ftp_adapter: Callable[[], AdaptedFTP]) -> None:
    before = datetime.now(tz=UTC)
    with make_ftp_adapter() as ftp:
      chunks = iter([b"x", b""])
      ftp.upload_file("timed.txt", lambda _size: next(chunks), file_size=1)

      (entry,) = list(ftp.listdir("."))

    assert entry.modified_time.astimezone(UTC) >= before.replace(microsecond=0)
