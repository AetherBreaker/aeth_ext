"""The exception contract `AdapterBase` documents, checked against both adapters with one test body.

`AdaptedFTP` and `AdaptedSFTP` used to raise unrelated types for the same condition (`ftplib.error_perm`
vs. `FileNotFoundError`), so a caller could not handle both with one `except`. Every test here runs
once per adapter against its real local server; the exact type is asserted (`type(exc) is ...`) so a
subclass can't satisfy a base-class case by accident.
"""

# Standard library imports
from errno import ENOSPC, ENOSYS
from ftplib import error_perm, error_proto, error_reply, error_temp
from typing import TYPE_CHECKING

# Third party imports
import pytest
from paramiko import SFTPError, SSHException

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable

  # First party imports
  from aeth_ext.ftp.session import AdaptedFTP, AdaptedSFTP


@pytest.fixture(params=["ftp", "sftp"])
def make_adapter(request: pytest.FixtureRequest) -> Callable[[], AdaptedFTP | AdaptedSFTP]:
  return request.getfixturevalue(f"make_{request.param}_adapter")


def _seed(adapter: AdaptedFTP | AdaptedSFTP, path: str, data: bytes = b"x") -> None:
  chunks = iter([data, b""])
  adapter.upload_file(path, lambda _size: next(chunks), file_size=len(data))


def _raises_exactly[E: BaseException](exc_type: type[E]) -> pytest.RaisesExc[E]:
  return pytest.raises(exc_type, check=lambda e: type(e) is exc_type)


class TestMissingPathIsFileNotFoundError:
  def test_get_size(self, make_adapter: Callable[[], AdaptedFTP | AdaptedSFTP]) -> None:
    with make_adapter() as adapter, _raises_exactly(FileNotFoundError):
      adapter.get_size("missing.bin")

  def test_download_file(self, make_adapter: Callable[[], AdaptedFTP | AdaptedSFTP]) -> None:
    with make_adapter() as adapter, _raises_exactly(FileNotFoundError):
      adapter.download_file("missing.bin", lambda _data: None)

  def test_remove(self, make_adapter: Callable[[], AdaptedFTP | AdaptedSFTP]) -> None:
    with make_adapter() as adapter, _raises_exactly(FileNotFoundError):
      adapter.remove("missing.bin")

  def test_rename(self, make_adapter: Callable[[], AdaptedFTP | AdaptedSFTP]) -> None:
    with make_adapter() as adapter, _raises_exactly(FileNotFoundError):
      adapter.rename("missing.bin", "renamed.bin")

  def test_listdir(self, make_adapter: Callable[[], AdaptedFTP | AdaptedSFTP]) -> None:
    """Only `OSError` here: pyftpdlib answers `MLSD` on a missing directory with `501`, not `550`, and
    code-based dispatch honestly reports that as a generic refusal. Real servers that say `550` get
    `FileNotFoundError`.
    """
    with make_adapter() as adapter, pytest.raises(OSError):
      list(adapter.listdir("missing_dir"))

  def test_upload_into_missing_directory(self, make_adapter: Callable[[], AdaptedFTP | AdaptedSFTP]) -> None:
    with make_adapter() as adapter, _raises_exactly(FileNotFoundError):
      _seed(adapter, "missing_dir/file.bin")


class TestOtherRefusalsArePlainOSError:
  def test_makedir_existing(self, make_adapter: Callable[[], AdaptedFTP | AdaptedSFTP]) -> None:
    """Neither protocol distinguishes "already exists" from other `MKD`/`mkdir` failures by code."""
    with make_adapter() as adapter:
      adapter.makedir("dir")
      with _raises_exactly(OSError):
        adapter.makedir("dir")


