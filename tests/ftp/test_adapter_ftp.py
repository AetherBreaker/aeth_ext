"""Tests for `aeth_ext.ftp.AdaptedFTP` against a real local `pyftpdlib` server."""

# Standard library imports
from datetime import UTC, datetime
from ftplib import error_perm
from typing import TYPE_CHECKING

# Third party imports
import pytest

# First party imports
from aeth_ext.ftp.errors import HandleReleasedError, LookupUnavailableError, MalformedReplyError
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable

  # First party imports
  from aeth_ext.ftp.session import AdaptedFTP


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


class TestListdirIteratorIsBoundToItsSession:
  """`listdir` streams from the live connection as it is iterated. Pooling makes an iterator that
  outlives its session actively dangerous -- the handle it is reading from may already have been
  checked out by another caller -- so advancing one past the `with` block raises instead."""

  def test_iterating_after_the_session_exits_raises(self, make_ftp_adapter: Callable[[], AdaptedFTP]) -> None:
    adapter = make_ftp_adapter()
    with adapter as ftp:
      _upload(ftp, "a.txt", b"a")
      entries = ftp.listdir(".")  # nothing read yet -- generators start on first next()

    with pytest.raises(HandleReleasedError):
      next(iter(entries))

  def test_a_partially_consumed_iterator_raises_on_the_next_step(self, make_ftp_adapter: Callable[[], AdaptedFTP]) -> None:
    # The dangerous shape: iteration started while the handle was held, then resumed after release.
    adapter = make_ftp_adapter()
    with adapter as ftp:
      for name in ("a.txt", "b.txt", "c.txt"):
        _upload(ftp, name, b"x")
      entries = ftp.listdir(".")
      first = next(iter(entries))

    assert first.filename in {"a.txt", "b.txt", "c.txt"}
    with pytest.raises(HandleReleasedError):
      next(iter(entries))

  def test_full_consumption_inside_the_session_still_works(self, make_ftp_adapter: Callable[[], AdaptedFTP]) -> None:
    # The guard must not disturb the ordinary case.
    with make_ftp_adapter() as ftp:
      for name in ("a.txt", "b.txt"):
        _upload(ftp, name, b"x")

      assert {entry.filename for entry in ftp.listdir(".")} == {"a.txt", "b.txt"}


def _upload(adapter: AdaptedFTP, remote_path: str, data: bytes) -> None:
  chunks = iter([data, b""])
  adapter.upload_file(remote_path, lambda _size: next(chunks), file_size=len(data))


class TestListdirLeavesTheConnectionInBinaryMode:
  """`ftplib.FTP.mlsd` routes through `retrlines`, which sends `TYPE A` and never restores `TYPE I`.
  Because connections are pooled, that left the handle in ASCII for whatever checked it out next, so
  a later transfer went through the server's newline translation and silently corrupted binary
  payloads -- a CRLF file arriving two bytes shorter per line, with the size check reporting a
  mismatch it could not account for. `listdir` therefore issues MLSD itself over the binary data
  connection instead of delegating."""

  def test_listdir_does_not_switch_the_connection_to_ascii(self, make_ftp_adapter: Callable[[], AdaptedFTP]) -> None:
    with make_ftp_adapter() as ftp:
      assert ftp.handler is not None
      sent: list[str] = []
      original_sendcmd = ftp.handler.sendcmd

      def _tracking_sendcmd(cmd: str) -> str:
        sent.append(cmd)
        return original_sendcmd(cmd)

      ftp.handler.sendcmd = _tracking_sendcmd
      list(ftp.listdir("."))
      list(ftp.listdir("."))  # a restore-afterwards fix would still show TYPE A on a repeat listing

    # pyftpdlib does not do the newline translation Pure-FTPd does, so a round-tripped CRLF payload
    # survives here either way and cannot pin the corruption itself; the mode the handle is left in
    # is what is checkable on any server, and is what the corruption followed from.
    assert [cmd for cmd in sent if cmd.startswith("TYPE")] == []


class TestGetSizeExceptionContract:
  """`get_size` must expose one contract regardless of protocol -- see the paired SFTP tests.

  The bug these cover: `AdaptedFTP` let `ftplib.error_perm` (not an `OSError`) escape while
  `AdaptedSFTP` raised `FileNotFoundError`, so no single `except` clause in a caller handled both.
  """

  def test_missing_file_raises_file_not_found(self, make_ftp_adapter: Callable[[], AdaptedFTP]) -> None:
    with make_ftp_adapter() as ftp, pytest.raises(FileNotFoundError):
      ftp.get_size("definitely_not_here.bin")

  def test_missing_file_does_not_leak_ftplib_error(self, make_ftp_adapter: Callable[[], AdaptedFTP]) -> None:
    """The specific regression: consumers must never need to import from `ftplib` to catch this."""
    with make_ftp_adapter() as ftp:
      try:
        ftp.get_size("definitely_not_here.bin")
      except error_perm as e:  # pragma: no cover - only reached on regression
        pytest.fail(f"raw ftplib.error_perm leaked to the caller: {e}")
      except OSError:
        pass

  def test_refused_lookup_is_not_reported_as_absent(self, make_ftp_adapter: Callable[[], AdaptedFTP]) -> None:
    """A `550` the server gives for a reason other than absence must not become `FileNotFoundError`.

    Reproduces the production failure (`550 Can't check for file existence`): folding it into
    "absent" makes every path look missing on such a server.
    """
    with make_ftp_adapter() as ftp:
      assert ftp.handler is not None

      def _refuse(_cmd: str) -> str:
        raise error_perm("550 Can't check for file existence")

      ftp.handler.sendcmd = _refuse  # pyright: ignore[reportAttributeAccessIssue]
      with pytest.raises(LookupUnavailableError):
        ftp.get_size("whatever.bin")

  def test_unparseable_size_reply_raises_malformed(self, make_ftp_adapter: Callable[[], AdaptedFTP]) -> None:
    """`ftplib.FTP.size` returns None on a non-213 reply; that must not surface as a size."""
    with make_ftp_adapter() as ftp:
      assert ftp.handler is not None
      ftp.handler.sendcmd = lambda _cmd: "200 Sure thing"  # pyright: ignore[reportAttributeAccessIssue]
      with pytest.raises(MalformedReplyError):
        ftp.get_size("whatever.bin")
