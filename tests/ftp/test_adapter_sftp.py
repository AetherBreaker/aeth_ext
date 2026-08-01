"""Tests for `aeth_ext.ftp.adapter.AdaptedSFTP` against a real loopback `paramiko` SFTP server."""

# Standard library imports
from datetime import UTC, datetime
from typing import TYPE_CHECKING

# First party imports
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable

  # First party imports
  from aeth_ext.ftp.adapter import AdaptedSFTP


class TestUploadDownloadRoundTrip:
  def test_byte_exact_round_trip(self, make_sftp_adapter: Callable[[], AdaptedSFTP]):
    data = b"hello world, this is a real sftp transfer"
    with make_sftp_adapter() as sftp:
      chunks = iter([data, b""])
      sftp.upload_file("file.txt", lambda _size: next(chunks), file_size=len(data))

      received = bytearray()
      sftp.download_file("file.txt", received.extend)

    assert bytes(received) == data


class TestTestConnection:
  def test_returns_true_when_server_is_reachable(self, make_sftp_adapter: Callable[[], AdaptedSFTP]):
    adapter = make_sftp_adapter()

    assert adapter.test_connection() is True

  def test_returns_false_when_server_is_unreachable(self, make_sftp_adapter: Callable[[], AdaptedSFTP]):
    adapter = make_sftp_adapter()
    adapter.proto_instance._port = 1  # pyright: ignore[reportAttributeAccessIssue]

    assert adapter.test_connection() is False


class TestGetSizeRenameRemoveMakedir:
  def test_get_size_matches_uploaded_content(self, make_sftp_adapter: Callable[[], AdaptedSFTP]):
    data = b"0123456789"
    with make_sftp_adapter() as sftp:
      chunks = iter([data, b""])
      sftp.upload_file("sized.bin", lambda _size: next(chunks), file_size=len(data))

      assert sftp.get_size("sized.bin") == len(data)

  def test_get_size_returns_none_and_logs_on_failure(self, make_sftp_adapter: Callable[[], AdaptedSFTP]):
    with make_sftp_adapter() as sftp:
      assert sftp.get_size("does_not_exist.bin") is None

  def test_rename(self, make_sftp_adapter: Callable[[], AdaptedSFTP]):
    with make_sftp_adapter() as sftp:
      chunks = iter([b"x", b""])
      sftp.upload_file("old_name.txt", lambda _size: next(chunks), file_size=1)

      sftp.rename("old_name.txt", "new_name.txt")

      assert sftp.get_size("new_name.txt") == 1

  def test_remove(self, make_sftp_adapter: Callable[[], AdaptedSFTP]):
    with make_sftp_adapter() as sftp:
      chunks = iter([b"x", b""])
      sftp.upload_file("to_delete.txt", lambda _size: next(chunks), file_size=1)

      sftp.remove("to_delete.txt")

      assert list(sftp.listdir(".")) == []

  def test_makedir_then_listdir_sees_it_as_a_directory_entry(self, make_sftp_adapter: Callable[[], AdaptedSFTP]):
    with make_sftp_adapter() as sftp:
      sftp.makedir("a_new_dir")

      names = {entry.filename for entry in sftp.listdir(".")}
      assert "a_new_dir" in names


class TestListdirModifiedTime:
  def test_modified_time_is_timezone_aware_and_matches_settings_tz_by_default(
    self, make_sftp_adapter: Callable[[], AdaptedSFTP]
  ):
    with make_sftp_adapter() as sftp:
      chunks = iter([b"x", b""])
      sftp.upload_file("timed.txt", lambda _size: next(chunks), file_size=1)

      (entry,) = list(sftp.listdir("."))

    assert entry.filename == "timed.txt"
    assert entry.modified_time.tzinfo == BaseSettings.get_settings(caller_file=__file__).tz

  def test_modified_time_is_recent(self, make_sftp_adapter: Callable[[], AdaptedSFTP]):
    before = datetime.now(tz=UTC)
    with make_sftp_adapter() as sftp:
      chunks = iter([b"x", b""])
      sftp.upload_file("timed.txt", lambda _size: next(chunks), file_size=1)

      (entry,) = list(sftp.listdir("."))

    assert entry.modified_time.astimezone(UTC) >= before.replace(microsecond=0)
