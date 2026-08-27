"""Tests for `aeth_ext.ftp.AdaptedSFTP` against a real loopback `paramiko` SFTP server."""

# Standard library imports
from datetime import UTC, datetime
from typing import TYPE_CHECKING

# Third party imports
import pytest
from paramiko import SFTPError

# First party imports
from aeth_ext.ftp.errors import HandleReleasedError, LookupUnavailableError, MalformedReplyError
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable

  # First party imports
  from aeth_ext.ftp.session import AdaptedSFTP


class TestUploadDownloadRoundTrip:
  def test_byte_exact_round_trip(self, make_sftp_adapter: Callable[[], AdaptedSFTP]) -> None:
    data = b"hello world, this is a real sftp transfer"
    with make_sftp_adapter() as sftp:
      chunks = iter([data, b""])
      sftp.upload_file("file.txt", lambda _size: next(chunks), file_size=len(data))

      received = bytearray()
      sftp.download_file("file.txt", lambda chunk: received.extend(bytes(chunk)))

    assert bytes(received) == data


class TestTestConnection:
  def test_returns_true_when_server_is_reachable(self, make_sftp_adapter: Callable[[], AdaptedSFTP]) -> None:
    adapter = make_sftp_adapter()

    assert adapter.test_connection() is True

  def test_returns_false_when_server_is_unreachable(self, make_sftp_adapter: Callable[[], AdaptedSFTP]) -> None:
    adapter = make_sftp_adapter()
    adapter._provider._port = 1  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage]

    assert adapter.test_connection() is False


class TestGetSizeRenameRemoveMakedir:
  def test_get_size_matches_uploaded_content(self, make_sftp_adapter: Callable[[], AdaptedSFTP]) -> None:
    data = b"0123456789"
    with make_sftp_adapter() as sftp:
      chunks = iter([data, b""])
      sftp.upload_file("sized.bin", lambda _size: next(chunks), file_size=len(data))

      assert sftp.get_size("sized.bin") == len(data)

  def test_get_size_raises_file_not_found_for_missing_file(self, make_sftp_adapter: Callable[[], AdaptedSFTP]) -> None:
    with make_sftp_adapter() as sftp, pytest.raises(FileNotFoundError):
      sftp.get_size("does_not_exist.bin")

  def test_rename(self, make_sftp_adapter: Callable[[], AdaptedSFTP]) -> None:
    with make_sftp_adapter() as sftp:
      chunks = iter([b"x", b""])
      sftp.upload_file("old_name.txt", lambda _size: next(chunks), file_size=1)

      sftp.rename("old_name.txt", "new_name.txt")

      assert sftp.get_size("new_name.txt") == 1

  def test_remove(self, make_sftp_adapter: Callable[[], AdaptedSFTP]) -> None:
    with make_sftp_adapter() as sftp:
      chunks = iter([b"x", b""])
      sftp.upload_file("to_delete.txt", lambda _size: next(chunks), file_size=1)

      sftp.remove("to_delete.txt")

      assert list(sftp.listdir(".")) == []

  def test_makedir_then_listdir_sees_it_as_a_directory_entry(self, make_sftp_adapter: Callable[[], AdaptedSFTP]) -> None:
    with make_sftp_adapter() as sftp:
      sftp.makedir("a_new_dir")

      names = {entry.filename for entry in sftp.listdir(".")}
      assert "a_new_dir" in names


class TestListdirModifiedTime:
  def test_modified_time_is_timezone_aware_and_matches_settings_tz_by_default(
    self, make_sftp_adapter: Callable[[], AdaptedSFTP]
  ) -> None:
    with make_sftp_adapter() as sftp:
      chunks = iter([b"x", b""])
      sftp.upload_file("timed.txt", lambda _size: next(chunks), file_size=1)

      (entry,) = list(sftp.listdir("."))

    assert entry.filename == "timed.txt"
    assert entry.modified_time.tzinfo == BaseSettings.get_settings(caller_file=__file__).tz

  def test_modified_time_is_recent(self, make_sftp_adapter: Callable[[], AdaptedSFTP]) -> None:
    before = datetime.now(tz=UTC)
    with make_sftp_adapter() as sftp:
      chunks = iter([b"x", b""])
      sftp.upload_file("timed.txt", lambda _size: next(chunks), file_size=1)

      (entry,) = list(sftp.listdir("."))

    assert entry.modified_time.astimezone(UTC) >= before.replace(microsecond=0)


class TestPoolWiring:
  def test_build_session_hands_the_pool_not_the_adapter_as_provider(self) -> None:
    # First party imports
    from aeth_ext.ftp.credentials import SFTPCredentials
    from aeth_ext.ftp.pool.sftp_adapter import SFTPAdapter

    adapter = SFTPAdapter(SFTPCredentials(host="127.0.0.1", username="u", password="p"))  # pyright: ignore[reportArgumentType]

    session = adapter.start_session()

    assert session._provider is adapter._ledger.pool  # pyright: ignore[reportPrivateUsage]
    assert session._provider is not adapter  # pyright: ignore[reportPrivateUsage]

  def test_transport_dialer_delegates_to_the_adapters_slot_bookkeeping(self) -> None:
    """`SFTPAdapter` no longer implements `open_transport`/`transport_dropped` itself (that
    responsibility moved to `TransportDialer`, built once in `__init__` and stored on
    `adapter._ledger.transports`) -- exercise the same behavior through that new indirection."""
    # First party imports
    from aeth_ext.ftp.credentials import SFTPCredentials
    from aeth_ext.ftp.pool.sftp_adapter import SFTPAdapter

    adapter = SFTPAdapter(SFTPCredentials(host="127.0.0.1", username="u", password="p"), max_connections=1)  # pyright: ignore[reportArgumentType]
    adapter._current_size = 1  # pyright: ignore[reportPrivateUsage] -- simulate the ceiling already reached

    assert adapter._ledger.transports.open_transport() is None  # pyright: ignore[reportPrivateUsage] -- _open_new_slot refuses past max_connections

    adapter._ledger.transports.transport_dropped()  # pyright: ignore[reportPrivateUsage]

    assert adapter._current_size == 0  # pyright: ignore[reportPrivateUsage]