class TestFTPReplyCodeDispatch:
  """`ftplib` carries the reply code only as the message's first three characters -- the text after it
  is server-specific and is never matched.
  """

  @pytest.mark.parametrize(
    ("reply", "expected"),
    [
      ("550 Can't check for file existence", FileNotFoundError),
      ("553 Could not create file", PermissionError),
      ("530 Not logged in", PermissionError),
      ("532 Need account for storing files", PermissionError),
      ("534 Request denied for policy reasons", PermissionError),
      ("500 Syntax error", OSError),
      ("451 Requested action aborted: local error", OSError),
      ("421 Service not available, closing control connection", ConnectionAbortedError),
      ("425 Can't open data connection", BlockingIOError),
      ("450 Requested file action not taken: file busy", BlockingIOError),
    ],
  )
  def test_get_size(self, make_ftp_adapter: Callable[[], AdaptedFTP], reply: str, expected: type[OSError]) -> None:
    with make_ftp_adapter() as ftp:
      assert ftp.handler is not None

      def _refuse(_cmd: str) -> str:
        raise (error_temp if reply[0] == "4" else error_perm)(reply)

      ftp.handler.sendcmd = _refuse  # pyright: ignore[reportAttributeAccessIssue]
      with _raises_exactly(expected):
        ftp.get_size("whatever.bin")

  @pytest.mark.parametrize(
    ("reply", "expected_errno"),
    [
      ("452 Insufficient storage space", ENOSPC),
      ("552 Exceeded storage allocation", ENOSPC),
      ("502 Command not implemented", ENOSYS),
    ],
  )
  def test_errno_tagged_replies(self, make_ftp_adapter: Callable[[], AdaptedFTP], reply: str, expected_errno: int) -> None:
    """No stdlib subclass exists for these, so the errno is the signal a caller can branch on."""
    with make_ftp_adapter() as ftp:
      assert ftp.handler is not None

      def _refuse(_cmd: str) -> str:
        raise (error_temp if reply[0] == "4" else error_perm)(reply)

      ftp.handler.sendcmd = _refuse  # pyright: ignore[reportAttributeAccessIssue]
      with _raises_exactly(OSError) as info:
        ftp.get_size("whatever.bin")
      assert info.value.errno == expected_errno

  def test_desynchronized_reply_is_connection_error(self, make_ftp_adapter: Callable[[], AdaptedFTP]) -> None:
    """`error_proto` means the control stream can no longer be parsed -- the handle must be dropped."""
    with make_ftp_adapter() as ftp:
      assert ftp.handler is not None

      def _garble(_cmd: str) -> str:
        raise error_proto("garbage")

      ftp.handler.sendcmd = _garble  # pyright: ignore[reportAttributeAccessIssue]
      with _raises_exactly(ConnectionError):
        ftp.get_size("whatever.bin")

  def test_unexpected_reply_class_is_oserror(self, make_ftp_adapter: Callable[[], AdaptedFTP]) -> None:
    with make_ftp_adapter() as ftp:
      assert ftp.handler is not None

      def _odd(_cmd: str) -> str:
        raise error_reply("150 Unexpected")

      ftp.handler.sendcmd = _odd  # pyright: ignore[reportAttributeAccessIssue]
      with _raises_exactly(OSError):
        ftp.get_size("whatever.bin")

  def test_non_213_size_reply_is_oserror(self, make_ftp_adapter: Callable[[], AdaptedFTP]) -> None:
    """`ftplib.FTP.size` returns None on a non-213 reply; that must not surface as a size."""
    with make_ftp_adapter() as ftp:
      assert ftp.handler is not None
      ftp.handler.sendcmd = lambda _cmd: "200 Sure thing"  # pyright: ignore[reportAttributeAccessIssue]
      with _raises_exactly(OSError):
        ftp.get_size("whatever.bin")

  def test_426_completion_reply_is_connection_error(self, make_ftp_adapter: Callable[[], AdaptedFTP]) -> None:
    """A `426` arrives on the completion reply after the data connection closes, not on the command
    that opened it -- it must be translated on that path too, not leak as a raw `error_temp`.
    """
    with make_ftp_adapter() as ftp:
      assert ftp.handler is not None
      real_getresp = ftp.handler.getresp

      def _abort_completion() -> str:
        # Only the transfer's own completion reply is replaced; `voidcmd` (TYPE, QUIT) and the
        # STOR's 150 all route through `getresp` too and must keep working.
        resp = real_getresp()
        if resp.startswith("226"):
          raise error_temp("426 Connection closed; transfer aborted.")
        return resp

      ftp.handler.getresp = _abort_completion
      with _raises_exactly(ConnectionAbortedError):
        _seed(ftp, "file.bin")

  def test_original_reply_is_chained(self, make_ftp_adapter: Callable[[], AdaptedFTP]) -> None:
    with make_ftp_adapter() as ftp, pytest.raises(FileNotFoundError) as info:
      ftp.get_size("missing.bin")
    assert isinstance(info.value.__cause__, error_perm)


class TestSFTPChannelFailuresAreConnectionError:
  """paramiko's two non-`OSError` failure types both mean the channel can't be trusted."""

  @pytest.mark.parametrize("native", [SFTPError("garbled"), SSHException("transport died")])
  def test_get_size(self, make_sftp_adapter: Callable[[], AdaptedSFTP], native: Exception) -> None:
    with make_sftp_adapter() as sftp:
      assert sftp.handler is not None

      def _fail(_path: str) -> None:
        raise native

      sftp.handler.stat = _fail  # pyright: ignore[reportAttributeAccessIssue]
      with _raises_exactly(ConnectionError) as info:
        sftp.get_size("whatever.bin")
      assert info.value.__cause__ is native
