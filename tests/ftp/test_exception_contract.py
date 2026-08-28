"""The exception contract `AdapterBase` documents, checked against both adapters with one test body.

`AdaptedFTP` and `AdaptedSFTP` used to raise unrelated types for the same condition (`ftplib.error_perm`
vs. `FileNotFoundError`), so a caller could not handle both with one `except`. Every test here runs
once per adapter against its real local server; the exact type is asserted (`type(exc) is ...`) so a
subclass can't satisfy a base-class case by accident.
"""

# Standard library imports
from ftplib import error_perm, error_temp
from typing import TYPE_CHECKING

# Third party imports
import pytest
from paramiko import SFTPError

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


def _raises_exactly(exc_type: type[BaseException]) -> pytest.RaisesExc[BaseException]:
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
    `FileNotFoundError`."""
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
  is server-specific and is never matched."""

  @pytest.mark.parametrize(
    ("reply", "expected"),
    [
      ("550 Can't check for file existence", FileNotFoundError),
      ("553 Could not create file", PermissionError),
      ("530 Not logged in", PermissionError),
      ("532 Need account for storing files", PermissionError),
      ("500 Syntax error", OSError),
      ("450 Requested file action not taken", OSError),
      ("421 Service not available, closing control connection", ConnectionError),
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

  def test_non_213_size_reply_is_oserror(self, make_ftp_adapter: Callable[[], AdaptedFTP]) -> None:
    """`ftplib.FTP.size` returns None on a non-213 reply; that must not surface as a size."""
    with make_ftp_adapter() as ftp:
      assert ftp.handler is not None
      ftp.handler.sendcmd = lambda _cmd: "200 Sure thing"  # pyright: ignore[reportAttributeAccessIssue]
      with _raises_exactly(OSError):
        ftp.get_size("whatever.bin")

  def test_original_reply_is_chained(self, make_ftp_adapter: Callable[[], AdaptedFTP]) -> None:
    with make_ftp_adapter() as ftp, pytest.raises(FileNotFoundError) as info:
      ftp.get_size("missing.bin")
    assert isinstance(info.value.__cause__, error_perm)


class TestSFTPMalformedReplyIsOSError:
  def test_get_size(self, make_sftp_adapter: Callable[[], AdaptedSFTP]) -> None:
    with make_sftp_adapter() as sftp:
      assert sftp.handler is not None

      def _garble(_path: str) -> None:
        raise SFTPError("garbled")

      sftp.handler.stat = _garble  # pyright: ignore[reportAttributeAccessIssue]
      with _raises_exactly(OSError) as info:
        sftp.get_size("whatever.bin")
      assert isinstance(info.value.__cause__, SFTPError)
