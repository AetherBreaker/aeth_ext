# Standard library imports
from abc import abstractmethod
from enum import Enum, auto
from typing import TYPE_CHECKING, NamedTuple, Protocol, override

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Buffer, Callable, Iterator
  from datetime import datetime
  from ftplib import FTP
  from io import BytesIO
  from typing import Any

  # Third party imports
  from paramiko import SFTPClient

  # First party imports
  from sft_ext.ftp.adapter import AdaptedFTP, AdaptedSFTP


__all__ = ["AdapterProtocol", "FTPProtocol", "ListDirResult", "ProtocolEnum", "SFTPProtocol"]

BufferSize = int
TransferSuccess = bool


class ProtocolEnum(Enum):
  FTP = auto()
  SFTP = auto()


class ListDirResult(NamedTuple):
  filename: str
  modified_time: datetime


class FTPProtocolBase(Protocol):
  KIND: ProtocolEnum

  @abstractmethod
  def get_conn_handler(self) -> Any:
    raise NotImplementedError

  @abstractmethod
  def close_conn_handler(self) -> None:
    raise NotImplementedError


class FTPProtocol(FTPProtocolBase):
  KIND = ProtocolEnum.FTP

  @override
  @abstractmethod
  def get_conn_handler(self) -> FTP:
    raise NotImplementedError


class SFTPProtocol(FTPProtocolBase):
  KIND = ProtocolEnum.SFTP

  @override
  @abstractmethod
  def get_conn_handler(self) -> SFTPClient:
    raise NotImplementedError


class AdapterProtocol(Protocol):
  def test_connection(self, logit: bool = False) -> bool:
    """Tests the connection to the FTP/SFTP server. Returns True if successful, False otherwise."""
    raise NotImplementedError

  def get_size(self, path: str) -> int | None:
    """Expects an absolute path to a file on the FTP/SFTP server and returns its size in bytes."""
    raise NotImplementedError

  def upload_file(self, remote_path: str, callback: Callable[[BufferSize], bytes], file_size: int, task_msg: str = "") -> None:
    """Expects an absolute path to a file on the FTP/SFTP server and returns a writable file-like object (e.g. socket or SFTPFile) that can be used to send the file's contents."""
    raise NotImplementedError

  def download_file(self, remote_path: str, callback: Callable[[Buffer], Any], task_msg: str = "") -> None:
    """Expects an absolute path to a file on the FTP/SFTP server and returns a readable file-like object (e.g. socket or SFTPFile) that can be used to read the file's contents."""
    raise NotImplementedError

  def transfer_file(
    self,
    source_remote_path: str,
    dest_remote_path: str,
    other: AdaptedFTP | AdaptedSFTP,
    task_msg: str = "",
    callback: Callable[[bytes], None] | None = None,
    mem_stream: BytesIO | None = None,
  ) -> TransferSuccess:
    """Transfers a file from source_remote_path to dest_remote_path on the FTP/SFTP server.
    This is intended to be used for server to server transfers that don't save the file locally."""
    raise NotImplementedError

  def rename(self, old_remote_path: str, new_remote_path: str) -> None:
    """Renames a file on the FTP/SFTP server from old_remote_path to new_remote_path."""
    raise NotImplementedError

  def remove(self, remote_path: str) -> None:
    """Removes a file on the FTP/SFTP server at the given absolute path."""
    raise NotImplementedError

  def listdir(self, path: str) -> Iterator[ListDirResult]:
    """Expects an absolute path to a directory on the FTP/SFTP server and returns an iterator of ListDirResult containing the filename and modification time of each file in the directory.
    The filename is not a full path, just the name of the file. The modification time is a datetime object representing the last modification time of the file on the server.
    Note that the modification time may be None if it cannot be determined, and in that case the tuple will not be yielded.
    """
    raise NotImplementedError

  def makedir(self, remote_path: str) -> None:
    """Creates a directory on the FTP/SFTP server at the given absolute path."""
    raise NotImplementedError