class TestListdirIteratorIsBoundToItsSession:
  """`listdir_iter` genuinely streams -- it fetches further batches from the channel as it is
  consumed -- so an iterator outliving its session would read from a channel the pool may already
  have handed to another caller."""

  def test_iterating_after_the_session_exits_raises(self, make_sftp_adapter: Callable[[], AdaptedSFTP]) -> None:
    adapter = make_sftp_adapter()
    with adapter as sftp:
      _sftp_upload(sftp, "a.txt", b"a")
      entries = sftp.listdir(".")

    with pytest.raises(HandleReleasedError):
      next(iter(entries))

  def test_a_partially_consumed_iterator_raises_on_the_next_step(self, make_sftp_adapter: Callable[[], AdaptedSFTP]) -> None:
    adapter = make_sftp_adapter()
    with adapter as sftp:
      for name in ("a.txt", "b.txt", "c.txt"):
        _sftp_upload(sftp, name, b"x")
      entries = sftp.listdir(".")
      first = next(iter(entries))

    assert first.filename in {"a.txt", "b.txt", "c.txt"}
    with pytest.raises(HandleReleasedError):
      next(iter(entries))

  def test_full_consumption_inside_the_session_still_works(self, make_sftp_adapter: Callable[[], AdaptedSFTP]) -> None:
    with make_sftp_adapter() as sftp:
      for name in ("a.txt", "b.txt"):
        _sftp_upload(sftp, name, b"x")

      assert {entry.filename for entry in sftp.listdir(".")} == {"a.txt", "b.txt"}


def _sftp_upload(adapter: AdaptedSFTP, remote_path: str, data: bytes) -> None:
  chunks = iter([data, b""])
  adapter.upload_file(remote_path, lambda _size: next(chunks), file_size=len(data))


class TestListdirGuardRunsBeforeTheChannelRead:
  """`listdir_iter` fetches each further batch inside its own `next()`. Checking the guard after
  advancing would already have put a read onto a channel the pool may have reassigned -- the exact
  interleaving the guard exists to prevent -- so it has to run first."""

  def test_no_further_read_is_issued_after_release(self, make_sftp_adapter: Callable[[], AdaptedSFTP]) -> None:
    advanced: list[str] = []
    adapter = make_sftp_adapter()
    with adapter as sftp:
      for name in ("a.txt", "b.txt", "c.txt"):
        _sftp_upload(sftp, name, b"x")
      handler = sftp.handler
      assert handler is not None
      real_listdir_iter = handler.listdir_iter

      def spy(path: str = ".") -> object:
        for item in real_listdir_iter(path):
          advanced.append(item.filename)
          yield item

      handler.listdir_iter = spy  # pyright: ignore[reportAttributeAccessIssue]
      entries = sftp.listdir(".")
      next(iter(entries))
      reads_while_held = len(advanced)

    assert reads_while_held >= 1
    with pytest.raises(HandleReleasedError):
      next(iter(entries))

    assert len(advanced) == reads_while_held, "the guard let a read reach the reassigned channel"


class TestGetSizeExceptionContract:
  """Mirrors `test_adapter_ftp.TestGetSizeExceptionContract` -- the two must stay in lockstep.

  If a case is added to one adapter's contract, add it here too: the original bug was precisely the
  two adapters drifting apart on the same method.
  """

  def test_missing_file_raises_file_not_found(self, make_sftp_adapter: Callable[[], AdaptedSFTP]) -> None:
    with make_sftp_adapter() as sftp, pytest.raises(FileNotFoundError):
      sftp.get_size("definitely_not_here.bin")

  def test_errnoless_failure_is_not_reported_as_absent(self, make_sftp_adapter: Callable[[], AdaptedSFTP]) -> None:
    """paramiko raises a bare `OSError(text)` with no errno for any status it has no mapping for.

    That says only that the lookup failed -- never that the file is absent -- so it must not become
    `FileNotFoundError`.
    """
    with make_sftp_adapter() as sftp:
      assert sftp.handler is not None

      def _refuse(_path: str) -> object:
        raise OSError("Can't check for file existence")

      sftp.handler.stat = _refuse  # pyright: ignore[reportAttributeAccessIssue]
      with pytest.raises(LookupUnavailableError):
        sftp.get_size("whatever.bin")

  def test_sftp_error_raises_malformed_rather_than_returning_none(self, make_sftp_adapter: Callable[[], AdaptedSFTP]) -> None:
    """Previously absorbed into a `None` return, conflating a misbehaving server with a real answer."""
    with make_sftp_adapter() as sftp:
      assert sftp.handler is not None

      def _garble(_path: str) -> object:
        raise SFTPError("garbled reply")

      sftp.handler.stat = _garble  # pyright: ignore[reportAttributeAccessIssue]
      with pytest.raises(MalformedReplyError):
        sftp.get_size("whatever.bin")
